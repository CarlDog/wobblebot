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

1. **Stage 2.1 – Kraken Adapter (Read‑Only + Minimal Data Collector)** ✅ (2026-05-14) – Integrate with the Kraken API using a read‑only key.  Fetch tickers, order books, and account balances.  Provide a minimal `DataCollector v1` that supplies current prices and balances to the Bot Core.  *Read paths only (Ticker + BalanceEx). Order placement, OpenOrders, and TradesHistory parsing deferred to Stage 2.3. Live integration verified via `python -m wobblebot.cli.status`.*
2. **Stage 2.2 – Micro-Grid Engine** ✅ (2026-05-14) – Implement the configurable grid logic per asset (grid boundaries, spacing, order sizing).  Enforce per‑coin caps on maximum orders and maximum funds in play.  *Five slices landed: config schemas (`GridConfig`, `SafetyConfig`, YAML loader), pure grid math (`compute_grid_levels`, `next_counter_action`, `is_offside`), `GridEngine` service with `GridState` persistence, safety cap enforcement (per-coin/total exposure + daily-spend), end-to-end integration test (1000-tick oscillation, 500 cycles, positive realized P&L). Six ratified design decisions in ADR-006. Counter orders match filled-order base amounts. Engine wires to `MockExchangeAdapter`; Stage 2.3 swaps in real Kraken via the `ExchangePort` contract.*
3. **Stage 2.3 – Live Paper Mode / Tiny‑Size Mode** ✅ (2026-05-14) – Enable live trading against Kraken with minimal order sizes (or full paper mode via configuration).  Track profit and loss, cycle counts, and basic volatility metrics.  *Five slices landed: KrakenAdapter trading methods + AssetPairs precision cache, live integration tests via validate=true, cli/preflight diagnostic CLI, cli/live operational CLI with hard caps + clean SIGINT shutdown, plus stage-close operator handoff docs. Verified live with tools/first_real_trade.py: real-money round-trip on the operators account cost $0.08 (2x 0.40% taker fee), 148ms fill latency, perfect cleanup. Live taker fee 0.40% vs mocks 0.26% — only matters for marketable orders; engines normal maker-side use case unaffected.*
4. **Stage 2.4 – Multi‑Asset Support** ✅ (2026-05-14) – Extend the core to run grids for multiple whitelisted coins (e.g., DOGE, ADA, SOL, MATIC, ETH).  Enforce shared safety rules such as daily spend caps and global exposure limits.  *cli/live takes --symbols comma-separated; each tick steps every symbol in series through the same GridEngine. Per-symbol step errors swallowed at the CLI so one bad coin cannot kill the session. Caps: total/daily-spend global across symbols; per-coin scoped per symbol. Engine layer required ZERO changes (every per-coin entity already keys by symbol, hex layer purity paid off again). Five new multi-coin engine tests; 296 unit tests pass total.*
5. **Stage 2.5 – Phase 2 Integration Check** ✅ (2026-05-14) – Demonstrate a full pipeline: configuration → live Kraken adapter + `DataCollector v1` → micro-grid engine → logs and database entries.  All withdrawals remain disabled at the API level.  *Live multi-coin verification: cli/live --symbols BTC/USD,ETH/USD ran 304.6s against the operators account, 54 ticks per coin, 0 fills, 6/6 open orders cleanly cancelled on runtime-cap shutdown, session PnL $0.0000. Closing summary at docs/planning/phase-2-summary.md. Phase 2 total real-money cost across both verifications: $0.08.*

## Phase 3 – Strategy Advisor & Analytics ✅ Complete (2026-05-15)

**Goal:** Add intelligence and observability without giving any LLM power over execution.

**Stage 3.0 – Observer & Shadow Mode** ✅ (2026-05-14) – Two non-money-touching entry points landed before the advisor work begins, per ADR-008. `cli/observe` polls live Kraken Ticker on a configurable interval and persists prices + balance snapshots to SQLite — pure data collection, no engine, no LLM. `cli/shadow` swaps `KrakenAdapter` for a new `ShadowExchangeAdapter` that uses live Kraken for prices but matches orders against a synthetic balance ledger; same engine code as `cli/live`, no real money. Together: a 24/7 sandbox for everything in 3.1–3.5 to play in, plus a live-tape backtest framework. (Flavor B — `cli/lurker` = observer + advisor commentary on the live market without trading — is a natural follow-on once Stage 3.2 lands. As of 2026-05-15 `cli/lurker` exists as a one-line alias for `cli/observe` so the operator-facing name is reserved; when Stage 3.4ish lands, `cli/observe` stays the bare data-collection variant and `cli/lurker` becomes the richer "watch with LLM commentary" entry point.)

**Config consolidation audit** ✅ (2026-05-14) – Eight slices, no live-money risk. Closed before Stage 3.1 to clean up the config story. Slice 1: `config/settings.example.yml` redesigned as the operator-facing API + ADR-009 written. Slice 2: per-CLI Pydantic schemas (`LiveConfig`, `ShadowConfig`, `ObserveConfig`, `PreflightConfig`, `StatusConfig`, `SandboxConfig`) + `AdvisorConfig` (MoE + ≥3 experts validator). Slice 3: profile resolver with `deep_merge` semantics (dicts recurse, lists override). Slice 4 (a/b/c): renamed `cli/simulate→sandbox` / `cli/check→status` / `cli/validate→preflight`, then wired every CLI to load YAML via `wobblebot.config.runtime.load_resolved_config(...)` with `--config` + `--profile` + flag-override layering; profiles cover both `live` and `shadow`. Slice 5: prompt-file infrastructure (`python-frontmatter` dep + `config/prompts/{quant,risk,news,arbitrator}.md` + `wobblebot.config.prompts.load_prompt`). Slice 6: schema-drift tests for `settings.example.yml↔settings.yml` and `.env.example↔.env` pairs (one-way default; `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` for bidirectional). Slice 7: `docker/env.example` moved to repo-root `.env.example` and refreshed (`KRAKEN_TRADER_API_KEY`, cloud-LLM keys, harvester key for Phase 4). Slice 8: docs + memory close. 399 unit tests pass; mypy clean (42 src files); pylint 10.00/10.

1. **Stage 3.1 – Data Collector & Metrics (v2)** ✅ (2026-05-15) – Extend the data collector to centralize historical pricing and compute derived metrics (volatility, cycle counts, win rates, flatness, drawdown, etc.). *Four slices landed: StoragePort.get_price_snapshots + SQLiteStorageAdapter impl with a new PriceSnapshot domain model; pure-math services/metrics.py (compute_volatility, compute_max_drawdown, compute_flatness, compute_cycle_stats); DataCollector v2 takes (exchange, storage) and exposes get_price_history + windowed metric methods via timedelta lookback; tools/show_metrics.py inspection script (read-only, safe against the live observe DB). CycleStats moved to domain.models to keep ports out of the services import graph. 458 unit tests pass (was 401 at Phase 2 close); services/metrics.py and services/data_collector.py both at 100% line coverage; mypy clean (44 src files); pylint 10.00/10. Verified end-to-end against the live observe DB while cli/observe kept polling undisturbed.*
2. **Stage 3.2 – Advisor Port & Single-Model Integration** ✅ (2026-05-15) – Implement an `AdvisorPort` and a baseline single-LLM adapter (Ollama). Define and enforce a JSON schema for recommendations. Get the loop working end-to-end before adding complexity. *Five slices landed: Slice A reconciled `AdvisorRecommendation` with the prompt files' `advisor_recommendation_v1` wire format (renamed `config_changes`→`recommendations`, confidence is now `Literal['high','medium','low']`, added `role: str`) and extended `PerformanceSummary` with Stage 3.1 metrics. Slice B shipped `OllamaAdapter` (httpx-based, MockTransport test seam, AdvisorError wrapping). Slice C added single-mode `provider`/`model`/`prompt_file`/`inference_params` fields to `AdvisorConfig` with a required-when-single validator; default example yml flips to `type: single` (Ollama + quant.md) and the former MoE block becomes a `profiles.moe-advisor` profile. Slice D shipped `tools/run_advisor.py` (reads observe DB → builds PerformanceSummary → calls advisor → JSONL receipt). Slice E added thinking-model support: name-pattern detection for R1/o1/Thinker/Thinking/Reasoning families, drops `format: "json"` for them, and walks the response with `json.JSONDecoder.raw_decode` to pull the last JSON object out of the free-text body. Verified live against six local Ollama models (phi4:14b, qwq:32b, gemma3:27b, nous-hermes2-mixtral, mistral-nemo:12b, deepseek-r1:14b) — phi4 emerged as the strongest calibrated quant on the trial window; five working models converged on the same `spacing_percentage: 1.2` proposal, with confidence calibration being the meaningful differentiator. 523 unit tests pass (was 458 at Stage 3.1 close); mypy clean (45 src files); pylint 10.00/10.*
3. **Stage 3.2.5 – News Ingestion** ✅ (2026-05-15) – Implement a `NewsPort` and adapters for crypto news polling. Persist to a `news_items` SQLite table. No LLM consumption yet; just structured collection. Per ADR-007. *Five slices landed. The original ADR-007 plan named CryptoPanic + Whale-alert but both went paid-only (~$2,600/yr + ~$300/yr) since the ADR was written, so v1 pivoted to RSS + CryptoCompare (free; NewsPort kept abstract so paid sources can plug in later). Slice A: `NewsPort` + `NewsItem` frozen domain model + `news_items` table with `UNIQUE(source, external_id)` dedup + `save_news_item` / `get_news_items` on StoragePort. Slice B: `RssNewsAdapter` (feedparser-based, one instance per feed, `follow_redirects=True` per the live-verification finding). Slice C: `CryptoCompareAdapter` for `/data/v2/news/` (API key in `authorization` header, never query string). Slice D: `cli/news` long-running poller with per-source fault isolation. Slice E: `news:` config section with four default RSS feeds (CoinDesk, Decrypt, The Block, CoinTelegraph) + CryptoCompare enabled. Verified live: 131 items pulled in one poll across all four sources. 619 unit tests pass (was 525 at Stage 3.2 close); mypy clean (49 src files); pylint 10.00/10. New runtime dep: `feedparser`. CryptoCompare's redundancy with RSS is acknowledged — re-evaluate at 90 days (2026-08-13).*
4. **Stage 3.3 – Passive Advisory Workflow** ✅ (2026-05-15) – Engine periodically sends metrics + recent news to the advisor; advisor's JSON suggestions persist to an `advisor_suggestions` table. Operator reviews; nothing auto-applies. *Four-and-a-half slices landed. Slice A: new AdvisorSuggestion frozen domain model (wraps the LLM recommendation with input_summary + model_name for forensic audit) + `advisor_suggestions` SQLite table + StoragePort save/get. Slice B: new SummaryBuilder service that composes Stage 3.1 metrics + Stage 3.2.5 news (via new NewsItemSummary, a narrowed view of NewsItem) + operator grid params into a complete PerformanceSummary; takes optional separate `news_storage` so prices+trades and news can live in separate DBs. Slice C.0: unified `schedules:` config block — every periodic-task cadence (observe_prices, observe_balances, news, advise, plus future maintenance_*) lives in one place with duration- string format (30s/10m/4h/7d). Hard cutover removed the previous per-CLI interval fields. Slice C: `cli/advise` long-running daemon — three-DB shape (read observe + news, write its own advise.db), per-cycle fault isolation (advisor or storage error → log + return, loop continues), AdvisorPort-agnostic so Stage 3.4a's MoE plugs in without rewiring. Slice D: `tools/show_suggestions.py` inspection + `advise:` example yml. Verified live: cli/advise ran a real cycle against the operator's observe DB + news DB → phi4:14b-q8_0 produced a recommendation in ~50s → persisted to advise.db → tools/show_suggestions.py printed it back cleanly. 675 unit tests pass (was 458 at Phase 2 close; +27 across the four 3.3 slices); mypy clean (52 src files); pylint 10.00/10. Ollama Desktop update mid-stage retagged local models with explicit quant suffixes; example settings.yml updated.*
5. **Stage 3.4a – Mixture of Experts (MoE)** ✅ (2026-05-15) – Replace the single-LLM advisor with a MoE adapter that orchestrates 2+ specialist LLMs (quant, risk, news) and aggregates their opinions via voting / weighted-confidence / arbitrator strategy. Per-expert raw opinions logged alongside the aggregated recommendation. Per ADR-007. *Four slices landed. Slice A: pure-function aggregators (`aggregate_voting`, `aggregate_weighted_confidence`) in `services/aggregators.py` — strict majority / weighted average / weighted mode. Slice B: `MoEAdvisorAdapter` fans out via `asyncio.gather`, swallows per-expert failures with structured-field logging, attaches per-expert opinions to a new recursive `AdvisorRecommendation.expert_opinions` field; new `MoEExpertEntry` frozen dataclass wraps `(name, role, advisor: AdvisorPort)` — no Expert ABC, AdvisorPort is the only abstraction. Slice C: `aggregate_arbitrator` async function + `ArbitratorAdvisor` Protocol; OllamaAdapter gained an `extra_context: str = ""` kwarg (kept off AdvisorPort itself — MoE-specific extension); MoEAdvisorAdapter accepts an optional arbitrator entry required iff `aggregator="arbitrator"`. Slice D: `cli/advise` dispatches on `advisor.type=single` vs `=moe`, builds OllamaAdapter per ExpertConfig + arbitrator when configured; `advisor_suggestions.expert_opinions` column added with in-place PRAGMA + ALTER TABLE migration for Stage 3.3 DBs; `tools/show_suggestions.py` shows `experts=N[roles]` on the one-liner. New `tools/run_moe_check.py` one-shot verification tool. **Verified live** with three local Ollama experts (phi4:14b-q8_0 quant, granite4.1:30b-q5_K_M risk, deepseek-r1:14b-qwen-distill-q8_0 news, phi4 arbitrator): `weighted_confidence` ran in 194s producing aggregated `spacing 1.29% / order_size $8 / levels±4` (high confidence); `arbitrator` ran in 191s producing `spacing 1.4% / order_size $9` with the arbitrator citing ADR-007's news auto-apply restriction in its rationale. 730 unit tests pass (was 675 at Stage 3.3 close, +55); mypy clean (54 src files); pylint 10.00/10. Cloud-provider adapters (anthropic/openai/google) raise "not implemented" — they land later.*
6. **Stage 3.4b – Optional Auto-Tuning (Guarded)** ✅ (2026-05-15) – Provide a configuration option to auto-apply safe, bounded recommendations (e.g., adjust grid spacing within pre-configured limits). Enforce strict range checks and safety rules. **News-derived suggestions never auto-apply** — they remain advisory-only per ADR-007. *Three slices landed. Slice A: pure-function gate `services/auto_apply.evaluate_auto_apply(suggestion, current_grid, auto_apply_config, *, symbol) -> AutoApplyResult`. Off-by-default `enabled=False` blanket-rejects; role='news' blanket-rejects; whitelist for v1 is `spacing_percentage` + `order_size_usd` with their configured `max_*_change_percentage` caps; level keys rejected pending an explicit cap; inclusive boundary; numeric coercion for int/float/Decimal/string with bool/zero/negative guard. Slice B: `cli/apply` dry-run reads latest (or `--recommendation-id`) suggestion from advise.db, logs per-key APPLIED/REJECTED with reasons. `--symbol` overrides advise.symbol for cross-coin evaluation. Slice C: `--commit` wires `services/settings_rewriter.apply_grid_overrides()` (ruamel.yaml round-trip preserving comments + numeric style, atomic .tmp + rename, returns unified diff) plus a new `AppliedSuggestion` frozen domain model + `applied_suggestions` SQLite table + StoragePort save/get. Stdouts the diff for operator review. **Verified live**: `python -m wobblebot.cli.apply` against the operator's real advise.db correctly surfaced the latest BTC suggestion and rejected every key with reason 'auto-apply disabled' (gate default-off posture holds end-to-end through the CLI). 792 unit tests pass (was 730 at Stage 3.4a close, +62); mypy clean (57 src files); pylint 10.00/10. New runtime dep: `ruamel.yaml`.*
7. **Stage 3.5 – Phase 3 Integration Check** – Demonstrate an “advisor‑in‑the‑loop” run with MoE + news: trading engine runs, advisor produces aggregated suggestions, operator reviews, optionally auto-applies bounded ones.

## Phase 4 – Harvester & Treasury Management ✅ Complete (2026-05-15)

**Goal:** Compartmentalized module that manages **funds transfers**, not trades.

1. **Stage 4.1 – Harvester Domain & Ports** ✅ (2026-05-15) – Define the Harvester domain model and HarvesterPort. Capture rules for minimum Kraken liquidity, surplus scraping, and top-up thresholds. Per ADR-004, Harvester uses Kraken withdrawal API (via ExchangePort) rather than separate banking integration. *Pure-domain slice — no I/O, no Kraken calls, no withdrawals; zero new real-money risk. HarvesterConfig in config/harvester.py with the four threshold knobs (min_exchange_liquidity_usd / topup_threshold_usd / surplus_threshold_usd / max_withdrawal_per_day_usd) and a model validator enforcing the ordering invariant min < topup < surplus. enabled flag defaults to False mirroring the auto-apply gate posture (ADR-012). services/harvester.propose_transfer() pure function takes (balance_usd, config, today_total_withdrawn_usd) and returns TransferProposal | None per four bands: deficit (below min) → no proposal, operator-only territory; top-up band → propose bank→exchange deposit to midpoint of (topup, surplus); hold band → no proposal; surplus → propose exchange→bank scrape to same midpoint. Day-cap shrinks proposals when over and refuses entirely when exhausted; doesn’t apply to inflows. 24 new unit tests; 824 total pass (was 800 at Stage 3.6 close); mypy clean (59 src files); pylint 10.00/10. settings.example.yml harvester block reordered to match the new invariant and gained an operator-facing explanation of the three bands.*
2. **Stage 4.2 – Read‑Only Balance Monitoring** ✅ (2026-05-15) – Harvester reads Kraken balances and, if available, bank balances. Log hypothetical transfers without moving any money. *cli/harvest daemon polls Kraken USD balance on schedules.harvest cadence, runs the Stage 4.1 propose_transfer() decision, and logs what WOULD be proposed. No transfers, no DB writes; uses the read-only KRAKEN_READER_API_KEY. HarvestConfig per-CLI section (log_format only for now). Tagged "HYPOTHETICAL proposal (no money moved)" log message so a glance at logs can’t be mistaken for real actions. Stub ExchangePort’s withdraw() raises NotImplementedError with a "4.2 must not call withdraw" message — surfaces accidental cross-wiring as a hard test failure. **Verified live** against the operator’s real Kraken account: daemon read $99.92 USD balance (current state), correctly classified as deficit (below the $200 min_exchange_liquidity_usd threshold), logged "no proposal" with full band context. 14 new tests; 838 total pass (was 824 at Stage 4.1 close); mypy clean (60 src files); pylint 10.00/10.*
3. **Stage 4.3 – Passive Mode Transfers** ✅ (2026-05-15) – Harvester produces "transfer proposals" (amount, direction, and rationale) for manual review. *Persistence + inspection slice. New transfer_proposals SQLite table with UNIQUE(proposal_id) guard, CHECK on direction, indexes on (created_at) and (direction, created_at). TransferProposal gained created_at: Timestamp for forensic ordering. StoragePort.save_transfer_proposal / get_transfer_proposals (filter by since / direction / asset / limit; DESC by created_at). HarvestConfig gained db: str (per-CLI DB convention matching advise.db). cli/harvest persists every non-None proposal on every tick; storage write failure logs + continues (missing audit row < missing every subsequent tick). Persistence is INDEPENDENT of HarvesterConfig.enabled — that flag gates execution (Stage 4.4+), not the forensic record. New tools/show_proposals.py mirrors tools/show_suggestions.py shape (--since-hours / --direction / --asset / --limit / --log-format). 15 new tests; 853 total pass (was 838 at Stage 4.2 close); mypy clean (60 src files); pylint 10.00/10. **Verified live** against the operator’s real Kraken account: daemon read $99.92 USD → deficit band → no proposal → transfer_proposals empty → tools/show_proposals.py correctly reports "no transfer proposals match". persistence_enabled: true confirmed in session-start log.*
4. **Stage 4.4 – Active Mode (Guarded Withdrawals)** ✅ (2026-05-15) – Enable actual **Kraken → bank withdrawals** within strict caps. *Four-slice landing. **4.4a** implemented KrakenAdapter.withdraw() against /0/private/Withdraw (was a stub since Phase 1.2); HarvesterConfig grew api_key_env_var, api_secret_env_var, and withdrawal_destinations (asset → label dict, pre-registered in Kraken Pro). cli/harvest switched to the Harvester key. **4.4b** added the transfer_results SQLite table + compute_today_total_withdrawn_usd helper (rolling 24h window of exchange→bank withdrawals, status != failed); cli/harvest now feeds the real total to propose_transfer instead of Decimal("0"). **4.4c** added the cli/apply-mirror operator-approval gate: --execute <proposal-id> runs six defense layers (enabled=True; proposal exists; not stale per proposal_max_age_hours; destination label resolves; current balance ≥ proposal amount; day-cap headroom) before calling adapter.withdraw(); persists TransferResult with status=pending on success or status=failed if Kraken refused. **4.4d** added tools/show_transfers.py inspector + this close. 888 unit tests pass (was 853 at Stage 4.3 close, +35 across the four slices); mypy clean (60 src files); pylint 10.00/10. No real Kraken withdrawal in the slice tests — that’s the operator-triggered first execution, separate event. Running real-money cost still /usr/bin/bash.08 until the operator runs their first --execute against a real proposal (currently 9.92 balance is in deficit band; no proposal generated).*
5. **Stage 4.5 – Phase 4 Integration Check** – Demonstrate a scenario in which trading grows the exchange balance, Harvester scrapes the surplus, and the audit trail confirms the actions.  Confirm that no unauthorized transfers occur.

## Phase 5 – Operator Interaction Engine ✅ Complete (2026-05-16)

**Goal:** A bidirectional Discord interface with multi-turn conversational LLM intent parsing, structured command + query catalog, and ADR-002-preserving confirm-before-execute. `cli/live` stays Discord-ignorant; a new `cli/operator` daemon owns the chat surface and communicates with the engine through SQLite tables in a new `operator.db`. Reframed from the original Phase 5 (dashboard + notifier + control + reliability + maintenance + performance + v1.0 release) on 2026-05-16 after the operator surfaced a broader vision; per ADR-013. The displaced original-Phase-5 stages reorganize into Phases 6–8 below.

