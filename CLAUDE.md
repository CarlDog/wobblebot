# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Single source of truth: `docs/planning/roadmap.md`** — every stage carries a ✅
completion date. Do NOT duplicate project status here (per the documentation-discipline
rule); this section is a pointer, not a changelog.

- **Current:** Phase 8 (Hardening & v1.0 Release). Phases 1–7 + Stages 8.0–8.3 closed;
  **Stage 8.4.E v1.0 soak in progress**, with Stage 8.5 (advisor heuristic+LLM cascade)
  landed as a pre-soak value-add (closed 2026-05-29). The gating soak runs on the NAS
  Docker deployment, restarting ~2026-06-01 post-move. Phase 9 (Kraken Securities
  equities) is committed to start after the v1.0 tag.
- **Detail:** per-phase closing summaries at `docs/planning/phase-{2..7}-summary.md`;
  the day-by-day soak log lives in roadmap Stage 8.4.E.
- **Release docs:** `docs/release/v1.0-known-limitations.md`, `docs/release/v1.1/`
  (future improvements), `docs/release/v1.0-soak-runbook.md`.
- **Running real-money cost: $0.085018** (full breakdown in roadmap + phase summaries).
- Test counts, lint scores, and src-file counts are authoritative in the roadmap's
  per-stage entries — not duplicated here, to avoid drift.

**Before any non-trivial work:** read `docs/planning/roadmap.md`, confirm the request
matches the current stage, and name any drift before starting. If asked for Stage N+1
work while Stage N is in progress, flag it first.

Ratified design decisions live in two places: `docs/architecture/decisions.md` (formal
ADRs) and `docs/architecture/ratified-decisions.md` (operational decisions not yet ADRs —
Kraken adapter, dry-run semantics, caps split, etc.). Don't relitigate either without an ADR.

### Operator entry points

Seventeen surfaces (fifteen `cli/` + two `tools/`). One-line index; full behavior in each
module's `--help` and the roadmap stage that shipped it.

- `cli.sandbox` — Phase 1 mock-exchange paper-trade cycle (no real money).
- `cli.status` — read-only Kraken price + balance check.
- `cli.preflight` — one engine step via Kraken `validate=true` (nothing placed). **Run before every live session.**
- `cli.live` — **real-money** multi-asset grid trading. `--symbols` comma-list; hard caps; clean SIGINT cancels every open order. Exit codes: 0 clean / 1 loss-cap / 2 missing creds.
- `cli.observe` — read-only price/balance data collection.
- `cli.lurker` — one-line alias of `cli.observe` today (own `__main__`); reserved to grow advisor commentary on pure observation later.
- `cli.news` — long-running news poller (RSS + CryptoCompare); persists `news_items` with `(source, external_id)` dedup; feeds the advisor.
- `cli.shadow` — same engine, `ShadowExchangeAdapter` (live prices, synthetic ledger). Backtest sandbox.
- `cli.advise` — MoE advisor daemon; writes suggestions, **never executes** (ADR-002).
- `cli.apply` — operator-gated auto-tune. Dry-run default; `--commit` rewrites `settings.yml`. Default-off gate; news-role never auto-applies.
- `cli.harvest` — treasury daemon; `--execute <id>` is the **only** money-out path (seven defense layers, Harvester key).
- `cli.operator` — Discord interaction daemon (ADR-013). Intent → `pending_commands`; `WHERE status='approved'` is the ADR-002 firewall.
- `cli.web` — FastAPI dashboard (ADR-016/017). Read-mostly; mutations firewalled via `pending_commands`. Needs `WOBBLEBOT_WEB_SESSION_SECRET`.
- `cli.recalibrate` — scale USD-denominated knobs to a new target balance (operator-initiated; dry-run default).
- `cli.maintenance` — VACUUM / prune+archive / backup daemon (three concurrent scheduled tasks).
- `tools/first_real_trade.py` — one-shot live round-trip diagnostic.
- `tools/run_cloud_check.py` — one-shot cloud-LLM smoke test (`--provider`/`--role`/`--model`/`--dry-run`).

### Operator handoff: from dry-run to live trading

