# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Source of truth:** `docs/planning/roadmap.md`. Each completed stage carries a ✅ completion date.

**Phase 2 is complete as of 2026-05-14.** All five stages (2.1, 2.2, 2.3, 2.4, 2.5) closed in a single evening session. Closing summary lives at `docs/planning/phase-2-summary.md`. Total real-money cost across both live verifications: **$0.08** (the $0.08 first-trade test in `tools/first_real_trade.py` + the $0.00 multi-coin grid run in Stage 2.5). Five operator entry points work end-to-end:

- `python -m wobblebot.cli.simulate` — Phase 1 sandbox: buy-dip/sell-rebound cycle through `MockExchangeAdapter` + `SQLiteStorageAdapter`, persists to SQLite.
- `python -m wobblebot.cli.check` — Stage 2.1 live read check: read-only Kraken price + balance fetch.
- `python -m wobblebot.cli.validate` — Stage 2.3 diagnostic: runs ONE engine step against live Kraken with `KrakenAdapter(dry_run=True)`. Every order goes through Kraken's `validate=true` flag — request is signed, sent, validated end-to-end (auth / pair / precision / balance / ordermin / costmin) without placing. **Use this before every live run to confirm the config is acceptable to Kraken.**
- `python -m wobblebot.cli.live` — Stage 2.3 operational loop, **multi-asset since Stage 2.4**. Real-money trading. `--symbols BTC/USD,ETH/USD,DOGE/USD` accepts a comma-separated list; each tick steps every symbol in series. Hard caps (max session loss, max runtime, per-coin / total / daily-spend exposure) — total/daily caps are global across symbols, per-coin caps are per-symbol. Clean SIGINT/SIGTERM shutdown cancels every open order on every symbol. Exit codes: 0 clean stop, 1 loss-cap tripped, 2 missing creds. *(Originally `cli/grid`; renamed during Phase 3 sandbox prep per ADR-008 to make the live-money distinction loud vs the planned `cli/shadow`.)*
- `python tools/first_real_trade.py` — one-shot diagnostic: places a far-from-market BUY (cancels it) + a marketable BUY/SELL round-trip with hard caps. Forensic JSONL log to `data/`. Used 2026-05-15 00:51 UTC against the operator's account; total cost $0.08 (two 0.40% taker fees on a $10 round-trip; spread effectively zero).

296 unit tests pass by default; 21 integration tests (5 Kraken API drift + 3 live read + 2 simulator + 2 grid e2e + 9 live trading) on opt-in. mypy clean (33 src files), black/isort clean, pylint **9.98/10** on `src/`.

### Operator handoff: from dry-run to live trading

1. **Mint a Kraken trading key**, separate from the read-only key (per ADR-003-style separation). Permissions: Query Funds + Query open & closed orders & trades + Create & modify orders + Cancel & close orders. **Withdraw must stay off** — that scope is exclusive to the future Phase 4 Harvester key. Recommended: enable IP address restriction.
2. **Stash credentials in `.env`** as `KRAKEN_TRADE_API_KEY` / `KRAKEN_TRADE_API_SECRET` (separate from the existing `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` so the read-only key can keep being used for `cli/check`).
3. **Run `cli/validate`** — confirm Kraken accepts the grid config without spending anything. Exit 0 means every layout order would be accepted by Kraken's matching engine.
4. **Run `cli/live`** with eyes on the Kraken Pro Orders + Trade History tab. Defaults: $10 per order, 1% spacing, 3 above + 3 below = $60 total exposure, $5 max session loss, 60 minute max runtime, 5s tick. The first session is the highest-risk session — watch it.

### Stage 2.3 design decisions ratified (do not relitigate without an ADR)

