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
git clone https://github.com/wobblebot/wobblebot.git
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

This installs WobbleBot in **editable mode** with all development dependencies (pytest, black, mypy, etc.).

### 4. Verify Installation

```bash
pytest tests/test_import.py -v
```

You should see all smoke tests pass with 100% coverage.

---

## Development Tools

### Code Formatting

**Black** (code formatter):
```bash
black src/ tests/                    # Format code
black --check src/ tests/            # Check without modifying
```

**isort** (import sorting):
```bash
isort src/ tests/                    # Sort imports
isort --check-only src/ tests/       # Check without modifying
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
- **Black Formatter** (`ms-python.black-formatter`) – Auto-formatting
- **isort** (`ms-python.isort`) – Import sorting
- **Mypy Type Checker** (`ms-python.mypy-type-checker`) – Type checking
- **GitLens** (`eamodio.gitlens`) – Git superpowers
- **GitHub Copilot** (`github.copilot`) – AI assistance

### Format on Save

Workspace settings enable **format on save** with Black and isort. Files are automatically formatted when you save.

### Tasks

Use **Terminal → Run Task** or `Ctrl+Shift+P` → "Tasks: Run Task":

- **Test: All** – Run all tests
- **Test: Unit Only** – Run unit tests only
- **Format: Black** – Format all code
- **Format: isort** – Sort all imports
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

```bash
make help           # Show all available commands
make install        # Install dependencies
make test           # Run all tests
make test-unit      # Run unit tests only
make test-cov       # Run tests with coverage
make lint           # Run mypy + pylint
make format         # Format with black + isort
make format-check   # Check formatting without modifying
make check          # Run all checks (format + lint + test)
make clean          # Remove build artifacts and cache
```

---

## Pre-Commit Workflow

Before committing code, run all checks:

### Manual Checks

```bash
# Format code
black src/ tests/
isort src/ tests/

# Type check
mypy src/

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
    models.py          # Domain models (Order, Trade, Position)
    exceptions.py      # Domain-specific exceptions
  ports/               # Abstract interfaces
    __init__.py
    exchange.py        # ExchangePort interface
    storage.py         # StoragePort interface
  adapters/            # Concrete implementations
    __init__.py
    kraken.py          # Kraken API adapter
    sqlite.py          # SQLite storage adapter
  services/            # Orchestration
    __init__.py
    orchestrator.py    # Main coordinator
  cli/                 # Command-line tools
    __init__.py
    main.py            # CLI entry point
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
    "black>=23.7.0",
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

- **`main`** – Stable, tagged releases only
- **`develop`** – Integration branch for current phase
- **`feature/phaseX-stageY-description`** – Feature branches

Example:
```bash
git checkout develop
git checkout -b feature/phase1-stage2-domain-models
# ... make changes ...
git push origin feature/phase1-stage2-domain-models
# ... open pull request to develop ...
```

---

## Code Review Checklist

Before submitting a pull request:

- [ ] All tests pass (`pytest`)
- [ ] Code is formatted (`black --check src/ tests/`)
- [ ] Imports are sorted (`isort --check-only src/ tests/`)
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
