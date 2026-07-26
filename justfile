python := if os() == "windows" { "python" } else { "python3" }

default: run 
run *args="":
    {{ python }} tuner.py {{ args }}

# Run all test suites & static quality checks (unittest, format check, Pyflakes, Simplify, Bugbear, PyUpgrade, Comprehensions, Builtins, Pie, Return, Mypy)
test *args="":
    {{ python }} -m unittest test_tuner.py {{ args }}
    uvx ruff format --check tuner.py test_tuner.py
    uvx ruff check --select F,SIM,B,UP,C4,A,PIE,RET,I,N,FURB tuner.py test_tuner.py
    uvx mypy --check-untyped-defs tuner.py

# Remove build files
[unix]
clean:
    rm -rf __pycache__ .mypy_cache .ruff_cache .pytest_cache .coverage .coverage.* htmlcov

[windows]
clean:
    @if exist __pycache__ rmdir /s /q __pycache__
    @if exist .mypy_cache rmdir /s /q .mypy_cache
    @if exist .ruff_cache rmdir /s /q .ruff_cache
    @if exist .pytest_cache rmdir /s /q .pytest_cache
    @if exist htmlcov rmdir /s /q htmlcov
    @if exist .coverage del /f /q .coverage
    @del /s /q *.pyc >nul 2>&1 || true
