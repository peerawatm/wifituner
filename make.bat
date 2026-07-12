@echo off
if "%1"=="run" (
    python tuner.py
) else if "%1"=="test" (
    python -m unittest test_tuner.py
) else if "%1"=="format" (
    uvx ruff format tuner.py test_tuner.py
) else if "%1"=="lint" (
    uvx ruff check tuner.py test_tuner.py
) else if "%1"=="typecheck" (
    uvx mypy --check-untyped-defs tuner.py
) else if "%1"=="clean" (
    rmdir /s /q __pycache__ .mypy_cache .ruff_cache .pytest_cache htmlcov 2>nul
    del /f /q .coverage .coverage.* 2>nul
    del /s /q *.pyc 2>nul
) else (
    echo Available commands:
    echo   make run        - Run wifituner analyzer ^& optimizer
    echo   make test       - Run multi-platform unit tests
    echo   make format     - Format source code using Ruff
    echo   make lint       - Lint source code using Ruff
    echo   make typecheck  - Typecheck code using Mypy
    echo   make clean      - Remove build caches and temporary files
)
