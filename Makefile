ifeq ($(OS),Windows_NT)
    PYTHON = python
else
    PYTHON = python3
endif

.PHONY: check-python
check-python:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "Error: $(PYTHON) is not installed or not in PATH."; \
		echo "Install it from https://www.python.org/downloads/ or via your package manager."; \
		echo "  macOS:  brew install python"; \
		echo "  Debian: sudo apt install python3"; \
		echo "  Fedora: sudo dnf install python3"; \
		echo "  Windows: winget install Python.Python.3"; \
		exit 1; \
	}

.PHONY: help run test format lint typecheck clean check-python

help:
	@echo "Available commands:"
	@echo "  make run        - Run wifituner analyzer & optimizer"
	@echo "  make test       - Run multi-platform unit tests"
	@echo "  make format     - Format source code using Ruff"
	@echo "  make lint       - Lint source code using Ruff"
	@echo "  make typecheck  - Typecheck code using Mypy"
	@echo "  make clean      - Remove build caches and temporary files"

run: check-python
	$(PYTHON) tuner.py

test: check-python
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
