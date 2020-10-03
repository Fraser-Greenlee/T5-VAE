# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
    Modified version of Huggingface's run_lm.py for make a T5-based MMD-VAE.
"""

import logging
import os
import wandb
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
import random
import time
import pickle
from tqdm.auto import tqdm, trange
import torch
from torch import nn
from torch.nn import CrossEntropyLoss
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler
from torch.utils.data.dataset import Dataset
from torch.utils.data.dataloader import DataLoader
from unittest.mock import MagicMock

from transformers import (
    MODEL_WITH_LM_HEAD_MAPPING,
    AutoModelForSeq2SeqLM,
    DataCollatorForLanguageModeling,
    HfArgumentParser,
    Trainer,
    TrainingArguments,
    set_seed,
    T5Config,
)
from transformers.modeling_utils import PreTrainedModel
from transformers.optimization import AdamW, get_linear_schedule_with_warmup
from transformers.modeling_t5 import T5LayerFF
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR, TrainOutput


logger = logging.getLogger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_WITH_LM_HEAD_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


class NesT5Tokenizer:
    def __init__(self, vocab_file):
        with open(vocab_file, "r") as f:
            tokens = f.read().split("\n")
        tokens = list(filter(None, tokens))
        tokens = [txt.strip() for txt in tokens]
        tokens = list(set(tokens))
        assert len(tokens) > 10
        logger.info(f"Using vocab size {len(tokens)}")
        self.index2vocab = ["<pad>", "</s>"] + tokens
        self.pad_token_id = 0
        self.eos_token_id = 1
        self.vocab2index = {word: i for i, word in enumerate(self.index2vocab)}
        self.wait_amts = set()
        for word in self.index2vocab:
            if word[:2] == "WT":
                self.wait_amts.add(int(word.split("_")[1]))

    def get_index(self, word):
        if word in self.vocab2index:
            return self.vocab2index[word]
        else:
            assert word[:2] == "WT"
            wait_amt = int(word.split("_")[1])
            closest = min(self.wait_amts, key=lambda x: abs(x - wait_amt))
            return self.vocab2index["WT_{}".format(closest)]

    def tokenize(self, txt):
        return [self.get_index(word) for word in txt.split(" ")]

    def __len__(self):
        return len(self.index2vocab)


class LatentEncoderLargeTanh_1kLatent(nn.Module):
    def __init__(self, dim_m, set_input_size, latent_size, training_args):
        super().__init__()
        assert dim_m > 100
        self.shrink_tokens = nn.Linear(dim_m, 100)
        self.shrink_sequence = nn.Linear(100 * set_input_size, latent_size)
        self.tanh = nn.Tanh()

    def forward(self, encoding) -> torch.Tensor:
        batch_size = encoding.size(0)
        # shrink each tokens encoding
        encoding = self.shrink_tokens(encoding)
        encoding = self.shrink_sequence(encoding.view(batch_size, -1))
        return self.tanh(encoding)


class LatentDecoderLargeT5NormFF(nn.Module):
    def __init__(self, dim_m, set_input_size, latent_size, training_args, config):
        super().__init__()
        self.decode_latent = nn.Linear(latent_size, 10 * set_input_size)
        self.grow_sequence = nn.Linear(10 * set_input_size, 100 * set_input_size)
        self.grow_tokens = nn.Linear(100, dim_m)

        old_drop = config.dropout_rate
        config.dropout_rate = 0
        self.norm = T5LayerFF(config)
        config.dropout_rate = old_drop

    def forward(self, latent) -> torch.Tensor:
        batch_size = latent.size(0)
        # grow each tokens encoding
        latent = self.decode_latent(latent)
        latent = self.grow_sequence(latent)
        return self.norm(self.grow_tokens(latent.view(batch_size, -1, 100)))


class FullSeqAE(nn.Module):
    """
    An VAE to add to encoder-decoder modules.
    Encodes all token encodings into a single vector & spits them back out.

    Switching to an autoencoder to prevent posterior collapse.
    """

    def __init__(self, encoder, decoder, training_args):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.args = training_args

    def _model_forward(self, encoding):
        latent = self.encoder(encoding)
        return self.decoder(latent), latent

    def forward(self, input_encoding: torch.Tensor, just_get_latent=False, just_get_encoding=False):
        recon_encoding, latent = self._model_forward(input_encoding)
        if just_get_latent:
            return latent
        if just_get_encoding:
            return recon_encoding
        recon_loss = torch.nn.MSELoss(reduction="mean")(input_encoding, recon_encoding)
        reg_loss = self._regularliser_loss(input_encoding, latent)
        return recon_loss, reg_loss, recon_encoding

    @staticmethod
    def _compute_kernel(x, y):
        x_size = x.shape[0]
        y_size = y.shape[0]
        dim = x.shape[1]

        tiled_x = x.view(x_size, 1, dim).repeat(1, y_size, 1)
        tiled_y = y.view(1, y_size, dim).repeat(x_size, 1, 1)

        return torch.exp(-torch.mean((tiled_x - tiled_y) ** 2, dim=2) / dim * 1.0)

    def _compute_mmd(self, x, y):
        x_kernel = self._compute_kernel(x, x)
        y_kernel = self._compute_kernel(y, y)
        xy_kernel = self._compute_kernel(x, y)
        return torch.mean(x_kernel) + torch.mean(y_kernel) - 2 * torch.mean(xy_kernel)

    def _regularliser_loss(self, input_encoding, latent):
        loss = torch.tensor(0, dtype=torch.float).to(self.args.device)
        true_samples = torch.randn(latent.size()).to(latent.device)
        loss += self._compute_mmd(true_samples, latent)
        return loss


class t5_AE(PreTrainedModel):
    base_model_prefix = "t5_vae"

    def __init__(self, config, t5_model, vae, set_seq_size, tokenizer):
        super().__init__(config=config)
        self.t5_model = t5_model
        self.vae = vae
        self.config = config
        self.set_seq_size = set_seq_size
        self.tokenizer = tokenizer

    def pad_input_ids(self, input_ids):
        padedd_input_tokens = torch.ones(self.set_seq_size, dtype=torch.long) * self.tokenizer.pad_token_id
        padedd_input_tokens[: input_ids.size(0)] = input_ids
        return padedd_input_tokens.to("cuda").view(1, -1)

    def _decoder_logits(self, decoder_input_ids, encoding):
        sequence_output = self.t5_model.decoder(input_ids=decoder_input_ids, encoder_hidden_states=encoding)[0]
        # Rescale output before projecting on vocab
        # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/transformer/transformer.py#L586
        sequence_output = sequence_output * (self.t5_model.model_dim ** -0.5)
        logits = self.t5_model.lm_head(sequence_output)
        return logits

    def decoder_loss(self, labels, encoding, ignore_index=-100):
        decoder_input_ids = self.t5_model._shift_right(labels)
        logits = self._decoder_logits(decoder_input_ids, encoding)
        loss_fct = CrossEntropyLoss(ignore_index=ignore_index, reduction="none")
        loss = loss_fct(logits.view(-1, logits.size(-1)), labels.view(-1))
        # TODO(thom): Add z_loss https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/layers.py#L666
        return loss

    def decoder_loss_from_latent(self, labels, latent):
        encoding = self.vae.decoder(latent)
        return self.decoder_loss(labels, encoding)

    def get_latent(self, input_ids):
        attention_mask = input_ids.ne(self.config.pad_token_id).long()
        encoding = self.t5_model.encoder(input_ids=input_ids, attention_mask=attention_mask)[0]
        return self.vae(encoding, just_get_latent=True)

    def get_hidden(self, input_ids):
        attention_mask = input_ids.ne(self.config.pad_token_id).long()
        encoding = self.t5_model.encoder(input_ids=input_ids, attention_mask=attention_mask)[0]
        return self.vae(encoding, just_get_encoding=True)

    def _greedy_logits(self, encoding):
        # always start with 0 token
        decoder_input_ids = torch.tensor([[0]]).to(self.device)
        for i in range(self.set_seq_size):
            logits = self._decoder_logits(decoder_input_ids, encoding)
            _, chosen_token = torch.topk(logits[0, i], 1)  # get index of max logits[-1]
            if chosen_token == self.tokenizer.eos_token_id:
                break
            decoder_input_ids = torch.cat((decoder_input_ids, chosen_token.view(1, -1)), 1)
        return logits

    def greedy_logits(self, input_ids=None, latent=None, encoding=None):
        # get logits for given input_ids or latent
        assert input_ids is not None or latent is not None or encoding is not None
        if encoding is None and latent is None:
            if len(input_ids.size()) == 1 and input_ids.size(0) < self.set_seq_size:
                input_ids = self.pad_input_ids(input_ids)
            latent = self.get_latent(input_ids)
        if encoding is None:
            encoding = self.vae.decoder(latent.view(1, -1))
        return self._greedy_logits(encoding)

    def forward(self, input_ids):
        attention_mask = input_ids.ne(self.config.pad_token_id).long()

        encoding = self.t5_model.encoder(input_ids=input_ids, attention_mask=attention_mask)[0]
        recon_loss, reg_loss, encoding = self.vae(encoding)
        decoder_ce = self.decoder_loss(input_ids, encoding, ignore_index=self.config.pad_token_id)

        return decoder_ce, recon_loss, reg_loss


class NesDataset(Dataset):
    """
    Same as `LineByLineTextDataset` by Huggingface but modified to used fixed length sequences & to cache the result.
    Doesn't use an EOS token as we already know the sequence length and we're hoping to potentially blend larger sequences.
    """

    def __init__(self, tokenizer, file_path, set_seq_size, overwrite_cache=False):
        logger.info("Loading text.")

        directory, filename = os.path.split(file_path)
        cached_features_file = os.path.join(directory, f"nes_seq_size_{set_seq_size}_{filename}")

        if os.path.exists(cached_features_file) and not overwrite_cache:
            start = time.time()
            logger.info(f"Loading features from cached file {cached_features_file}...")
            with open(cached_features_file, "rb") as handle:
                self.examples = pickle.load(handle)
            logger.info("[took %.3f s]", time.time() - start)

        else:
            if not os.path.isfile(file_path):
                raise Exception(
                    f"Can't find true file:\n{file_path}\nAlso can't find cahced file:\n{cached_features_file}"
                )

            logger.info(f"Creating features from dataset file at {directory}")

            seq_texts = self._get_text_sequences(file_path)
            random.shuffle(seq_texts)

            tokenized_seqs = []
            for text in tqdm(seq_texts, desc="Tokenizing each sequence."):
                tokenized_seqs.append(tokenizer.tokenize(text))

            assert (
                max([len(seq) for seq in tokenized_seqs]) == min([len(seq) for seq in tokenized_seqs]) == set_seq_size
            )

            logger.info(f"Using seq len {set_seq_size}")

            self.examples = []
            for tokens in tokenized_seqs:
                self.examples.append(torch.tensor(tokens))

            logger.info(f"Got {len(self.examples)} examples.")

            start = time.time()
            with open(cached_features_file, "wb") as handle:
                pickle.dump(self.examples, handle, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info("Saving features into cached file %s [took %.3f s]", cached_features_file, time.time() - start)

    @staticmethod
    def _get_text_sequences(file_path):
        with open(file_path, encoding="utf-8") as f:
            seq_texts = f.read().split("\n")
        # remove empty strings & strip
        seq_texts = list(filter(None, seq_texts))
        seq_texts = [txt.strip() for txt in seq_texts]
        return seq_texts

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, i):
        return self.examples[i]


class Seq2SeqDataCollatorForLanguageModeling(DataCollatorForLanguageModeling):
    mlm: bool = False

    def __call__(self, examples: List[torch.Tensor]) -> Dict[str, torch.Tensor]:
        input_ids = pad_sequence(examples, batch_first=True)
        return {"input_ids": input_ids}


class T5_VAE_Trainer(Trainer):
    """
    Class for training T5-VAE.
    """

    tokenizer = None
    start_training_mode_step = 0
    log_stores = {
        "decoder_ce": [],
        "decoder_ce_sum": [],
        "recon_loss": [],
        "reg_loss": [],
        "reg_loss_w": [],
    }

    def _setup_wandb(self):
        # Overriding this to get all training args in the run.
        pass

    def get_optimizers(
        self, num_training_steps: int
    ) -> Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR]:
        """
        Setup the optimizer and the learning rate scheduler, modified for when training with a VAE with an input-decoder.
        """
        parameters = list(self.model.named_parameters())
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in parameters if not any(nd in n for nd in no_decay)],
                "weight_decay": self.args.weight_decay,
            },
            {
                "params": [p for n, p in parameters if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]

        optimizer = AdamW(optimizer_grouped_parameters, lr=self.args.learning_rate, eps=self.args.adam_epsilon)
        scheduler = get_linear_schedule_with_warmup(
            optimizer, num_warmup_steps=self.args.warmup_steps, num_training_steps=num_training_steps
        )
        return optimizer, scheduler

    def _regulariser_loss_weight_schedule(self):
        if self.args.reg_constant_weight is not None:
            return self.args.reg_constant_weight
        return torch.sigmoid(
            torch.tensor(self.global_step * self.args.reg_schedule_k - self.args.reg_schedule_b)
        ).item()

    def _run_training_step(self, model: nn.Module, inputs: Dict[str, torch.Tensor], log=True) -> float:
        input_ids = inputs["input_ids"].to(self.args.device)
        decoder_ce, recon_loss, reg_loss = model(input_ids)

        reg_loss_w = self._regulariser_loss_weight_schedule()
        loss = decoder_ce.sum() + reg_loss * reg_loss_w
        if self.args.use_recon_loss:
            loss += recon_loss

        if log and self.is_world_master():
            self.log_stores["decoder_ce_sum"].append(decoder_ce.sum().detach() / input_ids.size(0))
            self.log_stores["decoder_ce"].append(decoder_ce.mean().detach())
            self.log_stores["recon_loss"].append(recon_loss.detach())
            self.log_stores["reg_loss"].append(reg_loss.detach())
            self.log_stores["reg_loss_w"].append(reg_loss_w)

        if self.args.gradient_accumulation_steps > 1:
            loss = loss / self.args.gradient_accumulation_steps

        loss.backward()
        return loss.detach()

    def _training_step(self, *args) -> float:
        loss = self._run_training_step(*args)
        torch.cuda.empty_cache()
        return loss

    def _log(self, logs: Dict[str, float], iterator: Optional[tqdm] = None) -> None:
        """
        Log all loss components seperately.
        Seperated to remove use of TB-Writer
        """
        for k, v in self.log_stores.items():
            if len(v):
                logs[k] = sum(v) / float(len(v))
            else:
                logs[k] = 0
            self.log_stores[k] = []

        if self.epoch is not None:
            logs["epoch"] = self.epoch
        if self.global_step is None:
            # when logging evaluation metrics without training
            self.global_step = 0
        wandb.log(logs, step=self.global_step)
        output = {**logs, **{"step": self.global_step}}
        if iterator is not None:
            iterator.write(output)
        else:
            logger.info(output)

    def save_model(self, output_dir: Optional[str] = None):
        if self.is_world_master():  # Always save the tokenizer with the model
            self.model.tokenizer.save_pretrained(output_dir if output_dir is not None else self.args.output_dir)
        super().save_model(output_dir)

    def get_train_dataloader(self) -> DataLoader:
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")

        train_sampler = (
            RandomSampler(self.train_dataset) if self.args.local_rank == -1 else DistributedSampler(self.train_dataset)
        )

        data_loader = DataLoader(
            self.train_dataset,
            batch_size=self.args.train_batch_size,
            sampler=train_sampler,
            collate_fn=self.data_collator,
        )

        return data_loader

    def train(self, model_path: Optional[str] = None):
        """
        Main training entry point.
        Needed to add to fix the len(epoch_iterator) adding:
        ```
            epoch_iterator.__len__ = lambda: len(train_dataloader)
        ```

        Args:
            model_path:
                (Optional) Local path to model if model to train has been instantiated from a local path
                If present, we will try reloading the optimizer/scheduler states from there.
        """
        train_dataloader = self.get_train_dataloader()
        if self.args.max_steps > 0:
            t_total = self.args.max_steps
            num_train_epochs = (
                self.args.max_steps // (len(train_dataloader) // self.args.gradient_accumulation_steps) + 1
            )
        else:
            t_total = int(len(train_dataloader) // self.args.gradient_accumulation_steps * self.args.num_train_epochs)
            num_train_epochs = self.args.num_train_epochs

        optimizer, scheduler = self.get_optimizers(num_training_steps=t_total)

        # Check if saved optimizer or scheduler states exist
        if (
            model_path is not None
            and os.path.isfile(os.path.join(model_path, "optimizer.pt"))
            and os.path.isfile(os.path.join(model_path, "scheduler.pt"))
        ):
            # Load in optimizer and scheduler states
            optimizer.load_state_dict(
                torch.load(os.path.join(model_path, "optimizer.pt"), map_location=self.args.device)
            )
            scheduler.load_state_dict(torch.load(os.path.join(model_path, "scheduler.pt")))

        model = self.model

        # Train!
        total_train_batch_size = (
            self.args.train_batch_size
            * self.args.gradient_accumulation_steps
            * (torch.distributed.get_world_size() if self.args.local_rank != -1 else 1)
        )
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", self.num_examples(train_dataloader))
        logger.info("  Num Epochs = %d", num_train_epochs)
        logger.info("  Instantaneous batch size per device = %d", self.args.per_device_train_batch_size)
        logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d", total_train_batch_size)
        logger.info("  Gradient Accumulation steps = %d", self.args.gradient_accumulation_steps)
        logger.info("  Total optimization steps = %d", t_total)

        self.global_step = 0
        self.epoch = 0
        epochs_trained = 0
        steps_trained_in_current_epoch = 0
        # Check if continuing training from a checkpoint
        if model_path is not None:
            # set global_step to global_step of last saved checkpoint from model path
            try:
                self.global_step = int(model_path.split("-")[-1].split("/")[0])
                epochs_trained = self.global_step // (len(train_dataloader) // self.args.gradient_accumulation_steps)
                steps_trained_in_current_epoch = (
                    self.global_step * self.args.gradient_accumulation_steps % len(train_dataloader)
                )

                logger.info("  Continuing training from checkpoint, will skip to saved global_step")
                logger.info("  Continuing training from epoch %d", epochs_trained)
                logger.info("  Continuing training from global step %d", self.global_step)
                logger.info("  Will skip the first %d steps in the first epoch", steps_trained_in_current_epoch)
            except ValueError:
                self.global_step = 0
                logger.info("  Starting fine-tuning.")

        tr_loss = 0.0
        logging_loss = 0.0
        model.zero_grad()
        train_iterator = trange(epochs_trained, int(num_train_epochs), desc="Epoch", disable=not self.is_local_master())
        for epoch in train_iterator:
            model.train()
            if isinstance(train_dataloader, DataLoader) and isinstance(train_dataloader.sampler, DistributedSampler):
                train_dataloader.sampler.set_epoch(epoch)

            epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=not self.is_local_master())

            for step, inputs in enumerate(epoch_iterator):

                # Skip past any already trained steps if resuming training
                if steps_trained_in_current_epoch > 0:
                    steps_trained_in_current_epoch -= 1
                    continue

                tr_loss += self._training_step(model, inputs, optimizer)

                if (step + 1) % self.args.gradient_accumulation_steps == 0 or (
                    # last step in epoch but step is always smaller than gradient_accumulation_steps
                    epoch_iterator.__len__() <= self.args.gradient_accumulation_steps
                    and (step + 1) == epoch_iterator.__len__()
                ):
                    torch.nn.utils.clip_grad_norm_(model.parameters(), self.args.max_grad_norm)

                    optimizer.step()
                    scheduler.step()
                    model.zero_grad()
                    self.global_step += 1
                    self.epoch = self.global_step / (epoch_iterator.__len__() / self.args.gradient_accumulation_steps)

                    if (self.args.logging_steps > 0 and self.global_step % self.args.logging_steps == 0) or (
                        self.global_step == 1 and self.args.logging_first_step
                    ):
                        logs: Dict[str, float] = {}
                        tr_loss = tr_loss.item()
                        logs["loss"] = (tr_loss - logging_loss) / (
                            self.args.logging_steps * self.args.gradient_accumulation_steps
                        )
                        # backward compatibility for pytorch schedulers
                        logs["learning_rate"] = scheduler.get_last_lr()[0]
                        logging_loss = tr_loss

                        if self.is_world_master():
                            self._log(logs)

                        if self.args.evaluate_during_training:
                            self.evaluate()

                    if self.args.save_steps > 0 and self.global_step % self.args.save_steps == 0:
                        # In all cases (even distributed/parallel), self.model is always a reference
                        # to the model we want to save.
                        if hasattr(model, "module"):
                            assert model.module is self.model
                        else:
                            assert model is self.model
                        # Save model checkpoint
                        output_dir = os.path.join(self.args.output_dir, f"{PREFIX_CHECKPOINT_DIR}-{self.global_step}")

                        self.save_model(output_dir)

                        if self.is_world_master():
                            self.model.tokenizer.save_pretrained(self.args.output_dir)
                            self._rotate_checkpoints()

                        torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
                        torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))

                if self.args.max_steps > 0 and self.global_step > self.args.max_steps:
                    epoch_iterator.close()
                    break
            if self.args.max_steps > 0 and self.global_step > self.args.max_steps:
                train_iterator.close()
                break

        logger.info("\n\nTraining completed. Do not forget to share your model on huggingface.co/models =)\n\n")
        return TrainOutput(self.global_step, tr_loss / self.global_step)


@dataclass
class MyTrainingArguments(TrainingArguments):
    project_name: str = field(default=None, metadata={"help": "The Weights & Biases project name for the run."})
    reg_schedule_k: float = field(
        default=0.0025,
        metadata={"help": "Multiplied by global_step in a sigmoid, more gradually increase regulariser loss weight."},
    )
    reg_schedule_b: float = field(
        default=6.25,
        metadata={"help": "Added to global step in sigmoid, further delays increase in regulariser loss weight."},
    )
    reg_constant_weight: Optional[float] = field(
        default=None, metadata={"help": "Apply a constant weight to the regulariser."}
    )
    use_recon_loss: bool = field(
        default=False, metadata={"help": "Have the reconstructed encodings match their input encodings."}
    )


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
    """

    model_path: Optional[str] = field(
        default=None,
        metadata={
            "help": "The model checkpoint for weights initialization. Leave None if you want to train a model from scratch."
        },
    )
    t5_model_name: Optional[str] = field(
        default=None,
        metadata={"help": "Name of the T5 model being using for encoding & decoding."},
    )
    model_type: Optional[str] = field(
        default="t5",
        metadata={"help": "If training from scratch, pass a model type from the list: " + ", ".join(MODEL_TYPES)},
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    vocab_file: Optional[str] = field(
        default=None,
        metadata={"help": "A vocab file with one token per line in a text file, used with the NES tokenizer."},
    )
    cache_dir: Optional[str] = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    ae_latent_size: int = field(
        default=None, metadata={"help": "The size of the VAE's latent space, only valid with a T5 model."}
    )
    set_seq_size: int = field(default=None, metadata={"help": "Set sequence size, needed for VAE compression."})


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    train_data_file: Optional[str] = field(
        default=None, metadata={"help": "The input training data file (a text file)."}
    )
    overwrite_cache: bool = field(default=False, metadata={"help": "Overwrite the cached training and evaluation sets"})


def get_dataset(args: DataTrainingArguments, set_seq_size, tokenizer):
    file_path = args.train_data_file
    return NesDataset(tokenizer, file_path, set_seq_size, args.overwrite_cache)


def _log_load_failures(model, missing_keys, unexpected_keys, error_msgs):
    if len(missing_keys) > 0:
        logger.info(
            "Weights of {} not initialized from pretrained model: {}".format(model.__class__.__name__, missing_keys)
        )
    if len(unexpected_keys) > 0:
        logger.info(
            "Weights from pretrained model not used in {}: {}".format(model.__class__.__name__, unexpected_keys)
        )
    if len(error_msgs) > 0:
        raise RuntimeError(
            "Error(s) in loading state_dict for {}:\n\t{}".format(model.__class__.__name__, "\n\t".join(error_msgs))
        )


def _get_ae_encoder_decoder(t5_model_config, model_args, training_args):
    args = (t5_model_config.d_model, model_args.set_seq_size, model_args.ae_latent_size, training_args)
    return LatentEncoderLargeTanh_1kLatent(*args), LatentDecoderLargeT5NormFF(*(args + (t5_model_config,)))


def _get_t5_model(set_seq_size, vocab_file):
    tokenizer = NesT5Tokenizer(vocab_file)
    config = T5Config(
        vocab_size=len(tokenizer),
        n_positions=set_seq_size,
        pad_token_id=0,
        eos_token_id=1,
        decoder_start_token_id=0,
    )
    model = AutoModelForSeq2SeqLM.from_config(config)
    model.resize_token_embeddings(len(tokenizer))
    return model, tokenizer


def _get_ae(t5_model_config, model_args, training_args):
    encoder, decoder = _get_ae_encoder_decoder(t5_model_config, model_args, training_args)
    return FullSeqAE(encoder, decoder, training_args)


def _get_t5_vae_requirements(model_args, training_args):
    t5_model, tokenizer = _get_t5_model(model_args.set_seq_size, model_args.vocab_file)
    vae = _get_ae(t5_model.config, model_args, training_args)
    return t5_model, tokenizer, vae


def new_t5_vae(model_args, training_args):
    t5_model, tokenizer, vae = _get_t5_vae_requirements(model_args, training_args)
    return t5_AE(t5_model.config, t5_model, vae, model_args.set_seq_size, tokenizer)


def load_t5_vae(model_args, training_args):
    t5_model, tokenizer, vae = _get_t5_vae_requirements(model_args, training_args)
    return t5_AE.from_pretrained(
        model_args.model_path,
        config=t5_model.config,
        t5_model=t5_model,
        vae=vae,
        set_seq_size=model_args.set_seq_size,
        tokenizer=tokenizer,
        cache_dir=model_args.cache_dir,
    )


def load_t5_vae_from_args(args_list):
    # Use to load a T5_VAE from a jupyter notebook
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, MyTrainingArguments))
    model_args, _, training_args = parser.parse_args_into_dataclasses(args=args_list)
    assert model_args.model_path and os.path.isdir(model_args.model_path)
    return load_t5_vae(model_args, training_args)