1. **Mint a Kraken trading key**, separate from the read-only key (per ADR-003-style separation). Permissions: Query Funds + Query open & closed orders & trades + Create & modify orders + Cancel & close orders. **Withdraw must stay off** — that scope is exclusive to the future Phase 4 Harvester key. Recommended: enable IP address restriction.
2. **Stash credentials in `.env`** as `KRAKEN_TRADER_API_KEY` / `KRAKEN_TRADER_API_SECRET` (separate from the existing `KRAKEN_READER_API_KEY` / `KRAKEN_READER_API_SECRET` so the read-only key can keep being used for `cli/status`).
3. **Run `cli/preflight`** — confirm Kraken accepts the grid config without spending anything. Exit 0 means every layout order would be accepted by Kraken's matching engine.
4. **Run `cli/live`** with eyes on the Kraken Pro Orders + Trade History tab. Defaults: $10 per order, 1% spacing, 3 above + 3 below = $60 total exposure, $5 max session loss, 60 minute max runtime, 5s tick. The first session is the highest-risk session — watch it.

## Commands

The Windows-friendly Makefile uses `.venv/Scripts/python.exe` — if your shell can't run `make`, invoke the same commands directly through the venv interpreter or activate it first.

**First-time setup on a fresh clone** — once, before your first commit:

```bash
./scripts/install-hooks.sh        # or scripts\install-hooks.ps1 on PowerShell
```

This points `core.hooksPath` at `.githooks/`, enabling the repo-specific
pre-commit hook (gitleaks + PII pattern check + author-identity guard).
Without it, only the global `.git/hooks/pre-commit` runs, which only does
gitleaks — missing the PII/identity checks required for this repo.

| Task | Command |
|------|---------|
| Install (editable + dev extras) | `pip install -e ".[dev]"` |
| Run all tests | `pytest` |
| Run unit tests only | `pytest -m unit` |
| Run integration tests only | `pytest -m integration` |
| Run a single test | `pytest tests/path/to/test_file.py::TestClass::test_name` |
| Tests with coverage HTML | `pytest --cov=wobblebot --cov-report=html` |
| Format | `black src/ tests/ && isort src/ tests/` |
| Format check (no writes) | `black --check src/ tests/ && isort --check-only src/ tests/` |
| Type check | `mypy src/` |
| Lint | `pylint src/` |
| All pre-commit checks | `make check` (format + lint + test) |

**Pytest config gotchas** (`pyproject.toml`):
- `addopts` always runs with coverage enabled (`--cov=wobblebot`) — slow runs are expected even for single tests.
- `filterwarnings = ["error", ...]` — warnings other than `DeprecationWarning` fail the suite.
- `--strict-markers` — only `unit`, `integration`, `slow` markers are valid.

**Mypy config:** strict (`disallow_untyped_defs`, `strict_optional`, `warn_unused_ignores`). The `tests/` tree is excluded; `src/` must be clean.

## Architecture

Hexagonal (Ports & Adapters). Layer boundaries are load-bearing — violating them defeats the safety design.

```
src/wobblebot/
  domain/      # Pure business logic; ZERO imports from adapters/services
  ports/       # Abstract interfaces (ABCs) — the contracts adapters implement
  adapters/    # Concrete implementations (Kraken, SQLite, LLM, ...) — depend on domain + ports
  services/    # Orchestrators wiring ports to flows; the only place that knows multiple modules exist
  cli/         # Entry points
  config/      # Pydantic schemas + loaders
tests/         # Mirrors src/ structure
```

**Hard rules:**
- `domain/` must not import from `adapters/`, `services/`, or `cli/`. Run `grep -r "from wobblebot.adapters" src/wobblebot/domain/` — output should be empty.
- Dependencies flow inward only: adapters depend on ports, services depend on ports + domain, nothing depends on adapters.
- All cross-module wiring happens via constructor dependency injection of port interfaces.

### Financial Power Fragmentation (Safety Design)

This is the single most important invariant. No one module controls both trading and money movement:

| Module | What it does | What it CANNOT do |
|--------|-------------|-------------------|
| **Bot Core** | Trading decisions, micro-grid logic, P&L | Initiate transfers; knows nothing of LLM or Harvester |
| **Strategy Advisor (LLM)** | Produce JSON recommendations | Execute trades, initiate transfers, hit Kraken directly |
| **Harvester** | Initiate Kraken→bank withdrawals on thresholds | See trading logic internals or LLM suggestions |
| **Orchestrator** | Coordinate the three modules; aggregate logs | Bypass any port |