- **Dry-run = `validate=true`.** `KrakenAdapter(config, dry_run=True)` adds `validate=true` to every AddOrder request. Kraken validates auth + pair + precision + balance + ordermin + costmin without placing. The adapter synthesizes a `DRYRUN-<order.id>` exchange_id so the engine's bookkeeping path still works for diagnostic runs.
- **Per-pair precision quantization is mandatory.** AssetPairs cache (`pair_decimals`, `lot_decimals`, `ordermin`, `costmin`) populated lazily on first trading call. Price/volume rounded DOWN before submission — never up, since rounding up could push spending past the engine's intended `order_size_usd` budget.
- **Two separate Kraken keys, not one.** The read-only key (`cli/check`) and the trade key (`cli/validate` / `cli/live`) live side-by-side in `.env`. `KrakenConfig.from_env(key_var=..., secret_var=...)` parameterizes which env vars to read.
- **Live taker fee is 0.40%, not the mock's 0.26%.** Discovered during the 2026-05-15 first-trade test: $0.04 fee on each $9.99 leg of a marketable round-trip = 0.40%. The mock uses 0.26% (Kraken maker rate, conservative). The grid engine in normal operation places limit orders that sit on the book — those collect MAKER fees, so the mock's assumption is right *for the engine's normal mode*; the gap only shows up on marketable orders (which the engine doesn't normally place).
- **Cleanup discipline in the loop.** `cli/live`'s shutdown path cancels every open order for the symbol in a `finally` block, regardless of why the loop ended (signal, runtime cap, loss cap, exception). The session-end log records before/after USD balance, session PnL, cancellations succeeded/failed.

### Stage 2.4 design decisions ratified

- **Symbols step in series within a tick.** Per ADR-006 decision 5, the per-symbol asyncio.Lock makes parallelization safe — but at measured ~150ms per-symbol latency vs the 5s tick budget, even a 30-coin serial sweep finishes in well under one tick. Parallelization (asyncio.gather) deferred to Phase 5 hardening if profiling ever shows the master-task throughput is a bottleneck.
- **Per-symbol step errors are swallowed at the CLI layer.** One bad coin (network blip, Kraken returning EService:Unavailable) cannot kill the tick or the session. The engine surfaces the error; `_run_one_tick` logs it with structured fields and continues to the next symbol.
- **Caps split: total/daily are global, per-coin is per-symbol.** `max_total_exposure_usd` and `max_daily_spend_usd` count across every coin (computed via unfiltered `storage.get_open_orders()` / `storage.get_orders(side="buy", created_after=today)`). `max_per_coin_exposure_usd` and `max_orders_per_coin` are scoped to one symbol via the symbol filter. Same SafetyConfig instance passed to GridEngine; the engine's `_check_safety` was already symbol-aware.
- **`--symbols` deduplicates and preserves order.** Comma-separated input. Trailing/leading whitespace tolerated. Empty entries from trailing commas silently dropped.

**Next:** Phase 3 — Strategy Advisor & Analytics. Engine and adapter layers do not change; Phase 3 sits on top:
- **Stage 3.1:** Data Collector v2 — extend the existing `DataCollector` to compute volatility, cycle counts, win rates, drawdown over the trades/orders/balance_snapshots history.
- **Stage 3.2:** `AdvisorPort` + local LLM (Ollama) adapter producing JSON-schema-validated recommendations. Per ADR-002, advisor is advisory-only — no execution authority.
- **Stage 3.3:** Passive advisory workflow — periodically send summarized performance to advisor; persist suggestions; do not auto-apply.
- **Stage 3.4:** Optional auto-tuning, bounded by configured min/max ranges.
- **Stage 3.5:** Phase 3 integration check.

Phase 3 introduces no new live-money risk over Phase 2 — the advisor layer can't execute. Suitable for fresh-eyes daylight work.

**Design decisions ratified during Phase 1 + Stage 2.1 (do not relitigate without an ADR):**

