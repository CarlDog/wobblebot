# WobbleBot

**Deterministic, safety-first micro-trading system on Kraken using hexagonal architecture.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![CI](https://github.com/CarlDog/wobblebot/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/CarlDog/wobblebot/actions/workflows/docker-publish.yml)

> **⚠ Disclaimer.** WobbleBot is a personal hobby project. It places real
> orders against real money on a real exchange when you run it in `live`
> mode. **YOU CAN LOSE REAL MONEY.** The code is provided "AS IS" with no
> warranty (see [`LICENSE`](LICENSE)). Nothing here is investment advice;
> the maintainer is not your financial advisor. Audit the code, test in
> `cli/shadow` first, set hard caps you can afford to lose, and only
> proceed if you understand what you're running.
>
> WobbleBot is **not affiliated with or endorsed by** Kraken, Payward Inc.,
> Discord, Anthropic, OpenAI, Google, Ollama, or AtlasCloudAI. See [`NOTICE.md`](NOTICE.md)
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

The roadmap owns current phase, release, quality, and real-money receipts. Release history is
summarized in [`CHANGELOG.md`](CHANGELOG.md); do not copy point-in-time counts or phase tables from
either file into this README.

---

## Operator Entry Points

Every CLI accepts `--config PATH` and `--profile NAME` for YAML-driven configuration with deep-merge profile overrides; per-CLI flags override both. The table below names the primary surfaces; each module's `--help` is authoritative for flags and current behavior.

| CLI | Phase | Touches money? | Purpose |
|---|---|---|---|
| `python -m wobblebot.cli.sandbox` | 1 | ❌ | Mock-only paper buy-dip / sell-rebound cycle through `MockExchangeAdapter` + SQLite. |
| `python -m wobblebot.cli.status` | 2.1 | ❌ | Live Kraken read check — fetches current price + account balances. Read-only API key. |
| `python -m wobblebot.cli.observe` | 3.0 | ❌ | Pure data collection — polls Ticker per symbol, persists snapshots; tops up completed 60m bars hourly (P2). `--backfill` mode (P2 slice 1) pulls historical OHLC with `--days`/`--catchup`/`--resume`/`--intervals`/`--rate-limit-seconds`; the live endpoint retains only ~720 bars/interval — deeper history comes from `tools/import_kraken_history.py`. Read-only API key. |
| `python -m wobblebot.cli.lurker` | 3.0 | ❌ | One-line alias of `cli/observe` today (own `__main__`); reserved to grow advisor commentary on top of pure observation later. |
| `python -m wobblebot.cli.shadow` | 3.0 | ❌ | Same engine as `cli/live` against a synthetic balance ledger with live Kraken prices. Honest maker/taker fee modeling. |
| `python -m wobblebot.cli.preflight` | 2.3 | ❌ | Diagnostic: runs ONE engine step against live Kraken with `validate=true`. Verifies Kraken accepts the config without spending. **Run this before every live session.** |
| `python -m wobblebot.cli.live` | 2.3+2.4 | **✅ REAL MONEY** | Multi-asset operational loop. Hard caps: max session loss, max runtime, per-coin / total / daily-spend exposure. Clean SIGINT cancels all open orders. |
| `python -m wobblebot.cli.news` | 3.2.5 | ❌ | Long-running news poller (7 RSS feeds + Kraken's exchange-status feed; CryptoCompare retired — CoinDesk Data ended free API access 2026-05-21, off by default everywhere, re-enable requires a paid plan). Persists items to `news_items` with `(source, external_id)` dedup. Per-source fault isolation. |
| `python -m wobblebot.cli.advise` | 3.3 / 3.4a | ❌ | Long-running advisor daemon. Builds a `PerformanceSummary` from observe + news on a `schedules.advise` cadence, calls the configured advisor (single-LLM Ollama OR MoE with 2+ experts + optional arbitrator), persists `AdvisorSuggestion` rows for operator review. **Never mutates running config** — that's `cli/apply`'s job. |
| `python -m wobblebot.cli.apply` | 3.4b | ❌ (config writes) | Operator-in-the-loop auto-apply gate. Dry-run by default; `--commit` rewrites `settings.yml` (ruamel.yaml, comment-preserving) and persists an `AppliedSuggestion` audit row. Gate defaults OFF (`auto_apply.enabled=False`); news-role suggestions never auto-apply per ADR-007. |
| `python -m wobblebot.cli.harvest` | 4.2-4.4 | Daemon ❌ / `--execute` **✅ REAL MONEY** | Treasury monitor. Daemon mode polls Kraken USD balance, runs `propose_transfer()`, persists every non-None proposal to `transfer_proposals`, logs "HYPOTHETICAL proposal". `--execute <proposal-id>` runs seven defense layers, calls Kraken `/Withdraw`. The ONLY path by which money leaves the exchange. |
| `python -m wobblebot.cli.operator` | 5.6 | ❌ (chat surface only) | Discord-backed operator interaction daemon (ADR-013). Maintains a Gateway connection, drains the `notifications` SQLite table to Discord, parses inbound operator messages via `OllamaAssistantAdapter` into typed `OperatorIntent` payloads — Command → writes `PendingCommand` + posts confirm embed (cli/live polls the approved rows; that's the ADR-002 firewall); Query → reads engine + storage state via `OperatorService` and replies; Conversational / Unparseable → text reply. Background TTL expirer transitions abandoned `awaiting_confirmation` rows to `expired`. |
| `python -m wobblebot.cli.web` | 7.1 | ❌ (read-mostly; ADR-013-firewalled mutations) | FastAPI + Jinja2 + HTMX dashboard. `serve` subcommand boots uvicorn against `127.0.0.1:8000` (operator's reverse proxy fronts the LAN); `create-user` seeds a bcrypt-hashed `users` row. Status / cost / advisor / harvester / news / audit views. Pause/resume/stop buttons create `PendingCommand` rows in `awaiting_confirmation` — cli/live's `WHERE status='approved'` poll stays the only path from intent to engine. |
| `python -m wobblebot.cli.recalibrate` | 7.6 | ❌ (config writes) | Scales every USD-denominated knob in `settings.yml` proportionally to a new `--target-balance`. Reads live Kraken USD balance via the read-only key by default; `--current-balance X` overrides for what-if analysis. `--commit` rewrites `settings.yml` (ruamel.yaml, comment-preserving, atomic). Spacing %, level counts, max_loss_percentage, shadow:* are policy invariants and stay constant. |
| `python -m wobblebot.cli.maintenance` | 8.2+ | ❌ | Background maintenance daemon. Seven isolated scheduled tasks: VACUUM, prune+archive, backup, backup verification, Kraken-vs-local trade reconciliation, capital report (ADR-040), and ledger sync. Current cadence/config details live in the module's `--help` and `config/settings.example.yml`. |
| `python -m wobblebot.cli.screener` | P2.5 | ❌ (offline; no credentials) | Rank observed symbols by grid-suitability (P2 slice 5). Reads stored 60m bars only; vol + ATR% scored as distance-from-band-center (non-monotonic), flatness descending, rank-based composite; strongest \|Pearson\| vs the live lineup as a post-score annotation. Advisory only (ADR-002); log-table output, no DB table. |
| `python tools/first_real_trade.py` | 2.3 | **✅ REAL MONEY** | One-shot diagnostic: marketable round-trip with hard caps. Used 2026-05-15 against the operator's account; total cost $0.08. |
| `python tools/run_cloud_check.py` | 6.5 | **✅ REAL MONEY** (tiny) | One-shot cloud-LLM smoke test (`--provider anthropic|openai|google|atlas` / `--role` / `--model`). Lives under `tools/` (diagnostic, not daemon). |
| `python tools/import_kraken_history.py` | P2.2 | ❌ (offline) | Stream the local Kraken OHLCVT dump (base + quarterly CSVs) into `ohlc_bars`/`price_snapshots` through the idempotent write paths. The only deep-history path (the live OHLC endpoint retains ~720 bars/interval). |
| `python tools/auditor.py` | P2.4 | ❌ (pure replay) | Replay `settings.yml` through the real `GridEngine` over stored bars (ADR-028): fills/fees/cycles/PnL/drawdown per symbol. Directional, not exact; `_Sim`-equivalent at 1m. |
| `python tools/reconcile_trade_history.py` | operations | ❌ (read-only) | One-shot Kraken-vs-`live.db` trade/ledger reconciliation and backfill runbook; deeper manual companion to maintenance's daily reconciliation task. |

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
pytest                          # default unit suite with coverage
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
├── tests/                 # Unit/integration suites mirroring src/
├── docs/                  # Architecture, planning, implementation, reference
│   ├── architecture/      # System design, constraints, ADRs
│   ├── implementation/    # Coding guidelines, module specs, deployment guide
│   ├── planning/          # Roadmap, requirements, testing plan
│   └── reference/         # API, model-evaluation, and external-repository references
├── config/                # settings.example.yml + prompts/ (operator-editable)
├── scripts/               # install-hooks.{sh,ps1} for the pre-commit hook
└── vendor/                # third-party tools, vendored as git submodules
```

---

## Third-Party Tools

- **[Atlas Cloud CLI](https://github.com/AtlasCloudAI/cli)** (vendored as a
  git submodule at `vendor/atlascloud-cli`) — credit to AtlasCloudAI. This
  is a manual dev/ops tool for checking Atlas Cloud account balance, model
  availability, and API connectivity from the shell; wobblebot's own runtime
  code does **not** call it. The advisor's Atlas Cloud adapter talks to
  `https://api.atlascloud.ai/v1` directly via the existing
  `OpenAIAdvisorAdapter` (see `.env.example`'s Atlas Cloud section) — the
  CLI is a separate, optional tool, not a dependency of that path. After
  cloning, run `git submodule update --init --recursive` to pull it in. The
  gitlink pins the wrapper/docs snapshot, not the binary fetched by its
  installer; see [`vendor/README.md`](vendor/README.md) for the explicit-version
  and update-review policy.

---

## Development Workflow

### Running Tests

```bash
pytest                       # default unit suite, integration excluded
pytest -m unit               # explicitly unit only
pytest -m integration        # opt-in integration suite; inspect markers/credentials first
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

If you edit `config/settings.example.yml` or `.env.example`, the tests in `tests/config/test_schema_drift.py` enforce that operator copies stay in sync. Set `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` to fail (instead of warn) when an operator file is missing keys the example documents. The repo's own `.env` sets it and `tests/conftest.py` loads that, so it is already active for local runs; it is deliberately **not** wired into CI yet (a queued audit finding from 2026-09-04 — the previous wording here said "useful in CI", which read as though it were).

---

## Architecture

WobbleBot follows **hexagonal architecture** with strict layer boundaries:

- **Domain** — pure business logic, zero external I/O. `domain/` must not import from `adapters/`, `services/`, or `cli/`.
- **Ports** — abstract interfaces defining contracts. Adapters implement; services depend on these.
- **Adapters** — concrete implementations (KrakenAdapter, SQLiteStorageAdapter, ShadowExchangeAdapter, MockExchangeAdapter, ...).
- **Services** — orchestrators wiring ports to flows. The only place that knows multiple modules exist.
- **CLI / Config** — operator entry points and Pydantic-validated configuration.

All cross-module wiring happens via constructor dependency injection of port interfaces.

See [`docs/architecture/`](docs/architecture/) for the full architecture guide and [`docs/architecture/decisions.md`](docs/architecture/decisions.md) for the ADRs that drive code structure.

---

## Safety Invariants

The most important design constraint: **financial power is fragmented**.

1. Only **Harvester** initiates fund transfers (Kraken withdrawal API per ADR-004).
2. The Kraken **trading** API key must NOT have withdrawal permissions. The Harvester key is separate.
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
- **Project guide for AI assistants:** [`AGENTS.md`](AGENTS.md) / [`CLAUDE.md`](CLAUDE.md) — repository conventions and pointers to authoritative status/decision records.

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version: read the [current phase and stage](docs/planning/roadmap.md) first, follow the [coding guidelines](docs/implementation/coding-guidelines.md), respect the [architectural constraints](docs/architecture/constraints.md), and don't implement Phase N+1 features until Phase N is stable.

---

## Security

Found a vulnerability? See [`SECURITY.md`](SECURITY.md). Please report privately via GitHub's Security Advisories rather than opening a public issue.

---

## License

MIT — see [`LICENSE`](LICENSE) for details.
