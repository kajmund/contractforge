# Utvecklingskommandon (körs från projektroten)

PYTHON ?= .venv/bin/python
PIP := $(PYTHON) -m pip
RUFF := .venv/bin/ruff
PYTEST := $(PYTHON) -m pytest

.PHONY: install lint test test-all benchmark-offline clean

install:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

lint:
	$(RUFF) check src tests benchmarks

test:
	$(PYTEST) -m "not live and not slow"

test-all:
	$(PYTEST)

benchmark-offline:
	$(PYTHON) -m contractforge benchmark --type consulting --count 100 \
		--clauses 10 --difficulty 3 --language sv --provider mock --seed 1234 \
		--output benchmarks/results

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