**Non-negotiables:**
1. Only Harvester initiates fund transfers. Per ADR-004, it uses Kraken's withdrawal API via `ExchangePort` — there is no separate banking adapter or `BankingPort`.
2. The Kraken **trading** API key must NOT have withdrawal permissions. Withdrawal permissions live on a separate Harvester key.
3. LLM output is JSON-schema-validated and bounded by configured min/max ranges before any auto-application.
4. Max exposure caps and daily spend limits are enforced inside Bot Core, not by adapters.

Full constraint list: `docs/architecture/constraints.md`.

### Phase-Gated Development

Phases are strictly sequential. Do not implement Phase N+1 features until Phase N is stable.

- **Phase 1** – Foundation & sandbox (mock exchange, paper trades, SQLite, logging)
- **Phase 2** – Real Kraken adapter, tiny exposure, withdrawals disabled at API key level
- **Phase 3** – LLM Advisor (advisory only) + metrics
- **Phase 4** – Harvester + treasury management (real withdrawals, guarded)
- **Phase 5** – Dashboard, hardening, v1.0

Each stage's acceptance criteria live in `docs/planning/roadmap.md`. Per ADR-003, Phase 4 introduces the Harvester key separation, not earlier.

### Domain Model Conventions (ADR-005)

Domain models are deliberately Kraken-aligned to minimize adapter translation:
- **Dual ID strategy:** `Order.id: UUID` for DB, `Order.exchange_id: str | None` for Kraken txid.
- **Order status vocabulary:** `pending | open | closed | canceled | expired` (Kraken's canonical terms — note American "canceled").
- **Trade IDs:** Plain Kraken txid strings (`Trade.id: str`), not UUIDs.
- **`Position` model is deferred** to Phase 3+ (margin-specific; spot trading doesn't need it).

Use Pydantic models for domain entities, value objects in `domain/value_objects.py` (`Symbol`, `Price`, `Amount`, `Timestamp`).

## ADRs to Read Before Major Changes

`docs/architecture/decisions.md` is short and dense. The ones that drive code structure:
- **ADR-001:** Hexagonal architecture (the layer rules above).
- **ADR-002:** LLM is advisory only.
- **ADR-003:** Harvester is the sole module with transfer authority.
- **ADR-004:** No separate banking adapter — Harvester uses Kraken's withdrawal API via `ExchangePort`.
- **ADR-005:** Kraken-aligned domain models (status values, ID strategy).

If you're about to add an abstraction "for future flexibility," check that an ADR doesn't already reject it (ADR-004 explicitly rejects a `BankingPort`).

## Where to Find Things

- **Architecture:** `docs/architecture/` (start with `README.md`, then `architecture-components.md`, `constraints.md`, `decisions.md` for ADRs, `ratified-decisions.md` for operational decisions not yet ADRs)
- **Implementation:** `docs/implementation/coding-guidelines.md`, `module-specs.md`, `development-workflow.md`
- **Planning:** `docs/planning/roadmap.md` (source of truth — current phase + per-stage detail), `requirements.md`, `testing-plan.md`, plus per-stage `stage-N.M-design.md` slicing docs
- **Kraken API reference:** `docs/reference/kraken-api-reference.md`
- **Config example:** `config/settings.example.yml` (real `config/settings.yml` is gitignored). Per-CLI sections + grid/safety + advisor + profiles. Operators copy this to `settings.yml` and adjust values; comments and structure stay in sync per the schema-drift tests.
- **Prompt files:** `config/prompts/{quant,risk,news,arbitrator}.md` (committed defaults; operators edit freely). YAML frontmatter + Markdown body; loader in `wobblebot.config.prompts`.
- **Env vars example:** `.env.example` at the repo root (single source of truth — schema-drift tests verify operator `.env` files stay in sync)

## Project-Specific Conventions

- **Python 3.13+ required** (`requires-python = ">=3.13"`). Use `str | None`, `list[X]`, `match` statements — no `Optional`/`List` imports needed.
- **Never use `print()`.** Use the project logger (`wobblebot.config.logging.configure_logging`). Plain format renders message-only; put operator-facing data in the message string and structured fields in the `extra=` dict so JSON consumers see them too.
- **Pydantic v2 models** for structured data (domain entities, config schemas). The Pydantic **mypy plugin** is enabled in `pyproject.toml` and load-bearing — do not remove it.
- **Port error convention:** a domain-data miss returns `T | None`; a protocol/transport failure raises the port's error type (`ExchangeError`, `StorageError`, `DataCollectorError`, etc. — in `wobblebot.ports.exceptions`). More ratified conventions in `docs/architecture/ratified-decisions.md`.
- **Async ports:** `ExchangePort` and other I/O-bound ports are `async`. Use `pytest-asyncio` for tests of async code.
- **Line length 100** (black + isort + pylint all configured to this).
- **Keep files under ~300-400 lines.** Split modules that turn into junk drawers.
- **No `print()`, no swallowed exceptions, no real network calls in unit tests.** Use mocks/stubs (`httpx.MockTransport` is the test seam for `KrakenAdapter`). Integration tests carry the `integration` marker and are excluded from the default `pytest` run via `addopts`; run them explicitly with `pytest -m integration`.

## Phase-End Audit Checklist

Run a phase-end audit at every phase close (Phase 1 → Phase 2,
Phase 2 → Phase 3, etc.) before starting the next phase. The
**global rule lives at `~/.claude/rules/phase-end-audit.md`** —
read that first; the cadence table and process discipline apply
to every project. The wobblebot-specific items below extend it:

### Every phase end (wobblebot extras)

- **All 15 `cli/` entry points handle deprived envs cleanly.** Cycle
  each CLI through: no `.env`, no `config/settings.yml`, no `config/`
  directory at all, missing per-CLI section, empty credentials,
  bad `--config` path, bad `--profile` name. Expected: clean exit
  codes (2 for missing creds / config / section), no raw
  tracebacks. Verification #24 established the baseline 2026-05-15
  for the original 7 (sandbox / status / preflight / live /
  observe / shadow / first_real_trade); cli/apply added at Stage
  3.4b, cli/harvest at Stage 4.2, cli/operator at Stage 5.6 each
  carried their own deprived-env coverage in their slice work;
  cli/web, cli/recalibrate, cli/maintenance — plus the pre-existing
  cli/news + cli/lurker (observe alias) — round out the 15.
  When new entry points ship, add them to this walkthrough.
- **Schema-drift tests pass clean.** `pytest tests/config/test_schema_drift.py`
  runs without warnings (or with documented justification).
  Operator `.env` and `settings.yml` keys are a subset of their
  example counterparts; `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` for
  bidirectional strict mode in CI.
- **Per-stage receipts have completion dates.** Every closed stage
  in `docs/planning/roadmap.md` carries a ✅ date. Phase summary
  document exists if the phase had real-money or architectural
  significance (per `docs/planning/phase-2-summary.md` precedent).
- **OC project memory current.** `mcp__openchronicle__project_list`
  → match repo URL → `mcp__openchronicle__onboard_git` to pick up
  any commits made outside Claude sessions. Project state memory
  reflects current phase + health metrics.
- **Ratified design decisions section in this file is accurate.**
  Don't relitigate; do flag if a new ADR superseded one. New ADRs
  added during the phase get a one-line mention.
- **Real-money cost ledger updated.** If any live-money operations
  ran, the running total in the "Project Status" section reflects
  reality (currently $0.085018).

### Quarterly (wobblebot extras)

- **Pre-commit hook reference comparison.** Diff
  `.githooks/pre-commit` against the canonical reference at
  `https://github.com/CarlDog/plex-mcp/blob/main/.githooks/pre-commit`
  (cited in the global `security.md` rule). If the reference gained
  checks, port them. The reference's PII patterns and
  author-identity guard are the load-bearing parts.
- **Live taker fee re-verification.** The live Kraken fee schedule
  could shift over time. If a tiny live trade (`tools/first_real_trade.py`)
  runs during the audit window, capture the actual fee rate from
  the receipt and confirm it still matches the **0.40% taker / 0.26%
  maker** assumption documented in Stage 2.3 design decisions.

### Pre-1.0 one-shot (wobblebot extras, run when applicable)

- **Phase 4 Harvester key separation verified.** When Phase 4 lands
  and the Harvester is operational, audit-confirm that the Harvester
  key is genuinely separate from the trade key, has Withdraw scope
  on, and that the trade key has Withdraw scope OFF. This is
  ADR-003's load-bearing invariant.

### Tracking

Each audit pass opens a tracked task ("Phase N close audit") with
findings as sub-tasks. Findings get fixed in separate commits per
category (per the global rule's process discipline). Audit-fatigue
mitigation: if a category goes three audits with no findings, drop
its cadence per the global rule.
