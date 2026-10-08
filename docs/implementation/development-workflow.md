# Development Workflow

This guide covers local development setup, tooling, testing, and pre-commit workflows for WobbleBot contributors.

---

## Prerequisites

- **Python 3.13+** (verify with `python --version`)
- **Git** for version control
- **VS Code** (recommended) or your preferred editor
- **Make** (optional, for convenience commands)

---

## Initial Setup

### 1. Clone the Repository

```bash
git clone https://github.com/CarlDog/wobblebot.git
cd wobblebot
```

### 2. Create Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**
```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -e ".[dev]"
```

This installs WobbleBot in **editable mode** with all development dependencies (pytest, Ruff, mypy, pylint, etc.).

### 4. Verify Installation

```bash
pytest tests/test_import.py -v
```

The smoke tests should pass. Their coverage report covers the loaded package;
it is not evidence of full-product coverage. Install the repository hooks before
committing (`bash scripts/install-hooks.sh` or `scripts/install-hooks.ps1`).

---

## Development Tools

### Code Formatting

**Ruff** (code formatter):
```bash
ruff format src/ tests/                    # Format code
ruff format --check src/ tests/            # Check without modifying
```

**Ruff** (import sorting):
```bash
ruff check --select I --fix src/ tests/                    # Sort imports
ruff check src/ tests/       # Check without modifying
```

### Type Checking

**mypy**:
```bash
mypy src/                            # Type check source code
```

Configuration is in `pyproject.toml` with strict settings for `src/` and relaxed for `tests/`.

### Linting

**pylint**:
```bash
pylint src/                          # Lint source code
```

Configuration in `pyproject.toml`.

---

## Testing

### Running Tests

**All tests:**
```bash
pytest
```

**Unit tests only:**
```bash
pytest -m unit
```

**With coverage report:**
```bash
pytest --cov=wobblebot --cov-report=html --cov-report=term
```

Coverage HTML report is generated in `htmlcov/`.

**Specific test file:**
```bash
pytest tests/test_import.py -v
```

### Test Organization

Tests are organized to mirror `src/` structure:

```
tests/
  domain/        # Domain layer tests
  ports/         # Port interface tests
  adapters/      # Adapter implementation tests
  services/      # Service orchestration tests
  cli/           # CLI tests
  config/        # Configuration tests
```

### Test Markers

Tests are marked for selective execution:

- `@pytest.mark.unit` – Fast, isolated unit tests (no external dependencies)
- `@pytest.mark.integration` – Integration tests (may hit external services)
- `@pytest.mark.slow` – Slow-running tests

---

## VS Code Integration

### Recommended Extensions

The workspace recommends these extensions (install via `.code-workspace`):

- **Python** (`ms-python.python`) – Core Python support
- **Pylance** (`ms-python.vscode-pylance`) – Fast language server
- **Ruff** (`charliermarsh.ruff`) – Formatting, import sorting and lint
- **Mypy Type Checker** (`ms-python.mypy-type-checker`) – Type checking
- **GitLens** (`eamodio.gitlens`) – Git superpowers
- **GitHub Copilot** (`github.copilot`) – AI assistance

### Format on Save

Workspace settings enable **format on save** with Ruff. Files are automatically formatted when you save.

### Tasks

Use **Terminal → Run Task** or `Ctrl+Shift+P` → "Tasks: Run Task":

- **Test: All** – Run all tests
- **Test: Unit Only** – Run unit tests only
- **Format: Ruff** – Format all code
- **Format: Imports** – Sort all imports
- **Lint: mypy** – Type check source
- **Lint: pylint** – Lint source
- **Pre-commit: All Checks** – Run all checks (format, lint, test)

### Debugging

Launch configurations are pre-configured:

- **Python: Current File** – Debug the open file
- **Python: Pytest Current File** – Debug tests in the open file
- **Python: All Tests** – Debug entire test suite

Press `F5` to start debugging with the active configuration.

---

## Makefile Commands

If you have `make` installed, use these shortcuts:

The Makefile selects the checkout's Windows or Linux/macOS virtual environment.
Use `make PYTHON=/path/to/python check` to select a different interpreter.

```bash
make help           # Show all available commands
make install        # Install dependencies
make test           # Run all tests
make test-unit      # Run unit tests only
make test-cov       # Run tests with coverage
make lint           # Run Ruff + mypy + retained pylint
make format         # Format and sort imports with Ruff
make format-check   # Check formatting without modifying
make check          # Check formatting, types, lint and tests without rewriting source
make clean          # Remove build artifacts and cache
```

