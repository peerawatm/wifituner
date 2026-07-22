# Cross-platform justfile for wifituner

python := if os() == "windows" { "python" } else { "python3" }

# List available recipes
default:
    @just --list

# Run wifituner analyzer & optimizer
run *args="":
    {{ python }} tuner.py {{ args }}

# Run multi-platform unit tests
test *args="":
    {{ python }} -m unittest test_tuner.py {{ args }}

# Format source code using Ruff
format:
    uvx ruff format tuner.py test_tuner.py

# Lint source code using Ruff
lint:
    uvx ruff check tuner.py test_tuner.py

# Typecheck code using Mypy
typecheck:
    uvx mypy --check-untyped-defs tuner.py

# Remove build caches and temporary files
[unix]
clean:
    rm -rf __pycache__ .mypy_cache .ruff_cache .pytest_cache .coverage .coverage.* htmlcov
    find . -name "*.pyc" -delete

[windows]
clean:
    @if exist __pycache__ rmdir /s /q __pycache__
    @if exist .mypy_cache rmdir /s /q .mypy_cache
    @if exist .ruff_cache rmdir /s /q .ruff_cache
    @if exist .pytest_cache rmdir /s /q .pytest_cache
    @if exist htmlcov rmdir /s /q htmlcov
    @if exist .coverage del /f /q .coverage
    @del /s /q *.pyc >nul 2>&1 || true
