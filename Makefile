setup_venv:
	virtualenv venv

install:
	pip3 install -r requirements.txt

format:
	black -l 120 *.py

get-data:
	gsutil cp gs://fras/nes_tx1_full_seq_size_300.txt .
	gsutil cp gs://fras/nes_tx1_vocab.txt .
	gsutil cp gs://fras/nes_seq_size_300_nes_tx1_full_seq_size_300.txt .

common-fixes:
	pip uninstall tensorflow
