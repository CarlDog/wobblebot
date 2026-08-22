# Makefile for WobbleBot development tasks
# Use `make help` to see available commands

.PHONY: help install test test-unit test-cov lint format check check-config-drift clean

PYTHON := .venv/Scripts/python.exe
PIP := .venv/Scripts/pip.exe

# Schema-drift strict mode (2026-08-22). Makes the drift guard a
# property of the REPO rather than of one machine's untracked .env.
#
# tests/config/test_schema_drift.py only hard-fails on a key missing
# from the operator's settings.yml when this is set; otherwise it
# prints a warning nobody reads. CI can never enforce it at all --
# settings.yml is gitignored, so the test skips there entirely -- which
# leaves the dev machine as the only place it can run. This machine
# already had it enabled via .env, but .env is gitignored and
# per-machine: a fresh clone, a second workstation, or a rebuilt
# environment silently loses the guarantee. Declaring it here means the
# check travels with the repo.
#
# NB what this does NOT fix (2026-08-22 audit): three keys still went
# missing for weeks with strict mode already on, because the checkout
# running the tests was 235 commits behind main -- its
# settings.example.yml had never heard of those keys, so there was
# nothing to flag. A stale checkout defeats this guard entirely. Keep
# the working copy current.
#
# Unset it for one run if you deliberately want a key omitted.
export WOBBLEBOT_STRICT_CONFIG_DRIFT := 1

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

check-config-drift: ## Fail if settings.yml/.env drift from their example files
	$(PYTHON) -m pytest tests/config/test_schema_drift.py -q --no-cov

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
