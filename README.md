# WobbleBot

**Deterministic, safety-first micro-trading system using hexagonal architecture**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

---

## Overview

WobbleBot is a **controlled micro-trading system** that executes "buy the dip / sell the rebound" cycles on Kraken with strict safety guardrails, modular isolation, and complete operational transparency.

**Critical Design Principle:** No single module controls both trading logic AND fund transfers. Financial power is deliberately fragmented across:
- **Bot Core** – Trading decisions and execution
- **Strategy Advisor (LLM)** – Advisory recommendations only (no execution power)
- **Harvester** – Fund transfer authority (blind to trading internals)

Built using **hexagonal architecture (Ports & Adapters)** for clean boundaries, testability, and long-term maintainability.

---

## Project Status

**Current Phase:** Phase 1 – Foundation & Sandbox
**Stage:** 1.3 – Storage & Logging Backbone (storage complete, logging in progress)

See [docs/planning/roadmap.md](docs/planning/roadmap.md) for the full development roadmap.

---

## Quick Start

### Prerequisites

- **Python 3.13+** (verify with `python --version`)
- **pip** or **poetry** for dependency management
- **Git** for version control

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/wobblebot/wobblebot.git
   cd wobblebot
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Verify installation:**
   ```bash
   pytest tests/
   black --check src/ tests/
   mypy src/
   ```

---

## Project Structure

```
wobblebot/
├── src/wobblebot/          # Application code
│   ├── domain/            # Core models & business logic
│   ├── ports/             # Abstract interfaces
│   ├── adapters/          # Concrete implementations (Kraken, SQLite, LLM)
│   ├── services/          # Orchestrators & coordination
│   ├── cli/               # Command-line entry points
│   └── config/            # Configuration schemas & loaders
├── tests/                 # Test suite (mirrors src/)
├── docs/                  # Architecture & planning documentation
│   ├── architecture/      # System design, constraints, decisions
│   ├── implementation/    # Coding guidelines, module specs
│   └── planning/          # Roadmap, milestones, requirements
├── config/                # Configuration files (gitignored)
├── docker/                # Docker setup (Phase 2+)
└── scripts/               # Utility scripts
```

---

## Development Workflow

### Running Tests
```bash
pytest                    # Run all tests
pytest tests/unit/        # Run unit tests only
pytest -m integration     # Run integration tests only
```

### Code Formatting & Linting
```bash
black src/ tests/         # Format code
isort src/ tests/         # Sort imports
mypy src/                 # Type checking
pylint src/               # Linting
```

### Common Tasks
```bash
# Run all checks before committing
black src/ tests/ && isort src/ tests/ && mypy src/ && pytest

# Install in editable mode
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"
```

---

## Architecture

WobbleBot follows **hexagonal architecture** with strict layer boundaries:

- **Domain Layer** – Pure business logic, zero external I/O
- **Ports Layer** – Abstract interfaces defining contracts
- **Adapters Layer** – Concrete implementations (Kraken API, SQLite, LLM)
- **Services Layer** – Orchestration and coordination logic

**Key Constraint:** Domain code never imports adapters. All communication happens through ports with dependency injection.

See [docs/architecture/README.md](docs/architecture/README.md) for detailed architectural documentation.

---

## Safety & Constraints

1. **No module except Harvester may initiate fund transfers**
2. **LLM cannot send executable commands** – JSON schema validation enforced
3. **Kraken trading API key must exclude withdrawal permissions**
4. **Max exposure caps and daily spend limits are mandatory**
5. **Auto-applied Advisor suggestions must pass validation and range checks**

See [docs/architecture/constraints.md](docs/architecture/constraints.md) for complete safety rules.

---

## Documentation

- **Architecture:** [docs/architecture/](docs/architecture/)
  - [Architecture Overview](docs/architecture/README.md)
  - [Components & Boundaries](docs/architecture/architecture-components.md)
  - [Constraints & Safety Rules](docs/architecture/constraints.md)
  - [Architecture Decision Records](docs/architecture/decisions.md)

- **Implementation:** [docs/implementation/](docs/implementation/)
  - [Coding Guidelines](docs/implementation/coding-guidelines.md)
  - [Module Specifications](docs/implementation/module-specs.md)
  - [Operations Guide](docs/implementation/operations.md)

- **Planning:** [docs/planning/](docs/planning/)
  - [Roadmap (Phases & Stages)](docs/planning/roadmap.md)
  - [Requirements](docs/planning/requirements.md)
  - [Testing Plan](docs/planning/testing-plan.md)

---

## Contributing

We follow a **phase-gated development model**. Before contributing:

1. Review the [current phase and stage](docs/planning/roadmap.md)
2. Read the [coding guidelines](docs/implementation/coding-guidelines.md)
3. Ensure your changes respect [architectural constraints](docs/architecture/constraints.md)
4. Write tests and documentation alongside code

**Critical:** Do not implement Phase N+1 features until Phase N is stable.

---

## License

MIT License – See [LICENSE](LICENSE) for details.

---

## Contact

For questions, issues, or contributions, please open an issue on GitHub.
