# Project Roadmap – Phases & Stages

WobbleBot’s development is organized into **five phases**, each containing **five stages**.  We build like a house: lay the foundation, frame the structure, wire up systems, finish the surfaces, then polish and decorate.  This roadmap lays out what gets built when; it is a *guiding structure*, not a rigid contract—phases and stages may be merged or adjusted as we learn.

## Phase 1 – Foundation & Sandbox ✅ Complete (2026-05-13)

**Goal:** Bootstrapped skeleton of WobbleBot with no real trading risk.

1. **Stage 1.1 – Repo & Scaffolding** ✅ (2025-11-24) – Create the repository structure (`src/`, `docs/`, `config/`, `docker/`).  Add base Python project configuration, linting, and formatting tools.
2. **Stage 1.2 – Hex Core Skeleton** ✅ (2025-11-24) – Define core domain models (`Order`, `Trade`, `Position`).  Define abstract ports (e.g., `ExchangePort`, `StoragePort`, `AdvisorPort`, `HarvesterPort`, `NotifierPort`).
3. **Stage 1.3 – Storage & Logging Backbone** ✅ (2026-05-12) – Implement the SQLite adapter and configure logging.  Provide basic persistence for trades, configuration snapshots, and events.
4. **Stage 1.4 – Kraken Mock & Simulation Mode** ✅ (2026-05-12) – Implement a fake exchange adapter for dry‑run simulations.  Support a paper trading loop with hard‑coded scenarios.
5. **Stage 1.5 – Phase 1 Integration Check** ✅ (2026-05-13) – Demonstrate an end‑to‑end simulated cycle: load config → run core loop against the mock exchange → persist results → view logs.  No external API calls yet.

## Phase 2 – Core Trading Engine (Real Kraken, No Money Moves)

**Goal:** Deterministic micro-grid trading against **real Kraken** with tiny exposure and **no withdrawals**.

1. **Stage 2.1 – Kraken Adapter (Read‑Only + Minimal Data Collector)** ✅ (2026-05-14) – Integrate with the Kraken API using a read‑only key.  Fetch tickers, order books, and account balances.  Provide a minimal `DataCollector v1` that supplies current prices and balances to the Bot Core.  *Read paths only (Ticker + BalanceEx). Order placement, OpenOrders, and TradesHistory parsing deferred to Stage 2.3. Live integration verified via `python -m wobblebot.cli.check`.*
2. **Stage 2.2 – Micro-Grid Engine** ✅ (2026-05-14) – Implement the configurable grid logic per asset (grid boundaries, spacing, order sizing).  Enforce per‑coin caps on maximum orders and maximum funds in play.  *Five slices landed: config schemas (`GridConfig`, `SafetyConfig`, YAML loader), pure grid math (`compute_grid_levels`, `next_counter_action`, `is_offside`), `GridEngine` service with `GridState` persistence, safety cap enforcement (per-coin/total exposure + daily-spend), end-to-end integration test (1000-tick oscillation, 500 cycles, positive realized P&L). Six ratified design decisions in ADR-006. Counter orders match filled-order base amounts. Engine wires to `MockExchangeAdapter`; Stage 2.3 swaps in real Kraken via the `ExchangePort` contract.*
3. **Stage 2.3 – Live Paper Mode / Tiny‑Size Mode** ✅ (2026-05-14) – Enable live trading against Kraken with minimal order sizes (or full paper mode via configuration).  Track profit and loss, cycle counts, and basic volatility metrics.  *Five slices landed: KrakenAdapter trading methods + AssetPairs precision cache, live integration tests via validate=true, cli/validate diagnostic CLI, cli/grid operational CLI with hard caps + clean SIGINT shutdown, plus stage-close operator handoff docs. Verified live with tools/first_real_trade.py: real-money round-trip on the operators account cost $0.08 (2x 0.40% taker fee), 148ms fill latency, perfect cleanup. Live taker fee 0.40% vs mocks 0.26% — only matters for marketable orders; engines normal maker-side use case unaffected.*
4. **Stage 2.4 – Multi‑Asset Support** ✅ (2026-05-14) – Extend the core to run grids for multiple whitelisted coins (e.g., DOGE, ADA, SOL, MATIC, ETH).  Enforce shared safety rules such as daily spend caps and global exposure limits.  *cli/grid takes --symbols comma-separated; each tick steps every symbol in series through the same GridEngine. Per-symbol step errors swallowed at the CLI so one bad coin cannot kill the session. Caps: total/daily-spend global across symbols; per-coin scoped per symbol. Engine layer required ZERO changes (every per-coin entity already keys by symbol, hex layer purity paid off again). Five new multi-coin engine tests; 296 unit tests pass total.*
5. **Stage 2.5 – Phase 2 Integration Check** ✅ (2026-05-14) – Demonstrate a full pipeline: configuration → live Kraken adapter + `DataCollector v1` → micro-grid engine → logs and database entries.  All withdrawals remain disabled at the API level.  *Live multi-coin verification: cli/grid --symbols BTC/USD,ETH/USD ran 304.6s against the operators account, 54 ticks per coin, 0 fills, 6/6 open orders cleanly cancelled on runtime-cap shutdown, session PnL $0.0000. Closing summary at docs/planning/phase-2-summary.md. Phase 2 total real-money cost across both verifications: $0.08.*

