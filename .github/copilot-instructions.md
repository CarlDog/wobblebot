# WobbleBot AI Coding Agent Instructions

## Project Overview
WobbleBot is a **deterministic, safety-first micro-trading system** using hexagonal architecture (Ports & Adapters). The system executes controlled "buy the dip / sell the rebound" cycles on Kraken with strict safety guardrails, modular isolation, and complete operational transparency.

**Critical Principle:** No single module controls both trading logic AND fund transfers. Financial power is deliberately fragmented.

## Architecture Pattern: Hexagonal (Ports & Adapters)

All modules communicate through **abstract ports**, never direct dependencies:

- **Domain** (`src/wobblebot/domain/`) – Pure business logic, zero external I/O
- **Ports** (`src/wobblebot/ports/`) – Abstract interfaces (e.g., `ExchangePort`, `StoragePort`, `AdvisorPort`, `HarvesterPort`)
- **Adapters** (`src/wobblebot/adapters/`) – Concrete implementations (Kraken API, SQLite, LLM)
- **Services** (`src/wobblebot/services/`) – Orchestrators and coordination logic

**Never** let domain code import adapters. Use dependency injection via constructors.

## Core Components & Boundaries

### Bot Core (Trading Engine)
- Deterministic micro-grid logic, position tracking, P&L calculation
- **Knows:** Grid parameters, safety caps, current positions
- **Doesn't Know:** LLM exists, Harvester exists, how to withdraw funds
- **Accesses Exchange:** Only through `ExchangePort`

### Strategy Advisor (LLM)
- **Advisory ONLY** – produces JSON recommendations, never executable commands
- Receives sanitized summaries (no secrets, no raw credentials)
- Suggestions flow through Orchestrator, which may ignore or partially apply
- **Cannot:** Execute trades, initiate transfers, access Kraken directly

### Harvester Module
- **Sole module** allowed to initiate fund transfers (Kraken ↔ bank)
- Uses dedicated API key with withdrawal permissions (separate from trading key)
- Operates on strict thresholds: minimum liquidity, surplus scraping, top-up rules
- **Blind to:** Trading logic internals, LLM suggestions

### Orchestrator
- Central coordinator for lifecycle, scheduling, module interaction
- **Only component** that coordinates between Bot Core, Advisor, and Harvester
- Aggregates logs and state transitions for observability

## Safety Constraints (Non-Negotiable)

1. **No module except Harvester may initiate fund transfers**
2. **LLM cannot send executable commands** – JSON schema validation enforced
3. **Kraken trading API key must exclude withdrawal permissions**
4. **Max exposure caps and daily spend limits are mandatory** – enforced in Bot Core
5. **Auto-applied Advisor suggestions must:**
   - Pass JSON schema validation
   - Pass range checks against configured min/max bounds
   - Be fully logged with before/after config snapshots

See `docs/architecture/constraints.md` for complete list.

## Development Workflow

### Project Structure
```
src/wobblebot/
  domain/      # Core models, deterministic logic
  ports/       # Abstract interfaces
  adapters/    # Kraken, SQLite, LLM, banking implementations
  services/    # Orchestrators, schedulers
  cli/         # Command-line entry points
  config/      # Configuration schemas & loaders
tests/         # Mirrors src/ structure
```

### Phase-Based Roadmap
Currently in **Phase 1** (Foundation & Sandbox) – no real trading yet. See `docs/planning/roadmap.md`.

- **Phase 1:** Skeleton + paper trading simulation
- **Phase 2:** Real Kraken integration, tiny exposure, no withdrawals
- **Phase 3:** LLM Advisor + analytics (advisory only)
- **Phase 4:** Harvester + treasury management
- **Phase 5:** Dashboard, hardening, v1.0 release

**Critical:** Phases are strictly sequential. Do not implement Phase N+1 features until Phase N is stable.

### Branching Strategy
- `main` – Always stable, tagged releases
- `develop` – Integration branch for current phase
- `feature/phaseX-stageY-*` – Short-lived feature branches

### Code Style & Tools
- **Python 3.13+** required
- Use **black** for formatting, **isort** for imports, **mypy/pyright** for type checking
- Prefer **dataclasses or Pydantic models** for structured data
- **Never use `print()`** – use project logging utilities
- Keep files under 300-400 lines; split if becoming a "junk drawer"

## Testing Requirements

- Every non-trivial module **must have unit tests**
- **Never hit real services in unit tests** – use mocks/stubs
- Integration tests must be clearly marked and skipped by default
- When writing new code, tests are **part of the work, not optional**

## Documentation Requirements

Documentation is **definition of done**. When code changes:

- **Architecture changes** → Update `/docs/architecture/`
- **Planning/roadmap changes** → Update `/docs/planning/`
- **Module specs/operations** → Update `/docs/implementation/`

Key docs to reference:
- `docs/architecture/README.md` – Start here for architecture overview
- `docs/architecture/architecture-components.md` – Component boundaries
- `docs/architecture/solution-strategy.md` – Why hexagonal architecture
- `docs/implementation/coding-guidelines.md` – Code style & patterns
- `docs/planning/roadmap.md` – Current phase context

## Error Handling Patterns

- Raise **domain-specific exceptions** in domain layer (e.g., `ExposureLimitExceeded`)
- Wrap adapter failures in clear exception types with context (request params, endpoint)
- **Never swallow exceptions silently** – log at appropriate level with stack traces
- All external calls wrapped in retry logic with timeouts

## Configuration & Deployment

- Deployed via **Docker Compose** on Synology NAS (or locally)
- **SQLite** for persistence (Phase 1-2)
- Secrets in `.env` (never committed) – see `docker/env.example`
- Main config in `config/settings.yml` (coins, grids, safety caps)

### Environment Modes
- **Sandbox/Dev** – Paper trading, fake banking, verbose logging
- **Live/Low-Risk** – Real Kraken, tiny sizes, Harvester passive
- **Production** – Real trading with caps, Harvester active (future)

## Common Pitfalls to Avoid

1. **Don't let domain import adapters** – breaks hexagonal architecture
2. **Don't give LLM execution power** – it's advisory-only by design
3. **Don't implement Phase N+1 features prematurely** – respect the roadmap
4. **Don't skip tests or documentation** – they're part of the work
5. **Don't add heavy dependencies without justification** – keep it minimal

## When Uncertain

1. Check `/docs/architecture/` for system design context
2. Check `/docs/planning/roadmap.md` for current phase constraints
3. Check `/docs/implementation/coding-guidelines.md` for style questions
4. Review ADRs in `docs/architecture/decisions.md` for major decisions
5. Ask the user for clarification on ambiguous requirements