def main(alt_local_rank=None):

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, MyTrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    if alt_local_rank is not None:
        training_args.local_rank = alt_local_rank

    if (
        os.path.exists(training_args.output_dir)
        and os.listdir(training_args.output_dir)
        and training_args.do_train
        and not training_args.overwrite_output_dir
    ):
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty. Use --overwrite_output_dir to overcome."
        )

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if training_args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning("Process rank: %s, device: %s", training_args.local_rank, training_args.device)
    logger.info("Training/evaluation parameters %s", training_args)

    # Set seed
    set_seed(training_args.seed)
    if model_args.model_path and os.path.isdir(model_args.model_path):
        model = load_t5_vae(model_args, training_args)
    else:
        model = new_t5_vae(model_args, training_args)

    # Get datasets
    train_dataset = (
        get_dataset(
            data_args,
            model_args.set_seq_size,
            model.tokenizer,
        )
        if training_args.do_train
        else None
    )
    data_collator = Seq2SeqDataCollatorForLanguageModeling(tokenizer=model.tokenizer)
    data_collator.mlm = False

    trainer = T5_VAE_Trainer(
        model=model,
        args=training_args,
        data_collator=data_collator,
        train_dataset=train_dataset,
        eval_dataset=None,
        prediction_loss_only=True,
        tb_writer=MagicMock(),
    )

    if trainer.is_world_master():
        wandb.init(
            project=training_args.project_name,
            name=training_args.output_dir,
            config={**vars(training_args), **vars(data_args), **vars(model_args)},
        )

    # Training
    if training_args.do_train:
        model_path = (
            model_args.model_path
            if model_args.model_path is not None and os.path.isdir(model_args.model_path)
            else None
        )
        trainer.train(model_path=model_path)
        trainer.save_model()
        # For convenience, we also re-save the tokenizer to the same directory,
        # so that you can share your model easily on huggingface.co/models =)
        if trainer.is_world_master():
            model.tokenizer.save_pretrained(training_args.output_dir)


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main(index)


if __name__ == "__main__":
    main()