## Phase 3 – Strategy Advisor & Analytics

**Goal:** Add intelligence and observability without giving the LLM any power over execution.

1. **Stage 3.1 – Data Collector & Metrics (v2)** – Extend the data collector to centralize historical pricing and compute derived metrics (volatility, cycle counts, win rates, flatness, drawdown, etc.).
2. **Stage 3.2 – Advisor Port & Local LLM Integration** – Implement an `AdvisorPort` and an LLM adapter (e.g., via Ollama).  Define and enforce a JSON schema for recommendations.
3. **Stage 3.3 – Advisory Workflows (Passive)** – Periodically send summarized performance data to the Advisor.  Store LLM‑produced JSON suggestions in the database; do not auto‑apply yet.
4. **Stage 3.4 – Optional Auto‑Tuning (Guarded)** – Provide a configuration option to auto‑apply safe, bounded recommendations (e.g., adjust grid spacing within pre‑configured limits).  Enforce strict range checks and safety rules.
5. **Stage 3.5 – Phase 3 Integration Check** – Demonstrate an “advisor‑in‑the‑loop” run: trading engine runs, advisor produces suggestions, operator reviews them.  Auto‑application is optional.

## Phase 4 – Harvester & Treasury Management

**Goal:** Compartmentalized module that manages **funds transfers**, not trades.

1. **Stage 4.1 – Harvester Domain & Ports** – Define the Harvester domain model and `HarvesterPort`.  Capture rules for minimum Kraken liquidity, surplus scraping, and top‑up thresholds.  Per ADR-004, Harvester uses Kraken's withdrawal API (via ExchangePort) rather than separate banking integration.
2. **Stage 4.2 – Read‑Only Balance Monitoring** – Harvester reads Kraken balances and, if available, bank balances.  Log hypothetical transfers without moving any money.
3. **Stage 4.3 – Passive Mode Transfers** – Harvester produces “transfer proposals” (amount, direction, and rationale) for manual review.  Orchestrator surfaces proposals via logs or notifications.
4. **Stage 4.4 – Active Mode (Guarded Withdrawals)** – Enable actual **Kraken → bank withdrawals** within strict caps.  Automated bank → Kraken deposits may remain manual until additional ADRs and safety reviews are completed.
5. **Stage 4.5 – Phase 4 Integration Check** – Demonstrate a scenario in which trading grows the exchange balance, Harvester scrapes the surplus, and the audit trail confirms the actions.  Confirm that no unauthorized transfers occur.

## Phase 5 – UX, Dashboard, Hardening & Polish

**Goal:** Provide visibility, ergonomics, and resilience.

1. **Stage 5.1 – Operational Dashboard** – Implement a web UI or Grafana dashboards to display balances, P&L, cycles, harvested funds, and advisor suggestions.
2. **Stage 5.2 – Control Surface** – Provide controls to pause/resume per‑coin trading, toggle advisor/harvester modes, and adjust configurations via UI or CLI.
3. **Stage 5.3 – Reliability & Recovery** – Implement robust startup/shutdown behavior.  Support recovery from restarts: reload positions, open orders, and pending transfers without duplicating actions.  Introduce reconciliation logic to match the database state with the exchange.
4. **Stage 5.4 – Performance & Resource Tuning** – Tune polling intervals, batch operations, and database usage to fit Synology NAS resource constraints.  Optimize heavy processes (e.g., metrics computation) for responsiveness.
5. **Stage 5.5 – Phase 5 / v1.0 Release Check** – Run an extended soak test (weeks) under a low‑risk configuration.  Finalize v1.0: update documentation (architecture, planning, implementation), tag the release, and produce a changelog.  Identify known limitations and areas for future improvements.

## Phase Dependencies

Phases are strictly ordered—Phase N must be stable before Phase N+1 begins.  Within a phase, stages generally progress in order, but minor overlaps are allowed when safe.  Changes to the architecture or requirements should be captured via ADRs and reflected back into the respective docs.
