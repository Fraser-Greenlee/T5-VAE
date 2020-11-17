# coding=utf-8
# Copyright 2010, t5_vae authors
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
""" t5_vae model configuration """


import logging
from typing import Callable, Union

from .configuration_utils import PretrainedConfig


logger = logging.getLogger(__name__)

t5_vae_PRETRAINED_CONFIG_ARCHIVE_MAP = {
    "t5_vae-base-uncased": "https://s3.amazonaws.com/models.huggingface.co/bert/t5_vae-base-uncased-config.json",
    "t5_vae-large-uncased": "https://s3.amazonaws.com/models.huggingface.co/bert/t5_vae-large-uncased-config.json",
}


class t5_vaeConfig(PretrainedConfig):
    r"""
    This is the configuration class to store the configuration of a :class:`~transformers.t5_vaeModel` or a
    :class:`~transformers.T5_VAE_Model`. It is used to instantiate a t5_vae model according to the specified
    arguments, defining the model architecture. Instantiating a configuration with the defaults will yield a similar
    configuration to that of the t5_vae `t5_vae-base-uncased <https://huggingface.co/t5_vae/t5_vae-base-uncased>`__ architecture.

    Configuration objects inherit from :class:`~transformers.PretrainedConfig` and can be used
    to control the model outputs. Read the documentation from :class:`~transformers.PretrainedConfig`
    for more information.


    Args:
        vocab_size (:obj:`int`, `optional`, defaults to 30522):
            Vocabulary size of the t5_vae model. Defines the number of different tokens that can be represented by the
            :obj:`inputs_ids` passed when calling :class:`~transformers.t5_vaeModel` or
            :class:`~transformers.TFt5_vaeModel`.
        hidden_size (:obj:`int`, `optional`, defaults to 768):
            Dimensionality of the encoder layers and the pooler layer.
        num_hidden_layers (:obj:`int`, `optional`, defaults to 12):
            Number of hidden layers in the Transformer encoder.
        num_attention_heads (:obj:`int`, `optional`, defaults to 12):
            Number of attention heads for each attention layer in the Transformer encoder.
        hidden_act (:obj:`str` or :obj:`Callable`, `optional`, defaults to :obj:`"gelu"`):
            The non-linear activation function (function or string) in the encoder and pooler.

            If string, :obj:`"gelu"`, :obj:`"relu"`, :obj:`"swish"` and :obj:`"gelu_new"` are supported.
        hidden_dropout_prob (:obj:`float`, `optional`, defaults to 0.1):
            The dropout probability for all fully connected layers in the embeddings, encoder, and pooler.
        attention_probs_dropout_prob (:obj:`float`, `optional`, defaults to 0.1):
            The dropout ratio for the attention probabilities.
        max_position_embeddings (:obj:`int`, `optional`, defaults to 512):
            The maximum sequence length that this model might ever be used with.
            Typically set this to something large just in case (e.g., 512 or 1024 or 2048).
        type_vocab_size (:obj:`int`, `optional`, defaults to 2):
            The vocabulary size of the :obj:`token_type_ids` passed when calling :class:`~transformers.t5_vaeModel` or
            :class:`~transformers.TFt5_vaeModel`.
        initializer_range (:obj:`float`, `optional`, defaults to 0.02):
            The standard deviation of the :obj:`truncated_normal_initializer` for initializing all weight matrices.
        layer_norm_eps (:obj:`float`, `optional`, defaults to 1e-5):
            The epsilon used by the layer normalization layers.
        gradient_checkpointing (:obj:`bool`, `optional`, defaults to :obj:`False`):
            If :obj:`True`, use gradient checkpointing to save memory at the expense of slower backward pass.
        kwargs:
            Additional arguments for common configurations, passed to :class:`~transformers.PretrainedConfig`.
    """
    model_type = "t5_vae"

    def __init__(
        self,
        vocab_size: int = 50257,
        hidden_size: int = 1024,
        num_hidden_layers: int = 12,
        num_attention_heads: int = 12,
        hidden_act: Union[str, Callable] = "gelu",
        hidden_dropout_prob: float = 0.1,
        attention_probs_dropout_prob: float = 0.1,
        max_position_embeddings: int = 512,
        type_vocab_size: int = 2,
        initializer_range: float = 0.02,
        layer_norm_epsilon: float = 1e-5,
        gradient_checkpointing: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.hidden_act = hidden_act
        self.hidden_dropout_prob = hidden_dropout_prob
        self.attention_probs_dropout_prob = attention_probs_dropout_prob
        self.max_position_embeddings = max_position_embeddings
        self.type_vocab_size = type_vocab_size
        self.initializer_range = initializer_range
        self.layer_norm_epsilon = layer_norm_epsilon
        self.gradient_checkpointing = gradient_checkpointing