**Architectural reference:** [ADR-013 — Operator Interaction Engine](../architecture/decisions.md#adr-013--operator-interaction-engine-discord--conversational-llm).

1. **Stage 5.1 – Operator Domain & Ports** ✅ (2026-05-16) – Pure-domain stage. `OperatorPort` + `AssistantPort` ABCs; `OperatorIntent` typed sum (`Command | Query | Conversational | Unparseable`) using Pydantic discriminated unions; concrete `OperatorCommand` catalog (`Pause`, `Resume`, `PauseAll`, `ResumeAll`, `CancelOpenOrders`, `Stop`) and `OperatorQuery` catalog (`Status`, `OpenOrders`, `RecentFills`, `RecentSuggestions`, `RecentNews`, `HarvesterStatus`, `RecentProposals`, `GridConfig`, `Help`) with per-query `*Result` types; `PendingCommand` audit-trail model; `ConversationTurn` + `ConversationContext` for multi-turn prompt assembly; `OperatorError` + `AssistantError` exceptions. No I/O. No Discord. No LLM call. No SQLite table. *Four slices landed (the design doc planned three; 5.1.C inserted mid-stage on operator request to clear a pre-existing pylint warning): **5.1.A** operator types + port (`PauseCommand`/`ResumeCommand`/`PauseAllCommand`/`ResumeAllCommand`/`CancelOpenOrdersCommand`/`StopCommand`; nine concrete queries with typed `*Result`; `OperatorIntent` outermost discriminated union nesting `OperatorCommand` + `OperatorQuery`; `CommandResult`; `PendingCommand` with the six-state lifecycle; `OperatorPort` ABC; `OperatorError`; `SymbolInput` / `OptionalSymbolInput` BeforeValidator that accepts `"BTC/USD"` strings as well as `{base, quote}` dicts; 117 unit tests with 100% module coverage). **5.1.B** assistant types + port (`SymbolStateSnapshot` / `EngineStateSnapshot` for grounding the LLM in current engine state, `ConversationTurn` with `intent: OperatorIntent | None`, `ConversationContext` with `recent_turns: tuple[ConversationTurn, ...]` for the multi-turn prompt window, `AssistantPort.parse_intent`, `AssistantError`; 25 unit tests with 100% module coverage). **5.1.C** `sqlite_storage.py` refactor (split out `sqlite_storage_schema.py` and `sqlite_storage_rowmap.py`; main module 1073→753 lines; cleared the pre-existing `too-many-lines` pylint warning; no behavior change). **5.1.D** close. 1031 unit tests pass (was 892 at Phase 4 close, +139 across 5.1.A and 5.1.B); 21 integration tests opt-in; mypy clean across 64 src files; pylint **10.00/10** with no outstanding warnings; black + isort clean. Per [stage-5.1-design.md](stage-5.1-design.md).*
2. **Stage 5.2 – Discord Transport Adapter** ✅ (2026-05-16) – `discord.py`-backed bot client in `adapters/discord_transport.py`. Gateway lifecycle (connect / close). Inbound message + reaction handlers normalized to typed `InboundMessage` / `ReactionEvent` and dispatched after allowlist filter (user + channel both required, empty allowlists deny-by-default, bot's own user id always rejected). Outbound: `send_message`, `send_embed` (color-coded by level), `send_confirmation` (amber-bordered embed + ✅ / ❌ reactions wired for the ADR-013 confirm-before-execute gate). New runtime dep `discord.py>=2.3,<3` (2.7.1 currently). The adapter is concrete (no port wrapper) — only `cli/operator` consumes it; an abstraction would be speculative. *Single substantive slice: **5.2.A** transport adapter + 36 unit tests (90% module coverage; uncovered lines are the Gateway-bound event shims marked `# pragma: no cover` and the `discord.DiscordException` re-raise wrappers requiring contrived mocks). 1067 unit tests pass (was 1031 at Stage 5.1 close, +36). mypy clean across 65 src files. pylint **10.00/10** with no outstanding warnings. black + isort clean.*
3. **Stage 5.3 – Operator Assistant (Ollama)** ✅ (2026-05-16) – `OllamaAssistantAdapter` implementing `AssistantPort`. Uses Ollama's `/api/chat` endpoint (not `/api/generate` like the advisor) for native multi-turn role-tagged messages. System prompt = `config/prompts/operator.md` body + the engine state snapshot JSON; recent `ConversationTurn`s become user/assistant messages in chronological order; current operator message is the last user turn. LLM output is validated against the `OperatorIntent` discriminated-union `TypeAdapter` (both nesting levels resolve in one pass). Thinking-mode + split-response-envelope handling matches the advisor pattern. Constructor refuses non-operator-role prompts to fail loudly at wiring time. **Code reuse per operator guidance:** `is_thinking_model` and `extract_last_json_object` promoted to module-public helpers in `adapters/ollama.py` plus new `OllamaJsonExtractError`; both advisor and assistant adapters import and wrap as their port-specific error type. `PromptRole` literal gained `"operator"`. *Single substantive slice: **5.3.A** assistant adapter + operator.md prompt + tests. 19 new unit tests for the assistant; 2 existing advisor tests updated to expect the port-agnostic extractor error; 1 parametrize case added for the `"operator"` role; `TestShippedPrompts` extended to assert `operator.md` loads with `response_schema=operator_intent_v1`. 1088 unit tests pass (was 1067 at Stage 5.2 close, +21). mypy clean across 66 src files. pylint **10.00/10** with no outstanding warnings. black + isort clean.*
4. **Stage 5.4 – Engine Integration** ✅ (2026-05-16) – Engine gains `pause_symbol(symbol)`, `resume_symbol(symbol)`, `cancel_open_orders(symbol)`, and `request_stop()` methods (with new `StepAction` value `"skipped_paused"`, in-memory per-session pause set, exchange-authoritative cancel-all reading from `ExchangePort.get_open_orders`). New `services/operator_service.py` implements `OperatorPort` — match/case dispatch on the discriminated union for both `dispatch_command` (six commands) and `answer_query` (nine queries, with optional cross-database `advise_storage` / `news_storage` / `harvest_storage` for the queries that need them; graceful degrade to empty results when unwired). **First SQLite table lands here:** `pending_commands` (id PK, command_kind denormalized, command_json + result_json for schema-evolution headroom, full six-state CHECK on status, three indexes for poll + ordering + TTL cleanup). `cli/live` gains an optional `operator_db: str | None` config field; when set it opens a second `SQLiteStorageAdapter`, constructs the `OperatorService`, and drains `pending_commands WHERE status='approved'` before each tick. **ADR-002 confirm-before-execute firewall:** the `WHERE status='approved'` filter on the SELECT is the only path from a `PendingCommand` to the engine; per-row dispatch failures wrap as `failed` `CommandResult`s without aborting the batch. `engine.is_stop_requested` checked after the poll so a `StopCommand` processed this tick exits the loop cleanly. *Four substantive slices: **5.4.A** GridEngine operator-control methods (14 new tests); **5.4.B** `pending_commands` SQLite table + StoragePort `save/get_pending_command` + `get_pending_commands` (10 new tests; row mapper uses module-level `TypeAdapter[OperatorCommand]` for discriminator resolution on read); **5.4.C** OperatorService with all six commands + nine queries (25 new tests including the graceful-degrade paths when cross-database storages are unwired); **5.4.D** cli/live poll + `_process_pending_commands` helper that is the literal confirm-before-execute gate (8 new tests, including the "four rows / four statuses / only approved dispatches" firewall test). **5.4.E** close. 1145 unit tests pass (was 1088 at Stage 5.3 close, +57); 21 integration tests opt-in; mypy clean across 67 src files; pylint **10.00/10** with no outstanding warnings; black + isort clean.*
5. **Stage 5.5 – Outbound Notifications** ✅ (2026-05-16) – `SqliteNotifierAdapter` implementing `NotifierPort`; writes structured `Notification` rows to a new `notifications` SQLite table (id PK + level CHECK + title/message/timestamp + context_json + forwarded flag + forwarded_at + created_at; two indexes for the forwarded poll and timestamp). New `PersistedNotification` value object wraps the raw `Notification` with row-level fields. Three `StoragePort` methods (`save_notification` returning the row id, `get_notifications(forwarded=..., limit=...)` for cli/operator's poll, `mark_notification_forwarded` idempotent update). `cli/live` injection wired for **session start** (info), **per-tick fills** (info, when `StepResult.fills > 0`), **cap trips** (error), **session end** (info or error). `cli/harvest` injection wired for **proposal generated** (info), **withdrawal executed** (warning — money moved is the highest-value event), **withdrawal failed** (error). Both CLIs gain optional `operator_db` config field; when set, they open a second `SQLiteStorageAdapter` and wrap with `SqliteNotifierAdapter`. The `_notify` helpers in both CLIs swallow `NotifierError` so a broken notifier can never break the engine loop — Phase 5 treats notifications as forensic ledger entries. Per ADR-013 decision 9 neither CLI imports `discord.py`; the rows accumulate in SQLite for cli/operator (Stage 5.6) to forward. *Two substantive slices + close: **5.5.A** notifications table + adapter (14 new tests); **5.5.B** cli/live + cli/harvest wiring (8 new tests). 1167 unit tests pass (was 1145 at Stage 5.4 close, +22); 21 integration tests opt-in; mypy clean across 68 src files; pylint **10.00/10** with no outstanding warnings; black + isort clean.*
6. **Stage 5.6 – `cli/operator` Daemon** ✅ (2026-05-16) – The long-running CLI that ties Discord + Assistant + OperatorPort together. *Four substantive sub-slices + close:* **5.6.A** third Phase 5 SQLite table — `conversation_turns` with six-state CHECK on role + two indexes — plus `StoragePort.save_conversation_turn` (upsert) and `get_conversation_turns(channel_id, user_id, limit)` (chronological, with newest-N flip via DESC+LIMIT then Python reverse). `row_to_conversation_turn` uses a new module-level `TypeAdapter[OperatorIntent]` for discriminated-union rebuild from `intent_json` (10 new tests). **5.6.B** three new Pydantic models in `config/cli.py`: `AssistantLLMConfig` (provider/model/prompt_file/base_url/temperature/max_tokens/timeout), `OperatorAuthConfig` (bot_token_env_var + user/channel allowlists + outbound_channel_id), `OperatorConfig` composing them with `operator_db` + optional `live_db`/`advise_db`/`news_db`/`harvest_db` for cross-DB queries + the ADR-013 knobs (`context_window_turns` default 10, `confirm_ttl_seconds` default 300, `forwarder_poll_seconds` default 2.0). `WobbleBotConfig` gains `operator: OperatorConfig | None` (18 new tests). **5.6.C** new `cli/operator` daemon with three concurrent concerns: notification forwarder (background task drains `notifications WHERE forwarded=0` and posts color-coded embeds), conversation flow (message handler builds `ConversationContext` from engine snapshot + recent turns, calls `AssistantPort.parse_intent`, routes via match/case on the four `OperatorIntent` variants — Command writes a `PendingCommand` row + posts a confirm embed, Query calls `OperatorService.answer_query`, Conversational/Unparseable send a reply), confirmation flow (reaction handler transitions `awaiting_confirmation` → `approved`/`rejected` via in-memory message_id→pending_id map). Per ADR-013 decision 3 the daemon NEVER calls `dispatch_command` directly — every state mutation crosses `pending_commands` so cli/live's ADR-002 firewall is the only path to engine ops. v1 limitation: cli/operator's stub engine doesn't see cli/live's in-memory pause state; `StatusQuery` reports all symbols as `active` (14 new tests). **5.6.D** `tools/show_pending.py` operator inspection (`--status` / `--limit` / `--log-format`) + close. *1209 unit tests pass (was 1167 at Stage 5.5 close, +42 across the four sub-slices); 21 integration tests opt-in; mypy clean across 69 src files; pylint **10.00/10** with no outstanding warnings; black + isort clean.*
7. **Stage 5.7 – Phase 5 Integration Check** ✅ (2026-05-16) – End-to-end demo of the full operator-interaction round-trip. *Three sub-slices: **5.7.A+B** TTL expirer for abandoned `awaiting_confirmation` rows (new third background `asyncio.Task` in cli/operator + 5 unit tests) plus end-to-end integration test suite (5 tests covering the full pause→confirm→approve→dispatch→notify round-trip, reject path, multi-turn conversation, notification forwarding, and TTL expiry skipping in cli/live's poll). **5.7.C** Phase 5 closing summary at `docs/planning/phase-5-summary.md` (mirrors phase-2/3/4 precedent). All seven Phase 5 stages closed. 1214 unit tests pass (was 1209 at Stage 5.6 close, +5 ttl_expirer); 26 integration tests opt-in (was 21, +5 e2e operator round-trip suite); mypy clean across 69 src files; pylint **10.00/10** with no outstanding warnings; black + isort clean. Phase 5 total real-money cost: **$0.00** (every test stubs Discord / Ollama / Kraken; the live verification "real operator types in real Discord" is operator-driven and tracked separately).*

## Phase 6 – Cloud LLM Integration ✅ Complete (2026-05-17)

**Goal:** Operator-selectable cloud LLM providers for both the operator-assistant role (Phase 5 added) and the MoE trading-advisor roles (Phase 3 placeholder slots). Phase 5 ships with Ollama-only; this phase fills the long-standing `_build_advisor` cloud-provider placeholders (`anthropic`, `openai`, `google`) and extends the same machinery to `AssistantPort`. New concerns this phase introduces — that Ollama didn't have — are **per-call cost** (cloud APIs charge) and **provider availability** (cloud APIs fail). Both are ratified in their own ADRs at kickoff so they don't get reinvented mid-stage.

**Kickoff commit (2026-05-17)** ratifies ADR-014 (LLM cost caps) + ADR-015 (provider failover policy) + `docs/planning/stage-6.1-design.md`, mirroring the Phase 5 kickoff pattern (ADR-013 + `stage-5.1-design.md`).

1. **Stage 6.1 — Shared cloud-LLM infrastructure** ✅ (2026-05-17) — `llm_calls` SQLite table in `operator.db` + `StoragePort.save_llm_call` / `get_llm_calls`; `services/llm_pricing.py` (static per-provider/per-model price table with verified-date discipline); `services/llm_cost_gate.py` (`check_budget` enforcing ADR-014's daily + session caps); `services/llm_retry.py` (`retry_with_backoff` helper enforcing ADR-015's transient/permanent classification + exponential backoff); `LLMCostConfig` + `LLMRetryConfig` + `LLMConfig` Pydantic schemas; per-provider auth env vars in `.env.example` (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`); `tools/show_llm_costs.py` operator inspection. **No real API call** — pure foundation. Five sub-slices shipped:
    - **6.1.A — Cost-tracking domain + storage** ✅ (2026-05-17) — `LLMCallRecord` frozen Pydantic model + `llm_calls` SQLite table (UUID PK + timestamp + role + provider + model + tokens triple + cost_usd Decimal + request_id + success + error_kind) + three indexes (timestamp / provider+model / role) + `StoragePort.save_llm_call` / `get_llm_calls(since, role, provider, limit)` + `LLMCostCapExceeded` domain exception. 33 unit tests; drive-by `implicit-str-concat` fix in `sqlite_storage.get_conversation_turns`; file-level `# pylint: disable=too-many-lines` on sqlite_storage.py (1038 lines; adapter is naturally many-methods).
    - **6.1.B — Pricing table + cost gate** ✅ (2026-05-17) — `services/llm_pricing.py` with 8 in-scope models (Claude Sonnet 4.6 + Opus 4.7, gpt-4o + gpt-4o-mini + o1 + o3-mini, gemini-2.5-pro + gemini-2.5-flash); `cost_for()` with `tokens_reasoning` falling back to output rate unless overridden; `services/llm_cost_gate.py` with `LLMCostConfig` ($1.00/day + $0.50/session + `enforce=True` defaults) + `check_budget()` returning `GateAllow | GateDeny`; sliding 24h window; `enforce=False` dry-run posture per ADR-014 decision 8; `test_pricing_freshness.py` watchdog fails CI on entries >180 days old. 38 unit tests.
    - **6.1.C — Retry/backoff helper** ✅ (2026-05-17) — `services/llm_retry.py` with `LLMRetryConfig` (max_retries=3, initial_backoff=1.0, multiplier=2.0) + `default_classifier` (httpx Connect/Read/Write/Pool/RemoteProtocol → transient; HTTPStatusError 429+5xx transient, other 4xx permanent; everything else permanent) + `retry_with_backoff(fn, config, *, classifier, sleep_fn)`; raises `LLMRetryExhausted` chaining `__cause__` after budget; `sleep_fn` injection keeps tests millisecond-fast. 36 unit tests.
    - **6.1.D — Config schemas + env wiring** ✅ (2026-05-17) — `config/llm.py` with `LLMConfig` composing cost + retry; `WobbleBotConfig.llm: LLMConfig | None = None` (None = pure-Ollama deployment, gate inactive — opt-in posture); `.env.example` refreshed for Phase 6 / ADR-014/015 framing; `config/settings.example.yml` gains commented `llm:` block between operator and profiles. 13 unit tests + existing schema-drift tests guard the example/operator alignment.
    - **6.1.E — Inspection tool + stage close** ✅ (2026-05-17) — `tools/show_llm_costs.py` (`--since-hours` / `--provider` / `--role` / `--limit` / `--by-provider` / `--by-role` / `--log-format`, mutex on the two rollup flags). Deprived-env walkthrough green: missing DB → exit 2; empty table → exit 0; seeded rows → per-row print + total footer; rollups sort desc by cost. This stage-close commit.
2. **Stage 6.2 — Anthropic adapter** ✅ (2026-05-17) — `adapters/anthropic.py` (`AnthropicAdvisorAdapter` implementing `AdvisorPort` + shared Messages-API helpers `estimate_cost_ceiling` / `parse_text_blocks` / `build_call_record` / `post_messages`) + `adapters/anthropic_assistant.py` (`AnthropicAssistantAdapter` implementing `AssistantPort`). Both adapters run the full ADR-014 flow internally: estimate → `check_budget` → `retry_with_backoff(post_messages)` → persist `LLMCallRecord` → update `SessionCostTracker`. `cli/advise.py` refactored: `_build_ollama_advisor` → `_build_advisor_adapter` dispatches by provider (`ollama` / `anthropic` / `openai` and `google` still raise "not implemented"); new `_CloudWiring` dataclass bundles storage + tracker + LLMConfig and threads through `_build_advisor` / `_build_expert_entry` / `_build_arbitrator_entry`. `cli/operator.py` adds `_build_assistant` helper dispatching on `OperatorConfig.assistant.provider`. `AssistantLLMConfig.provider` Literal extends from `["ollama"]` to `["ollama", "anthropic"]`. Cost ledger writes land in `operator.db` per ADR-014 decision 5 — `cli/advise --provider=anthropic` requires both an `llm:` block AND an `operator:` block in settings.yml (errors at startup if either missing). Anthropic thinking tokens lumped with output (`tokens_reasoning=None`) — cost is correct via pricing fallback; operator visibility queued as v2. Three sub-slices shipped:
    - **6.2.A — Anthropic shared client + AdvisorAdapter** ✅ (2026-05-17) — 32 new unit tests (pure helpers + happy paths + cost gate + retry/backoff + parse failures + construction guards). New `SessionCostTracker` mutable class in `services/llm_cost_gate.py`.
    - **6.2.B — AnthropicAssistantAdapter** ✅ (2026-05-17) — 17 new unit tests (every OperatorIntent variant round-trips + wire shape + cost-tracking + retry + parse failures + construction guards).
    - **6.2.C — CLI dispatch wiring + stage close** ✅ (2026-05-17) — `cli/advise` + `cli/operator` provider dispatch; renamed `test_unimplemented_cloud_provider_rejected` test to use `openai` since `anthropic` is now implemented (added new sibling test `test_anthropic_without_cloud_wiring_rejected`). This stage-close commit.
3. **Stage 6.3 — OpenAI adapter** ✅ (2026-05-17) — `services/llm_cloud_call.py` shared orchestrator (`CloudCallContext` + `execute_cloud_call` + `classify_error`) extracts the ADR-014/015 flow that Stage 6.2 had duplicated across the two Anthropic adapters. Anthropic adapters refactored to use it (zero behavior change; all 39 existing tests stay green). `adapters/openai.py` lands both `OpenAIAdvisorAdapter` + `OpenAIAssistantAdapter` against the Chat Completions API (`/v1/chat/completions`; `Authorization: Bearer` header + optional `OpenAI-Organization`; `max_completion_tokens` for forward-compat with o-series). `extract_openai_tokens` normalizes o-series usage: `completion_tokens` includes reasoning, so the extractor subtracts `completion_tokens_details.reasoning_tokens` to satisfy the additive convention (`tokens_out + tokens_reasoning = total billable output`). `is_reasoning_model` detection drops `temperature` for o-series. `AssistantLLMConfig.provider` Literal extends to `["ollama", "anthropic", "openai"]`. Three sub-slices shipped:
    - **6.3.A — Shared cloud-call helper + refactor Anthropic** ✅ (2026-05-17) — pure refactor; +21 new shared-helper tests; 39 Anthropic tests stay green.
    - **6.3.B — OpenAI advisor + assistant adapters** ✅ (2026-05-17) — 31 new tests focused on OpenAI-specific bits (wire shape, reasoning-token normalization, o-series vs chat-model handling).
    - **6.3.C — CLI dispatch wiring + stage close** ✅ (2026-05-17) — `cli/advise._build_advisor_adapter` + `cli/operator._build_assistant` add `openai` branch with `OPENAI_API_KEY` env-var check + optional `OPENAI_ORGANIZATION` header support. `_UNIMPLEMENTED_PROVIDERS` shrinks to `("google",)`. Test refactor: `test_unimplemented_cloud_provider_rejected` now uses `google` since `openai` is implemented. This stage-close commit.
4. **Stage 6.4 — Google adapter** ✅ (2026-05-17) — `adapters/google.py` lands both `GoogleAdvisorAdapter` + `GoogleAssistantAdapter` against the Gemini REST API (`generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`). `x-goog-api-key` header auth; `systemInstruction` separate from the `contents` array; role mapping translates `assistant` → `model` on the wire (Gemini's role vocabulary). `extract_google_tokens` is the simplest reasoning-token normalization of the three Phase 6 providers: `thoughtsTokenCount` is reported separately from `candidatesTokenCount` and **additive natively** (unlike OpenAI which had to subtract; unlike Anthropic which lumps inside output). Adapter records as-is. `AssistantLLMConfig.provider` Literal closes with all four providers (`ollama`, `anthropic`, `openai`, `google`); `_UNIMPLEMENTED_PROVIDERS` becomes empty. Cloud-provider configuration without an `llm:` block is now the only `ValueError` path in `_build_advisor_adapter`. Two sub-slices shipped:
    - **6.4.A — Google advisor + assistant adapters** ✅ (2026-05-17) — single module since they share Gemini-specific helpers; 24 new tests focused on wire shape (header + URL + systemInstruction + role=model mapping), native additive thinking tokens, parse_candidate_text + part-filtering.
    - **6.4.B — CLI dispatch wiring + stage close** ✅ (2026-05-17) — `cli/advise._build_advisor_adapter` + `cli/operator._build_assistant` add `google` branch with `GOOGLE_API_KEY` env-var check. Test refactor: `test_unimplemented_cloud_provider_rejected` → `test_google_without_cloud_wiring_rejected` (the "missing llm:" guard is now the only error surface). This stage-close commit.
5. **Stage 6.5 — Phase 6 Integration Check** ✅ (2026-05-17) — Real-API smoke test across all three cloud providers under live cost-cap enforcement. Total cost: $0.005018 (anthropic $0.004248, openai $0.000185, google $0.000585). Receipts persisted in `llm_calls`; cost-tracking flow validated end-to-end including Google's native-additive reasoning-token normalization (43 thinking tokens recorded separately from output). `tools/run_cloud_check.py` operator-driven smoke-test tool + opt-in `tests/integration/test_cloud_llm_live.py` integration suite landed in 6.5.A; Phase 6 closing summary at `docs/planning/phase-6-summary.md` (mirrors phase-{2,3,4,5}-summary.md precedent) landed in 6.5.B. Audit-driven refactor pass at 6.5.A close consolidated `estimate_cost_ceiling` + `parse_advisor_recommendation` + `parse_intent_dict` into the shared services (collapsed ~270 LOC of mechanical duplication across providers). **CryptoCompare 90-day evaluation** deferred to its scheduled review date 2026-08-13 per ADR-010 — the proper 90-day observation window hasn't elapsed.

**Explicitly out of Phase 6 scope** (each documented here so it doesn't get pulled in mid-stage):
- Streaming responses (Discord posts only post-completion; no streaming UX surface).
- Provider-native function calling / tools (existing `extract_last_json_object` parser is portable across providers).
- Embeddings APIs.
- Fine-tuning.
- Cross-provider failover (deferred per ADR-015; v2 candidate).
- Per-role budget split (deferred per ADR-014; v2 candidate).
- Web-UI cost dashboard (Phase 7).

## Phase 7 – Web UI / Dashboard ✅ Complete (2026-05-18)

**Goal:** At-a-glance browser-based observability + the most-frequent operator mutations (pause / resume / stop) without leaving the dashboard. FastAPI app at `src/wobblebot/web/`, sibling to `cli/`; both presentation layers consume the existing ports (no business logic in templates / routes). Server-rendered Jinja2 + HTMX (no SPA / no build pipeline). Session-cookie auth with bcrypt-hashed-password (single-operator v1). Read-mostly with ADR-013-firewalled mutations: pause/resume/stop buttons create `PendingCommand` rows in `awaiting_confirmation` (same state machine as Discord's ✅/❌); a two-click confirm flow transitions to `approved`. **The ADR-002 firewall stays intact** — `cli/live`'s `WHERE status='approved'` poll remains the only path from intent to engine.

**Kickoff commit (2026-05-17)** ratifies ADR-016 (Web UI architectural commitments — FastAPI + Jinja2 + HTMX, port-DI in routes, mutations via `pending_commands`, `cli/web` daemon binding 127.0.0.1 by default with operator-managed reverse proxy) + ADR-017 (auth model — session cookie + bcrypt password in `operator.db`'s `users` table; CSRF synchronizer-token middleware; per-IP rate-limit on login) + `docs/planning/stage-7.1-design.md`, mirroring the Phase 5 + Phase 6 kickoff pattern.

Six new runtime dependencies (biggest dep-add since Phase 5's `discord.py`): `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `bcrypt`, `itsdangerous`. All in pyproject.toml's standard `[dependencies]` array.

1. **Stage 7.1 — Web app skeleton + auth.** ✅ (2026-05-17) `src/wobblebot/web/` package: `app.py` (FastAPI factory), `middleware.py` (CSRF synchronizer-token + per-IP login rate-limit), `auth.py` (bcrypt + session lookup + `AuthRedirectRequired`), `dependencies.py` (FastAPI DI for ports + templates), `routes/auth.py` (login GET/POST + logout), `routes/pages.py` (`/` → `/dashboard` redirect + three auth-gated stub pages), `templates/` (Jinja2 base + layout + login + stub), `static/` (HTMX placeholder + base.css). New `users` SQLite table in `operator.db` + `User` + `UserCredentials` Pydantic domain models + `StoragePort.create_user` / `get_user_by_username` / `update_user_last_login`. Thirteenth operator entry point: `python -m wobblebot.cli.web` with two subcommands — `serve` (default; runs uvicorn against `create_app`) and `create-user` (interactive seed). Five sub-slices shipped:
    - **7.1.A — Users table + domain model + StoragePort methods** ✅ (2026-05-17) — `domain/users.py` (User + UserCredentials, both `frozen=True`), `sqlite_storage_schema.py` adds `users` table with `UNIQUE(username)` + `CHECK(length(password_hash) > 0)`, `sqlite_storage_rowmap.py` adds `row_to_user`, three new StoragePort methods. 28 new unit tests.
    - **7.1.B — WebConfig + web/ package scaffolding** ✅ (2026-05-17) — `WebConfig` Pydantic block in `config/cli.py` (serving / auth / presentation / cross-DB-path groups; 13-field schema with bounds-checked validators); `WobbleBotConfig.web: WebConfig | None`. New `src/wobblebot/web/` package — `app.py` factory skeleton, `middleware.py` + `auth.py` skeletons, `dependencies.py` (8 DI factories), `routes/__init__.py`. Templates + static-asset placeholders committed. Six new runtime deps in pyproject.toml: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `bcrypt`, `itsdangerous`. 25 new unit tests for the config block.
    - **7.1.C — Login / logout / session middleware / CSRF** ✅ (2026-05-17) — `web/auth.py` bcrypt + session lookup + `AuthRedirectRequired`; `web/middleware.py` synchronizer-token CSRF helpers (`get_or_create_csrf_token` / `require_csrf_token` / `rotate_csrf_token`) + `LoginRateLimit` (asyncio.Lock-guarded per-IP bucket; resets on successful login per ADR-017 decision 8); `web/routes/auth.py` GET /auth/login + POST /auth/login (rate-limit → CSRF → bcrypt → session set → last-login bump → 302 /dashboard) + POST /auth/logout (CSRF-protected); `web/app.py` registers `AuthRedirectRequired` exception handler + instantiates `LoginRateLimit` on `app.state` + exposes `csrf_input` as a Jinja2 global so every form gets a token without per-template wiring. Templates: `login.html` (extends base.html, not layout.html, so chrome doesn't appear pre-auth) + layout.html CSRF macro. 108 new unit tests (FastAPI TestClient against in-memory SQLite per fixture).
    - **7.1.D — `cli/web` daemon + create-user + stub pages** ✅ (2026-05-17) — `cli/web.py` with two subcommands: `serve` (default) opens operator.db + the four optional cross-DB paths and hands the app to uvicorn; `create-user` prompts for username on stdin + password (twice) via `getpass`, hashes via bcrypt at the configured cost, inserts via `StoragePort.create_user`. Both fail-fast on bad config (exit 2). `web/routes/pages.py` fleshed out from the 7.1.B skeleton: `GET /` → 302 /dashboard, plus three auth-gated stubs at `/dashboard`, `/cost`, `/audit` (each renders the layout chrome + Phase 7.X placeholder copy). New `templates/stub.html` shared by all three. 40 new unit tests (14 pages + 26 cli/web). Per-test fixture in `tests/cli/test_web.py` snapshots + restores the `wobblebot` logger state so `configure_logging` side effects don't break downstream caplog-based tests.
    - **7.1.E — Stage close** ✅ (2026-05-17) — Roadmap ✅, CLAUDE.md Project Status bump (13th operator entry point), `config/settings.example.yml` gains the `web:` block, `.env.example` gains `WOBBLEBOT_WEB_SESSION_SECRET`, CHANGELOG entry, schema-drift tests pass clean.

   **Deprived-env walkthrough green** (all exit 2, no tracebacks): bad `--config` path, bad `--profile` name, missing `web:` block, missing `WOBBLEBOT_WEB_SESSION_SECRET` env var (with mint-command hint), EOF on stdin during `create-user`. 1608 unit tests pass (was 1460 at Phase 6 close, +148 across the five sub-slices); 29 integration tests opt-in; mypy clean across 89 src files; pylint **10.00/10**; black + isort clean. **Stage 7.1 total real-money cost: $0.00** (no live ops; the dashboard is read-mostly and mutations are firewalled per ADR-013).
2. **Stage 7.2 — Cost + status dashboards + mutation buttons.** ✅ (2026-05-17) Three sub-slices delivered the first real-data dashboards plus the architecturally significant mutation flow. **7.2.A — Cost dashboard.** `routes/cost.py` reads `operator.db`'s `llm_calls` (Phase 6 ledger), rolls up to 24h totals + per-day trends + per-provider/role breakdown. Pure-function `_rollup` keeps the math testable. Two routes: `/cost` (full page) + `/cost/card` (HTMX fragment for polled refresh). **7.2.B — Status dashboard.** `routes/status.py` replaces the 7.1 stub `/dashboard`. Reads `live.db`'s open orders + recent 20 trades. `dashboard.html` combines operator-actions card + HTMX-polled status card. Graceful-degrades to "unwired" when `live_db` isn't configured. **7.2.C — Mutation flow.** `routes/commands.py` wires pause/resume/stop through the ADR-013 firewall — POST creates a `PendingCommand` row in `awaiting_confirmation` with `channel_id="web"`; the confirm page summarizes it; approve/reject transitions to `approved` / `rejected`. The web UI **NEVER** calls `OperatorService.dispatch_command` directly — every mutation crosses `pending_commands` so cli/live's `WHERE status='approved'` poll stays the single source of truth. Idempotency: re-confirming a row in a terminal state surfaces the existing status (handles the Discord-confirmed-first race). 10-minute TTL on web-originated rows. CSRF protected on every POST. 29 new unit tests.
3. **Stage 7.3 — Advisor + harvester views.** ✅ (2026-05-17) Two read-only views surfaced existing Phase 3 + Phase 4 data. `routes/advisor.py` reads `advise.db`'s `advisor_suggestions` (limit 50, newest first); template renders the aggregated recommendation + per-expert opinions when MoE-derived (preserves `AdvisorRecommendation.expert_opinions` per ADR-007). `routes/harvester.py` reads `harvest.db`'s `transfer_proposals` + `transfer_results` (limit 50 each); read-only — per ADR-003 `cli/harvest --execute` remains the only path that moves money. Both routes graceful-degrade when their cross-DB storage is unwired. Nav links added to layout.html. 11 new unit tests.
4. **Stage 7.4 — News + audit log views.** ✅ (2026-05-18) The final two read-only surfaces. **7.4.A — `/news`** reads `news.db`'s `news_items` (limit 100); filter form with source dropdown (populated from a wider unfiltered slice for stability across views) + free-text coin filter (case-insensitive substring match against `mentioned_coins` server-side). **7.4.B — `/audit`** replaces the Stage 7.1.D stub; reads `operator.db`'s `pending_commands` + `notifications` (limit 100 each, newest first); lifecycle states color-coded. Cleanup: `pages.py` shrinks to just the `/` → `/dashboard` redirect; `templates/stub.html` removed (no longer used). Layout nav adds `/news`. 13 new unit tests.
5. **Stage 7.5 — Phase 7 close + integration check.** ✅ (2026-05-18) End-to-end TestClient walkthrough in `tests/web/test_phase7_e2e.py` exercises every Phase 7 surface in a single test: anonymous root redirect → login → all six pages (dashboard / cost / advisor / harvester / news / audit) → pause→confirm→approve mutation flow → **ADR-013 firewall verification** (the row is now `approved` in operator.db, which is what cli/live's `WHERE status='approved'` poll picks up) → logout → re-verified session is gone. One test, many assertions. Plus Phase 7 closing summary at `docs/planning/phase-7-summary.md` (mirrors phase-{2,3,4,5,6}-summary.md precedent). **Phase 7 total real-money cost: $0.00** (dashboard is read-mostly; mutations firewalled per ADR-013). Running project cost stays at **$0.085018** unchanged from Phase 6 close. 1656 unit tests pass (1460 at Phase 6 close → 1656 at Phase 7 close, +196 across five stages); mypy clean across 96 src files; pylint **10.00/10**; black + isort clean.
6. **Stage 7.6 — Operational ergonomics: cli/recalibrate.** ✅ (2026-05-18) Operator-initiated balance-scaling tool. Inserted as a polish slice between Phase 7 close and Phase 8 start; doesn't reopen Phase 7's commitments. **14th operator entry point** landed: `python -m wobblebot.cli.recalibrate`. **7.6.A — Calibrator service.** New `services/calibrator.recalibrate(current_balance, target_balance, current_config) -> RecalibrationProposal` pure function. Computes `target / current` scale factor, walks every USD-denominated knob in the operator's `WobbleBotConfig`, emits proposed deltas for grid.default + per-coin order_size_usd; safety total/daily/per_coin caps + non-zero emergency_stop floor; live.max_session_loss_usd; all four harvester thresholds. Spacing percentages, level counts, max_orders_per_coin, max_loss_percentage, runtime_minutes, and the entire shadow:* block stay constant (policy invariants, not USD amounts). Quantizes to cents; preserves harvester's `min<topup<surplus` invariant (scaling by positive ratio preserves ordering). 22 new unit tests. **7.6.B — `cli/recalibrate` dry-run + commit.** Default reads live Kraken USD balance via the read-only key (same path `cli/status` uses); `--current-balance` overrides for what-if analysis. Dry-run prints a per-knob delta table; `--commit` rewrites `settings.yml` via the new `apply_dotted_overrides()` companion to `apply_grid_overrides()` in `services/settings_rewriter` — round-trips ruamel.yaml preserving comments + quoting, atomic temp-file-rename. Refuses to create new keys (typo'd path raises). Per ADR-012's auto-tuning gate posture: operator-initiated, not LLM-initiated, so the gate's bounds don't apply. 18 new unit tests. Live verification: against operator's real $99.92 balance, `--target-balance 10` produces 14 changes including grid.default $10→$1.00, harvester surplus $500→$50.04. **Stage 7.6 total real-money cost: $0.00** (read-only Kraken balance read; no orders, no withdrawals). 1694 unit tests pass (was 1656 at Phase 7 close, +38 across the two sub-slices); mypy clean across 98 src files; pylint **10.00/10**; black + isort clean.

**Explicitly out of Phase 7 scope** (each documented here so it doesn't get pulled in mid-stage):
- Multi-user authentication / per-user permissions (Phase 8+ candidate).
- Password reset / change-password UI (operator deletes + re-seeds via `create-user`).
- WebSocket / SSE real-time updates (HTMX polling at 15s is sufficient).
- Bundled TLS (operator-managed reverse proxy).
- Custom 404 / 500 error pages with full chrome (FastAPI defaults are fine).
- Config-editing through the UI (settings.yml is operator-edited; `cli/apply --commit` is the only mutation surface for grid params per ADR-012).
- Web-UI-mediated trading mutations beyond pause/resume/stop (PauseAll, ResumeAll, CancelOpenOrders stay on Discord / CLI paths for v1).

## Phase 8 – Hardening & v1.0 Release

**Goal:** Final operational polish before v1.0 tag. Bundles the four old-Phase-5 stages that were displaced when Phase 5 became the Operator Interaction Engine, plus the three Phase-5-audit refactors deferred per the global rule against silent reworks during an audit pass.

**Kickoff commit (2026-05-18)** lands `docs/planning/stage-8.0-design.md` enumerating the three R5/R3/R2 sub-slices precisely, mirroring the Phase 5/6/7 kickoff pattern. No code in the kickoff commit. Phase 8 doesn't introduce cross-cutting ADRs at kickoff — Stage 8.1's reconciliation work may warrant an ADR-018 at its own stage kickoff; until then, the existing ADRs cover Phase 8 scope.

1. **Stage 8.0 – Deferred Phase 5 audit refactors** ✅ (2026-05-18) – Three medium refactors surfaced by the Phase 5 close audit punch list. Pure code organization — zero behavior change; every existing test stays green; every existing import path keeps resolving. Detailed slicing in `docs/planning/stage-8.0-design.md`.
    - **8.0.A (R5)** ✅ (2026-05-18) – Split `ports/operator.py` (734 lines) into `ports/operator_intents.py` (Command + Query + Intent variants + the three discriminated unions OperatorCommand / OperatorQuery / OperatorIntent — 367 lines), `ports/operator_results.py` (per-query Result types + QueryResult union + CommandResult + entry types — 302 lines), and a slimmer `ports/operator.py` (244 lines: OperatorPort ABC, PendingCommand, PendingCommandStatus, plus star-import re-exports preserving every existing import path). All 41 backward-compat imports keep resolving from `wobblebot.ports.operator`.
    - **8.0.B (R3)** ✅ (2026-05-18) – Extract module-level factory functions (`_empty_recent_suggestions`, `_empty_recent_news`, `_empty_recent_proposals`) for the three simple graceful-degrade blocks in `services/operator_service.answer_query`. Each callsite shrinks from ~5 lines of inline empty-result construction to one `return _empty_X(query)` line. The "what does graceful-degrade look like for this query" knowledge centralizes at the top of the module. `HarvesterStatusQuery`'s degraded shape stays inline since it's genuinely different (fetches live balance + classifies a band). 6 new factory tests + 25 existing service tests stay green.
    - **8.0.C (R2)** ✅ (2026-05-18) – Extract `cli/_common.run_poll_loop(do_one_cycle, *, interval_seconds, stop_event)` shared across **six** loops in **five** daemons (cli/observe, cli/news, cli/advise, cli/harvest, plus cli/operator's notification forwarder AND TTL expirer). Each migration is a structural extract: per-cycle work moves into a local async closure (so counter increments + mid-sweep stop_event checks stay in-place), then run_poll_loop wraps the closure. Session-start/end try/finally stays at the call site since metrics shape varies. Phase 8.1's reliability refinement now has ONE edit point for any shutdown-discipline change instead of seven. 5 new helper tests.
    - **8.0.D** ✅ (2026-05-18) – Stage close: roadmap + CHANGELOG + CLAUDE.md + project_state memory updates.

   **Numbers.** 1711 unit tests pass (was 1700 at Stage 8.0 entry, +11 across A/B/C: 6 factory + 5 poll-loop helper; A added zero net tests since re-export coverage piggy-backs on existing tests). mypy clean across 100 src files (was 98 — A added two new modules). pylint **10.00/10** maintained throughout. black + isort clean. **Stage 8.0 total real-money cost: $0.00** (pure code reorganization; no live ops touched). Running project cost stays at **$0.085018** unchanged from Phase 6 close.
2. **Stage 8.1 – Reliability & Recovery** ✅ (2026-05-18) – Robust startup/shutdown across cli/live + cli/shadow. Per ADR-018 the exchange is authoritative; reconciliation at startup catches storage drift from shutdown bugs + out-of-band exchange cancellations. The persistence-on-cancel bug surfaced 2026-05-18 in the shadow session is fixed; the daemon refuses to start if the adapter is unreachable rather than ticking against unreconciled state.
    - **8.1.A** ✅ (2026-05-18) – Kickoff: ADR-018 (Engine Reconciliation Strategy — 7 ratified decisions: exchange authoritative; storage-only orders → canceled; exchange-only orders → log + DO NOT adopt; reconciliation at startup only; same policy for shadow; harvester deferred to v1.1; pure-function service + thin async wrapper) + `docs/planning/stage-8.1-design.md` (8 implementation-level decisions). No code in the kickoff commit.
    - **8.1.B** ✅ (2026-05-18) – Persistence-on-cancel fix. After `adapter.cancel_order(o)` succeeds in cli/live + cli/shadow shutdown loops, `storage.save_order(o.model_copy(update={"status": "canceled", "updated_at": now}))`. Don't-lie-in-the-audit-trail discipline: cancel-raised → storage stays `open` so reconciler catches it next session. Storage-write-failure-after-cancel-success → log + continue (cancellation already happened on exchange; reconciler catches stragglers). 5 new tests.
    - **8.1.C** ✅ (2026-05-18) – New `services/reconciler.py` with the two-layer split: `reconcile_open_orders(*, exchange_open, storage_open, configured_symbols) -> ReconciliationPlan` (pure function) + `apply_reconciliation(adapter, storage, *, configured_symbols) -> ReconciliationReport` (async orchestrator). Per ADR-018 decision 8 + stage-8.1-design.md decision 7 the adapter timeout inherits; failure propagates and the daemon refuses to start. Per stage-8.1-design.md decision 8 the configured_symbols filter narrows orphan logging to the engine's actual trade set (manual orders on unrelated coins stay silent); storage-only reconciliation still runs against ALL storage rows regardless. Wired into cli/live + cli/shadow `_main_async` between storage open + signal handlers install, AFTER adapter construct + BEFORE engine first tick. 16 new tests (10 pure + 6 async).
    - **8.1.D** ✅ (2026-05-18) – Stage close.

   **Numbers.** 1732 unit tests pass (was 1711 at Stage 8.1 entry, +21 across B + C). mypy clean across 101 src files (was 100 — C added the reconciler module). pylint **10.00/10**. black + isort clean. **Stage 8.1 total real-money cost: $0.00** (shutdown-discipline fix + read-only adapter queries; no live engine operations). Running project cost stays at **$0.085018**.
3. **Stage 8.2 – Background Maintenance Worker** ✅ (2026-05-18) – `cli/maintenance` daemon shipping DB hygiene (`VACUUM`), retention pruning of `price_snapshots` to CSV archives, local backups via SQLite `.backup` API, and opt-in log rotation. Three concurrent scheduled tasks via the Stage 8.0.C `run_poll_loop` helper; each pulls its cadence from the unified `schedules:` block (7d / 1d / 1d defaults). **Fifteenth operator entry point**: `python -m wobblebot.cli.maintenance`.
    - **8.2.A** ✅ (2026-05-18) – Kickoff: `docs/planning/stage-8.2-design.md` ratifying 10 implementation decisions. No ADR (operational tooling, not cross-cutting policy). No code.
    - **8.2.B** ✅ (2026-05-18) – `services/maintenance.py` with three helpers: `vacuum_database(db_path)` calls raw `sqlite3.execute("VACUUM")` (with explicit `close()` to avoid the unraisable-warning trap); `archive_price_snapshots_to_csv(snapshots, dest_path)` is a pure CSV writer that refuses to overwrite; `prune_price_snapshots(storage, *, older_than, archive_dir, archive_name)` runs the archive-then-delete discipline. New `StoragePort.delete_price_snapshots(before)` method. 9 new tests.
    - **8.2.C** ✅ (2026-05-18) – `services/backuper.py` with `backup_database_locally(src, dest_dir, *, now=None)` using SQLite's online `.backup` API for atomic point-in-time copies (source DB stays usable for concurrent writers); `prune_old_backups(dest_dir, *, db_stem, keep_n_daily)` deletes oldest beyond retention horizon, scoped to one DB stem at a time so multiple DBs each get independent retention. `BackupDestination` Protocol declared for v1.1 remote variants. 10 new tests.
    - **8.2.D** ✅ (2026-05-18) – `cli/maintenance` daemon with three concurrent scheduled tasks (vacuum / prune+archive / backup) via `asyncio.gather` over three `run_poll_loop` invocations. New `MaintenanceConfig` Pydantic block in `config/cli.py` with seven knobs (target_dbs / prune knobs / archive_dir / backup_dir / keep_n_daily / log knobs). `configure_logging` gains an opt-in `rotating_file_path` kwarg using `TimedRotatingFileHandler` ALONGSIDE the stderr stream handler. Idempotent handler replacement closes the old file descriptor first. 13 new tests.
    - **8.2.E** ✅ (2026-05-18) – Stage close: roadmap + CHANGELOG + CLAUDE.md polish; `settings.example.yml` + `settings.yml` gain the `maintenance:` block + three new `schedules.maintenance_*` keys; schema-drift tests pass clean.

   **Numbers.** 1763 unit tests pass (was 1732 at Stage 8.2 entry, +31 across B + C + D). mypy clean across 104 src files (was 101 — three new modules: services/maintenance.py, services/backuper.py, cli/maintenance.py). pylint **10.00/10**. black + isort clean. **Stage 8.2 total real-money cost: $0.00** (pure local-FS + SQLite operations; no exchange queries). Running project cost stays at **$0.085018**.
4. **Stage 8.3 – Performance & Resource Tuning** ✅ (2026-05-18) – Universal SQLite easy wins (WAL mode, `synchronous=NORMAL`, `foreign_keys=ON`) plus an index audit against the engine's hot reads plus an operator-runnable profile harness. Speculative optimizations deferred until Stage 8.4's soak test provides measurement data.
    - **8.3.A** ✅ (2026-05-18) – Kickoff: `docs/planning/stage-8.3-design.md` ratifying 8 implementation decisions (WAL mode for all on-disk DBs; `synchronous=NORMAL` over the default `FULL` per published SQLite guidance; `foreign_keys=ON` as cheap v1.1 insurance; skip pragmas for in-memory DBs; index audit covers the engine's hot path via `EXPLAIN QUERY PLAN`; profile harness reports p50/p99 in ms; `tools/profile_storage.py` not `cli/profile_storage` per the diagnostic-vs-daemon convention; no CI perf regression check in v1.0 because CI runner variance makes it untrustworthy). No ADR (operational tooling, not cross-cutting policy). No code.
    - **8.3.B** ✅ (2026-05-18) – `SQLiteStorageAdapter.connect()` applies two new pragmas after the existing `foreign_keys=ON` step: `PRAGMA journal_mode=WAL` (concurrent readers don't block the writer; cli/maintenance's backup task can read mid-tick) and `PRAGMA synchronous=NORMAL` (fsync at WAL checkpoint boundaries instead of per-commit; ~50x faster commit throughput). Both pragmas skip for `:memory:` and anonymous on-disk DBs — WAL is a no-op there and confuses fixtures that introspect `journal_mode`. 5 new tests in `TestStage83Pragmas`.
    - **8.3.C** ✅ (2026-05-18) – Index audit + `tools/profile_storage.py`. Six `EXPLAIN QUERY PLAN` audits in `TestStage83IndexAudit` assert every hot read uses `SEARCH` (index access), never `SCAN` (full table). All six queries clean against current schema — no new indexes needed. New `tools/profile_storage.py` operator harness times each hot operation N times against an in-memory or operator-specified on-disk DB (the latter copied to a temp file first so the live DB can't be polluted with fixture rows). Reports `{operation, n, p50_ms, p99_ms, mean_ms, total_seconds}` per design decision 6. Pre-seeds 1000 closed orders / 20 open / 200 trades by default so timings reflect realistic load. Smoke-tested locally: `get_open_orders` p50 0.26ms / p99 0.60ms against 1020 seeded rows; `save_order` p50 0.06ms. 11 new tests for the timing helpers.
    - **8.3.D** ✅ (2026-05-18) – Stage close: roadmap + CHANGELOG + CLAUDE.md polish; project_state memory updated.

   **Numbers.** 1785 unit tests pass (was 1763 at Stage 8.3 entry, +22 across B + C). mypy clean across 104 src files (no new src modules — pragma changes localized in `sqlite_storage.py`; profile harness lives under `tools/` which is outside the mypy gate). pylint **10.00/10**. black + isort clean. **Stage 8.3 total real-money cost: $0.00** (pure local SQLite + timing operations; no exchange queries, no LLM calls). Running project cost stays at **$0.085018**. No new operator entry points — `tools/profile_storage.py` is a diagnostic, not a daemon.
5. **Stage 8.4 – Phase 8 / v1.0 Release Check** – Final pre-v1.0 stage. Documentation freeze for v1.0 (known-limitations + future-improvements docs); pre-1.0 one-shot audit (community standards, license recognition, author-identity sweep, Harvester-key separation verified); operator-driven multi-week soak under low-risk configuration; post-soak release ceremony (phase-8 summary, CHANGELOG flip, `pyproject.toml` version bump, annotated `v1.0.0` tag).
    - **8.4.A** ✅ (2026-05-18) – Kickoff: `docs/planning/stage-8.4-design.md` ratifying 10 implementation decisions (soak duration is operator-decided not Claude-mandated; low-risk soak configuration ratified in the runbook; documentation freeze not codebase freeze — soak-surfaced defects fix in focused commits then tag; known-limitations doc covers every ADR-deferred decision plus v1.0-specific deferrals like CryptoCompare 90-day eval + no remote backups + no CI perf check; future-improvements doc grouped by motivation [earned by soak data / by operator feedback / by code review]; pre-1.0 audit findings ship in focused commits per global rule; author-identity audit covers all branches + history; Harvester-key separation verified live with operator; v1.0.0 tag is annotated [`git tag -a`] not lightweight; pyproject.toml version bump in same commit as tag annotation). No ADR — release ceremony, not architectural change. No code.
    - **8.4.B** ✅ (2026-05-18) – v1.0 documentation freeze. New `docs/release/` directory with two operator-facing docs: `v1.0-known-limitations.md` (architectural / operational / observability / tooling / process boundaries; schema notes; soak-window boundary) and `v1.0-future-improvements.md` (v1.1+ candidates grouped by motivation: earned by soak data / operator feedback / code review / external triggers). Cross-reference index links the two.
    - **8.4.C** ✅ (2026-05-18) – Pre-1.0 one-shot audit. LICENSE clean (MIT 2025-2026 holder CarlDog), pre-commit hook complete (gitleaks + PII + author-identity), full-history author sweep clean (only the `40870663+CarlDog@users.noreply.github.com` noreply alias), community-standards files all present. **One audit finding fixed**: README significant drift (test counts 1214/26 stale → 1785/29; Phase status table missing Phases 6/7 + Stages 7.6 / 8.0-8.3; "Eleven CLIs" → "Fifteen entry points"; ADR count 9 → 18). Per-commit `pyproject.toml` version bump deferred to 8.4.F (design decision 10). Follow-up commit added `live.operator_db` + `harvest.operator_db` documentation to `settings.example.yml` (gap surfaced during soak Day 1 spot-check).
    - **8.4.D** ✅ (2026-05-18) – `docs/release/v1.0-soak-runbook.md`. Pre-soak checklist (8 hard gates incl. Harvester-key separation), recommended low-risk config (single coin BTC/USD, $1–2/order, 1.0%+ spacing, harvester daemon mode), daily check-in (5 questions), hard-stop / soft-watch / info-only categorization, abort + restart procedure, pass criteria (~2–4 weeks operator-decided).
    - **8.4.E** ✅ (2026-05-18 → 2026-07-31) – **Operator-driven multi-week soak started 2026-05-18**. Day 1 (2026-05-18 → 2026-05-19): thunderstorm-induced power outage during the night took the host's DNS resolution down. cli/live crashed via `httpx.ConnectError` inside `_session_usd_balance` during shutdown; one $10 BUY filled overnight while engine was dead. Day 2 (2026-05-19): operator-driven recovery via manual cancel-on-Kraken + `DELETE FROM grid_state` + cli/live restart. Fresh anchor at $76,894; 3-buy + 1-sell layout (sell uses orphaned BTC inventory). **Two soak-surfaced findings** addressed in focused commits per stage-8.4-design.md decision 3: (1) [`e2b6cfc`] — defect fix: cli/live + cli/shadow `finally` block restructured so each cleanup step has its own try/except — a transient `_session_usd_balance` failure no longer skips `_cancel_all_open` (last night's traceback was this pattern); regression test in `TestSessionEndResilience` covers the path; (2) [`9eea1b8`] — known-limitation documentation: reconciler matches by `exchange_id` and cannot distinguish fill from cancel for storage-only orders, so a fill-while-down leaves BTC inventory orphaned from the strategy (added to `v1.0-known-limitations.md` as the engine-reconciliation entry's "fill-vs-cancel disambiguation" subsection and to `v1.0-future-improvements.md` Group 3). **Three v1.1 candidates added during the soak** based on operator feedback: operator-initiated re-anchor command (Group 2; `c0ff561`), reconciler fill-vs-cancel disambiguation (Group 3; in the `9eea1b8` commit above), and web UI per-entity action buttons (Group 2; `91d8538` + `99d79b9` — Apply/Execute/Approve/Acknowledge labels per domain, Reject universal). Soak continues; minimum useful end is approximately **2026-06-02** (adjusted +1d for the outage-day interruption); comfortable end approximately **2026-06-15**.
    - **8.4.E (cont.) — soak Days 3–11 digest.** The laptop run was reframed (Day 6) as **pre-soak**; the v1.0-gating soak restarts ~2026-06-01 on the NAS Docker deployment post-move. Code stays feature-frozen — everything below is soak-surfaced defect fixes, operator-requested UX, or docs. Full per-day detail lives in git history, `docs/release/v1.1/`, and the `project_state` memory.
        - **Day 3 (2026-05-20):** transient-failure-resilience hotfixes (`e2b6cfc` finally-block isolation, `a9b9e43` per-tick balance fetch, `ae58c52` harvest per-tick today-total); web UX (Kraken Pro nav link/refresh `031fb55`, news fuzzy dedup + `rapidfuzz` dep `da8b1e4`, trading fees on cost dashboard `20a8bd8`, per-user settings + timezone + `tzdata`/`user_preferences` table `746c6cc`); config validator rejecting spacing ≤ 2× maker fee (`8c1acfa`). Process: a `Get-CimInstance` audit revealed harvest/advise/maintenance had died silently — runbook now mandates a process audit after any crash. Brand mark shipped (`ddaefc5`→`eec7f6b`→`c7fba08`→`39b0ac2`) plus status-card UX (current price / trend arrows / `humanize_duration`).
        - **Day 5 (2026-05-22):** engine resilience — mark-to-market session-loss cap (`87cc23b`), auto re-layout when no open orders remain (`e936f2b`), daily-spend counts only committed BUYs (`3ac3757`). Health observability — `/health` page + dashboard traffic-light (`d2da41a`/`d938044`), Kraken SystemStatus probe + 7-daemon freshness via a `daemon_heartbeats` table, config-derived thresholds (`a544d8d`). Discord cli/operator restored (task #84) — root cause was a channel-level permission grant.
        - **Day 6 (2026-05-23):** graceful-shutdown timeout for daemons (`cli/_common.safe_shutdown`, `49e53a7` + wiring `34c9619`/`a998b71`/`8a85cbd`/`516f4f8`); logging audit rebalancing 28 calls (`c9e7781`/`664fbfb`/`28c903e`/`6b0f770`/`042f51b`); news publisher attribution + click-through URLs (`9dd8640`); v1.1 doc reorg split into `docs/release/v1.1/` (`a52feb2`).
        - **Day 8 (2026-05-25):** reasoning-model diagnostic arc reversed two long-standing "broken" verdicts via direct probes; `force_json` per-(model,role) finding; new `tools/diagnose_reasoning_model.py` + `tools/sweep_reasoning_fixes.py` + `docs/reference/sweep-2026-05-25-reasoning-models.md`; web `/audit` → `/history` rename (`d4f387a`).
        - **Day 9 (2026-05-26):** today's-PnL verification (`a49b9bc` — cycle_matcher fallback paired a 3-day-old BUY; arithmetically correct, misleading as "today"); reasoning-model verdict closed as DROPPED (`1940faf`); Kraken UI upsell standing rule (`a9b38db`); friend-deployment 5-tier reframe (`7acfee2`/`e40e01a`); NAS-Docker identified as the gating-soak target.
        - **Day 10 (2026-05-27):** NAS Docker deployment — 7 daemons live on the Synology DS1823xs+ as a Portainer stack pulling `ghcr.io/carldog/wobblebot`. `feature/docker-support` merged (`a6a9bfa`); GHCR CI workflow (`f58bb6c`/`d594beb`); Kraken env rename to READER/TRADER/HARVESTER (`ed5033e`). **Secret-exposure incident:** `docker compose config` without `--quiet` printed real `.env` secrets — all credentials rotated; `--quiet` is now the only safe validation form. Six v1.1 entries logged; repo flipped public for anonymous GHCR pulls.
        - **Day 11 (2026-05-28):** NAS post-deploy stabilization. `PlainExtraFormatter` surfaces `extra.error` across 94 log sites (`497ae89` + cli/operator fix `5c0d6a5`); Discord AssistantError root-caused to qwen2.5:3b-q4 stochastic JSON-key corruption; a 16+8-model NAS sweep (`tools/sweep_assistant_nas.py`) crowned `qwen2.5:1.5b-instruct-q4_K_M` (8/8, no cache-warm tax) — cpu-only profile updated (`44a9ae2`), record at `docs/reference/sweep-2026-05-27-nas-operator-models.md`. High-frequency-grid v1.1 entry (`d3ca7ad`/`8a79c7e`). Operator-model flip to `qwen2.5:1.5b-instruct-q4_K_M` confirmed live (operator daemon warmed it 01:25 UTC; no more JSON-corruption AssistantErrors). **Advise daemon found failing 100% since deploy** — the cpu-only single-LLM advisor (`llama3.1:8b-q4`) generated toward `num_predict=512` at ~4 tok/sec and exceeded the 120s client timeout every tick (Ollama GIN log `500 | 2m0s` from the advise container IP at the exact failure timestamps; root-caused via Portainer + Ollama logs). Fix: `quant.md` rationale capped at ≤2 sentences + cpu-only advisor `timeout_seconds` 120→180 + Ollama adapters now surface the exception type (the empty `Ollama request failed:` string had masked the timeout). Open thread: apply the NAS bind-mount `settings.yml`/`quant.md` edits, redeploy the stack for the logging fix, re-enable Portainer AutoUpdate (stripped by MCP redeploys). **Advisor model reselection (2026-05-29):** rather than bump the advisor timeout and hope, built + verified a CPU-only-NAS advisor model-selection sweep — `tools/probe_advisor.py` rebuilt around a 12-fixture accuracy battery (decoupled current-spacing, no-partial-credit rubric, ~52% inherent constant ceiling, oracle 36/36) + `tools/pull_and_probe_advisors.py` retargeted at the NAS Ollama over HTTP (tiered `--tier1`, resumable, disk-bounded) + `tests/tools/test_probe_advisor_scoring.py` (10 cases). Two multi-agent verification workflows (5-agent blind adjudication 12/12 both rounds + adversarial review) caught + fixed 6 defects (one high: truncated-pull-laundered-as-success). 600s advisor timeout is the stopgap until the sweep names the model; sweep run is operator-paced (pause `operator`/`advise`, `--tier1` first).
        - **2026-06-02 — gating soak restarted MULTI-COIN on the hardened `:main` candidate.** Single-coin BTC had gone offside + parked (ADR-006) → near-zero engine coverage, so the soak restarted across 5 alts (ETH/SOL/XRP/DOGE/ADA; BTC held out of the grid; +$150 operator deposit → ~$242 account). The restart surfaced **two real safety defects, both fixed on `main`** (the candidate is **no longer frozen** — it takes soak-hotfixes): (1) **per-symbol `OpenOrders` rate-limit storm** — the engine fetched open orders once per *symbol* per tick, so 5 coins tripped `EAPI:Rate limit exceeded`, blocking startup reconciliation + the shutdown cancel; fixed to one global fetch/tick (`abf3aa6`; ~3 private calls/tick regardless of coin count). (2) **dead-man's-switch disarmed on a failed shutdown cancel** — a rate-limited `_cancel_all_open` fetch-failure returned `(0,0)` → `cancel_clean=True` → `set_dead_mans_switch(0)`, leaving ~15 orders open AND unprotected ~10 min; same `abf3aa6` fixes it (fetch now *propagates* → `cancel_clean=False` → switch stays armed), regression test `8b25feb`, arm verified live via `tools/check_dead_mans_switch.py` (Kraken `triggerTime` = `currentTime` + timeout). Also: DOGE `order_size_usd` $5→$6 (fixed 50-DOGE ordermin; $5 = 49.99 DOGE at ~$0.10); a P3 buying-power-card v1.1 entry (`38b8678`). **Ops finding:** the Portainer stack's `AutoUpdate.Interval: 5m` (git-tracked to `main`) silently revived stopped `restart:"no"` containers every ~5 min — the "mystery restart" (incl. the earlier 4:22am one); disable stack auto-update to keep a daemon down. Deploy = bump the `IMAGE_TAG` stack env var; the soak is pinned so `main` pushes don't auto-redeploy. A 9-agent v1.1 plan review (2026-06-02) surfaced 4 money-correctness gaps now in v1.1 P1 (harvester replay guard, harvester-key separateness, today's-PnL `limit=100` truncation, dead `EmergencyStopConfig`). Multi-coin soak targets ~1 month; pass = engine-coverage + reconciliation + all-daemon-cycles + no-hard-stops (profit/BTC NOT a criterion).
        - **2026-06-02 (late night) — three v1.1-planning audits**, each a multi-agent workflow with an adversarial verify pass, folded into `docs/release/v1.1/README.md`: **deep-scan** (`8c6668b`, 15-agent/7-dimension sweep, 11 findings — F1 live partial-fill Trade-drop is the one real correctness gap, rest are branch-safe one-liners/config candidates); **test-honesty** (`3de8bc7`, 12-path mutation-mindset audit — hygiene gold-standard, zero tautological/over-mock tests across 153 files, but a meta-pattern surfaced: decision logic is pinned, consequence/orchestration paths mostly aren't, folded as P1 test-hardening rows); **pylint-disable audit** (`4586cf7`, all 122 disables — zero correctness-class suppressions, one actionable finding: `GridEngine.cancel_open_orders` returning `(0,0)` on a fetch-failure is indistinguishable from "nothing to cancel," reached only via the operator cancel command). Codebase graded clean for a solo-operator $100-test grid bot.
        - **2026-06-03 — operator-cancel `(0,0)` fix** (`be1db4f`) closes the one pre-tag-worthy pylint-audit finding: `cancel_open_orders` no longer swallows a failed `get_open_orders` fetch into a false all-clear (lets `ExchangeError` propagate; `_dispatch_cancel_open_orders` reports failure instead of silent success); `success = failed==0` tightened so a partial per-order cancel failure no longer reads green. **Dashboard-confirmed soak result:** 3 closed profitable alt cycles this session (ADA/DOGE/ADA, ≈+$0.50 net of fees) validate the multi-coin thesis — alts cycle at 3%/$5-6 spacing where single-coin BTC just parks. **Web UI batch** (mode-badge → whole-UI review → notif-color fix → single-mode-source refactor → soak-window slices → full UI batch, 4 rounds of commits): dynamic LIVE/SHADOW mode badge consolidated into one `application.mode` field (`19ec0ee`→`963b796`, retiring a duplicate `web.mode` knob the operator caught); F2 NaN-guard on `_coerce_numeric` closing an auto-apply crash path (`d35f17e`); harvester-key separateness startup check refusing to run if the harvest key lacks Withdraw scope or matches the trade key (`afe9bb9`, ADR-003); account scoreboard strip (`cbb9430`), per-symbol grid-band sparklines, `/cost` daily-spend bar chart, a minimal responsive `@media` breakpoint, signed vs-market % column, and Kraken-style fill toasts (`31e3627`→`f69ca13`). 2309 tests, pylint 10.00/10 by session end. **From here forward the deployed soak ran unattended-stable on the `abf3aa6`/`8b25feb` candidate** — all subsequent feature/hardening work continued on the `v1.1` branch per operator decision, with no further `main` hotfixes needed.
        - **2026-06-04 — ADR-022, advisor reorientation (successor to ADR-019).** Root-caused the soak's "advisor keeps recommending TIGHTEN below the 3% grid" complaint: the Stage-8.5 vol→spacing curve's ceiling (2.70%) sits below the live 3% grid, so it mechanically recommended TIGHTEN on nearly every non-guard tick. Retired the curve outright (not recalibrated — ADR-019 already rejected "rest at 3%"); the heuristic is now guards-only, and every non-guard tick escalates to an LLM **free judge**. A new application-time floor (`8500226`, ADR-002/019 defense-in-depth) means auto-apply can never land a spacing tighter than the configured value regardless of what the LLM recommends — the LLM keeps full rein to *recommend* (so advisory accuracy stays trackable) but a bad tighten can never *apply*. Cloud model bake-off (o3 / o3-mini / o4-mini / claude-haiku-4-5 / gemini-flash) picked **`gpt-5-mini`** — best judgment on the cases that reach the LLM at ~1/3 the cost of o3. New no-guard fixture battery (`tools/probe_freejudge.py`) as the offline lab: gpt-5-mini measured a **stable 62% OK / 20% UNSAFE tighten-into-risk bias across 84 judgments** — every unsafe call lands below the floor and is structurally inert. Web: sub-floor suggestions dimmed + badged on `/advisor`. Same-day follow-up: risk/news/arbitrator role prompts realigned to the post-ADR-022 charter, the quant free-judge prompt corrected (trend asymmetry, fee floor, metrics sync), and a "chaos-gremlin" (loose-reasoning, scored-not-applied) advisor concept drafted and queued to the v1.1 backlog.
        - **2026-06-05 — ADR paper trail for the P1-P3 backlog** (no code): wrote ADR-023 through ADR-027 (P1) plus an ADR-007 news-firewall amendment resolving a collision with ADR-022; wrote ADR-028 through ADR-031 (P2/P3: auditor, counter-target, engine-state, re-anchor). Dependency fix: `starlette` 1.0.0→1.0.1 for CVE-2026-48710.
        - **2026-06-17 (fleet-review #12, claude-opus-4-8).** First fleet-review pass on the branch: money-movement paths (harvest gates, DMS, shutdown cancel, nonce/signing, withdrawal idempotency) all clean, no blockers. 3 should-fix items shipped the same day: a duplicate-withdrawal guard on repeat `harvest --execute`, a pre-commit PII-scan gap (git-renamed/copied files surfaced as `AM`/`ACMR` status weren't being scanned), and a stale-pricing fix folded into the maintenance pass below.
        - **2026-07-12 → 2026-07-25 — routine maintenance, no features.** `claude-sonnet-4-6` pricing re-verified at $3/$15 (verified_date bumped); harvester defense-layer comments renumbered 1-9; `httpx2` added as a dev dependency so the starlette 1.x `TestClient` can collect; stale `gemini-flash` thoughts rate dropped; Dependabot bumped `starlette` 1.0.0→1.3.1, a python-minor-and-patch group (7 updates), `bcrypt`, `types-pyyaml`, six GitHub Actions (checkout/setup-python/buildx/login/build-push-action), and the Docker base image (3.13-slim→3.14-slim). One security fix (2026-07-25) removed an internal hostname that had leaked into tracked files.
        - **2026-07-26 (fleet-review #19, claude-fable-5) → 2026-07-31 — second fleet-review pass, full resolution.** Reviewed 159 commits since #12; same verdict on the money-movement core (solid, no blockers), confirmed all 3 of #12's should-fix items landed, and surfaced 8 new should-fix items in config plumbing/bookkeeping/web+advisor surfaces. ADR-032 written (cost-basis sell guard; retires `EmergencyStopConfig`). 3 findings closed 2026-07-28 (`a4064d9` CI now runs the test job on `v1.1` pushes too, image publish stays main-only; `211f521` notification query ordered newest-first so `LIMIT` returns the newest rows, not an arbitrary slice), plus a Dependabot batch (Python 3.13→3.14 slim, 6 Actions bumps, a python-minor-and-patch group, `types-pyyaml`). **The remaining 5 closed 2026-07-31** (`2f509ef ab0e172 ce911ba 1450813 3813067 7a31f7b d0d6e50 59bf7c9 3ce07c5`): cost-estimate crash-loop reintroduction in 3 cloud assistant adapters (fixed via an `estimate_cost_fn` callable evaluated *inside* `wrap_provider_errors`); `get_trade_history` unpaginated (now paginates via Kraken's `ofs`/`count`, capped at 20 pages); `profiles.<name>` config overlay never validated against the schema (`extra='ignore'` silently dropped typos — new recursive guard in `config/resolver.py`); Ollama thinking/response concatenation order reversed so "last JSON wins" extraction picks the real answer, not CoT-echoed JSON; web + Discord confirm routes both now check `ttl_expires_at` before acting on a pending command. A 5-agent adversarial re-review of the first 5 fixes caught two real gaps, fixed same session: the profile-overlay guard didn't handle `RootModel`-backed sections or `list[SubModel]` fields (would have reproduced the original silent-drift bug through an uncovered shape); the trade-history pagination fix was still called once per symbol per tick, risking the same rate-limit-storm class `abf3aa6` already fixed once (consolidated to one shared fetch/tick via a new `GridEngine.has_pending_fill_candidates()`). **All 8 of #19's findings are now closed on `v1.1`** — GitHub issues #19 and #12 remain open pending eventual merge to `main`.
        - **2026-07-20 → 2026-07-31 — an 11-day host-wide NAS reboot, discovered via log review, resolved clean.** The whole NAS host rebooted 2026-07-20 ~18:23 UTC (confirmed host-wide: all 30+ containers, not just wobblebot's, showed identical "Up 11 days" uptime). `wobblebot-live` and `wobblebot-harvest` run `restart:"no"` (deliberate — real-money daemons shouldn't blind-resume after an uncontrolled restart), so both sat EXITED for 11 days while every `unless-stopped` daemon (web/operator/maintenance/advise/observe/news) auto-recovered and kept the dashboard looking healthy the whole time. No trades placed, no treasury monitoring, for 11 days — unnoticed until a full Portainer log pull. Operator confirmed directly on Kraken: no stranded open orders. Resumed on the same `sha-5cb2863` image: reconciler cleanly marked 13 stale storage-only orders `canceled`, grid re-laid out at the existing anchor, a few symbols offside/parked from 11 days of price drift (expected, non-fatal, same behavior seen pre-outage). **This is the incident that most directly exercises ADR-018's reconciliation design, and it held — no fund loss, no silent state corruption.** The one gap it exposed (nothing pushes a stale-heartbeat signal when a daemon simply isn't running) is queued to the v1.1 backlog (P3, `6835bd5`), not a blocker for the v1.0 tag. **Soak pass-criteria verdict: MET** — engine-correctness coverage, reconciliation-across-restarts (proven twice, by the 06-02 storm and this blackout), at least one cycle of every daemon, and no hard-stop that corrupted state or lost funds. Full narrative in `docs/planning/phase-8-summary.md`.
        - **2026-07-31 — CryptoCompare retired** (`1f5da22`). ADR-010's 90-day evaluation (due 2026-08-13) closed ~2 weeks early: CoinDesk Data (the CryptoCompare rebrand) retired free API access on 2026-05-21; `cli/news` had been failing its ~30-min CryptoCompare poll for 3+ days with a rate-limit error — an external business decision, not a cadence bug. RSS (7 feeds) unaffected throughout. `news.cryptocompare.enabled` flipped to `false` in `settings.example.yml`; `v1.0-known-limitations.md` updated with the actual outcome instead of a dangling evaluation note.
        - **2026-07-31 — PR #24 opened, then `v1.0.0` tagged on `main` separately.** PR #24 ("v1.1 branch checkpoint") opened as a review-prep checkpoint. Fixed a genuine 2-file merge conflict against `origin/main` (`pyproject.toml` — kept main's newer pinned `fastapi`/`starlette`/`uvicorn`/`httpx2` since the CVE fix is a lower-bound patch any later release still carries; `llm_pricing.py` — pure addition, no real conflict) plus a black line-length CI failure (`c0adf19`). Addressed a Copilot automated-review finding on `web/routes/advisor.py`'s `_as_float()` — verified the precise mechanism before fixing (NaN comparisons are always `False` so `below_floor` wasn't actually flipped by NaN, but `-Infinity < any_finite` **is** `True`, so `-Infinity` was the real bug) and fixed with `math.isfinite()` rejection (`efe2794`), replied to the review thread, and resolved it. **Separately, the operator decided to tag `main` as `v1.0.0` as it stood** (not merge PR #24) and continue fleshing out `v1.1` on the branch — see 8.4.F below. Re-merged `origin/main` (the tag commit) into `v1.1` afterward to keep PR #24 conflict-free.
    - **8.4.F** ✅ (2026-07-31) – Post-soak release ceremony, executed on `main`. `docs/planning/phase-8-summary.md` written; `pyproject.toml` + `src/wobblebot/__init__.py` version 0.1.0 → 1.0.0; CHANGELOG `[Unreleased]` → `[1.0.0] - 2026-07-31` (stale pre-Phase-8 `[v1.0.0] — TBD` stub retired); annotated `git tag -a v1.0.0` on `main`. **Deliberately scoped to `main` as it stood at tag time** — the substantial work already built on `v1.1` (ADR-022 advisor reorientation, web UI expansion, both fleet-review passes' fixes, ADR-023–032) is **not** part of this release; the operator chose to keep developing it on the branch rather than force an unreviewed bulk merge at tag time. `v1.1` continues per `docs/release/v1.1/README.md`.

   **Numbers on `v1.1` (2026-07-31, pre-tag-merge)**: 2369 unit tests pass. mypy clean across 116 src files. pylint **10.00/10**. black + isort clean. **Stage 8.4 real-money cost unchanged at $0.00** since the 2026-06-03 alt-cycle profit — the 2026-07-20→07-31 NAS outage window placed no trades. Running project real-money cost stays at **$0.085018**.

   **Numbers on `main` at the `v1.0.0` tag (2026-07-31)**: 2267 unit tests pass (was 2121 at 8.4.E day 11 — the gap is soak-period defect fixes + Stage 8.5's advisor cascade, both already on `main` before the soak restart; `v1.1`'s 2369 includes ~100 additional tests from branch-only work not in this tag). mypy clean across 116 src files. pylint **10.00/10**. black + isort clean. **Stage 8.4 real-money cost: $0.00** beyond the running ledger. Running project real-money cost at tag: **$0.085018**.
6. **Stage 8.5 – Advisor Engine: Heuristic + LLM Cascade** ✅ (2026-05-29, pre-soak) – A pre-soak value-add slotted before the 8.4.E gating-soak restart (~2026-06-01) so the month-long soak runs on the real advisor; ships before the v1.0 tag. Continues the Day-11 advisor-reselection arc: a one-session investigation (probe + 12-core / 8-held-out fixture batteries) settled "would an LLM advisor help?" empirically — **no local CPU model reasons well enough** (best 16/36 on the core battery; a constant scores ~19/36), but a frontier reasoning model + a complete prompt is genuinely good (openai `o3` + `claude-opus-4-8` each 4/4 on the held-out conflict discriminators). Operator chose `o3`, then refined to a heuristic+LLM cascade. Full investigation + design + as-built in `docs/planning/stage-8.5-advisor-cascade-design.md`.
    - **Operator-tunable heuristic spec.** `config/heuristic.py` (`HeuristicSpec`: ideal(vol) `curve` + `fee_floor` + `hold_deadband` + four guard sub-models each with an `enabled` toggle + an `escalation` band; `load_heuristic_spec()` mirrors the prompt loader). Committed default `config/heuristic/quant.yml`, operator-editable like the prompt files (bind-mount-friendly). Curve + thresholds are DATA; the guard algorithm + priority order stay in CODE (a new guard is a code change with a fixture test). Operator-ratified shape: YAML data file (not markdown), thresholds + per-guard on/off toggles (not a rule DSL).
    - **`HeuristicAdvisorAdapter`** (`adapters/heuristic_advisor.py`): ideal(vol) piecewise-linear + fee-floor clamp + four guards (directional-runaway → defensive-drawdown → dont-fix-working → fee-floor-calm → first-order). `evaluate() -> HeuristicVerdict` (recommendation + `clear_match` escalation signal + direction + reason). The unit test loads the SHIPPED `quant.yml` and reproduces both probe batteries — **core 36/36, held-out 24/24** — so editing the curve and breaking a fixture fails loudly.
    - **`CascadingAdvisorAdapter`** (`adapters/cascading_advisor.py`): heuristic-first; escalate ambiguous calls to the LLM; fall back to the heuristic on `AdvisorError` / `LLMCostCapExceeded`. No `mode` enum — `cli/advise._build_advisor` dispatches on `advisor.engine` (`heuristic` → bare heuristic / `llm` → bare LLM, preserving the existing isinstance tests / `cascade` → wrapper).
    - **Config + wiring.** `AdvisorConfig.engine` (`heuristic | llm | cascade`) + `heuristic_file`. `engine` defaults to `llm` (back-compat: a new composite defaulting ON would break every existing config + many tests); the `cpu-only` profile sets `engine: cascade` + heuristic + cloud `o3` (temp 1.0 / max_tokens 4000 / timeout 120) — the retired local llama3.1:8b advisor confirmed non-viable by the sweep.
    - **Pre-existing bug fixed (in-scope cleanup).** `cli/advise._run_cycle` caught only `AdvisorError`, but the cloud adapters let `LLMCostCapExceeded` (a domain exception) bubble raw — the `engine: llm` cloud path would have crashed the daemon on a cap trip, contradicting the ADR-014 "catches and skips" promise. Now caught + skips the tick; the cascade is independently robust via its fallback.
    - **Follow-up (done 2026-05-29, commit `56a6e5f`):** the complete reason-first + override-aware prompt was merged into `quant.md` (levels/order_size guidance retained) and re-validated vs o3 — 17/24, 4/4 on the held-out conflict discriminators, no regression; the 6 ablation prompts were removed. Role heuristics for risk/news/arbitrator deferred to v1.1 (`docs/release/v1.1/adaptive-grid.md`). **Operator NAS actions to run the cascade:** cut/paste the synced `settings.yml` + add the top-level `llm:` block + `OPENAI_API_KEY` + `engine: cascade` (else the advisor degrades to heuristic-only, a valid $0 mode).

   **Numbers.** 2225 unit tests pass (was 2121 at the Day-11 close; + heuristic-spec, heuristic-adapter, cascade-adapter, engine-dispatch, and cost-cap coverage). mypy clean (116 src files). pylint **10.00/10**. black + isort clean on touched files. **Stage 8.5 real-money cost: $0.00** (the investigation's cloud probe calls were already in the ledger). Running project real-money cost stays at **$0.085018**.

   **Post-build backtest (2026-05-29) — the vol→spacing premise is NOT validated.**
   A historical backtest over Kraken 1m BTC (2013–2025) found the heuristic's core
   premise doesn't hold: realized BTC vol sits below the shipped curve's floor (it
   flat-clamps to 0.65% in every regime), and even a recalibrated curve is beaten by
   a fixed ~1.5% spacing everywhere — **trend, not volatility, is the signal that
   matters**, and the long-biased grid bleeds in sustained downtrends regardless of
   spacing. Parking-when-offside beats re-anchoring (vindicates ADR-006); a naive
   trend-pause filter doesn't help (pausing ≠ defending). **The cascade architecture
   + LLM are NOT rejected — only the vol-curve-as-P&L-driver.** Soak is unaffected
   (advisor is advisory-only). Full record + reproducible tooling:
   `docs/reference/grid-backtest-findings-2026-05-29.md` (`tools/heuristic_backtest.py`
   + `tools/grid_backtest.py`). **Actionable:** consider widening the live grid toward
   ~1.5%; pivot the advisor to trend/regime→posture (operator-confirmed, reasoning
   shown); graduated auto-apply (bounded knobs auto, de-risk-to-cash escalates).
   **Update (multi-coin + chop tested — 5 coins, 2024/2025 + per-coin chop windows):**
   the findings generalize, and two were REVISED. "Choppy alts shine" holds in
   *genuine* chop (alts +8% to +28%; the 2025 rejection was a crash-year confound),
   and the vol→spacing relationship is **mis-calibrated + mis-applied, not dead** —
   chop-window optima are 3–5% and diverge by asset, so the live 1% grid is far too
   tight and **per-symbol spacing is validated**. Two-lever model: vol→spacing sets
   the per-symbol/per-regime *spacing* (calibration), trend/regime sets *win-vs-lose*
   (defense). Still pending: more windows + the adversarial flip-the-script pass. No
   new algorithm built yet — diagnosis, not a replacement.
   **Flip-the-script DONE (2026-05-29):** two independent blind adversarial red-teams
   + an arbiter ruled the verdict `verdict-mostly-holds-with-revisions` (5 of 6
   skeptic biases run *against* the grid; both deliberate break-paths failed; §10 of
   the findings doc). An out-of-sample 2026 Q1 quarter (broad −21–33% downturn)
   reproduced "wider beats tighter" on all 5 coins and grid<hold. The actionable
   widen + advisor pivot are now **Stage 8.6**.

7. **Stage 8.6 – Advisor HARDENING + Grid Widen** ✅ 2026-05-30 (kickoff 2026-05-29,
   **RESCOPED then CLOSED 2026-05-30**). The regime-switching research arc closed first
   (heuristic regime detection does NOT beat hold), cutting 8.6 from "advisor regime
   reorientation" to hardening only; then measurement during the slices cut it further.
   **What shipped:**
   - **Slice C — grid widen** (`a1b39c4`): live BTC `grid.default.spacing_percentage`
     1.0 → 3.0% (the least-bad *static default*; exposure unchanged at $60 = 3+3 × $10;
     fee-floor validator + schema-drift green). ADR-006 park-when-offside unchanged.
   - **Slice B — lookback finding + guard dormancy** (`83d4589`): measurement REVERSED
     the planned "widen the metrics window" fix. At 3% the grid completes only
     ~0.2–0.4 cycles/day (2013–2025 BTC), so `dont_fix_working` (cycles_min 8) is
     unreachable in any vol-current window; widening the window would make the −5%
     drawdown guards fire on ordinary daily noise (24h dips ≥5% ~13% vs ~2% at 6h). So
     `metrics_lookback_hours` stays at **6h**; `dont_fix_working` is left enabled but
     documented-dormant at wide spacing (it auto-re-arms for the MoE world's tight
     grids). Numbers in the stage design doc's "Slice B finding" section.
   - **Slice A — curve recalibration: DEFERRED to the Oracle/regime track** (no code).
     Two findings collided: (1) the curve change would invalidate the blessed 20-fixture
     judgment battery (5-agent-adjudicated 2026-05-29), and (2) recalibrating to "rest at
     3%, never tighten" would bake in a false absolute — a tight grid *chosen in chop and
     pulled before the trend* genuinely works (proven live + oracle +164.6%). The advisor
     is advisory-only (`auto_apply` off) during the soak, so its mis-calibrated curve is
     harmless log-noise. The proper curve + battery rework belongs on the regime track
     where it can be built against real detection.
   - **Slice D — close-out:** ratified **ADR-019** (advisor purpose: regime reader +
     guardrail, not a vol-tuner; posture-advisory-only invariant; refines ADR-002/007).
     **ADR-020** (regime as a first-class metric) **DEFERRED** with the parked track.
   **Parked (not deleted), on the Oracle/MoE research track** (synthesis §4): the
   first-class regime classifier + posture output, and the curve/battery rework.
   Full account: `docs/reference/grid-strategy-research-synthesis-2026-05-30.md`; rescoped
   design + per-slice findings in
   `docs/planning/stage-8.6-advisor-regime-reorientation-design.md`. **Real-money cost: $0.00**
   (offline backtests only); running project total unchanged at **$0.085018**. Closes the
   pre-soak hardening; the ~2026-06-01 gating soak now runs the widened grid. Original
   kickoff text below is superseded.)
   _(original kickoff, 2026-05-29):_ (kickoff 2026-05-29,
   pre-soak) – Acts on the backtest verdict (Stage 8.5 post-build + flip-the-script):
   widen the live BTC grid off the catastrophic 1% toward ~3% (per-symbol; exposure
   unchanged at $60), and **reorient the advisor from a vol→spacing tuner to a
   regime reader + guardrail** — because the advisor is currently blind to the
   variable that decides win-vs-lose (trend/regime is not in `PerformanceSummary`).
   Two levers: (1) volatility → *base* spacing calibration (recalibrate the curve to
   real BTC vol so its resting call is ~3%, not the mis-calibrated 0.65% floor);
   (2) a **first-class regime classifier** (new `compute_regime` metric + `RegimeSignal`
   domain value object) → a **posture** (harvest / cautious / defensive) + projected
   downside that is **advisory-only, never auto-applied** (the backtest proved
   mechanical auto-de-risk fails; only bounded spacing stays auto-applicable, per the
   ADR-002/007 firewall). Also fixes the lookback coupling (a 6h metrics window
   completes ~0 cycles at 3%, breaking the cycle-based guards). Lands **before** the
   ~2026-06-01 gating-soak restart so the month forward-validates both the wider grid
   and the regime-aware advisor (operator decision 2026-05-29). Introduces **ADR-019**
   (advisor purpose: regime reader, not vol-tuner — refines ADR-002/007) and
   **ADR-020** (regime classification as a first-class metric). Full design + slice
   plan (A–F) + the 2026 Q1 out-of-sample check in
   `docs/planning/stage-8.6-advisor-regime-reorientation-design.md`.

## v1.1 Track — ships as `2.0.0` (developed on the `v1.1` branch)

**Status:** development began 2026-06-01 on the `v1.1` branch while the v1.0 gating soak
ran on the NAS; `main` stayed frozen at the soak commit until v1.0 tagged 2026-07-31. **P1
closed 2026-08-01 and merges to `main` as `2.0.0`, not `1.1.0`** — the branch's own
`EmergencyStopConfig` removal (ADR-032) is a breaking config-schema change, and ADR-022
fully replaced the advisor's decision architecture, both past what a minor bump should
carry under this project's SemVer discipline (see `CHANGELOG.md`'s `[2.0.0]` section). The
branch name, the ADR numbers, and this `docs/release/v1.1/` planning directory all keep
their `v1.1` name for history — only the released version number changed. **The sequenced
plan — phases P0–P4, the dependency spine, and the parked register — lives in
[`docs/release/v1.1/README.md`](../release/v1.1/README.md)** (the per-area files there hold
the detail; the full backlog index is
[`docs/release/v1.0-future-improvements.md`](../release/v1.0-future-improvements.md)). Only
*shipped* items are receipted here.

1. **Dead man's switch** ✅ 2026-06-01 — server-side `CancelAllOrdersAfter` safety net
   (`ExchangePort.set_dead_mans_switch` + per-tick pet/disarm in `cli/live`), on by default
   at 60s. Kraken auto-cancels all open orders if the host goes silent (crash/power/network
   loss) — the failure the `finally`-block cancel can't cover (2026-05-19 outage). **ADR-021**.
   Real-money cost $0.00. See [`docs/release/v1.1/engine.md`](../release/v1.1/engine.md).

2. **P1 — Safety-hardening + ready-now backlog** ✅ **COMPLETE 2026-07-31 → 2026-08-01**
   (`docs/release/v1.1/README.md`'s P1 table). One focused commit per item, full gate
   (pytest/mypy/pylint 10.00/black/isort) green before each; 2582 tests passing by the end
   (was 2369 at the `v1.0.0`-tag-adjacent checkpoint). Real-money cost **$0.00** — pure
   code/test work, no live trading. Sequential engine-safety items (each its own ADR +
   test-for-the-bug):
   - **ADR-032** — cost-basis sell guard; retires the dead `EmergencyStopConfig` (`f96eb34`).
   - **ADR-023** — unified terminal-order resolution (fill-vs-cancel + the F1 live
     partial-fill Trade-drop), one `_resolve_terminal_order` shared by the startup
     reconciler and the live `_detect_fills` gate (`3ba7e9f`).
   - **ADR-024** — session-loss-cap cool-down period, new `cap_trips` table (`b9cf5a1`).
   - **ADR-025** — pre-placement slippage/spread guard via a new `get_ticker` port method
     (`12a2bc5`); a same-day follow-up caught exchange-side placement errors aborting a
     whole tick instead of skipping the one order (`242fbf0`).
   - Dead-man's-switch arm confirmation — logs Kraken's `triggerTime` instead of discarding
     the response (`b42cf74`).
   - Boot-time stale-anchor WARN on restart re-layout (`617a7bb`).
   - Per-tick price-fetch dedup — one fetch threaded through, not two (`a4979c1`).
   - **ADR-027** — Kraken rate-limit backoff + inter-cancel pacing (`1b9e9b9`).
   - Partial-grid placement: insufficient-balance WARN demoted to DEBUG (`e05a931`).
   - Engine ordermin-awareness — **containment half only**: an uncaught exchange-side
     ordermin/costmin rejection was aborting every remaining level in the same
     layout/re-layout loop, not just the one doomed order; now caught alongside
     `InsufficientBalance` (`242fbf0`). The proactive half (bump volume to clear ordermin,
     or skip with a clear INFO, before attempting placement) was not built — the operator's
     per-coin `order_size_usd` workaround remains the mitigation.

   Parallel/independent items:
   - **ADR-026** — harvester `--execute` replay guard, DB-enforced via a UNIQUE index on
     `transfer_results.proposal_id` (`69a4519`).
   - **ADR-007 amendment** — structural MoE news-firewall fix: `news_materially_drove` flag
     blocks `role='aggregated'` auto-apply when news was the effective driver (`ee324ee`).
   - Today's-PnL truncation fix — `get_trades(limit=100)` → `limit=10_000` (`054690a`).
   - Content-Security-Policy middleware (`a19816e`).
   - Monthly backup-restoration smoke test — `PRAGMA integrity_check` + representative
     SELECTs against the latest backup (`b5dbfba`).
   - Kraken exchange-status news adapter, `status.kraken.com` → tagged `news_items`
     (`39cac1a`).
   - Footer "update available" indicator via `release_checker` polling GitHub (`dd35431`).
   - Four test-hardening additions closing the 2026-06-02 test-honesty audit's
     consequence/orchestration gaps: loss-cap trip E2E, preflight-gate orchestration,
     operator firewall-bypass negative test, reconciler fail-soft continuation (`2d168d6`).
   - Dashboard session-cap card — a durable Session banner (last `cap_trips` row + live
     cool-down state) so a trip during a missed Discord ping still has an on-dashboard
     signal; new `CapTripRecord`/`get_last_cap_trip` (`b33fbef`→`b5b57d6`).

   **Explicitly NOT built** (per the plan's own open questions, not oversights): `cli/up`
   (one-command daemon orchestrator) stays parked — "promote only if real restart friction
   is real," none reported; more Kraken pairs / which coins is an operator risk-budget call,
   not a code task. Both remain open in the Parked register / Open Questions section of
   `docs/release/v1.1/README.md`.

   Also landed alongside P1 in this window (fleet-review + soak-adjacent, not P1 items
   themselves): the second fleet-review pass's remaining 5 findings (cost-estimate
   crash-loop reintroduction, trade-history pagination, profile-overlay schema validation,
   Ollama thinking/response concat order, TTL check on pending-command confirm routes) plus
   an adversarial re-review catching 2 further gaps in those fixes; the 11-day NAS host
   reboot incident (resolved clean, reconciler held); a CI gitleaks secret-scanning workflow.
   Full narrative in Stage 8.4.E's digest above.

3. **Cache-aware LLM cost accounting** ✅ 2026-08-02 — **ADR-033**, from the prompt-caching
   investigation. All three cloud extractors now capture provider cache-usage fields
   (`TokenUsage` disjoint buckets replacing the `TokenTuple` 4-tuple); `LLMPricePoint` gains
   verified cached-input/cache-write rates (fallback = full input rate, never under-reports);
   `llm_calls` gains `tokens_cache_read`/`tokens_cache_write` via additive migration; `/cost`
   card + `show_llm_costs` surface cached totals. Fixes a live mispricing: `gpt-5-mini`
   escalations with OpenAI automatic-cache hits were billed at full input rate in the
   ADR-014 ledger. Actively *enabling* Anthropic `cache_control` is **deferred with
   triggers** (ADR-033 decision 5; Parked register → CI/infra): the 4h advisor cadence
   exceeds both cache TTLs and no deployed path reaches Anthropic. Real-money cost $0.00
   (pricing pages re-verified, no API calls). Test count 2582 → 2603.

4. **P2 — data-infrastructure spine: ✅ COMPLETE (2026-08-07 → 2026-08-08).**
   Slice 1 — backfill ergonomics
   ✅ **2026-08-07**: all seven polish items from `adaptive-grid.md`'s catalog, one
   focused commit each, full gate green per commit (`--days`, `--catchup`/`--since auto`,
   per-chunk progress logging, `--rate-limit-seconds`, `--resume` on a new
   interval-scoped `StoragePort.get_latest_ohlc_opened_at` cursor, `--intervals`,
   horizon-truncation WARN). Live scratch-DB verification surfaced a material fact the
   scenario catalog missed: **Kraken's live OHLC endpoint retains only ~720 bars per
   interval** (an 8-day 1m request returned 721 bars; the new WARN fired exactly as
   designed) — deep history is import-dump-only, confirming slice 2's necessity.
   Real-money cost $0.00 (public read-only endpoints). Test count 2609 → 2657.

   **Slice 2 — history import** ✅ **2026-08-07** (same day): the `OHLCBar`
   `low<=open/close<=high` validator (adapter wraps violations as `ExchangeError`;
   importer skip-and-logs), the `StoragePort.get_ohlc_bars` read-side (the
   blueprint's published contract — ASC, `[]` on miss, inclusive bounds),
   `synthesize_snapshots` promoted public (importer + backfill share the one
   bar→snapshot rule), shared interval parsers + public `symbol_to_kraken_altname`,
   and `tools/import_kraken_history.py` (streams base + quarterly OHLCVT CSVs,
   batch INSERT OR IGNORE, `vwap=0` no-vwap sentinel). Live-verified on the real
   dump: BTC/USD @ 1h = 98,541 rows (2013-10-06 → 2026-03-31) in ~1.5s, 0 skips,
   byte-exact first/last round-trip, idempotent re-run inserts 0. Known data gap:
   quarter-end (2026-03-31) to the live ~720-bar horizon stays unfillable until
   Kraken publishes the next quarterly dump (the horizon WARN flags it).
   Real-money cost $0.00 (fully offline). Test count 2657 → 2685.

   **The one-shot bulk import ran 2026-08-08** against the operator's local
   `data/wobblebot-observe.db`: all 11 `observe.symbols` at 1h — **670,111 bars**
   (each pair's full Kraken listing history → 2026-03-31) + matching synthesized
   snapshots, 0 skipped rows, ~70s, idempotency spot-verified.

   **Slice 3 — OHLC+TA indicators** ✅ **2026-08-08**: `services/ta_metrics.py`
   (RSI/MACD/Bollinger/SMA/EMA/ATR/ADX/Stochastic, hand-rolled textbook formulas,
   frozen compound results, private `_compute_*_series` for the auditor/screener);
   16 `float|None` TA fields on `PerformanceSummary` wired via `SummaryBuilder`
   over a 260-bar 60m `get_ohlc_bars` window; `quant.md` vocabulary update (ADX +
   price-vs-SMA as the direct trend reads ADR-019 wanted). Staleness guard: >3
   intervals old → all-null TA + actionable WARN; live verification caught and
   fixed the out-of-window silent-DEBUG case (a quarter-old dump import now WARNs).
   Real-window verification against the imported BTC data produced coherent
   textbook values (Bollinger middle == SMA20, stochastic/RSI agreement).
   **Open operational gap (operator decision pending):** nothing maintains fresh
   1h bars steady-state, so production TA is null-with-WARN until a
   `--resume --intervals 1h` cron or a small observe-daemon hourly top-up ships.
   Advisor-only per the blueprint — nothing wired into `cli/live`.
   Real-money cost $0.00. Test count 2685 → 2721.

   **Slice 3 follow-up — steady-state hourly-bar top-up** ✅ **2026-08-08**
   (operator-approved): `cli/observe`'s poll loop now resumes each symbol's 60m
   bars hourly from the interval-scoped cursor (completed bars only — the live
   endpoint's in-progress bar would freeze partial under INSERT OR IGNORE);
   `bar_topup_enabled` flag. Live-verified: BTC cursor 2026-03-31 → last
   completed hour, TA fields went from null-with-WARN to live values. The
   forced companion: observe.py crossed pylint's 1000-line gate, so the
   `--backfill` mode split into `cli/observe_backfill.py` (pure move).
   Test count 2721 → 2727.

   **Slice 4 — auditor config-replay** ✅ **2026-08-08** (**ADR-028**, +
   implementation note): `tools/auditor.py` replays `settings.yml` through the
   REAL `GridEngine` over stored bars (4-price sequence per bar; fresh engine +
   `:memory:` SQLite + `AuditorExchangeAdapter` per symbol). All three judge
   corrections honored — daily cap neutered (and ONLY that one: the ADR's
   bar-time alternative is a trap, see the ADR's new implementation note),
   placement-fill suppressed, bar-0-open anchor by construction. BTC 1m history
   imported (4.77M bars). Live-verified: March 2026 BTC at 1m (43,182 bars,
   ~95s) through the operator's real 3% config — 1 completed cycle +$0.13,
   offside park/resume exercised. Rec-scoring half stays P4.
   Real-money cost $0.00. Test count 2727 → 2733.

   **Slice 5 — `cli/screener` v1** ✅ **2026-08-08**: rank observed symbols by
   grid-suitability, per the blueprint (no ADR, no DB table, log-table output,
   fully offline in v1 — no credentials). New `ScreenerConfig` section +
   `StoragePort.list_ohlc_symbols` discovery read + `services/screener.py`
   (rank-based composite; vol + ATR% as distance-from-band-center — the
   non-monotonic read; flatness descending; Pearson-from-scratch correlation as
   a post-score annotation, n/a under 50 aligned bars, self-correlation
   excluded). Live-verified on the full 11-symbol lineup after a `--resume`
   1h top-up: default band centers landed mid-distribution of the real vol
   spread (0.30–0.72%); SOL ranked #1, ADA last (hottest + trendiest), ETH–BTC
   correlation +0.86, POL the diversifier at +0.30. v1.5 (spread/volume) and
   v2 (RSI/ADX/BB) recorded, not built. Real-money cost $0.00.
   Test count 2733 → 2756.
   **Slice 6 — counter-order target** ✅ **2026-08-08** (**ADR-029** + implementation
   note): `counter_target_mode` on `GridLevels` — `spacing_up` (default, byte-identical
   behavior) | `top_sell` (BUY-fill counter SELL → band ceiling; SELL-fill counter
   unchanged — the ADR's asymmetric design). Read live from `coin_cfg` each tick
   (`order_size_usd` precedent), never snapshotted into `GridState` — no re-anchor, no
   migration. Auto-apply exclusion automatic (non-numeric key outside
   `_WHITELISTED_NUMERIC_KEYS`) + pinned with gate and `proposed_grid` passthrough
   tests. Two beyond-ADR touchpoints wired + regression-tested (recorded as the ADR's
   implementation note): `for_coin`'s explicit field enumeration would have silently
   dropped the mode for override-less coins, and the ADR-023 startup-recovery counter
   path needed `grid_ceiling` threaded through. `!grid` surfaces the mode. Inventory-
   accumulation risk documented in `settings.example.yml` + known-limitations.
   Real-money cost $0.00. Test count 2756 → 2767. **P2 COMPLETE — all six slices
   shipped 2026-08-07 → 2026-08-08.**

5. **P3 — ops/observability/UX: IN PROGRESS (started 2026-08-08).**
   **Slice 1 — stale-heartbeat Discord push alert** ✅ **2026-08-08**
   (operator-flagged from the 2026-07-20 NAS-reboot incident — cli/live +
   cli/harvest dead 11 days while the pull-only /health looked fine): new
   `_heartbeat_alert_loop` in `cli/operator` (third sibling of the forwarder +
   TTL-expirer tasks), 60s check cadence reusing the ONE staleness definition
   (`fetch_daemon_freshness` + `derive_thresholds_from_config` — no new
   hardcoded multiplier); pure `_HeartbeatAlertTracker` pins the rules:
   **stale-on-first-check alerts immediately** (the reboot scenario — state is
   deliberately in-memory so a restarted watcher re-alerts anything already
   down), fresh→stale transition alerts, 6h repeat while down, one info
   recovery notice, UNKNOWN is no-signal (never alerts, preserves stale
   memory), `critical` for the restart:"no" money-path daemons (live/harvest)
   vs `warning` for the rest, self-row skipped. Alerts are `Notification` rows
   via the existing `notify()` — the 2s forwarder pushes them to Discord; no
   new table, no new config, no ADR (per the observability.md design).
   Real-money cost $0.00. Test count 2768 → 2779.

   **Deployed same-session** (NAS → `sha-2c65ec8`) — and the monitor **caught a real
   incident on its first check**: `cli/harvest` had been DOWN since the 2026-08-05
   2.0.0 bump (`Exited (3)` — the P1 key-scope gate refusing per ADR-003: the NAS
   Harvester key lacks Kraken Withdraw scope; `restart:"no"` kept it down, invisible
   in running-container views). **Operator decision 2026-08-08: harvester stays off
   for now.** Second alert: cli/news WARNING was the content-freshness blind spot
   (news_items inserts are dedup-gated → quiet night reads stale).

   **Slice 2 — alert-quality follow-up** ✅ **2026-08-08** (both findings from
   slice 1's first live check): (1) `operator.heartbeat_alert_mute` — explicit
   expected-down list consumed by the alert tracker (NAS mutes `cli/harvest` per the
   operator decision; deliberately NOT keyed off `harvester.enabled`, which gates
   withdrawal *execution* — the daemon legitimately runs proposals-only with it off);
   muting silences the push only, /health keeps showing truth. (2) cli/news moves to
   the heartbeat classifier — new `news.operator_db` + `emit_heartbeat` per poll
   cycle; `fetch_daemon_freshness` drops the `news_db` param (news was misfiled as
   Approach-B; its primary writes are dedup-gated, the exact conditional-write case
   the heartbeat table exists for). Reclassification pin test: quiet news window +
   fresh heartbeat = FRESH. Real-money cost $0.00. Test count 2779 → 2783.

   **Slice 3 — `engine_state` keystone** ✅ **2026-08-08** (**ADR-030** +
   implementation note): the re-anchor chain's shared unblock. New `engine_state`
   table in operator.db (per-symbol paused/offside/offside_ticks/reference_price/
   anchored_at/updated_at, PK (base,quote)); frozen-dataclass `EngineStateRow`
   (cost_basis precedent — deliberately not pydantic); `StoragePort.save_engine_state`
   / `get_engine_states` on the heartbeat contract. `cli/live` publishes per symbol
   per tick from `_run_loop` (engine accessors ONLY — new `GridEngine.offside_ticks()`;
   StepResult.offside is False on non-"stepped" actions and must not feed the row);
   nullable anchor fields; failed grid-state read degrades, never drops. Dashboard
   renders PAUSED/OFFSIDE badges from rows fresher than 3× `live.tick_seconds`
   (threaded to cli/web like cool_down_minutes) — absent/stale rows render nothing;
   a dead engine's claim ages out within one refresh. Closes the "web sees all
   symbols active" gap. No config keys, no migration (new table). Recorded follow-ups
   in the ADR note: shadow-mode discriminator; feeding the assistant's hardcoded-
   "active" snapshot from this table. Real-money cost $0.00. Test count 2783 → 2804.

   **Slice 4 — operator-initiated re-anchor command** ✅ **2026-08-09** (**ADR-031** +
   implementation note): `GridEngine.request_reanchor` under the per-symbol lock —
   **cancel-FIRST atomically** (any failed cancel OR an indeterminate open-order fetch
   aborts before `save_grid_state`; regression-pinned with a one-cancel-fails stub),
   then a fresh `GridState` from the CURRENT coin config at execution price, offside
   counter cleared, auto-resume, and the layout placed **in-process** via the new
   shared `_place_layout` (judge correction A — pinned by the offside test: re-anchor
   while parked places orders, never a silent zero-order park). `ReanchorCommand`
   rides the existing firewall with zero new machinery (union + TypeAdapter); all
   kind-sensitive surfaces updated in one commit (confirm-embed symbol, 3 Jinja
   guards, help 16→17, operator.md vocabulary + the reanchor-vs-cancel
   disambiguation for the 1.5B parser). **Bundled: the command-catalog SSOT drift
   test** (union ↔ _HELP_ENTRIES ↔ operator.md, three-way; the P3 table scheduled it
   for exactly this moment) — passed first try. Real-money cost $0.00.
   Test count 2804 → 2820.

   **Live e2e verification** ✅ **2026-08-09** (operator-approved, real money, NAS
   deployment): full chain exercised — Discord `re-anchor BTC` → 1.5B parse → confirm
   embed → operator ✅ → firewall dispatch (2s) → engine. Audit trail: `re-anchored
   BTC/USD: 74769.80000 -> 65193.50000; cancelled 0, placed 0/6 (3 refused) (3 sells
   deferred)` — BTC had been parked offside since the 16:42 UTC restart (hence
   cancelled 0); all 3 BUYs refused (free USD ≈ $2.81 after ETH's $15 of open-BUY
   reservations), all 3 SELLs cost-basis-deferred. `engine_state` confirmed the new
   anchor + badges. **Three findings queued from the test** (operator paused BTC via
   the command path to stop the loop, itself a second successful e2e): (1) zero-order
   layout starvation → per-tick silent retry loop (`engine.md` new entry); (2) command
   results never echo to Discord — the ✅ gets silence, which hid "placed 0/6"
   (`operator-ux.md` new entry); (3) first parse after a daemon restart blows the 60s
   Ollama client timeout on cold-cache full-prompt eval — retry parsed in ~26s warm
   (`observability.md`, folded into the Ollama hang audit). Also reconfirmed the
   logging-audit case: plain format hid which symbol was looping and that placed=0.
   Real-money cost $0.00 (re-anchor placed nothing; pause has no order side effects).

   **Slice 5 — re-anchor banner action button + snooze** ✅ **2026-08-09** (built to
   the 2026-06-03 blueprint): the v1.0 info-only banners grow their two buttons.
   **Re-anchor** posts `ReanchorCommand` through the existing web firewall flow
   (`POST /commands/reanchor` → shared confirm page → `status='approved'` →
   cli/live's poll — zero new confirm machinery); **Snooze 24h** upserts the new
   `reanchor_snoozes` table (operator.db, PK (base,quote)) and is deliberately
   **UI-local per the blueprint** — no `pending_commands` row (pinned by test: a
   snooze leaves the firewall table empty). `_load_reanchor_snoozes` degrades
   fail-open (a lookup failure SHOWS all banners — the bad outcome is a reappearing
   banner, never a hidden recommendation); expired rows are ignored on read.
   Banner adds the blueprint's fee-only economics line: `projected_fee_usd` =
   `KRAKEN_TAKER_FEE_RATE × open-notional × 2` (cancelled + re-laid ladder,
   approximated equal; paper-loss-on-stranded rejected in the blueprint as
   misleading), with the honest tooltip on the estimate. New StoragePort pair
   `save_reanchor_snooze`/`get_reanchor_snoozes` mirrors the engine_state
   contract (storage reports what was written; consumers own the now-comparison).
   Real-money cost $0.00. Test count 2820 → 2833.

   **Slice 6 — state-aware per-symbol pause/resume buttons** ✅ **2026-08-09** (the
   re-anchor chain's last link, built to the 2026-06-03 blueprint): the status
   card's per-symbol actions collapse from both-buttons-always to exactly ONE,
   branched on the FRESH `engine_state` row the keystone already delivers to the
   template — paused → resume; active, **absent, or stale → pause (the ratified
   safe default: pausing an already-paused symbol is an idempotent no-op, while a
   blind resume could unknowingly restart trading)**. The ADR-030 freshness
   invariant extends to actions (pinned by test: a 300s-old paused row offers
   pause, not resume). Offside stays a badge, never a button. Paused sections get
   the blueprint's dimmed-row visual (body at 0.55 opacity + dashed border; the
   header with the badge and the resume control stays full-strength). Pure
   template + CSS — zero Python changes; both POSTs still cross
   `pending_commands`. Real-money cost $0.00. Test count 2833 → 2837.

   **Slice 7 — bespoke notification-card renderers + command-result echo** ✅
   **2026-08-09** (built to the 2026-06-03 Approach-B blueprint): new
   `ports/notification_events.py` — 8 frozen models on a `kind`-discriminated
   union (the blueprint's 7 proactive events + `command_result`, the 2026-08-09
   e2e finding's echo) — and `services/notification_embed_render.py`, the
   push-side twin of the v1.0 query renderer (`match` over the union, no
   fallthrough; green=wanted activity, red=stop-the-presses, amber=money-moved).
   **Zero schema migration as ratified:** the event serializes into the existing
   `context_json` column; `row_to_notification` reconstructs via a module
   `TypeAdapter` keyed on `kind` (the `_COMMAND_ADAPTER` pattern), old rows and
   unknown kinds degrade to the legacy title/message/context-fields path (which
   moved from `cli/operator` into the service), and typed rows still expose the
   raw dict as `context` so the web /notifications + /history pages render
   unchanged. All 7 raise sites migrated to `event=` (fixing the blueprint's two
   latent warts: `symbols` as a real sequence; `session_end`'s "unknown"
   sentinels → `None`). **The echo (finding 2) ships typed from day one:**
   `_process_pending_commands` gains a notifier and emits `CommandResultEvent`
   after every dispatch — the operator's ✅ now gets "re-anchored BTC/USD: … ;
   cancelled N, placed M/L" back in Discord instead of silence (pinned by an
   e2e test on a real reanchor dispatch). Heartbeat-alert + maintenance
   notifications stay on the legacy path deliberately (purpose-written
   titles; typing them is renderer work without a payoff today). Real-money
   cost $0.00. Test count 2837 → 2854.

   **Slice 5 follow-up — banner redesign** ✅ **2026-08-09** (operator design
   review over the live dashboard, driven via Claude-in-Chrome against a real
   ADA/USD moderate banner): the prose-paragraph banner — the only prose
   component on the page — became a structured card matching the dashboard's
   own scoreboard idiom: header row (title + severity chip), a
   big-number/small-label **stat row** (current price / spacings off grid /
   grid anchor / oldest order / projected fee — the fee promoted from an
   italic afterthought to a first-class stat with the honest tooltip), and an
   action row with real hierarchy (Re-anchor = filled severity-tinted
   primary; Snooze = quiet ghost; right-aligned; ellipsis dropped). Template
   + CSS only; all slice-5 test assertions unchanged. Side observations
   queued from the same review: BABY/USD dust balance renders a full empty
   card; Recent Fills paints BUY rows with a loss-red ▼ (money-out ≠ loss).

   **Slice 8 — Docker HEALTHCHECKs on all 8 services** ✅ **2026-08-09**: new
   `tools/healthcheck.py` (exit strictly 0/1 — Docker reserves 2, so even
   config failures map to 1) with two modes: `--daemon cli/X` classifies
   freshness through the SAME machinery /health uses
   (`fetch_daemon_freshness` + `derive_thresholds_from_config` — one
   staleness definition, thresholds track operator-tuned cadences), and
   `--http URL` does a liveness GET for the web container against the new
   **unauthenticated `/healthz`** (content-free `{"status":"ok"}` — the real
   /health stays behind auth). Compose gains per-service `healthcheck:`
   blocks (interval `${HEALTHCHECK_INTERVAL:-60s}` per the compose-var rule;
   120s start_period covers boot; UNKNOWN counts unhealthy past it). Closes
   the wedged-but-alive gap: a daemon whose loop stopped looping now goes
   red in Portainer instead of green forever. Manually verified per the
   scripts rule: the local repo's 74-day-old cli/live heartbeat correctly
   reads `unhealthy: stale`, exit 1. Test count 2855 → 2864.

   **Slice 9 — logging-quality audit, installment 1 (engine path)** ✅
   **2026-08-09**: audit-and-enrich per the ratified observability.md plan —
   `grid_engine` + `reconciler` + `cost_basis`, the tail the operator
   watches during live trading and the source of every recent
   symbol-less-log incident. Every state-change line now answers
   what/which/how-much in the MESSAGE string (plain format is
   message-only; `extra=` stays for JSON consumers): fills carry
   symbol/side/amount/price ("grid fill: BTC/USD BUY 0.001 @ 65100"),
   re-layout completions carry placed/target + refusal/deferral counts (the
   line that would have made the 2026-08-09 starvation loop diagnosable
   from the tail), offside/spread/sell-guard transitions name the symbol
   and the numbers, reconciler lines carry symbol/side/price/exchange_id
   (previously 11 identical anonymous lines per restart), refusals name
   side/price/reason. New `docs/implementation/logging-conventions.md`
   ratifies the six rules + the incident receipts; message-prefix pin
   tests updated. Remaining modules (adapters/services/cli/web) follow in
   later installments — per-module commits as planned. Zero behavior
   change. Test count 2864 (log-text only).

   **Slice 10 — LLM health on /health + cold-start parse-timeout fix** ✅
   **2026-08-09** (the LLM subsystem pair): (a) new `services/llm_health.py`
   — `LLMHealthChecker` mirroring the Kraken probe (TTL cache on app.state,
   lock-serialized) probing only what's configured via each provider's
   cheapest NON-BILLABLE endpoint (Ollama `/api/tags`; Anthropic/OpenAI
   models-list; Google via the `x-goog-api-key` HEADER — keys never ride
   URLs); empty-string env keys count as unset (the MCP-host lesson);
   /health gains an "LLM Endpoints" card and an unhealthy endpoint pulls
   the overall dot to YELLOW, never RED (LLMs are advisory infrastructure
   per ADR-002); 401/403 says "key rejected (rotated?)". (b) Finding 3
   CLOSED: `_post_and_extract` raises a `_OllamaReadTimeoutRetry` marker on
   `httpx.ReadTimeout` and `_request_with_retry` grants it the same
   one-retry treatment as empty-content — the live-verified cold-start case
   (server finishes full prompt eval just after the client gives up; the
   retry rides the warm KV cache, ~26s) becomes a slow success instead of
   "Sorry, I couldn't process that"; a second timeout surfaces as a real
   outage. Test count 2864 → 2874.

   **Slice 11 — zero-order layout starvation back-off** ✅ **2026-08-09**
   (closes the LAST re-anchor e2e finding; built to the engine.md design):
   a layout that places 0/N enters a **starved** state — ONE WARNING with
   the refusal/deferral breakdown, then the no-orders self-heal retries
   only every `_STARVED_RETRY_EVERY_TICKS` (60, about 5 min at the 5s
   cadence, measured in the writer's cadence) with the standard
   transition + heartbeat logging, instead of the old silent every-tick
   busy loop. Any placement (including a partial) clears the state with
   an INFO; orders appearing by any path quietly clear it. All three
   layout sites participate — initialize, the auto-re-layout branch, and
   `request_reanchor` (the original incident: a 0/6 re-anchor now backs
   off immediately). Pinned by 5 tests incl. warn-once, retry-tick
   fires, funded-retry recovers, partial-never-starves, and the
   reanchor path. Test count 2874 → 2879.

   **Slice 12 — web wait-for-completion (row-watch core)** ✅ **2026-08-09**
   (the operator-filed item's first layer; the modal-card presentation is
   the follow-up layer): the post-approve result page no longer dead-ends
   at "approved" — a new `_command_watch.html` partial polls
   `GET /commands/{id}/watch` (htmx, 2s) until the row reaches a TERMINAL
   state, then shows the actual `CommandResult` ("executed — paused
   BTC/USD" / FAILED / expired). **Watching only, never executing** —
   cli/live's approved-poll remains the sole row→engine path
   (ADR-002/ADR-013). Honest slow-pickup warning past 30s ("is the live
   daemon running? Check /health") instead of an infinite spinner; the
   no-JS fallback simply stays on the waiting block until a manual
   refresh. Pinned by 3 tests (self-polling markup, terminal result stops
   polling, unknown-id partial). Test count 2879 → 2882.

   **Slice 13 — modal-card action flow** ✅ **2026-08-09** (the operator's
   amendment, completing the wait-for-completion experience): dashboard
   actions (pause/resume icons, banner Re-anchor, Emergency Stop) now open
   a **modal card over the dashboard** — confirm details in place,
   Approve/Reject in the card, then the slice-12 row-watch runs inside the
   same card to the real outcome, and on completion the status card
   refreshes immediately underneath (`?ctx=modal` threads the context so
   the standalone result page stays console-clean). Pure progressive
   enhancement: htmx `hx-post` branches on the `HX-Request` header; the
   no-JS path keeps every full-page redirect flow unchanged (pinned by the
   pre-existing redirect tests). CSP-clean — close affordances live in
   `static/modal.js` (script-src 'self', no inline). Includes a
   pylint-caught lesson: an untested per-verb htmx branch shipped an
   undefined variable — now every verb's modal path is exercised by an
   all-verbs test. Test count 2882 → 2886. *Follow-up (operator-caught,
   same day):* closing the modal mid-watch (Close/Cancel/Escape before the
   terminal state) killed the poll with the modal and left the dashboard
   stale until its next natural refresh — `closeModal()` now fires a
   status-card refresh on every close (guarded on `#status-wrap` + htmx
   presence; a duplicate refresh after an in-card terminal update is
   harmless).

   **Slice 14 — Execute a proposal from the web (ADR-034)** ✅ **2026-08-09**: the
   Harvester page grows an **Execute** button on actionable proposals, running the
   slice-13 modal flow over a new `ExecuteProposalCommand` — confirm the **amount and
   destination** (not an opaque id), approve, then watch `cli/harvest` execute it.
   Deliberately NOT in the LLM-emittable `OperatorCommand` union: it joins a separate
   `QueueableCommand`, so ADR-002 holds structurally — a crafted assistant payload naming
   `execute_proposal` fails validation. `get_pending_commands` gains a `kinds` allowlist and
   `cli/live`'s poll is scoped to the engine kinds (closing the latent bug where it would
   have claimed a withdrawal row and marked the operator's transfer `failed`);
   `cli/harvest` gains a 15s command poll beside its hours-long proposal cycle. The seven
   defense layers moved to `cli/harvest_execute.py` and return a structured `ExecuteOutcome`
   so `--execute` and the queued path share ONE implementation — plus a new echo-validation
   gate that refuses when the approved amount/destination no longer matches the stored
   proposal. The page warns honestly when the harvest daemon's heartbeat is stale. Dormant
   in production by design (harvest container stopped). Apply + Acknowledge deferred with
   reasons recorded in the ADR. Test count 2886 → 2922.

   **Slice 15 — Discord confirmations become buttons** ✅ **2026-08-10**: the
   ✅/❌ reaction flow is replaced by Approve / Reject **buttons**, built to the
   2026-06-03 blueprint's load-bearing part (`interaction_check` → the
   allowlist) with one deliberate deviation: they are `discord.ui.DynamicItem`
   buttons rather than a per-message `_ConfirmView`, so the pending-command id
   rides in the component's `custom_id` and **a click still works after a daemon
   restart**. That matters here specifically — the container restarts on every
   deploy, and the old flow resolved a reaction through an in-memory
   `pending_message_map` that each restart emptied (silently orphaning every
   outstanding confirmation, and leaking — deep-scan F7c disappears with the
   map). `interaction_check` defers to the transport's own `is_allowed` rather
   than re-implementing the allowlist, and fails CLOSED when the daemon never
   wired a handler. Transition rules moved to `services/confirm_decision.py`
   (TTL-beats-click, idempotency, never-dispatches) so the button path and any
   future confirming surface share one implementation; its tests migrated with
   it. Requires `discord.py>=2.4` (floor bumped). **Live Discord verification is
   the operator's step — this ships unit-tested only.** Also repaired
   `tests/integration/test_phase5_operator_e2e.py`, which had rotted into a
   collection error (a port method added after the stub was written, plus three
   signature drifts) and — being deselected from the default run — went
   unnoticed by CI; it now runs green and covers the button path end to end.
   Test count 2922 → 2940 unit, plus 5 integration tests back from the dead.

   **Slice 16 — logging-quality audit, installment 2 (daemons + money path)**
   ✅ **2026-08-10**: the second module pass, scoped by "if this fires at 3am,
   can the operator act on the line alone?" — so it covers the ALWAYS-ON
   daemons and the money path at WARNING/ERROR/EXCEPTION:
   `cli/harvest_execute` (every withdrawal refusal now names the proposal id
   and both numbers — "refusing p-x: $342.18 would push today's withdrawals
   ($700) past the $1000 daily cap"), `cli/harvest`, `cli/live`,
   `cli/operator`, `cli/_common`. A rule-1 violation turns out to have a
   MECHANICAL signature — a static message paired with a non-empty `extra=` —
   so the audit is a greppable scan, not a taste judgment: **239 violations
   before, 165 after**, the remainder being the deliberate scope boundary
   (one-shot CLIs + web routes + the INFO/DEBUG tier = installment 3). The
   50 daemon call-sites were rewritten by an AST transformer that reuses the
   EXACT value expressions already in `extra=`, so the message and the
   structured field cannot drift. Also lands **`cli/_common.fmt_decimal`**
   (the queued Decimal-display item): `%s` on a stored Decimal printed
   `342.18000000` for $342.18 and `1E+2` for a round $100 — E-notation in a
   withdrawal line is a genuine misread risk. Strips trailing zeros without
   forcing a scale, so a live BTC quantity doesn't render as `0.00` (13 tests,
   incl. a round-trip property). Zero behavior change. Test count 2948 → 2961.
   **Deferred to installment 3:** the Ollama hang-audit's loop-blocking half.

   **Slice 17 — logging installment 3 (sweep complete) + Ollama loop audit**
   ✅ **2026-08-10**: finished the module sweep — every remaining file, every
   severity, including the one-shot CLIs and web routes. **The rule-1 scan now
   reports ZERO** (239 → 165 → 0 across the three installments). Two findings
   worth more than the line count. First, the mechanical transformer produced
   8–11 `key=%s` dumps on summary lines, which is a different way of being
   unreadable — in-message context is now capped at 4 fields, the rest staying
   in `extra=`. Second, and the reason installment 3 exists at all: reading the
   LIVE log after installment 2 shipped showed
   `below avg cost 73390.78543435964243143764881` — a line that PASSES the
   rule-1 scan (it does interpolate its data) while being unreadable, because a
   `Decimal` division keeps 28 significant digits. **The rule-1 scan is blind to
   that class by construction**, so a second Decimal-readability scan now
   complements it, and `fmt_decimal` gained `max_significant` (capping
   significant digits, not decimal places, so one setting works for a $73k BTC
   price and a $0.069 DOGE one). `fmt_decimal` also moved `cli/_common` →
   `domain` — services cannot import cli, and cost_basis needed it. Finally the
   **Ollama hang-audit's loop-blocking half closed CLEAN**: no `time.sleep`
   anywhere in `src/`, async clients with bounded timeouts, MoE experts under
   `gather`, and — the reassuring one — cli/operator's heartbeat rides the
   FORWARDER task, not the message handler, so a stalled LLM parse cannot make
   the daemon look dead on /health. Zero behavior change. Test count 2961.

   **Slice 18 — status_report tally compactness** ✅ **2026-08-10**: the
   status_report embed stacked its eight tallies at full width — ~16 vertical
   lines that buried the narrative the operator actually asked for (flagged
   2026-05-24 after a probe battery). Built to the blueprint's option 1:
   `DiscordTransport.send_embed` fields now accept an optional third element
   (`(name, value, inline)`), and the status_report renderer emits
   `inline=True`, so Discord packs the tallies three-per-row — three rows
   instead of sixteen lines. Every tally is a short label + short value (a
   count, a dollar figure, a band name), which is what a third-width column
   holds comfortably. Kept as PLAIN TUPLES rather than a shared field type:
   the renderer services import only from `ports`, so a type defined in the
   adapter would have forced a services→adapters import for a cosmetic change
   — the third such layering call this session, and the first one gotten right
   without a later move. 2-tuples still mean `inline=False`, so all 25 existing
   field-builders are untouched (pinned by a back-compat test, plus a mixed-shape
   test). **Follow-up the same day, operator-requested:** `session_start`'s four
   counters got the identical treatment — the observation was surfaced rather
   than acted on unasked, and the operator called it in. `harvest_proposal` stays
   stacked deliberately (a proposal id and a `$X → $Y` transition are too long for
   a narrow column), which is the rule that fell out: inline suits a ROW OF
   COUNTERS, not a value that is a sentence, an id, or a transition.
   Test count 2961 → 2973.

   **Slice 19 — notifications server-side read-state + Acknowledge + deep
   links** ✅ **2026-08-10**: the bell badge lived in browser localStorage, so
   clearing the dot on the desktop left the phone dotted, and merely *opening*
   `/notifications` counted as reading everything. Now a `read_at` column
   (additive migration; pre-slice rows land on NULL = unread, the honest
   reading since nobody could acknowledge them), `count_unread_notifications` /
   `mark_notifications_read` / `mark_all_notifications_read` on `StoragePort`,
   a per-row **Acknowledge** button and a **Mark all read** action, and a badge
   driven by a real server-side count. Acknowledgement is deliberately
   **explicit** rather than implicit-on-visit — the whole value of a badge is
   that you have to dismiss it. `notifications-seen.js` is deleted.
   **Firewall call:** these writes are **UI-local**, straight to operator.db,
   following the `reanchor_snoozes` precedent from slice 5 — reading a
   notification moves no money and touches no engine state, so a
   `pending_commands` round-trip would put a row in the ADR-002 queue that no
   daemon should ever act on. Pinned by a zero-firewall-rows test; auth + CSRF
   still gate both endpoints. **Deep links** come free off slice 7's typed
   event union: trading events → `/`, treasury events → `/harvester`, untyped
   and legacy rows → no link at all (a link that lands somewhere unhelpful is
   worse than none). Two traps recorded in the migration's own docstring: the
   unread **partial index must live in the migration, not SCHEMA** — SCHEMA is
   `executescript`'d BEFORE any migration, so an index over `read_at` aborts
   `connect()` on every pre-slice operator DB (pinned against a genuinely
   pre-slice table, not a fresh one) — and `mark_notifications_read([])` is a
   no-op that must never be read as "all", so an empty computed selection can't
   silently clear the table. One column, not one per user: ADR-017 ships a
   single operator account. Verified live in a browser against a seeded
   instance — unread accent bar, per-row acknowledge, mark-all, and the bell
   clearing from server state (DOM + computed styles; the pane wouldn't
   composite for a raster capture). Real-money cost $0.00.
   Test count 2973 → 2997.

   **Slice 20 — Today's PnL: realization-day vs earning-day** ✅ **2026-08-10**:
   built to the design note's recommended **option 2** (annotate per cycle), NOT
   option 1 (re-bucket the headline). `RecentCycle` gains `pairing_method`
   (`engine_counter` | `fallback`, set by whichever heuristic actually fired in
   the matcher loop), a `hold_duration` property, and `is_long_hold` against a
   24h `LONG_HOLD_THRESHOLD`. Recent Cycles renders two **independent** tags by
   the timestamp: **"held 3d 0h"** (this row's PnL is mostly multi-day drift,
   not grid spread) and **"inferred"** (no same-size counter existed, so which
   BUY it closed is an inference — pre-engine inventory, manual fills, or a
   counter canceled by a cap trip / re-anchor). Independence is pinned: a real
   counter pair that merely took days to fill is long-hold but NOT inferred.
   **Zero change to the money math** — a test asserts `net_pnl` and
   `today_realized_pnl` are identical with the annotation in place; this slice
   is presentation only. Reused the existing `humanize_duration` Jinja filter
   instead of adding a near-twin. Threshold is a display heuristic, deliberately
   not a correctness boundary — nothing branches on it but the tag.
   **Option 1 deliberately NOT promoted.** The design note gates it on "if the
   headline keeps producing confusion," which is an operator preference call,
   and the discriminator this slice adds is exactly what it would need
   (`today_realized_pnl` takes a `pairing_filter`). Evidence captured for
   whoever makes that call: in a seeded preview reproducing the 2026-05-26 shape
   next to three normal cycles, the headline reads **+$0.5691 "today's PnL"**
   when the grid earned ~$0.22 today and $0.3460 was three days of drift. The
   annotation now says so on the row; the headline still doesn't.
   Verified in a browser — the outlier carries both tags, the slow-but-real
   pair carries only "held 2d 0h", and the three normal cycles stay unadorned.
   Real-money cost $0.00. Test count 2997 → 3006.

   **Slice 21 — per-symbol held inventory + Recent Fills rework** ✅
   **2026-08-10**: two dashboard leaves, batched because both are the same
   "the card doesn't tell you enough to act" complaint.
   **(a) Held inventory.** Each symbol header now carries
   `holding 0.00131400 BTC ≈ $101.83` — the per-coin half of the two-sided
   framing whose aggregate strip shipped 2026-06-03. Without it a flat-start
   `insufficient balance` refusal is unexplainable from the card: you can see
   the orders and the price but not what you already hold. Extracted
   `held_by_symbol` as the ONE rule for "what counts as held and what it's
   worth," and re-derived the scoreboard's `in positions` total from it — so a
   card row can never disagree with the total above it (pinned by a
   sums-to-the-total test). An unpriced holding is listed with its amount and
   no valuation rather than hidden: the position is real either way.
   **(b) Recent Fills.** `last fill X ago` moved out of card-meta (which was
   collecting every freshness signal at once, and with six symbols can't say
   WHICH one filled) into a subhead above the table, joined by a buy/sell
   split, signed net USD flow, and total fees for exactly the rows below. Added
   a per-row **age** column reusing the existing `humanize_duration`;
   `trade_ages` is precomputed in the loader so Jinja only formats, never does
   datetime arithmetic (the `order_ages` rule).
   **Bug caught by rendering it, not by the tests:** the summary first summed
   the per-row "net USD" column verbatim and called the result *net*. That
   column carries direction in an arrow, not a sign — so a buy-heavy window
   reported a large POSITIVE "net" that reads exactly like profit. Now signed
   (SELL adds `cost − fee`, BUY subtracts `cost + fee`), tooltip says it's cash
   flow and not profit, and a regression test asserts a buy-only window nets
   negative. The first version's own test was wrong in the same direction,
   which is why looking at the page is a gate and not a courtesy.
   Real-money cost $0.00. Test count 3006 → 3015.

   **Slice 22 — re-anchor viability weighting (the full item)** ✅
   **2026-08-10**: drift + age answer "is this grid misplaced?"; they cannot
   answer "is re-anchoring worth it here, now?" — the 2026-08-09 case where an
   operator-executed BTC re-anchor produced correctly-positioned orders that
   nothing touched. The v0 stat (2h range vs spacing) shipped same-day; this is
   the deferred full item. Wilder **ATR(14) over 14 days of stored hourly bars,
   divided by grid spacing**: "0.15×" = a typical hour moves price a seventh of
   a spacing, so even a perfect ladder waits ~7 hours per fill. Reuses
   `services/ta_metrics.compute_atr` — the screener's own primitive — rather
   than a second implementation. **Hourly, not daily:** hourly is the interval
   `cli/observe`'s top-up maintains, and ATR does not rescale across intervals
   by any honest constant, so a per-hour figure stays literal.
   **Merged into ONE stat cell with the v0 number** (`activity: 2h · ATR/hr`)
   instead of becoming a seventh banner stat. They answer the same question at
   two horizons and only mean something beside each other: a quiet 2h inside a
   lively fortnight is noise; a quiet 2h inside a dead fortnight is the idle
   ladder. (Prompted by not being able to obtain a rendered layout measurement
   for a 7-stat row — the right answer turned out to be fewer cells, not a
   width check.)
   **The design note's three open questions, answered:** window = 14d hourly;
   a poor-viability STRONG-drift banner still renders loud — a drifted ladder
   in a dead market is idle capital and the right response may be *pause*, not
   silence (pinned by `test_poor_viability_never_suppresses_the_banner`); and
   annotation-only-never-suppression is now a stated rule in the `status.py`
   module docstring with its ADR-002 rationale, not just a slice intention.
   Cost discipline: fetched only AFTER the severity gate, so the 15s poll pays
   one bounded read for the 0–2 banner symbols rather than six reads a poll
   forever. Degrades to "—" on unwired observe.db / thin bars / storage failure
   — an annotation must never break what it annotates.
   Verified against a seeded strong-drift + dead-market instance: the banner
   renders `strong` with `0.0× · 0.15×`, i.e. "badly misplaced AND not worth
   re-anchoring" — exactly the state the operator needs to see to choose pause.
   Real-money cost $0.00. Test count 3015 → 3021.

   **Same-slice fix — `status.py` split at the 1000-line gate.** The
   viability code pushed `web/routes/status.py` to 1085 lines; pylint kept
   scoring 10.00 and exited **16**. My local check missed it because I piped
   pylint through `tail` and read `tail`'s exit code — the exact mistake the
   standing note warns about, repeated. **CI caught it and the merge chain
   correctly refused to merge.** Extracted `web/routes/status_reanchor.py`
   (298 lines): the banner DTO, severity tiering, snooze + recommendation
   loaders, and the viability reader — one cohesive question (*should the
   operator re-anchor, and is it worth it?*) with its own thresholds and
   storage reads, and nothing else on the status page depends on its
   internals. `status.py` drops to 838. The annotate-never-suppress rule
   moved with it, so it lives next to the code it constrains. Loaders became
   public (`load_reanchor_*`) at the module boundary. From here every gate is
   run unpiped with its own exit code echoed.

   **Slice 23 — design-review leaves** ✅ **2026-08-10**: the five remaining
   items from the 2026-06-03 whole-UI review. (1) **Fill flash** on the 15s
   swap, gated on the fill's real AGE via slice 21's `trade_ages` rather than
   "is it the top row" — the naive version re-flashes the newest fill every
   poll forever, at which point the highlight stops meaning *this just
   happened*; `prefers-reduced-motion` degrades to a static tint so the
   information survives. (2) **`/cost` gains `transition:true`** to match the
   dashboard; its swap was a hard cut that reads as a flicker. (3) **Advisor
   collapse** — cards become native `<details>` (no JS; the CSP is
   `script-src 'self'`) with the newest three open, so the page still ANSWERS
   on arrival while a busy `cli/advise` stops producing a wall. The collapsed
   `<summary>` keeps symbol / time / model / confidence / below-floor: exactly
   the fields you triage on. (4) **High-consequence confirm weight** —
   `stop` / `pause_all` / `cancel_open_orders` were being confirmed with a
   routine single-symbol pause's chrome on BOTH the modal and the no-JS full
   page; they now carry an amber-ruled consequence line, deliberately quieter
   than the money-out warning because nothing here is irreversible.
   (5) **Shared command vocabulary** — a `command_label` Jinja global replaces
   the modal's local dict, because the full-page confirm rendered the same
   decision and printed the raw `stop` discriminator where the modal said
   "Stop the engine". One vocabulary, both surfaces, pinned by a test.
   **Deliberately NOT done — typography/brand elevation (Tier 3).** Numeric
   columns already carry `tabular-nums`; the remaining half was re-tinting
   `--color-link` to the login teal `#4dd0e1`, which fails contrast against
   the light-mode white surface. Shipping a contrast regression for aesthetics
   is the wrong trade — the brand teal stays on the dark chrome where it
   reads, and that's recorded rather than silently skipped.
   Verified in a browser: 5 advisor cards, newest 3 open, collapsed summaries
   still carrying their triage fields; the `stop` confirm leading with the
   amber consequence line and naming the command "Stop the engine (stop)".
   Real-money cost $0.00. Test count 3021 → 3026.

   **P3 COMPLETE (buildable scope) — slices 1–23 shipped 2026-08-08 →
   2026-08-10.** Three items remain and are **gated, not skipped**:
   the **anomaly detector** needs ~30 days of baseline (the heartbeat /
   `engine_state` clock starts 2026-08-08, so it matures ~2026-09-07);
   **disk-space awareness** bundles onto that daemon AND is gated behind the
   data-retention policy; and the advisor's **Apply / Approve-Reject** is
   blocked by ADR-034's scope note — no daemon can own a `settings.yml`
   rewrite and `cli/apply --daemon` was explicitly dropped, so unblocking it
   needs an ADR, not code.

   **Post-P3 — Ollama schema-constrained generation** ✅ **2026-08-10** (not a
   P3 slice; fell out of the ADR-035 groundwork). `OllamaAdapter` sent the bare
   string `format: "json"`, which guarantees the body *parses* and says nothing
   about its *shape*. That gap was live, not theoretical: a MoE panel run had
   two of four models return valid JSON with invented keys —
   `{bollinger_middle, recommend}` from the quant expert,
   `{"**Recommendation", "Rationale"}` (markdown headings as keys) from the
   arbitrator — each surfacing only as a post-hoc
   `missing required field 'confidence'`, and the second killing the dispatch.
   Ollama (0.32.6 locally) has supported schema-constrained generation since
   0.5, so `format` now carries the real JSON Schema and the server cannot emit
   a violating body. Per the standing rule: prefer the upstream's own
   validation over a local copy of it. The confidence enum is **derived from
   `ConfidenceLevel`**, never retyped, so grammar and Pydantic literal cannot
   drift; the schema sticks to the `type`/`properties`/`required`/`enum` subset
   llama.cpp's GBNF converter handles, leaving `minLength` to Pydantic so a
   conversion failure can't take out the call path. The thinking-model gate is
   unchanged — enforcement applies exactly where the weaker constraint already
   did. Test count 3026 → 3032.

   **⚠️ Recorded honestly: the fix trades a LOUD failure for a QUIET one.** The
   re-run completed instead of erroring, but `phi4:14b-q8_0` (quant *and*
   arbitrator) then produced schema-*valid* token salad —
   `"ration}d_ema_data"`, `"ema_stochart_ata"` — with `confidence: "high"`,
   which now flows into the aggregate as though it were a real opinion. A
   grammar can force shape; it cannot force meaning. `granite4.1:30b-q5_K_M`
   (risk) in the same run returned a genuinely in-role answer citing exposure,
   drawdown and cap headroom — so the panel *can* do this. Two caveats before
   anyone judges a model on this: the input had **null TA fields** (local
   observe.db 51.5h stale) while the prompt carries TA vocabulary, which is the
   likely cause of the TA-flavoured garbage; and `qwen3.6:35b-a3b-q8_0` (news)
   now **500s** under schema constraint where it succeeded under
   `format: "json"` — a per-model regression, probably grammar conversion.
   Follow-ups NOT taken here: a plausibility gate on the parse side (the real
   fix for well-formed nonsense), and re-running against fresh NAS data before
   drawing any model conclusion. Constraining `recommendations` to the four
   auto-apply keys was **considered and rejected** — it needs a forbidden
   adapter→`services/auto_apply` import, and it would muzzle the advisor from
   ever proposing anything outside what auto-apply consumes (e.g. ADR-029's
   `counter_target_mode`).

   **Post-P3 — MoE seat measurement session** ✅ **2026-08-10** (operator-driven;
   not a P3 slice). Ran the existing quant battery across six candidates, added a
   new arbitrator battery, and settled two seats on evidence. Cloud spend for the
   whole session: **$0.0628** (28 calls, isolated `probe_llm_cost.db`).

   **Quant seat — DECIDED: cloud `gpt-5-mini`.** Core battery (12 fixtures,
   chance = 12/36): gpt-5-mini **30/36**; llama3.1:8b-q8 15; qwen3.6:35b-a3b 13;
   qwen2.5:7b-q8 12; phi4:14b-q8 9; granite4.1:30b-q5 **6**. **No local model is
   meaningfully better than chance.** gpt-5-mini took 8/8 action fixtures with
   zero MISS/WRONG/ERROR. Held-out discriminator set: **14/24** — a real drop, and
   `heldout_drawdown_overrides_calm` (expected widen, said tighten) shows it is
   *partly rule-following*: when a secondary signal should override volatility, it
   follows volatility. Suspected PROMPT gap (does `quant.md` state the override?),
   not a model gap — filed, not chased. Production already escalates to this
   model, so this validates existing config rather than changing it.

   **Arbitrator seat — DECIDED: an LLM arbitrator DOES earn its cost.** New
   `tools/probe_arbitrator.py`: 8 fixtures, one per arbitration rule in
   `arbitrator.md` plus both hard constraints, scored for a candidate model AND
   for the two free aggregators on identical inputs. Result: **gpt-5-mini 8/8,
   `voting` 5/8, `weighted_confidence` 1/8.** This REVERSES the morning's read
   ("voting beat the broken arbitrator, maybe skip the seat"): three of voting's
   five passes are ACCIDENTAL — it omits the key whenever experts differ, so it
   can HOLD safely but can never take the conservative ACTION rules 1 and 4
   require. Both free aggregators are structurally incapable of rules 1 and 2
   (no concept of expert role). **Safety finding pinned by test:**
   `aggregate_weighted_confidence` emits spacing **below the fee floor** (0.525
   vs the 0.66 break-even) and lets **news alone drive a number** (5.0),
   contra ADR-007 — the auto-apply floor is what actually stops these reaching
   the engine, so it is defence-in-depth, but the aggregator does not honour the
   documented arbitration rules. Baselines are pinned in
   `tests/tools/test_probe_arbitrator_rubric.py` so a future aggregator change
   forces a deliberate re-baseline.

   **⚠️ BUG FOUND — `risk.md` promises inputs that do not exist.** The prompt tells
   the model it receives "current open exposure vs the configured caps,
   time-to-recovery from the last loss, and daily spend so far vs the daily cap".
   `PerformanceSummary` contains **none of those** (only `max_drawdown`;
   `active_orders` is a count, and no cap value is passed at all). This is why the
   live MoE risk expert wrote "the account has not yet approached its drawdown or
   daily spend caps" — it **confabulated**, fluently, and was initially read as an
   excellent answer. **The risk battery is BLOCKED on this**: fixtures built today
   would grade models on three-quarters-imaginary inputs. Fix is either extending
   the DTO (preferred — the engine and config have the data, it just isn't
   plumbed) or narrowing the prompt to what exists.

   **↳ FIXED 2026-08-10 (same session) — the risk battery is UNBLOCKED.** Both
   halves, because the claim above was only three-quarters right: the DTO was
   extended where real data existed, and the prompt narrowed where it did not.

   - **New `services/exposure.py`** holds the three cap computations, and
     `GridEngine._check_safety` now calls it instead of its own inline copies.
     That sharing is the point, not tidiness: the engine ENFORCES these caps
     and the advisor now REPORTS them, so two implementations would drift and
     the advisor would reason about headroom the engine denies. The
     committed-funds rule (canceled/expired BUYs excluded — the 2026-05-22
     soak Day 5 incident where 11 canceled BUYs ate the day's headroom and
     blocked placements at $110/$100) now lives in exactly one place.
   - **Seven `PerformanceSummary` fields**: `total_exposure_usd`,
     `coin_exposure_usd`, `daily_spend_usd`, their three caps, and
     `max_orders_per_coin` (pairs with the existing `active_orders`).
   - **`risk.md` rewritten** to enumerate exactly the fields it receives,
     require the model to QUOTE figures ("exposure $48 of $100"), report the
     tightest binding constraint rather than averaging, and — the direct
     anti-confabulation clause — "do not cite a limit, headroom figure, or
     recovery time that is not in the fields above." **The
     "time-to-recovery from the last loss" clause is DELETED**: `CycleStats`
     is aggregate-only with no per-cycle timestamps, so there was no source.
     Inventing a metric to justify existing prose is the wrong direction.
   - **⚠️ The trap that nearly shipped, now pinned by a test.** `cli/advise`
     reads prices from the OBSERVE db, which holds **zero orders**. Defaulting
     exposure to that same storage — the obvious wiring — would have reported
     "$0 exposure against a $100 cap" = FULL HEADROOM, which for a risk model
     is a worse failure than the confabulation this slice set out to fix. So
     `exposure_storage` is a separate, deliberately un-defaulted parameter and
     `advise.orders_db` is opt-in; unset, the fields are `null`, which the
     prompt defines as "unknown, never zero."
   - **Live-verified** against the real live DB: coin exposure $40.30/$40.00,
     total $40.30/$150, daily spend $0/$120, and `None` (not `0.0`) on the
     unwired path. Tests 3074 → 3092.

   **↳ Observation from that verification, unrelated to the change:** BTC sits
   at **$40.30 against its $40.00 per-coin cap** (4 open SELLs at ~$10.10, placed
   2026-05-26/27 when `order_size_usd` was $10; it is $5–6 now). Because the gate
   is `existing + proposed > cap`, that symbol can place nothing in this DB. The
   engine's own comment assumes the price×amount rounding artifact sits "far
   above any rounding artifact" relative to cap thresholds — an assumption that
   breaks when the cap is an exact multiple of the order size. Seen in a local
   DB copy (last written 12:24), so production state is unconfirmed. Not chased
   here; flagged for the operator.

   **Method note, twice-learned:** fluency is not correctness. `granite4.1:30b`
   produced the most impressive-sounding risk prose of the session and scored
   **worst of six** on the quant battery; the risk expert's confabulated
   cap-headroom claim read as rigour. Both were caught only by a scored battery,
   which is what these tools are for. Also: all five role prompts exist —
   `quant` / `risk` / `news` / `arbitrator` / **`gremlin`** — and only quant has
   ever run in production.

   **Post-P3 — news battery + Atlas Cloud provider** ✅ **2026-08-10**
   (operator-driven). Third role battery plus a multi-model gateway for
   evaluation.

   **`tools/probe_news.py`** — 12 hand-labelled news windows graded against
   `news.md`'s own rubric (one lever, `spacing_percentage`; WIDEN or HOLD only;
   nothing substantive → low confidence + no recommendations; `order_size_usd`
   and level counts are out-of-lane on EVERY fixture). Deliberately imbalanced
   **7 hold / 5 widen** so constant-WIDEN caps at 5/12 and constant-HOLD at
   7/12 — neither degenerate strategy can look competent. Labelled fixtures
   rather than live headlines because live news has no answer key without
   waiting for the outcome, which is ADR-035's ledger, not a probe.

   **⚠️ RUBRIC BUG, caught before it was reported as a result.** The first
   draft graded any `spacing <= current` as TIGHTEN. But a model that restates
   the CURRENT spacing is expressing a HOLD — and "never tighten" is the single
   most dangerous news-role failure. The bug scored `qwen3.6:35b-a3b` **5/12**
   and made it look like a degenerate constant-widen model; corrected, the same
   run scores **10/12** — a genuinely discriminating reasoner that ignored
   clickbait, trivia volume, an other-chain exploit and a routine non-event,
   and widened on all five material events. Its only two errors (widening on
   bullish news and on a stale resolved incident) are conservative; it never
   narrowed. **A grader that calls the right answer the worst failure is worse
   than no grader.** Fixed, and pinned by `tests/tools/test_probe_news_rubric.py`
   with the bug's origin in the docstring. Fourth wrong-first-read of the
   session and the first where the fault was the instrument, not a model.

   **Atlas Cloud as a first-class probe provider.** `--provider atlas` on all
   three batteries, reusing `OpenAIAdvisorAdapter` with a `base_url` override —
   Atlas is OpenAI-compatible, so no adapter and no dependency were added.
   Modelled as a named provider rather than a bare `--base-url` flag so the
   endpoint and key env var can never be mismatched. Verified end-to-end:
   catalogue 200, authenticated chat 200. **465 models (129 Text)** including
   `anthropic/claude-opus-5`, `google/gemini-3-flash-preview`, `openai/gpt-5.2`
   — which resolves the operator's observation that the recorded sweep in
   `docs/reference/advisor-llm-models.md` tested only superseded Anthropic
   (4-6/4-7/4-8) and May-vintage Gemini.

   **Debug note worth keeping:** the first Atlas calls returned **403 on an
   UNAUTHENTICATED endpoint**, which exonerated the key immediately — a public
   path cannot fail on bad auth. Cause was the WAF rejecting `Python-urllib`;
   `httpx` (what the adapters actually use) passes on its default User-Agent,
   so no header plumbing was needed. Model IDs are namespaced
   (`anthropic/claude-opus-5`), and a wrong id returns `400 "not found"`, not 403.

   **⚠️ Cost-gate caveat, recorded in `.env.example`** — *this paragraph
   originally said the gate "falls back to its heuristic"; that was **wrong**
   and is corrected here (caught 2026-08-10 by the roster session's first live
   call).* `get_price_point()` **raises** `PricingLookupError` on an unmodeled
   `(provider, model)`, so an Atlas model must be priced in
   `services/llm_pricing.py` before it will run at all — there are no
   approximate Atlas spend figures, only priced models and hard failures.
   Probes write to an isolated `data/probe_llm_cost.db`, never the operator
   ledger. Nothing in any daemon reads `ATLASCLOUD_API_KEY`; setting it changes
   no runtime behaviour.

   Test count 3041 → 3047.

   **Post-P3 — Claude-5 roster run + the Anthropic temperature bug** ✅
   **2026-08-10** (operator-driven; operator specified the roster as "Claude 5
   or lower, leave Fable 5 alone"). All three batteries run across four models
   on **native provider paths**, with `gpt-5-mini` re-run in-session as a
   control rather than compared against its recorded score.

   | model | quant-core | quant-heldout | arbitrator | news |
   |---|---|---|---|---|
   | `gpt-5-mini` (control) | **29/36** | 14/24 | **8/8** | 11/12 |
   | `claude-opus-5` | **29/36** ¹ | 14/24 | **8/8** | **12/12** |
   | `claude-sonnet-5` | 26/36 | **17/24** | 7/8 | **12/12** |
   | `claude-haiku-4-5` | 8/36 | 14/24 | **8/8** | **12/12** |

   ¹ one call died retries-exhausted, worth up to 3 points; true range 29–32.
   Control reproduced its record within 1 point of 68 graded points
   (core 29 vs the recorded 30; heldout 14 = 14; arbitrator 8/8), so the
   harness is stable enough to read Claude scores against prior sessions.

   **⚠️ THE HEADLINE IS NOT THE RANKING — five fixtures were failed by EVERY
   model tested** (4 models, 2 vendors, a 20x price range):
   `heldout_drawdown_overrides_calm` (0/4), `heldout_fee_floor` (0/4, all
   OVERTRADE), `heldout_clear_widen` (0/4, all OVERSHOOT),
   `widen_tight_moderate` (0/4, all OVERSHOOT), `hold_quiet_matched` (0/4, all
   OVERTRADE). When `claude-opus-5` and a 3B-class local model fail the same
   fixture the same way, that is not a model gap.

   **↳ ROOT-CAUSED 2026-08-10, same session — and it is NOT a `quant.md` gap.**
   This paragraph originally called it "a rule `quant.md` never states or a
   fixture whose ideal band is wrong" and filed a prompt-gap slice as the top
   advisor item. **Both readings were wrong.** Running every fixture through
   the shipped `HeuristicAdvisorAdapter` settles it deterministically, offline:

   | fixture | reaches the LLM in production? |
   |---|---|
   | `hold_quiet_matched` | **no** — `fee_floor_calm` guard fires |
   | `heldout_fee_floor` | **no** — `fee_floor_calm` guard fires |
   | `heldout_drawdown_overrides_calm` | **no** — `defensive_drawdown` guard fires |
   | `heldout_clear_widen` | yes (OVERSHOOT: magnitude only) |
   | `widen_tight_moderate` | yes (OVERSHOOT: magnitude only) |

   Three of the five are cases the deterministic guards handle *before* the LLM
   is consulted — and `quant.md` explicitly tells the model "the clear cases (a
   directional run-away, a sharp drawdown, a demonstrably working grid, spacing
   already at the fee floor) are already handled before you." Every model read
   that, correctly inferred it was not looking at a fee-floor or drawdown case,
   and was marked wrong for it. **The models were graded on the guards' job
   after being told it was not theirs.** The other two are pure OVERSHOOT —
   right direction, magnitude outside an ideal band derived from the
   **vol→spacing curve ADR-022 RETIRED**, against a prompt that says in as many
   words "There is no formula to apply and no target curve to follow."

   Overall only **3 of 8** heldout and **11 of 12** core fixtures escalate at
   all. **None of this was new information** — ADR-022's own consequences say
   these fixtures were "repurposed as the LLM-grading oracle (not rebuilt)",
   `tools/probe_freejudge.py`'s docstring says the heldout battery has "only 3
   fixtures [that] actually escalate", and the 2026-07-31 bake-off already ran
   heldout "as directional context only … most of it never escalates in
   production". The prompt-gap slice filed above was re-deriving a known
   answer; **it is withdrawn, not scheduled.**

   **Consequence for the roster table above: the two quant columns measure the
   pre-ADR-022 contract and must not be read as quant-seat scores.** In
   particular `claude-sonnet-5`'s 17/24 — reported mid-session as "the best
   held-out score ever measured" — is exactly what
   `docs/reference/advisor-llm-models.md` warns about: "the hold-more bias
   flattering the maintainer curve, not judgment". Corroborating evidence that
   the instrument is noisy off-contract: `gpt-5-mini` scored 8/24 on it in July
   and 14/24 today, unchanged model.

   **↳ MEASURED 2026-08-11, and it is worse than "flattering":
   `quant/heldout` is DEGENERATE.** A constant-HOLD strategy scores
   **18/24 = 75%** on it — beating sonnet-5's 17/24 and every other model
   ever measured on that set. The hold-bias was diagnosed by intuition in
   July and cited again mid-session; the uniform constant-baseline audit
   (advisor-llm-models.md Rev 2026-08-11d) put a number on it. Two
   independent disqualifiers now: off-contract (3 of 8 fixtures escalate)
   AND rock-passable. `quant/core` is clean on that axis (best constant
   33%). Neither set is being fixed — both stay frozen so historical
   scores remain comparable, marked unusable rather than repaired.

   **Seat decisions: NO SWITCH** — deciding evidence is the freejudge run
   recorded below, NOT the two quant columns, which the root-cause above
   disqualifies for seat selection. On the off-contract batteries for the
   record: `gpt-5-mini` ties `claude-opus-5` on quant-core at ~1/10th the cost,
   and Opus 5's 5x premium buys nothing held-out (14/24, identical to the
   incumbent); `claude-sonnet-5`'s 17/24 is one fixture of margin
   (`heldout_directional_downtrend`) while scoring 3 lower on core at 6x the
   price — and is hold-bias, not judgment, per the root-cause note. Scoring
   nuance worth keeping: MISS and WRONG both score 0, so Sonnet 5 and Opus 5
   failing `heldout_drawdown_overrides_calm` *less badly* than gpt-5-mini (held
   rather than tightened) contributes nothing to their score.

   **↳ RE-MEASURED ON THE RIGHT INSTRUMENT** ✅ (same session, operator-approved
   after the root-cause above; Opus 5 dropped by the operator on cost).
   `tools/probe_freejudge.py` — 14 no-guard fixtures × 3 runs = 42 judgments
   per model, scored OK / SUBOPTIMAL / **UNSAFE** against the bot's risk model
   rather than a retired curve:

   | model | OK | SUBOPT | **UNSAFE** | $/call | vs champion |
   |---|---|---|---|---|---|
   | `gpt-5-mini` (champion) | 83% | 5 | **2 (5%)** | $0.00221 | 1x |
   | `claude-sonnet-5` | **88%** | 5 | **0 (0%)** | $0.01345 | **6.1x** |
   | `claude-haiku-4-5` | 79% | 5 | **4 (10%)** | $0.00323 | 1.5x |

   **NO SWITCH — `gpt-5-mini` holds the quant seat.** Sonnet 5's clean UNSAFE
   card is the best ever recorded on this battery and still does not file:
   Fisher exact on 0/42 vs 2/42 is **p = 0.49** (indistinguishable from chance
   at this n), +5 OK misses the routine's `OK+10`, and at **6.1x** per-call it
   fails the routine's own ≤3x cost pre-filter — the gate that left
   gemini-3.5/3.6-flash and gpt-5.5 unprobed in July. Scale check:
   ~$0.61/day / ~$18/mo at ADR-022 full escalation, on a bot running $10 orders
   and $60 exposure. Watch item only, and its introductory pricing expires
   2026-08-31 moving it *further* out of band ($2/$10 → $3/$15).

   **Champion reproduced across six weeks** (81%→83% OK, 1→2 UNSAFE of 42 vs
   the 2026-07-31 baseline), and **July's `claude-haiku-4-5` "no verdict" is
   now closed as NOT champion-class** — that bake-off died on an exhausted
   Anthropic credit balance; three clean runs give 4 UNSAFE (double the
   champion) at `calm_well_matched_lowcycle` x2, `developing_downtrend_mild`,
   `recovering_after_dip`. **Two independent instruments agree** on haiku's
   over-tightening bias (freejudge UNSAFE 10%; the off-contract core battery
   8/36, tightening on all four `hold_*_matched`), so that finding survives the
   instrument correction. Full record in
   `docs/reference/advisor-llm-models.md` Rev 2026-08-10. Freejudge spend
   **$0.79** / 126 calls.

   **⚠️ BUG FOUND + FIXED — the Anthropic adapters 400 on every Claude 5 call.**
   `AnthropicAdvisorAdapter` and `AnthropicAssistantAdapter` both sent
   `temperature` unconditionally; Anthropic **deprecated** that field from the
   Claude 5 generation and returns `400 invalid_request_error: 'temperature'
   is deprecated for this model`. The first roster attempt scored sonnet-5 and
   opus-5 at 0/36 — **not a model result, a broken request**, and the failure
   surfaces as a generic transport error rather than "unsupported model".
   Latent, not an active outage: live `settings.yml` runs `claude-sonnet-4-6`
   (generation 4), so the bug was armed for the first Claude 5 upgrade — of
   the advisor escalation seat *or* the Discord operator assistant. Fixed with
   `anthropic.supports_temperature()`, parsing the MAJOR generation out of the
   model id (sibling of `openai.is_reasoning_model`). The boundary is the
   point: `claude-haiku-4-5` contains a "5" but is generation 4 and still
   accepts the field, so a naive substring check would strip it from every
   4.5-tier model — pinned by `tests/adapters/test_anthropic_temperature_support.py`
   plus caller-side request-body tests in both adapters' existing MockTransport
   harnesses (a pure-function test cannot catch a body builder that forgets to
   ask). Live-verified: sonnet-5 went 0/8 → 7/8 on the arbitrator battery.

   **Pricing: `claude-opus-5` + `claude-sonnet-5` added** (Anthropic's published
   table, verified 2026-08-10). Sonnet 5's `$2/$10` is **introductory through
   2026-08-31**, reverting to `$3/$15`; the entry deliberately encodes the
   STANDARD rate because over-pricing is the module's stated safe direction and
   the 180-day freshness test cannot catch a price that changes on a known
   future date. Consequence while the promo runs: Sonnet 5 spend reads ~50%
   high. Revisit after 2026-09-01, when the entry becomes exact on its own.

   **⚠️ TWO EARLIER CLAIMS IN THIS SESSION WERE WRONG, both corrected above:**
   (a) the cost gate does NOT fall back to a heuristic for unpriced models — it
   RAISES, which is how the bug above was caught on the first live call; (b) the
   per-model Atlas price table produced earlier was **not sourced from Atlas**.
   Atlas publishes flat rates for 61 of its models and **none of its ~25
   Anthropic entries**; the quoted Claude/gpt-5-mini figures were upstream list
   prices misattributed to Atlas, and are withdrawn. Where Atlas's published set
   overlaps ours it matches upstream exactly (`gpt-5.5` 5/30,
   `gemini-3.1-pro-preview` 2/12, `gemini-3.5-flash` 1.5/9) — evidence of
   pass-through, not proof for an unlisted model. **Consequence: Atlas is a
   DISCOVERY tool, not a measurement path** — this roster ran native, where
   pricing is published and verifiable and no gateway sits in the comparison.

   **Bias controls** (operator asked how to rule out home-team bias when the
   evaluator is itself an Anthropic model): rubrics frozen in `main` with pinned
   tests BEFORE any Claude model ran; operator picked the roster; control
   re-run in-session; every lost fixture named in the table above. The Anthropic
   models won the two rule-following batteries, tied-or-lost the two numeric
   ones, and the cheapest of them posted **the worst quant-core score ever
   recorded against this battery, local or cloud** (8/36 — below the 19/36
   constant ceiling AND below 12/36 chance, worse than `granite4.1:30b`'s 6/36
   only in that it is not, at 2 OK / 4 OVERTRADE / 4 MISS, a constant). Its
   profile is near-inverted on the hold/tighten axis: it tightened on all four
   `hold_*_matched` fixtures — including `hold_moderate_matched`, where current
   spacing EQUALS the ideal — and held on three of four `tighten_*` fixtures.
   That is textbook fee-churn behaviour and disqualifies it for the quant seat
   regardless of its perfect news and arbitrator cards.

   Session probe spend: **$1.71 recorded / ~$1.52 actual** (the delta is the
   deliberate Sonnet 5 over-pricing above), 243 calls, isolated
   `data/probe_llm_cost.db`. 81 of those calls were the pre-fix 400s, which
   cost $0.

   **P3 close audit** ✅ **2026-08-10.** Green: schema-drift 19/19 clean;
   `tools/scan_logging.py --check rule1` exit 0; `domain/` imports zero
   adapters; pylint 10.00 exit 0; mypy strict clean; 3026 tests (2973 at P3
   slice 18 → 3026, no deletions). Entry points unchanged at 21, so the
   deprived-env baseline stands. **Two findings, both queued rather than
   fixed in the audit pass:**
   1. **Three files carry a bare `# pylint: disable=too-many-lines` with no
      rationale** — `cli/operator.py` (1625), `services/grid_engine.py` (1307),
      `cli/live.py` (1248). The suppression may well be right (each is one
      cohesive concern), but an undocumented suppression is indistinguishable
      from a junk drawer, and the phase-end rule asks for the judgement to be
      written down. Queue: add a one-line justification, or split.
   2. **`services/operator_service.py` is 993 lines — 7 from the hard gate.**
      Worth naming because that exact near-miss bit this session: `status.py`
      crossed the gate mid-slice and only CI caught it. The next feature to
      touch that file will trip it.

   **Same-session addendum — activity stat** ✅ **2026-08-09**
   (operator-requested, the v0 of the new re-anchor-viability item): a sixth
   banner stat, **2h range vs spacing** ("0.4×" = the market isn't moving
   enough to cycle even a correctly-placed grid — the BTC-re-anchor-sat-idle
   observation, made visible). Computed from the sparkline series already in
   the snapshot (one fetch now serves sparklines + banner; zero new
   queries); "—" when the series is thin; deliberately a fact, not a
   probability claim. The full screener-backed viability weighting is filed
   in `operator-ux.md` + the v1.1 README P3 table, deferred behind the
   committed ops items. Test count 2854 → 2855.
   **Round-2 polish** ✅ **2026-08-09** (operator: the tinted slab + the
   left-crammed layout both failed the bar; new STANDING RULE recorded — the
   web UI must look professional, render-and-look before claiming done): the
   banner became a clean card on the normal surface — white values over
   muted labels, scoreboard idiom — with severity in exactly four accents
   (left edge, chip, drift number, primary button) and the stat row
   distributed edge-to-edge. A recommendation is not an alarm: the
   session-cap banner keeps its wash deliberately. Live-verified via
   Claude-in-Chrome screenshot. Known nit for the next web slice: the
   projected-cost label's capital P breaks label-case consistency.

## Phase 9 – Kraken Securities Equities (Committed Track, Post-v1.0)

**Status:** Operator-committed 2026-05-20 (during soak Day 2). Starts after v1.0 tag. No work has begun; this is the scoping sketch.

**Motivating context.** Kraken added US-listed stock + ETF trading via a FINRA-regulated Kraken Securities LLC (broker-dealer partnership with Alpaca; announced April 2025; ~11,000 commission-free symbols; 24h M-F on Kraken Pro). Kraken extended their REST API in August 2025 with an additive `asset_class` parameter on existing endpoints (Add Order, Open Orders, Ticker, etc.) — equities support is API-accessible via the same authentication + signing path as crypto. Operator's strategic case: decorrelation from crypto (alt-to-alt grids are highly correlated; stock-to-crypto less so), larger universe (11k vs ~50 useful Kraken crypto pairs), volatile single-stocks (TSLA, NVDA) have wider daily ranges than BTC = real edge multiplier when capital allows.

**Central design constraint: PDT.** SEC limits accounts under $25k equity to 3 day-trades per 5 trading days. Kraken Securities is FINRA-regulated; PDT applies. **Operator's stated capital reality:** $25k threshold is aspirational ("not likely in the next decade"); realistic trajectory is $100 → $1000+ via deposits + grid earnings. Therefore the PDT-aware design is the load-bearing piece of Phase 9, not a flag. The crypto grid cycles intra-day (sometimes in minutes); the equity grid must cycle multi-day, achieved via wider spacing (3-5% vs crypto's 1%) so individual round-trips span multiple trading sessions (= not PDT day trades). The engine code is largely unchanged — same grid math, same fill detection, same counter-placement — but a new "PDT-aware safety layer" gates counter-placement based on a rolling 5-trading-day same-day-round-trip counter.

**Activation threshold:** equity grids only make economic sense above ~$500 account equity. Below that, allocated capital per grid is too small for meaningful per-cycle profit. **The design work happens in advance; activation waits for the operator's capital to reach a viable threshold.**

**Proposed slicing.** Six substantive slices + a closing check. Approximate effort: 2-3 months of focused work.

1. **Stage 9.0 – Kickoff + ADR-019.** Ratify the equity-grid risk model: PDT-aware grid variant; settlement-aware cycle pacing; earnings-pause posture; wash-sale-aware tax accounting; below-$25k operating profile. New `docs/planning/stage-9.0-design.md` ratifying ~10 implementation decisions. No code in kickoff.
2. **Stage 9.1 – `KrakenAdapter` equities extension.** Add `asset_class` parameter awareness to relevant calls; stock-symbol parsing + asset-pair metadata handling for stocks (precision, lot size, market-hours metadata); new error mapping for equities-specific Kraken responses. Doesn't change the engine — just teaches the adapter to talk stocks. ~2-3 weeks; substantial tests.
3. **Stage 9.2 – PDT-aware safety layer.** New `services/pdt_safety.py` maintains a rolling 5-trading-day same-day-round-trip count from the existing `trades` table; new safety check refuses counter-placement that *could* complete a same-day round-trip if it would push the count to 4-in-5-days. Account-equity check at engine startup: refuse to operate (or warn loudly) if equity < $25k AND PDT-aware mode is not explicitly opted into. New `safety.pdt:` config block. The Stage 8.1 reconciler patterns transfer cleanly — this is the same "engine knows its own history" shape.
4. **Stage 9.3 – Earnings calendar integration.** New `services/earnings_calendar.py` ingests earnings dates from a data source (TBD: Alpaca's calendar endpoint? EDGAR? a third-party feed?). New safety check pauses the grid for a configurable window around announced earnings (default e.g. 2 days before, 1 day after). Operator-overridable per-symbol. New `notifications` events for pause-entered/pause-exited.
5. **Stage 9.4 – `cli/live --symbols TSLA,AAPL` first live test.** End-to-end live test with tiny position (single share or fractional, single cycle, single security to start). Validate the full path: layout → fill → PDT-aware counter → settlement-aware cycle. Operator-driven, same posture as Stage 2.3's "first real trade" diagnostic. **This is the equity-grid Stage 2.3 equivalent — the moment the project officially trades equities.**
6. **Stage 9.5 – Tax export + wash-sale tracking.** New `cli/tax-export` (or `tools/tax_export.py`) producing 1099-B-compatible CSV from the `trades` table. Wash-sale lot tracking per IRS rules (substantially-identical security + 30-day rule; the grid does this every cycle by design). Integration with web UI's cost dashboard for year-to-date tax-relevant summaries. Tax accounting is non-optional for equities long-term; ship it before tax filing season.
7. **Stage 9.6 – Phase 9 Integration Check.** Multi-symbol equity-grid live test (TSLA + 2-3 other choppy names); PDT counter exercised end-to-end; earnings-pause exercised against a real upcoming earnings date; tax export verified. Closing summary at `docs/planning/phase-9-summary.md`.

**Open design questions for ADR-019 to settle** (deferred until kickoff):

- PDT counter implementation: rolling 5-trading-day window vs. calendar-week approximation. Trading-day awareness adds NYSE calendar dependency (holidays, half-days, early closes).
- Day-trade-vs-swing classification: when does a "fill + counter-fill" pair count as a day trade for our purposes? At actual execution timestamp on the exchange? At intent timestamp on our side?
- Wash-sale tracking granularity: per-symbol or per-substantially-identical-cluster (TSLA + TSLA-options = same cluster)?
- Earnings-pause data source: Alpaca's calendar (we already partner with them via Kraken), EDGAR (free, official, fiddly), or a third-party feed (paid, polished)?
- T+1 settlement and cash-account rules: can the counter-order place before settlement? Margin account would solve this but introduces margin's risk model (gated by ADR-019's risk model decisions).
- Multi-grid portfolio sizing: capital allocator across crypto + stocks. Operator's $100 → $1000 trajectory makes this a real concern, not hypothetical.
- Symbol format: confirm via live API exploration whether TSLA equity is `TSLA` or `TSLA.US` or some other format; document the asset-class metadata schema.

**Not in scope for Phase 9** (deferred to v1.2+ or never):
- Margin trading on equities — gated by the standing operator-experience rule in `docs/release/v1.0-future-improvements.md`.
- Options trading — Kraken mentioned options exploration in their launch announcement, but options strategies don't map cleanly to grid mechanics.
- Long-hold (non-grid) equity positions — a fundamentally different strategy path; if ever pursued, lives as Phase 10+ with its own design surface.
- Multi-exchange equity trading (Interactive Brokers, Alpaca direct, etc.) — single-venue first; multi-venue is a separate concern.

## Phase Dependencies

7. **Stage 3.5 - Phase 3 Integration Check** ✅ (2026-05-15) - Demonstrate an advisor-in-the-loop run with MoE + news: trading engine runs, advisor produces aggregated suggestions, operator reviews, optionally auto-applies bounded ones. *End-to-end chain verified: 6520 price snapshots (24h cli/observe soak) → 131 news items in one cli/news poll (matched the Stage 3.2.5 closing receipt to the row) → fresh cli/advise cycle (39s phi4:14b-q8_0, news-aware via SummaryBuilder's 20-item recent_news) → cli/apply dry-run correctly rejected every key with reason 'auto-apply disabled' (gate default-off posture holds end-to-end). Notable observation: same parameter recommendation as the previous cycle but confidence dropped from `high` (no news) to `medium` (news context present) — calibration shift even when the proposed params hold. Closing summary at `docs/planning/phase-3-summary.md`. Phase 3 total real-money cost: **$0.00** (advisor never executes per ADR-002). Running project cost still **$0.08** unchanged from Phase 2 close.*
8. **Stage 3.6 - Operational polish (pre-Phase 4)** ✅ (2026-05-15) - Post-Phase-3 polish to remove operational friction before Phase 4's Harvester loop lands. Two independent slices, both shipped:
    - **Slice 3.6a - Indefinite runtime** ✅ (2026-05-15) - `LiveConfig.max_runtime_minutes` (and `ShadowConfig`'s twin) became `Optional[float]` with `None` meaning "no runtime cap." Loop check at `cli/live.py` and `cli/shadow.py` skips the elapsed-comparison when `None`. Pre-3.6a the field was `Field(gt=0)` so `0` didn't work and operators had to bump to a sentinel like 525600 (a year of minutes); the Optional shape lets the type system express "run forever" honestly. 6 new tests covering both bounded (default) and unbounded paths; SIGINT/SIGTERM and the session-loss cap still apply.
5. **Stage 4.5 – Phase 4 Integration Check** ✅ (2026-05-15) – Demonstrate a scenario in which trading grows the exchange balance, Harvester scrapes the surplus, and the audit trail confirms the actions. Confirm that no unauthorized transfers occur. *Audit found and fixed one real defect: cli/harvest --execute on a bank_to_exchange proposal would have called Kraken /Withdraw with the wrong direction semantics (deposits are operator-pushed from the bank side; no API path exists for API-initiated deposits). Added defense layer 3 to refuse bank_to_exchange proposals at the gate with a clear operator-facing message pointing them to Kraken Pro "Funding → Deposit" for the manual push instructions. The gate now has seven defense layers (was six). Other paths verified end-to-end against the operator’s real account: cli/harvest read $99.92 USD via the Harvester key + correctly classified as deficit + persistence_enabled=true confirmed; tools/show_proposals + tools/show_transfers both correctly report "no rows match" against empty tables. Closing summary at docs/planning/phase-4-summary.md (mirrors phase-3-summary.md). **Phase 4 total real-money cost: $0.00** (no live withdrawal in slice tests; the first $1 ACH is the operator-triggered first execution, separately tracked). Running project cost still $0.08 unchanged from Phase 2 close.*
