ifeq ($(OS),Windows_NT)
    PYTHON = python
else
    PYTHON = python3
endif

.PHONY: help run test format lint typecheck clean

help:
	@echo "Available commands:"
	@echo "  make run        - Run wifituner analyzer & optimizer"
	@echo "  make test       - Run multi-platform unit tests"
	@echo "  make format     - Format source code using Ruff"
	@echo "  make lint       - Lint source code using Ruff"
	@echo "  make typecheck  - Typecheck code using Mypy"
	@echo "  make clean      - Remove build caches and temporary files"

run:
	$(PYTHON) tuner.py

test:
	$(PYTHON) -m unittest test_tuner.py

format:
	uvx ruff format tuner.py test_tuner.py

lint:
	uvx ruff check tuner.py test_tuner.py

typecheck:
	uvx mypy --check-untyped-defs tuner.py

clean:
	rm -rf __pycache__ .mypy_cache .ruff_cache .pytest_cache .coverage .coverage.* htmlcov
	find . -name "*.pyc" -delete
