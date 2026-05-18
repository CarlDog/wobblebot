# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Source of truth:** `docs/planning/roadmap.md`. Each completed stage carries a ✅ completion date.

**Phase 2 closed 2026-05-14; Phase 3 closed 2026-05-15; Phase 4 closed 2026-05-15; Phase 5 closed 2026-05-16; Phase 6 closed 2026-05-17; Phase 7 closed 2026-05-18.** Closing summaries at `docs/planning/phase-{2,3,4,5,6,7}-summary.md`. Running project real-money cost: **$0.085018** ($0.08 through Phase 4 + $0.005018 across Phase 6's three cloud-LLM smoke calls; Phase 7 added $0.00). Thirteen operator entry points (Phase 7 added `python -m wobblebot.cli.web` per Stage 7.1; the numbered list below covers the trading + operator + treasury + dashboard surfaces; per the Phase 6 close prose `tools/run_cloud_check.py` is the twelfth entry point counted but lives under `tools/` rather than `cli/`):

- `python -m wobblebot.cli.sandbox` — Phase 1 sandbox: buy-dip/sell-rebound cycle through `MockExchangeAdapter` + `SQLiteStorageAdapter`, persists to SQLite.
- `python -m wobblebot.cli.status` — Stage 2.1 live read check: read-only Kraken price + balance fetch.
- `python -m wobblebot.cli.preflight` — Stage 2.3 diagnostic: runs ONE engine step against live Kraken with `KrakenAdapter(dry_run=True)`. Every order goes through Kraken's `validate=true` flag — request is signed, sent, validated end-to-end (auth / pair / precision / balance / ordermin / costmin) without placing. **Use this before every live run to confirm the config is acceptable to Kraken.**
- `python -m wobblebot.cli.live` — Stage 2.3 operational loop, **multi-asset since Stage 2.4**. Real-money trading. `--symbols BTC/USD,ETH/USD,DOGE/USD` accepts a comma-separated list; each tick steps every symbol in series. Hard caps (max session loss, max runtime, per-coin / total / daily-spend exposure) — total/daily caps are global across symbols, per-coin caps are per-symbol. Clean SIGINT/SIGTERM shutdown cancels every open order on every symbol. Exit codes: 0 clean stop, 1 loss-cap tripped, 2 missing creds. *(Originally `cli/grid`; renamed during Phase 3 sandbox prep per ADR-008 to make the live-money distinction loud vs the planned `cli/shadow`.)*
- `python tools/first_real_trade.py` — one-shot diagnostic: places a far-from-market BUY (cancels it) + a marketable BUY/SELL round-trip with hard caps. Forensic JSONL log to `data/`. Used 2026-05-15 00:51 UTC against the operator's account; total cost $0.08 (two 0.40% taker fees on a $10 round-trip; spread effectively zero).
- `python -m wobblebot.cli.observe --symbols BTC/USD,ETH/USD --price-interval-seconds 30` — Stage 3.0 pure data collection. Read-only. Polls Ticker per symbol, persists to `price_snapshots` table; optionally polls BalanceEx on a slower cadence. Build a multi-week price dataset.
- `python -m wobblebot.cli.shadow --symbols BTC/USD,ETH/USD --initial-shadow-usd 10000` — Stage 3.0 shadow trading. Same engine code as `cli/live` but with `ShadowExchangeAdapter`: live Kraken prices, synthetic balance ledger, honest maker/taker fee modeling. Real-time backtest framework + Phase 3 advisor sandbox.
- `python -m wobblebot.cli.apply` — Stage 3.4b operator-in-the-loop auto-tuning gate. Dry-run by default: reads latest (or `--recommendation-id`) `AdvisorSuggestion`, runs it through `evaluate_auto_apply`, prints per-key APPLIED/REJECTED breakdown. `--commit` rewrites `settings.yml` via ruamel.yaml (comment-preserving, atomic) and persists an `AppliedSuggestion` audit row. Default-off gate (`auto_apply.enabled=False`) + ADR-007's "news-role never auto-applies" rule are both load-bearing safety properties enforced inside the gate.
- `python -m wobblebot.cli.harvest` — Stage 4.2-4.4 treasury monitor. **Daemon mode (no flags)**: polls Kraken USD balance on `schedules.harvest`, runs `propose_transfer()`, persists every non-None proposal to `transfer_proposals`, logs "HYPOTHETICAL proposal" lines. Read-only against Kraken; uses the Harvester key. **`--execute <proposal-id>` mode (Stage 4.4c)**: operator-approved one-shot withdrawal. Reads the persisted proposal, runs seven defense layers, calls Kraken `/0/private/Withdraw`, persists `TransferResult`. The ONLY path in the codebase by which money leaves the exchange.
- `python -m wobblebot.cli.operator` — **Stage 5.6 daemon** (Phase 5's Operator Interaction Engine; ADR-013). Long-running Discord-bot connection that maintains a Gateway session, drains the `notifications` SQLite table to Discord (color-coded embeds, per Stage 5.5 outbound wiring in cli/live + cli/harvest), parses inbound operator messages via `OllamaAssistantAdapter` into typed `OperatorIntent` payloads, and routes by variant: Command → writes a `PendingCommand` row + posts confirm embed (cli/live polls the approved rows; the `WHERE status='approved'` filter is the ADR-002 firewall); Query → reads engine + storage state via `OperatorService` and posts the result; Conversational → posts the reply text; Unparseable → asks for clarification. Background TTL expirer transitions abandoned `awaiting_confirmation` rows to `expired` on `schedules.operator_ttl`. New `OperatorConfig` block in settings.yml composes Discord auth + assistant LLM + four DB paths + the ADR-013 knobs (context window, confirm TTL, forwarder + ttl-expirer poll cadences). Discord-ignorant from `cli/live`'s perspective — they communicate only via SQLite tables in operator.db.
- `python -m wobblebot.cli.web` — **Stage 7.1 daemon** (Phase 7's Web UI; ADR-016 + ADR-017). Two subcommands: `serve` (default) boots uvicorn against the FastAPI app `wobblebot.web.app.create_app(...)`, opening `operator.db` plus four optional cross-DB paths (live / advise / harvest / observe / news) for the dashboards that read them; `create-user` prompts on stdin for username + on the terminal (via `getpass`) for password (twice for confirmation) and seeds a bcrypt-hashed row in the `users` table. Single-operator-v1 auth: session cookie signed by Starlette's `SessionMiddleware`, per-IP login rate-limit (default 5 attempts / 60s), CSRF synchronizer-token middleware. Stage 7.1 ships the shell + three navigable empty stub pages (`/dashboard`, `/cost`, `/audit`); real data lands in Stages 7.2-7.4. Per ADR-016 binds 127.0.0.1:8000 by default — operator-managed reverse proxy fronts the LAN. Refuses to start without `WOBBLEBOT_WEB_SESSION_SECRET` env var (32+ random bytes; `python -c "import secrets; print(secrets.token_urlsafe(32))"` mints one). Mutations land in Stage 7.2 via `PendingCommand` rows in `awaiting_confirmation`; cli/live's `WHERE status='approved'` poll stays the only path from intent to engine — ADR-002 firewall intact.

1656 unit tests pass by default (up from 1460 at Phase 6 close after Phase 7's five stages added 196 across 7.1 / 7.2 / 7.3 / 7.4 / 7.5); 29 integration tests (5 Kraken API drift + 3 live read + 2 simulator + 2 grid e2e + 9 live trading + 5 Phase 5 operator e2e + 3 Phase 6 cloud-llm live) on opt-in. mypy clean (96 src files), black/isort clean, pylint **10.00/10** on `src/`. Stage 6.1.A re-crossed the 1000-line cap on `sqlite_storage.py` (now 1037 lines) and added a file-level `# pylint: disable=too-many-lines` — the adapter is naturally many-methods (one per port API method); splitting two cohesive llm_calls methods to a sibling mixin is over-engineering. The Stage 5.1.C precedent (split schema + rowmap) still holds for off-to-the-side helpers. New runtime dep added in 3.2.5: `feedparser`. New runtime dep added in 3.4b: `ruamel.yaml`. New runtime dep added in 5.2: `discord.py>=2.3,<3`. **Six new runtime deps in Stage 7.1.B** (biggest dep-add since Phase 5's `discord.py`): `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `jinja2>=3.1`, `python-multipart>=0.0.12`, `bcrypt>=4.2`, `itsdangerous>=2.2`. No new runtime deps in Phase 6 — pricing table is data, cost gate / retry helper / config schemas / cloud adapters are pure Python on existing httpx + pydantic.

### Operator handoff: from dry-run to live trading

1. **Mint a Kraken trading key**, separate from the read-only key (per ADR-003-style separation). Permissions: Query Funds + Query open & closed orders & trades + Create & modify orders + Cancel & close orders. **Withdraw must stay off** — that scope is exclusive to the future Phase 4 Harvester key. Recommended: enable IP address restriction.
2. **Stash credentials in `.env`** as `KRAKEN_TRADE_API_KEY` / `KRAKEN_TRADE_API_SECRET` (separate from the existing `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` so the read-only key can keep being used for `cli/status`).
3. **Run `cli/preflight`** — confirm Kraken accepts the grid config without spending anything. Exit 0 means every layout order would be accepted by Kraken's matching engine.
4. **Run `cli/live`** with eyes on the Kraken Pro Orders + Trade History tab. Defaults: $10 per order, 1% spacing, 3 above + 3 below = $60 total exposure, $5 max session loss, 60 minute max runtime, 5s tick. The first session is the highest-risk session — watch it.

### Stage 2.3 design decisions ratified (do not relitigate without an ADR)

- **Dry-run = `validate=true`.** `KrakenAdapter(config, dry_run=True)` adds `validate=true` to every AddOrder request. Kraken validates auth + pair + precision + balance + ordermin + costmin without placing. The adapter synthesizes a `DRYRUN-<order.id>` exchange_id so the engine's bookkeeping path still works for diagnostic runs.
- **Per-pair precision quantization is mandatory.** AssetPairs cache (`pair_decimals`, `lot_decimals`, `ordermin`, `costmin`) populated lazily on first trading call. Price/volume rounded DOWN before submission — never up, since rounding up could push spending past the engine's intended `order_size_usd` budget.
- **Two separate Kraken keys, not one.** The read-only key (`cli/status`) and the trade key (`cli/preflight` / `cli/live`) live side-by-side in `.env`. `KrakenConfig.from_env(key_var=..., secret_var=...)` parameterizes which env vars to read.
- **Live taker fee is 0.40%, not the mock's 0.26%.** Discovered during the 2026-05-15 first-trade test: $0.04 fee on each $9.99 leg of a marketable round-trip = 0.40%. The mock uses 0.26% (Kraken maker rate, conservative). The grid engine in normal operation places limit orders that sit on the book — those collect MAKER fees, so the mock's assumption is right *for the engine's normal mode*; the gap only shows up on marketable orders (which the engine doesn't normally place).
- **Cleanup discipline in the loop.** `cli/live`'s shutdown path cancels every open order for the symbol in a `finally` block, regardless of why the loop ended (signal, runtime cap, loss cap, exception). The session-end log records before/after USD balance, session PnL, cancellations succeeded/failed.

### Stage 2.4 design decisions ratified

- **Symbols step in series within a tick.** Per ADR-006 decision 5, the per-symbol asyncio.Lock makes parallelization safe — but at measured ~150ms per-symbol latency vs the 5s tick budget, even a 30-coin serial sweep finishes in well under one tick. Parallelization (asyncio.gather) deferred to Phase 5 hardening if profiling ever shows the master-task throughput is a bottleneck.
- **Per-symbol step errors are swallowed at the CLI layer.** One bad coin (network blip, Kraken returning EService:Unavailable) cannot kill the tick or the session. The engine surfaces the error; `_run_one_tick` logs it with structured fields and continues to the next symbol.
- **Caps split: total/daily are global, per-coin is per-symbol.** `max_total_exposure_usd` and `max_daily_spend_usd` count across every coin (computed via unfiltered `storage.get_open_orders()` / `storage.get_orders(side="buy", created_after=today)`). `max_per_coin_exposure_usd` and `max_orders_per_coin` are scoped to one symbol via the symbol filter. Same SafetyConfig instance passed to GridEngine; the engine's `_check_safety` was already symbol-aware.
- **`--symbols` deduplicates and preserves order.** Comma-separated input. Trailing/leading whitespace tolerated. Empty entries from trailing commas silently dropped.

**Config consolidation audit ✅ closed 2026-05-14** (queued before Stage 3.1, landed in eight slices). Every CLI now loads its config via `wobblebot.config.runtime.load_resolved_config(...)` with three-layer precedence (base YAML → `--profile` deep-merge → CLI flag overrides). Per-CLI sections (`live`, `shadow`, `observe`, `preflight`, `status`, `sandbox`) live in `config/settings.example.yml` alongside engine sections (`grid`, `safety`) and the Phase-3 `advisor:` block (MoE schema, ≥3 experts validator, prompt-file references). Profiles `conservative` / `aggressive` cover both `live` and `shadow`; `cloud-only-moe` swaps Ollama experts for cloud equivalents. Stale `docker/env.example` moved to repo-root `.env.example` and refreshed for Phase 2.3 reality. Schema-drift tests in `tests/config/test_schema_drift.py` keep the example/operator pairs aligned for both `settings.yml` and `.env`; `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` promotes warnings to hard failures. Prompt-file infrastructure (`config/prompts/{quant,risk,news,arbitrator}.md` + `wobblebot.config.prompts.load_prompt`) is in place for Stage 3.4a to consume. New runtime dep: `python-frontmatter`.

**Stage 3.6 closed 2026-05-15.** Slice 3.6a: `max_runtime_minutes` on `LiveConfig` + `ShadowConfig` is `Optional[float]` (`None` = run indefinitely). Slice 3.6b: `cli/advise` is multi-symbol (`AdviseConfig.symbols: list[Symbol]`) with per-symbol-isolated LLM calls — the daemon iterates serial per tick so each cycle's `PerformanceSummary` carries one coin's context. cli/apply filters suggestions by symbol so the right row gets gated against the right grid. **Verified live** with `--symbols BTC/USD,ETH/USD`: distinct recommendations per coin (BTC: spacing 1.1, high; ETH: spacing 0.7, medium).

**Stage 4.1 closed 2026-05-15.** First Phase 4 slice; pure-domain (zero new real-money risk). `HarvesterConfig` schema with four thresholds (min/topup/surplus/day-cap), `enabled: bool = False` mirroring the auto-apply gate posture, model validator enforcing `min < topup < surplus`. `services/harvester.propose_transfer()` pure function carves four bands: deficit (no proposal), top-up band (bank→exchange to midpoint), hold band (no proposal), surplus (exchange→bank to midpoint). Day-cap shrinks/refuses proposals.

**Stage 4.2 closed 2026-05-15.** `cli/harvest` daemon polls Kraken USD balance on `schedules.harvest`, runs `propose_transfer()`, logs "HYPOTHETICAL proposal (no money moved)" lines. No transfers, no DB writes; uses the read-only `KRAKEN_API_KEY`.

**Stage 4.3 closed 2026-05-15.** Every non-None proposal now persists to a new `transfer_proposals` SQLite table for operator review.

**Phase 4 closed 2026-05-15.** All five stages shipped (4.1 domain, 4.2 daemon, 4.3 persistence + inspection, 4.4 active withdrawals, 4.5 integration check). Stage 4.5 audit caught one real defect: `cli/harvest --execute` was missing a guardrail for `bank_to_exchange` proposals (Kraken's `/Withdraw` is exchange→bank only; deposits are operator-pushed from bank side). Fixed; gate now has seven defense layers. Closing summary at `docs/planning/phase-4-summary.md`. **No real withdrawal during slice work** — the operator's first $1 ACH to "360 Performance Savings" is a separately-tracked event. Running real-money cost still **$0.08** unchanged from Phase 2 close.

**Phase 5 in progress 2026-05-16** — Operator Interaction Engine (reframed 2026-05-16 per ADR-013; kickoff commit landed ADR-013 + `docs/planning/stage-5.1-design.md` + roadmap rewrite). The Phase 1-4 internals work end-to-end; Phase 5's job is to give the operator a bidirectional Discord interface with multi-turn conversational LLM intent parsing and ADR-002-preserving confirm-before-execute. Seven stages: 5.1 Operator Domain & Ports → 5.2 Discord Transport → 5.3 Operator Assistant (Ollama) → 5.4 Engine Integration → 5.5 Outbound Notifications → 5.6 `cli/operator` Daemon → 5.7 Phase 5 Integration Check.

**Stage 5.1 closed 2026-05-16.** Pure-domain stage — four sub-slices: 5.1.A operator types + port (`OperatorIntent` typed sum, 6-command + 9-query catalogs, per-query `*Result` types, `PendingCommand` audit-trail model, `OperatorPort` ABC, `OperatorError`), 5.1.B assistant types + port (`EngineStateSnapshot`, `ConversationTurn`, `ConversationContext`, `AssistantPort` ABC, `AssistantError`), 5.1.C `sqlite_storage.py` split (cleared pre-existing `too-many-lines` lint flag by extracting `sqlite_storage_schema.py` + `sqlite_storage_rowmap.py`), 5.1.D close.

**Stage 5.2 closed 2026-05-16.** Single substantive slice. `adapters/discord_transport.py` wraps `discord.py`'s Gateway client: `DiscordTransportConfig` (token env var + user / channel allowlists), `InboundMessage` / `ReactionEvent` normalized value objects, `DiscordTransport` adapter with `on_message` / `on_reaction` handler registration, allowlist filtering (deny-by-default; bot self-rejection), outbound `send_message` / `send_embed` / `send_confirmation` (amber-bordered embed + ✅ / ❌ reactions for the ADR-013 confirm-before-execute gate). New runtime dep `discord.py>=2.3,<3`. The adapter is concrete (no port wrapper) — only `cli/operator` (Stage 5.6) will consume it; `cli/live` remains Discord-ignorant per ADR-013.

**Stage 5.3 closed 2026-05-16.** Single substantive slice. `adapters/ollama_assistant.py` implements `AssistantPort` via Ollama's `/api/chat` endpoint (multi-turn role-tagged messages) — sister adapter to the existing `OllamaAdapter` (which implements `AdvisorPort` via `/api/generate`). System prompt = `config/prompts/operator.md` body + engine state snapshot JSON; recent `ConversationTurn`s become user/assistant messages; current operator message is the last user turn. LLM output validated against `OperatorIntent` discriminated-union `TypeAdapter`. Per operator guidance "always reuse what makes sense" the shared helpers (`is_thinking_model`, `extract_last_json_object` + new `OllamaJsonExtractError`) were promoted to module-public in `adapters/ollama.py` and imported by both adapters — each wraps the helper's error as its port-specific `AdvisorError` / `AssistantError`. `PromptRole` literal gained `"operator"`.

**Stage 5.4 closed 2026-05-16.** Four substantive sub-slices + close. **5.4.A** GridEngine operator-control methods (`pause_symbol` / `resume_symbol` / `cancel_open_orders` / `request_stop` + new `StepAction` "skipped_paused"). **5.4.B** first Phase 5 SQLite table — `pending_commands` with six-state CHECK + three indexes — plus `StoragePort` `save_pending_command` / `get_pending_command` / `get_pending_commands` and a `row_to_pending_command` mapper that uses a module-level `TypeAdapter[OperatorCommand]` for discriminator resolution on read. **5.4.C** `services/operator_service.py` implementing `OperatorPort` — match/case dispatch on the discriminated union for both `dispatch_command` (six commands) and `answer_query` (nine queries with graceful degrade when optional cross-database storages are unwired). **5.4.D** `cli/live` gains optional `operator_db: str | None`; when set it opens a second `SQLiteStorageAdapter`, constructs `OperatorService`, and drains approved pending commands via `_process_pending_commands` — the `WHERE status='approved'` filter on the SELECT is the literal ADR-002 confirm-before-execute firewall.

**Stage 5.5 closed 2026-05-16.** Two substantive sub-slices + close. **5.5.A** new `notifications` SQLite table (id PK + level CHECK + title/message/timestamp + context_json + forwarded flag + forwarded_at + created_at; two indexes); new `PersistedNotification` wrapper in `ports/notifier.py`; three `StoragePort` methods (`save_notification` returning row id, `get_notifications(forwarded, limit)` for cli/operator's poll, `mark_notification_forwarded` idempotent); `adapters/sqlite_notifier.py` SqliteNotifierAdapter wrapping any StoragePort. **5.5.B** both `cli/live` and `cli/harvest` gain `operator_db: str | None` config field + `_notify(notifier, ...)` helper that swallows NotifierError so a broken notifier can NEVER break the engine loop. cli/live emits notifications on session start / per-tick fills / cap trip / session end; cli/harvest emits on proposal generated / withdrawal failed / withdrawal executed (level=warning for the last because money moved is the highest-value event). Per ADR-013 decision 9 neither CLI imports `discord.py`.

**Stage 5.6 closed 2026-05-16.** Four substantive sub-slices + close. **5.6.A** third Phase 5 SQLite table — `conversation_turns` with role CHECK + two indexes — plus `StoragePort` `save_conversation_turn` (upsert) and `get_conversation_turns(channel_id, user_id, limit)` (chronological, newest-N via DESC+LIMIT+reverse); `row_to_conversation_turn` uses a new `TypeAdapter[OperatorIntent]` for discriminator rebuild. **5.6.B** three Pydantic models in `config/cli.py`: `AssistantLLMConfig` (provider/model/prompt_file/temperature/max_tokens), `OperatorAuthConfig` (bot_token_env_var + user/channel allowlists + outbound_channel_id), `OperatorConfig` composing them with `operator_db` + optional `live_db`/`advise_db`/`news_db`/`harvest_db` + ADR-013 knobs (`context_window_turns` 10, `confirm_ttl_seconds` 300, `forwarder_poll_seconds` 2.0). `WobbleBotConfig` gains `operator: OperatorConfig | None`. **5.6.C** new `cli/operator` daemon with three concurrent concerns: notification forwarder (background task drains `notifications WHERE forwarded=0` and posts color-coded embeds), conversation flow (match/case on the four `OperatorIntent` variants; Command writes `PendingCommand` + posts confirm embed, Query calls `OperatorService.answer_query`, Conversational/Unparseable send a reply), confirmation flow (reaction handler transitions `awaiting_confirmation` → `approved`/`rejected` via in-memory message_id→pending_id map). Per ADR-013 decision 3 the daemon NEVER calls `dispatch_command` directly — every state mutation crosses `pending_commands` so the ADR-002 firewall in cli/live's poll layer is the only path to engine. v1 limitation: cli/operator's stub engine doesn't see cli/live's in-memory pause state; `StatusQuery` reports all symbols as `active`. **5.6.D** `tools/show_pending.py` operator inspection (`--status` / `--limit` / `--log-format`) + close.

**Phase 5 closed 2026-05-16.** All seven stages shipped (5.1 Operator Domain & Ports, 5.2 Discord Transport, 5.3 Operator Assistant (Ollama), 5.4 Engine Integration, 5.5 Outbound Notifications, 5.6 cli/operator Daemon, 5.7 Phase 5 Integration Check). Stage 5.7 added a TTL expirer (third cli/operator background task — drains abandoned `awaiting_confirmation` rows) + a five-scenario end-to-end integration test suite that exercises the full pause→confirm→approve→dispatch→notify round-trip + reject path + multi-turn conversation + notification forwarding + TTL expiry, all against stubbed Discord transport + stubbed assistant + real storage + real engine. Closing summary at `docs/planning/phase-5-summary.md`. **Phase 5 total real-money cost: $0.00** (every test stubs Discord / Ollama / Kraken; the live verification "real operator types in real Discord → cli/live actually pauses BTC" is operator-driven and tracked separately). Running real-money cost still **$0.08** unchanged from Phase 2 close.

**Phase 7 closed 2026-05-18** — **Web UI / Dashboard**. All five stages closed: 7.1 web app skeleton + auth → 7.2 cost + status dashboards + ADR-013-firewalled mutation flow → 7.3 advisor + harvester read-only views → 7.4 news + audit-log views → 7.5 phase close + integration check. Closing summary at `docs/planning/phase-7-summary.md`. The dashboard is read-mostly; mutations (pause/resume/stop) route through `pending_commands` rows in `awaiting_confirmation` → confirm page → `approved`, preserving the ADR-002/ADR-013 firewall — cli/live's `WHERE status='approved'` poll remains the single source of truth for "intent → engine". `channel_id="web"` on web-originated rows distinguishes them from Discord-originated rows in the audit log. Idempotency on the confirm step: re-confirming a row already in a terminal state surfaces the existing status (handles the Discord-confirmed-first race). Six new runtime deps from Stage 7.1.B: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `bcrypt`, `itsdangerous`. **Phase 7 added $0.00 of real-money cost** (every mutation is firewalled; the e2e walkthrough is a unit test against in-memory storage). Running project real-money cost stays at $0.085018 unchanged from Phase 6 close.

**Stage 7.1 closed 2026-05-17** — **Web app skeleton + auth**. Five sub-slices delivered the web layer substrate Phase 7's feature stages then wedged into. **7.1.A** new `users` SQLite table in operator.db + `User` + `UserCredentials` domain models + three StoragePort methods (28 tests). **7.1.B** `WebConfig` Pydantic block (13 fields across serving / auth / presentation / cross-DB-path groups) + `WobbleBotConfig.web: WebConfig | None` + six new runtime deps + `src/wobblebot/web/` package scaffolding (FastAPI factory skeleton, middleware + auth + dependencies modules, templates + static-asset placeholders) (25 tests). **7.1.C** bcrypt + session lookup + `AuthRedirectRequired`; CSRF synchronizer-token helpers + `LoginRateLimit` (asyncio.Lock-guarded per-IP bucket; resets on success); `GET /auth/login` + `POST /auth/login` (rate-limit → CSRF → bcrypt → session → last-login bump → 302 /dashboard) + `POST /auth/logout`; CSRF token rotates on both transitions (108 tests). **7.1.D** `cli/web serve` (default; runs uvicorn against `create_app`) + `cli/web create-user` (stdin username + getpass password twice; bcrypt hash + StoragePort.create_user); three auth-gated stub pages `/dashboard`/`/cost`/`/audit` via `require_user`; `/` → 302 /dashboard (40 tests). **7.1.E** roadmap + CLAUDE.md + CHANGELOG + settings.example.yml `web:` block + .env.example `WOBBLEBOT_WEB_SESSION_SECRET` + project_state memory all reflect the close. Deprived-env walkthrough green for all five scenarios (bad --config; bad --profile; missing web: block; missing session secret env var; EOF on create-user stdin). **Stage 7.1 total real-money cost: $0.00**; running project cost still **$0.085018** unchanged from Phase 6 close.

**Phase 6 closed 2026-05-17** — **Cloud LLM Integration**. Operator-selectable cloud LLM providers for both the operator-assistant role (Phase 5 added) and the MoE trading-advisor roles (Phase 3 placeholder slots in `_build_advisor`). `AssistantPort` + `AdvisorPort` are provider-neutral by construction; Phase 6 extends `AssistantLLMConfig.provider` and `AdvisorConfig.provider` from `Literal["ollama"]` to the full set `["ollama", "anthropic", "openai", "google"]` with per-provider adapters. New concerns Phase 6 introduces (that Ollama didn't have) are **per-call cost** and **provider availability**, both ratified at kickoff in ADR-014 (LLM cost caps — daily + session USD caps via `services/llm_cost_gate.py` against an `llm_calls` SQLite table in `operator.db`; hard-stop on cap trip; pricing-table-as-code with verified-date discipline) and ADR-015 (provider failover policy — fail-loudly default + transient-only retry with exponential backoff; no silent cross-provider or cloud-to-Ollama failover). All five stages closed 2026-05-17 (6.1 Shared cloud-LLM infrastructure → 6.2 Anthropic adapter → 6.3 OpenAI adapter + shared `services/llm_cloud_call.py` orchestrator → 6.4 Google adapter → 6.5 Integration Check). Per-provider reasoning-token normalization differs (Anthropic lumps in output, OpenAI subtracts from completion, Google is additive natively) but each adapter satisfies the same `tokens_out + tokens_reasoning` additive convention via its `extract_<provider>_tokens` closure. Twelfth operator entry point landed in 6.5.A: `python tools/run_cloud_check.py` (one-shot live smoke test; `--provider` / `--role` / `--model` / `--max-tokens` / `--dry-run`). Phase 6 closing summary at `docs/planning/phase-6-summary.md` (mirrors phase-{2,3,4,5}-summary.md). **Phase 6 added $0.005018 of real-money cost** across three smoke-test calls (anthropic claude-sonnet-4-6 $0.004248 / openai gpt-4o-mini $0.000185 / google gemini-2.5-flash $0.000585 with 43 reasoning tokens correctly normalized). Running project real-money cost: $0.08 → **$0.085018**. **Phase 7 in progress 2026-05-17** — **Web UI / Dashboard**. FastAPI app at `src/wobblebot/web/`, sibling to `cli/`. Server-rendered Jinja2 + HTMX (no SPA / no build pipeline). Session-cookie auth + bcrypt-hashed-password (single-operator v1). Read-mostly with ADR-013-firewalled mutations: pause/resume/stop buttons create `PendingCommand` rows in `awaiting_confirmation` (same state machine as Discord's ✅/❌); a two-click confirm page transitions to `approved`. **The ADR-002 firewall stays intact** — `cli/live`'s `WHERE status='approved'` poll remains the only path from intent to engine. Kickoff commit ratifies ADR-016 (Web UI architectural commitments) + ADR-017 (auth model) + `docs/planning/stage-7.1-design.md`. Five stages planned: 7.1 Skeleton + auth → 7.2 Cost + status + mutation buttons → 7.3 Advisor + harvester views → 7.4 News + audit logs → 7.5 close. **Six new runtime deps:** fastapi, uvicorn[standard], jinja2, python-multipart, bcrypt, itsdangerous (biggest dep-add since Phase 5's discord.py). Phase 8 (Hardening & v1.0 Release; Stage 8.0 deferred Phase-5-audit refactors [R5 `ports/operator.py` split, R3 storage-fallback helper, R2 generic poll-loop helper] + 8.1 reliability + 8.2 background maintenance worker + 8.3 performance tuning + 8.4 v1.0 soak) follows. Per ADR-003 the Harvester remains the only module with transfer authority; per ADR-004 it uses Kraken's withdrawal API via `ExchangePort` (no separate banking adapter). CryptoCompare 90-day evaluation still due **2026-08-13** per ADR-010 (deferred from Stage 6.5 close — the proper 90-day observation window hasn't elapsed yet).

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
- **Config example:** `config/settings.example.yml` (real `config/settings.yml` is gitignored). Per-CLI sections + grid/safety + advisor + profiles. Operators copy this to `settings.yml` and adjust values; comments and structure stay in sync per the schema-drift tests.
- **Prompt files:** `config/prompts/{quant,risk,news,arbitrator}.md` (committed defaults; operators edit freely). YAML frontmatter + Markdown body; loader in `wobblebot.config.prompts`.
- **Env vars example:** `.env.example` at the repo root (single source of truth — schema-drift tests verify operator `.env` files stay in sync)

## Project-Specific Conventions

- **Python 3.13+ required** (`requires-python = ">=3.13"`). Use `str | None`, `list[X]`, `match` statements — no `Optional`/`List` imports needed.
- **Never use `print()`.** Use the project logger (`wobblebot.config.logging.configure_logging`). Plain format renders message-only; put operator-facing data in the message string and structured fields in the `extra=` dict so JSON consumers see them too.
- **Pydantic v2 models** for structured data (domain entities, config schemas).
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

- **All 10 CLIs handle deprived envs cleanly.** Cycle each CLI
  through: no `.env`, no `config/settings.yml`, no `config/`
  directory at all, missing per-CLI section, empty credentials,
  bad `--config` path, bad `--profile` name. Expected: clean exit
  codes (2 for missing creds / config / section), no raw
  tracebacks. Verification #24 established the baseline 2026-05-15
  for the original 7 (sandbox / status / preflight / live /
  observe / shadow / first_real_trade); cli/apply added at Stage
  3.4b, cli/harvest at Stage 4.2, cli/operator at Stage 5.6 each
  carried their own deprived-env coverage in their slice work.
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
  reality (currently $0.08).

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
