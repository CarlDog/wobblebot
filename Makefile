# Makefile for WobbleBot development tasks
# Use `make help` to see available commands

.PHONY: help install test test-unit test-cov lint format check clean

PYTHON := .venv/Scripts/python.exe
PIP := .venv/Scripts/pip.exe

help: ## Show this help message
	@echo "WobbleBot Development Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install the package in editable mode with dev dependencies
	$(PIP) install -e ".[dev]"

test: ## Run all tests
	$(PYTHON) -m pytest tests/ -v

test-unit: ## Run unit tests only
	$(PYTHON) -m pytest tests/ -v -m unit

test-cov: ## Run tests with coverage report
	$(PYTHON) -m pytest tests/ -v --cov=wobblebot --cov-report=html --cov-report=term

lint: ## Run all linters (mypy, pylint)
	$(PYTHON) -m mypy src/
	$(PYTHON) -m pylint src/

format: ## Format code with black and isort
	$(PYTHON) -m black src/ tests/
	$(PYTHON) -m isort src/ tests/

format-check: ## Check if code is formatted correctly
	$(PYTHON) -m black --check src/ tests/
	$(PYTHON) -m isort --check-only src/ tests/

check: format lint test ## Run all checks (format, lint, test)

clean: ## Remove build artifacts and cache files
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf htmlcov
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
