# WobbleBot

**Deterministic, safety-first micro-trading system on Kraken using hexagonal architecture.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Tests](https://img.shields.io/badge/tests-1907%20unit%20%2B%2029%20integration-brightgreen.svg)](docs/planning/testing-plan.md)
[![Pylint](https://img.shields.io/badge/pylint-10.00%2F10-brightgreen.svg)](pyproject.toml)

> **⚠ Disclaimer.** WobbleBot is a personal hobby project. It places real
> orders against real money on a real exchange when you run it in `live`
> mode. **YOU CAN LOSE REAL MONEY.** The code is provided "AS IS" with no
> warranty (see [`LICENSE`](LICENSE)). Nothing here is investment advice;
> the maintainer is not your financial advisor. Audit the code, test in
> `cli/shadow` first, set hard caps you can afford to lose, and only
> proceed if you understand what you're running.
>
> WobbleBot is **not affiliated with or endorsed by** Kraken, Payward Inc.,
> Discord, Anthropic, OpenAI, Google, or Ollama. See [`NOTICE.md`](NOTICE.md)
> for trademark + brand attributions.

---

## Overview

WobbleBot runs a **micro-grid trading strategy** on Kraken: anchor at a reference price, place a layered set of buy and sell limit orders around that anchor, replace each fill with a counter-order at the next grid level. Strict safety guardrails, modular isolation, and complete operational transparency.

**Critical Design Principle.** No single module controls both trading logic AND fund transfers. Financial power is deliberately fragmented across:

- **Bot Core** — trading decisions and execution.
- **Strategy Advisor (LLM)** — JSON-schema-validated recommendations only; no execution power. Phase 3.
- **Harvester** — Kraken-side fund-transfer authority via the Kraken withdrawal API; blind to trading internals. Phase 4.

Built on **hexagonal architecture (Ports & Adapters)** for clean boundaries, testability, and long-term maintainability.

---

## Project Status

**Source of truth:** [`docs/planning/roadmap.md`](docs/planning/roadmap.md). Each completed stage carries a ✅ completion date.

| Phase | Status |
|---|---|
| **Phase 1** — Foundation & Sandbox | ✅ closed 2026-05-13 |
| **Phase 2** — Real Kraken adapter, micro-grid, multi-asset | ✅ closed 2026-05-14 (total real-money cost: **$0.08**) |
| **Phase 3 / Stage 3.0** — Observer & Shadow Mode | ✅ closed 2026-05-14 |
| **Config consolidation audit** (8 slices, no live-money risk) | ✅ closed 2026-05-14 |
| **Phase 3 / Stage 3.1** — Data Collector & Metrics v2 | ✅ closed 2026-05-15 |
| **Phase 3 / Stage 3.2** — Advisor Port + single-LLM Ollama | ✅ closed 2026-05-15 |
| **Phase 3 / Stage 3.2.5** — News Ingestion (RSS + CryptoCompare) | ✅ closed 2026-05-15 |
| **Phase 3 / Stage 3.3** — Passive Advisory Workflow (`cli/advise`) | ✅ closed 2026-05-15 |
| **Phase 3 / Stage 3.4a** — Mixture of Experts (MoE) | ✅ closed 2026-05-15 |
| **Phase 3 / Stage 3.4b** — Bounded Auto-Tuning Gate (`cli/apply`) | ✅ closed 2026-05-15 |
| **Phase 3 / Stage 3.5** — Phase 3 integration check | ✅ closed 2026-05-15 ([summary](docs/planning/phase-3-summary.md)) |
| **Stage 3.6** — Operational polish (indefinite runtime + multi-symbol advise) | ✅ closed 2026-05-15 |
| **Phase 4 / Stage 4.1** — Harvester domain + decision logic | ✅ closed 2026-05-15 |
| **Phase 4 / Stage 4.2** — `cli/harvest` read-only balance monitor | ✅ closed 2026-05-15 |
| **Phase 4 / Stage 4.3** — Transfer proposal persistence + `tools/show_proposals.py` | ✅ closed 2026-05-15 |
| **Phase 4 / Stage 4.4** — Active-mode withdrawals (`cli/harvest --execute`) | ✅ closed 2026-05-15 |
| **Phase 4 / Stage 4.5** — Phase 4 integration check | ✅ closed 2026-05-15 ([summary](docs/planning/phase-4-summary.md)) |
| **Phase 5** — Operator Interaction Engine (Discord + LLM intent parsing; ADR-013) | ✅ closed 2026-05-16 ([summary](docs/planning/phase-5-summary.md)) |
| **Phase 6** — Cloud LLM Integration (Anthropic / OpenAI / Google adapters; cost gate; ADR-014 + ADR-015) | ✅ closed 2026-05-17 ([summary](docs/planning/phase-6-summary.md)) |
| **Phase 7** — Web UI / Dashboard (FastAPI + Jinja2 + HTMX; ADR-016 + ADR-017) | ✅ closed 2026-05-18 ([summary](docs/planning/phase-7-summary.md)) |
| **Stage 7.6** — `cli/recalibrate` operator tool (balance-scaling polish) | ✅ closed 2026-05-18 |
| **Stage 8.0** — Deferred Phase-5-audit refactors (ports/operator split; run_poll_loop helper) | ✅ closed 2026-05-18 |
| **Stage 8.1** — Reliability & Recovery (engine reconciliation; ADR-018) | ✅ closed 2026-05-18 |
| **Stage 8.2** — Background Maintenance Worker (`cli/maintenance`; VACUUM + prune + backup) | ✅ closed 2026-05-18 |
| **Stage 8.3** — Performance & Resource Tuning (SQLite pragmas + index audit + profile harness) | ✅ closed 2026-05-18 |
| **Stage 8.4** — Phase 8 / v1.0 Release Check | in progress 2026-05-18 |

**Health:** 1907 unit tests pass by default; 29 integration tests opt-in. mypy clean (110 src files), black/isort clean, pylint **10.00/10**.

---

## Operator Entry Points

Fifteen entry points cover the full operational surface (fourteen `cli/` daemons + `tools/run_cloud_check.py` counted as the twelfth per Phase 6's close prose). Every CLI accepts `--config PATH` and `--profile NAME` for YAML-driven configuration with deep-merge profile overrides; per-CLI flags override both.

| CLI | Phase | Touches money? | Purpose |
|---|---|---|---|
| `python -m wobblebot.cli.sandbox` | 1 | ❌ | Mock-only paper buy-dip / sell-rebound cycle through `MockExchangeAdapter` + SQLite. |
| `python -m wobblebot.cli.status` | 2.1 | ❌ | Live Kraken read check — fetches current price + account balances. Read-only API key. |
| `python -m wobblebot.cli.observe` | 3.0 | ❌ | Pure data collection — polls Ticker per symbol, persists snapshots. Read-only API key. (`cli/lurker` is a one-line alias today; Stage 3.4-ish, lurker grows advisor commentary on top.) |
| `python -m wobblebot.cli.shadow` | 3.0 | ❌ | Same engine as `cli/live` against a synthetic balance ledger with live Kraken prices. Honest maker/taker fee modeling. |
| `python -m wobblebot.cli.preflight` | 2.3 | ❌ | Diagnostic: runs ONE engine step against live Kraken with `validate=true`. Verifies Kraken accepts the config without spending. **Run this before every live session.** |
| `python -m wobblebot.cli.live` | 2.3+2.4 | **✅ REAL MONEY** | Multi-asset operational loop. Hard caps: max session loss, max runtime, per-coin / total / daily-spend exposure. Clean SIGINT cancels all open orders. |
| `python -m wobblebot.cli.news` | 3.2.5 | ❌ | Long-running news poller (RSS feeds + CryptoCompare). Persists items to `news_items` with `(source, external_id)` dedup. Per-source fault isolation. |
| `python -m wobblebot.cli.advise` | 3.3 / 3.4a | ❌ | Long-running advisor daemon. Builds a `PerformanceSummary` from observe + news on a `schedules.advise` cadence, calls the configured advisor (single-LLM Ollama OR MoE with 2+ experts + optional arbitrator), persists `AdvisorSuggestion` rows for operator review. **Never mutates running config** — that's `cli/apply`'s job. |
| `python -m wobblebot.cli.apply` | 3.4b | ❌ (config writes) | Operator-in-the-loop auto-apply gate. Dry-run by default; `--commit` rewrites `settings.yml` (ruamel.yaml, comment-preserving) and persists an `AppliedSuggestion` audit row. Gate defaults OFF (`auto_apply.enabled=False`); news-role suggestions never auto-apply per ADR-007. |
| `python -m wobblebot.cli.harvest` | 4.2-4.4 | Daemon ❌ / `--execute` **✅ REAL MONEY** | Treasury monitor. Daemon mode polls Kraken USD balance, runs `propose_transfer()`, persists every non-None proposal to `transfer_proposals`, logs "HYPOTHETICAL proposal". `--execute <proposal-id>` runs seven defense layers, calls Kraken `/Withdraw`. The ONLY path by which money leaves the exchange. |
| `python -m wobblebot.cli.operator` | 5.6 | ❌ (chat surface only) | Discord-backed operator interaction daemon (ADR-013). Maintains a Gateway connection, drains the `notifications` SQLite table to Discord, parses inbound operator messages via `OllamaAssistantAdapter` into typed `OperatorIntent` payloads — Command → writes `PendingCommand` + posts confirm embed (cli/live polls the approved rows; that's the ADR-002 firewall); Query → reads engine + storage state via `OperatorService` and replies; Conversational / Unparseable → text reply. Background TTL expirer transitions abandoned `awaiting_confirmation` rows to `expired`. |
| `python -m wobblebot.cli.web` | 7.1 | ❌ (read-mostly; ADR-013-firewalled mutations) | FastAPI + Jinja2 + HTMX dashboard. `serve` subcommand boots uvicorn against `127.0.0.1:8000` (operator's reverse proxy fronts the LAN); `create-user` seeds a bcrypt-hashed `users` row. Status / cost / advisor / harvester / news / audit views. Pause/resume/stop buttons create `PendingCommand` rows in `awaiting_confirmation` — cli/live's `WHERE status='approved'` poll stays the only path from intent to engine. |
| `python -m wobblebot.cli.recalibrate` | 7.6 | ❌ (config writes) | Scales every USD-denominated knob in `settings.yml` proportionally to a new `--target-balance`. Reads live Kraken USD balance via the read-only key by default; `--current-balance X` overrides for what-if analysis. `--commit` rewrites `settings.yml` (ruamel.yaml, comment-preserving, atomic). Spacing %, level counts, max_loss_percentage, shadow:* are policy invariants and stay constant. |
| `python -m wobblebot.cli.maintenance` | 8.2 | ❌ | Background maintenance daemon. Three concurrent scheduled tasks via `asyncio.gather` over `run_poll_loop`: VACUUM (default 7d cadence) → `services.maintenance.vacuum_database`; prune+archive (1d) → archive-then-delete `price_snapshots` to CSV; backup (1d) → SQLite online `.backup` API to `data/backups/`. Opt-in log rotation via `TimedRotatingFileHandler` ALONGSIDE stderr. |
| `python tools/first_real_trade.py` | 2.3 | **✅ REAL MONEY** | One-shot diagnostic: marketable round-trip with hard caps. Used 2026-05-15 against the operator's account; total cost $0.08. |
| `python tools/run_cloud_check.py` | 6.5 | **✅ REAL MONEY** (tiny) | One-shot cloud-LLM smoke test (`--provider anthropic|openai|google` / `--role` / `--model`). Counted as the 12th entry point per Phase 6's close prose; lives under `tools/` (diagnostic, not daemon). |

**Inspection tools** (read-only, safe against live DBs while their CLIs run):

| Tool | Purpose |
|---|---|
| `python tools/show_proposals.py` | Print persisted `transfer_proposals` rows (Stage 4.3). `--direction` / `--asset` / `--since-hours` / `--limit` filters. |
| `python tools/show_transfers.py` | Print persisted `transfer_results` rows (Stage 4.4d). `--status` / `--direction` / `--asset` filters. |
| `python tools/show_pending.py` | Print persisted `pending_commands` rows (Stage 5.6.D). `--status` filter across the six lifecycle states. |
| `python tools/show_suggestions.py` | Print persisted `advisor_suggestions` rows (Stage 3.3). `--symbol` / `--model` filters. |
| `python tools/show_metrics.py` | Compute and print metrics windows from `price_snapshots` (Stage 3.1). |
| `python tools/show_llm_costs.py` | Print persisted `llm_calls` rows (Stage 6.1). `--provider` / `--role` / `--since-hours` filters; daily / session cost rollups. |
| `python tools/run_advisor.py` | One-shot advisor call against the observe DB; JSONL receipt to `data/` (Stage 3.2). |
| `python tools/run_moe_check.py` | One-shot MoE advisor exerciser (Stage 3.4a). |
| `python tools/profile_storage.py` | Storage-layer latency harness (Stage 8.3.C). Reports p50/p99 ms per operation; pre-seeds fixtures so timings reflect realistic index-vs-scan behavior. Safe against live DBs (copies to temp file first). |

---

## Quick Start

### Prerequisites

- **Python 3.13+** (verify with `python --version`)
- **Git** for version control

### Installation

```bash
# 1. Clone
git clone https://github.com/CarlDog/wobblebot.git
cd wobblebot

# 2. Create + activate a virtualenv
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell
# source .venv/bin/activate     # macOS/Linux

# 3. Install in editable mode + dev extras
pip install -e ".[dev]"

# 4. Install the repo's pre-commit hook (gitleaks + PII + author-identity guard)
./scripts/install-hooks.sh      # macOS/Linux
# scripts\install-hooks.ps1     # Windows PowerShell

# 5. Verify the install
pytest                          # 1785 unit tests; ~20s with coverage
black --check src/ tests/
mypy src/
```

### Configuration

```bash
# Copy the example config + .env templates and fill them in
cp config/settings.example.yml config/settings.yml
cp .env.example .env
```

Both copies stay schema-aligned with their examples — `tests/config/test_schema_drift.py` enforces it. See [`config/settings.example.yml`](config/settings.example.yml) for the full operator-facing API.

### First run (no money risk)

```bash
python -m wobblebot.cli.sandbox
```

Runs a paper buy-dip / sell-rebound cycle through the mock exchange and SQLite — no Kraken contact, no credentials needed. Verifies the hex layers wire up after a fresh checkout.

### From dry-run to live trading

See **[Operator handoff](CLAUDE.md#operator-handoff-from-dry-run-to-live-trading)** in `CLAUDE.md`. Short version: mint a separate Kraken trade key (Withdraw OFF), set it in `.env` as `KRAKEN_TRADER_API_KEY`, run `cli/preflight` to verify Kraken accepts your config, then `cli/live` for the operational loop. The first session is the highest-risk session — watch it.

---

## Project Structure

```
wobblebot/
├── src/wobblebot/          # Application code
│   ├── domain/            # Core models & business logic (zero adapter imports)
│   ├── ports/             # Abstract interfaces (the contracts adapters implement)
│   ├── adapters/          # Concrete implementations (Kraken, SQLite, shadow, mock)
│   ├── services/          # Orchestrators wiring ports to flows
│   ├── cli/               # Operator entry points
│   └── config/            # Pydantic schemas + YAML loader + profile resolver
├── tests/                 # 1785 unit + 29 integration; mirrors src/
├── docs/                  # Architecture, planning, implementation, reference
│   ├── architecture/      # System design, constraints, ADRs
│   ├── implementation/    # Coding guidelines, module specs, deployment guide
│   ├── planning/          # Roadmap, requirements, testing plan
│   └── reference/         # Kraken API reference
├── config/                # settings.example.yml + prompts/ (operator-editable)
└── scripts/               # install-hooks.{sh,ps1} for the pre-commit hook
```

---

## Development Workflow

### Running Tests

```bash
pytest                       # default — 1785 unit tests, integration excluded
pytest -m unit               # explicitly unit only
pytest -m integration        # opt-in: 29 integration tests (some hit live Kraken)
pytest tests/path/to/test_file.py::TestClass::test_name   # one test
```

### Code Quality

```bash
black src/ tests/            # format
isort src/ tests/            # imports
mypy src/                    # type check (strict)
pylint src/                  # lint (currently 10.00/10)
make check                   # all of the above + tests
```

`pyproject.toml` config gotchas: `addopts` always runs with coverage; `filterwarnings = ["error", ...]` makes warnings other than `DeprecationWarning` fail the suite; only `unit`, `integration`, `slow` markers are valid.

### Schema-drift safety net

If you edit `config/settings.example.yml` or `.env.example`, the tests in `tests/config/test_schema_drift.py` enforce that operator copies stay in sync. Set `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` to fail (instead of warn) when an operator file is missing keys the example documents — useful in CI.

---

## Architecture

WobbleBot follows **hexagonal architecture** with strict layer boundaries:

- **Domain** — pure business logic, zero external I/O. `domain/` must not import from `adapters/`, `services/`, or `cli/`.
- **Ports** — abstract interfaces defining contracts. Adapters implement; services depend on these.
- **Adapters** — concrete implementations (KrakenAdapter, SQLiteStorageAdapter, ShadowExchangeAdapter, MockExchangeAdapter, ...).
- **Services** — orchestrators wiring ports to flows. The only place that knows multiple modules exist.
- **CLI / Config** — operator entry points and Pydantic-validated configuration.

All cross-module wiring happens via constructor dependency injection of port interfaces.

See [`docs/architecture/`](docs/architecture/) for the full architecture guide and [`docs/architecture/decisions.md`](docs/architecture/decisions.md) for the eighteen ADRs that drive code structure.

---

## Safety Invariants

The most important design constraint: **financial power is fragmented**.

1. Only **Harvester** initiates fund transfers (Kraken withdrawal API per ADR-004).
2. The Kraken **trading** API key must NOT have withdrawal permissions. The Phase 4 Harvester key is separate.
3. **LLM advisor cannot execute trades.** JSON-schema-validated recommendations only; bounded auto-tuning is opt-in and constrained by `max_*_change_percentage`.
4. **News-derived advisor recommendations NEVER auto-apply** regardless of bounds (ADR-007).
5. Max exposure caps + daily spend limits are enforced inside Bot Core, not at the adapter layer.

Full constraint list: [`docs/architecture/constraints.md`](docs/architecture/constraints.md).

---

## Documentation

- **Architecture:** [`docs/architecture/`](docs/architecture/) — start with `README.md`, then `architecture-components.md`, `constraints.md`, `decisions.md`.
- **Implementation:** [`docs/implementation/`](docs/implementation/) — `coding-guidelines.md`, `module-specs.md`, `deployment-guide.md`.
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md) at the repo root.
- **Planning:** [`docs/planning/`](docs/planning/) — `roadmap.md` (current phase + per-stage receipts), `requirements.md`, `testing-plan.md`.
- **Kraken API reference:** [`docs/reference/kraken-api-reference.md`](docs/reference/kraken-api-reference.md).
- **Project guide for AI assistants:** [`CLAUDE.md`](CLAUDE.md) — phase status, ratified design decisions, and project-specific conventions.

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version: read the [current phase and stage](docs/planning/roadmap.md) first, follow the [coding guidelines](docs/implementation/coding-guidelines.md), respect the [architectural constraints](docs/architecture/constraints.md), and don't implement Phase N+1 features until Phase N is stable.

---

## Security

Found a vulnerability? See [`SECURITY.md`](SECURITY.md). Please report privately via GitHub's Security Advisories rather than opening a public issue.

---

## License

MIT — see [`LICENSE`](LICENSE) for details.