*Domain / safety:*
- `Balance` is an immutable snapshot (`frozen=True`). Funds "locked for an order" come from Kraken's `hold_trade` (live) or are derived from the open-order set (mock).
- `OrderSide` is a `StrEnum` (`OrderSide.BUY`, `OrderSide.SELL`), not a Pydantic model. SQL drivers and JSON serialize it as the plain string value.
- Port error convention: domain-data miss returns `T | None`, protocol/transport failure raises the port's error type (`ExchangeError`, `StorageError`, `DataCollectorError`, etc. — all in `wobblebot.ports.exceptions`).
- `StoragePort` callers must serialize per-entity writes themselves (no optimistic concurrency control in the adapter).
- `Timestamp` normalizes all tz-aware inputs to UTC so ISO 8601 string ordering matches chronological ordering.
- Pydantic mypy plugin is enabled in `pyproject.toml` and load-bearing — do not remove.

*Kraken adapter (Stage 2.1):*
- **DIY HMAC signing on `httpx`, not `python-kraken-sdk`.** SDK was considered and rejected: its only abstraction over httpx is signing + nonce + WebSocket; REST interface is generic `client.request("POST", path)`, same manual parsing burden. ~20 lines of crypto, gold-cased against Kraken's published example signature.
- **`/0/private/BalanceEx`, not `/0/private/Balance`.** BalanceEx returns `hold_trade` per asset, mapping straight to `Balance.locked`.
- **Asset/symbol aliasing lives in the adapter, not the domain.** Module-level `_INTERNAL_TO_KRAKEN_ALTNAME` for colloquial conventions (BTC↔XBT, DOGE↔XDG). Legacy X/Z-prefixed response codes (XXBT, ZUSD) resolve via a lazy `/0/public/Assets` cache. `Symbol.to_kraken_format()` removed from the domain — it violated hex-layer rules and was broken.
- **`pytest -m 'not integration'` is the default** via pyproject `addopts`. Integration tests opt in with `pytest -m integration`.
- **`.env` loaded session-wide via `python-dotenv` in `tests/conftest.py`.** Unit tests still use `monkeypatch.setenv` for isolation.

Before responding to any non-trivial request, read `docs/planning/roadmap.md` and cross-check that the requested work matches the current stage. If the user asks for Stage N+1 work while Stage N is in progress, name the drift before starting.

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

- **Architecture:** `docs/architecture/` (start with `README.md`, then `architecture-components.md`, `constraints.md`, `decisions.md`)
- **Implementation:** `docs/implementation/coding-guidelines.md`, `module-specs.md`, `development-workflow.md`
- **Planning:** `docs/planning/roadmap.md` (current phase), `requirements.md`, `testing-plan.md`, `stage-2.2-design.md` (next stage's slicing + ratified decisions)
- **Kraken API reference:** `docs/reference/kraken-api-reference.md`
- **Config example:** `config/wobblebot.example.yml` (real `config/wobblebot.yml` is gitignored)
- **Docker env example:** `docker/env.example` (Phase 2+ deployment)

## Project-Specific Conventions

- **Python 3.13+ required** (`requires-python = ">=3.13"`). Use `str | None`, `list[X]`, `match` statements — no `Optional`/`List` imports needed.
- **Never use `print()`.** Use the project logger (`wobblebot.config.logging.configure_logging`). Plain format renders message-only; put operator-facing data in the message string and structured fields in the `extra=` dict so JSON consumers see them too.
- **Pydantic v2 models** for structured data (domain entities, config schemas).
- **Async ports:** `ExchangePort` and other I/O-bound ports are `async`. Use `pytest-asyncio` for tests of async code.
- **Line length 100** (black + isort + pylint all configured to this).
- **Keep files under ~300-400 lines.** Split modules that turn into junk drawers.
- **No `print()`, no swallowed exceptions, no real network calls in unit tests.** Use mocks/stubs (`httpx.MockTransport` is the test seam for `KrakenAdapter`). Integration tests carry the `integration` marker and are excluded from the default `pytest` run via `addopts`; run them explicitly with `pytest -m integration`.
