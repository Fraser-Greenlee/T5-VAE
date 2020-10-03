setup_venv:
	virtualenv venv

install:
	pip3 install -r requirements.txt

format:
	black -l 120 *.py