---

## Pre-Commit Workflow

Before committing code, run all checks:

### Manual Checks

```bash
# Check formatting without rewriting the candidate
ruff format --check src/ tests/
ruff check src/ tests/

# Type check
mypy src/
pylint src/

# Run tests
pytest
```

### Automated (via Task or Makefile)

**VS Code Task:**
- `Ctrl+Shift+P` → "Tasks: Run Build Task" (or `Ctrl+Shift+B`)
- Runs: format → lint → test

**Makefile:**
```bash
make check
```

---

## File Structure Conventions

### Package Layout

```
src/wobblebot/
  __init__.py          # Package metadata (__version__, __author__)
  domain/              # Pure business logic, no I/O
    __init__.py
    models.py          # Domain models (Order, Trade, Balance)
    exceptions.py      # Domain-specific exceptions
  ports/               # Abstract interfaces
    __init__.py
    exchange.py        # ExchangePort interface
    storage.py         # StoragePort interface
  adapters/            # Concrete implementations
    __init__.py
    kraken_exchange.py # Kraken API adapter
    sqlite_storage.py # SQLite storage adapter
  services/            # Orchestration
    __init__.py
    grid_engine.py     # Trading engine orchestration
  cli/                 # Command-line tools
    __init__.py
    sandbox.py         # Offline simulation entry point; see README for all CLIs
  config/              # Configuration loading
    __init__.py
    loader.py          # Config file loader
```

### File Naming

- Use `snake_case` for module names (`exchange_port.py`, not `ExchangePort.py`)
- Use `PascalCase` for class names (`ExchangePort`, `Order`, `KrakenAdapter`)
- Use `snake_case` for functions and variables (`get_balance`, `max_exposure_usd`)

---

## Dependency Management

### Adding Dependencies

1. **Add to `pyproject.toml`:**
   ```toml
   dependencies = [
       "pydantic>=2.0.0",
       "pyyaml>=6.0.0",
       "new-package>=1.0.0",  # Add here
   ]
   ```

2. **Reinstall:**
   ```bash
   pip install -e ".[dev]"
   ```

3. **Document why** in your pull request.

### Dev Dependencies

Dev-only dependencies (testing, linting) go under `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "ruff==0.16.10",
    # Add dev tools here
]
```

---

## Common Issues

### Import Errors

**Problem:** `ModuleNotFoundError: No module named 'wobblebot'`

**Solution:** Install in editable mode:
```bash
pip install -e .
```

### Tests Not Discovered

**Problem:** pytest doesn't find tests

**Solution:** Ensure you're in the project root and tests follow naming conventions (`test_*.py`).

### Type Checking Failures

**Problem:** mypy reports errors in dependencies

**Solution:** Add type stubs or ignore:
```bash
pip install types-pyyaml  # For pyyaml
```

Or add to `pyproject.toml`:
```toml
[[tool.mypy.overrides]]
module = "problematic_module.*"
ignore_missing_imports = true
```

---

## Branching Strategy

- **`main`** – Accepted integration line; tags identify reviewed release commits.
- **`codex/<work-item>`** – Short implementation branches from the accepted baseline.
- `develop` and `v1.1` describe historical workflows, not another active integration line.

Example:
```bash
git switch main
git switch -c codex/work-item
# ... make changes ...
# ... run the required checks and prepare a reviewable local commit ...
```

Preserve existing work before switching branches. Publication, pull requests,
merges, releases and deployment follow the user's explicit authorization;
local verification does not imply any of these actions. See the authoritative
[development process](../planning/process.md) and [roadmap](../planning/roadmap.md).

---

## Code Review Checklist

Before submitting a pull request:

- [ ] All tests pass (`pytest`)
- [ ] Code is formatted (`ruff format --check src/ tests/`)
- [ ] Imports are sorted (`ruff check src/ tests/`)
- [ ] Type checking passes (`mypy src/`)
- [ ] Linting passes (`pylint src/`)
- [ ] New code has tests (unit tests minimum)
- [ ] Documentation updated (if architecture/API changed)
- [ ] Commit messages are clear and descriptive
- [ ] PR description explains **what** and **why**

---

## References

- [Coding Guidelines](coding-guidelines.md) – Style and patterns
- [Architecture Overview](../architecture/README.md) – System design
- [Roadmap](../planning/roadmap.md) – Current phase context
