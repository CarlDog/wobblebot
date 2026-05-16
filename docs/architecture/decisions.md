# Architectural Decision Records (ADR)

This file tracks major system decisions.

## ADR-001 — Use Hexagonal Architecture
**Status:** Accepted
**Context:** Need for modularity & testability
**Decision:** Use Ports/Adapters pattern across all modules
**Consequences:** Clean module isolation, easier long-term extensibility

## ADR-002 — LLM Is Advisory-Only
**Status:** Accepted
**Decision:** LLM cannot generate executable commands
**Reason:** Safety and determinism

## ADR-003 — Separate Withdrawals into Harvester Module
**Status:** Accepted
**Context:** Need to keep Kraken trading key safe
**Decision:** Only Harvester key may initiate transfers
**Consequence:** Strong compartmentalization of financial power

## ADR-004 — Use Kraken API for Fund Transfers (No Separate Banking Integration)
**Status:** Accepted
**Date:** November 24, 2025
**Context:** Initial assumption was that Harvester would need to integrate with separate bank APIs (ACH, wire transfer systems) in addition to Kraken. Upon creating a live Kraken account with $100 deposit, discovered that Kraken's API provides withdrawal endpoints that handle bank transfers directly.
**Decision:** Harvester will use Kraken's withdrawal API for all fund transfers (exchange → bank). No separate BankingPort or banking adapter is needed.
**Alternatives Considered:**
- Build separate banking API integration (rejected: unnecessary complexity, YAGNI)
- Abstract BankingPort for future flexibility (rejected: premature abstraction for Phase 1-5)
**Consequences:**
- **Positive:** Simpler architecture, single integration point, less code, easier testing
- **Positive:** Phase 4 implementation is significantly simpler
- **Positive:** Single API key strategy works (Harvester key has withdrawal permissions)
- **Negative:** Tightly coupled to Kraken (acceptable for Phase 1-5, can abstract later if multi-exchange needed)
**Implementation:** Harvester depends only on ExchangePort (with withdrawal-enabled Kraken adapter) and StoragePort

## ADR-005 — Align Domain Models with Kraken API Data Structures
**Status:** Accepted
**Date:** November 24, 2025
**Context:** Phase 1.2 domain model design required decisions on entity IDs (UUID vs string), order status vocabulary, timestamp formats, and field naming conventions. Research into Kraken REST API v0 revealed specific data structure patterns that affect adapter implementation complexity.

**Decision:** Adopt Kraken-aligned data structures in domain models with strategic abstractions:

1. **Dual ID Strategy:**
   - Internal `id: UUID` for database primary keys
   - External `exchange_id: str | None` for Kraken transaction IDs (txid format)

2. **Order Status Values:**
   - Use Kraken's canonical: `"pending"`, `"open"`, `"closed"`, `"canceled"`, `"expired"`
   - Replace `"filled"` → `"closed"` (Kraken's term)
   - Replace `"cancelled"` → `"canceled"` (American spelling matches Kraken)
   - Remove `"failed"` (Kraken doesn't use this; submission failures don't create order records)

3. **Trade Model:**
   - `id: str` (Kraken trade txid, not UUID)
   - `order_id: str` (Kraken parent order txid)
   - `fee: Decimal` (simplified from `Amount` — fee currency is always quote)
   - Add `cost: Decimal` field (Kraken provides this as price × volume)

4. **Timestamp Extensions:**
   - Add `Timestamp.to_unix_seconds()` method for Kraken API format (float seconds)

5. **Position Model:**
   - Defer to Phase 3+ (margin-specific, not needed for spot trading)

**Alternatives Considered:**
- Pure domain-driven design with custom vocabulary (rejected: adapter complexity)
- Abstract exchange interface with factory pattern (rejected: premature for single exchange)

**Consequences:**
- **Positive:** Minimal adapter mapping code, direct API compatibility
- **Positive:** String IDs are industry-standard for crypto exchanges
- **Positive:** Dual ID strategy preserves multi-exchange flexibility
- **Positive:** Clearer semantics (`"closed"` is self-explanatory)
- **Negative:** Domain models favor Kraken conventions (acceptable trade-off for Phase 1-4)
- **Negative:** Breaking change for existing tests (not yet merged, safe to update)

**Compliance:** Aligns with ADR-001 (hexagonal architecture), ADR-003 (safety constraints), and Constraint C-003 (no adapter dependencies in domain).

**References:**
- [Kraken API Reference](../reference/kraken-api-reference.md)
- [Kraken REST API Docs](https://docs.kraken.com/rest/)
- krakenex Python client (reference implementation)

## ADR-006 — Grid Engine Architecture
**Status:** Accepted
**Date:** 2026-05-14
**Context:** Stage 2.2 introduces the first money-touching engine code: a configurable per-coin grid that places limit orders above and below a reference price and rotates buy/sell pairs into realized cycles. Stage 2.2 wires the engine to `MockExchangeAdapter` (paper); Stage 2.3 swaps in `KrakenAdapter` (real money, tiny size). The engine code is identical between the two — the adapter swap is the only delta. Several design choices recur in grid-bot literature with different tradeoffs; pinning them here prevents mid-slice relitigation. Full design discussion: [stage-2.2-design.md](../planning/stage-2.2-design.md).

**Decision:** Adopt the following six policies for the Stage 2.2 grid engine. Keep them stable across Stages 2.2-2.4; revisit only via a follow-on ADR.

1. **Grid re-centering policy: stay parked.**
   If price exits the configured grid window (above the highest sell or below the lowest buy), the engine does not re-center. The grid sits where it was placed and waits for price to return. Emit an "offside" log signal so the operator can intervene; consider an automatic per-coin pause after N ticks offside (deferred to slice 2.2.4).
   *Rejected:* automatic re-centering. The grid is a mean-reversion bet; chasing trend defeats the strategy and converts a controlled losing scenario (idle while offside) into an uncontrolled one (filling fresh buys at the top of a downtrend).

2. **Partial fill handling: leave the remainder open; counter the filled portion.**
   When a grid order partially fills, the unfilled remainder stays as-is on the exchange. The engine places a counter-order sized to the *filled* portion at the next grid level.
   *Rejected:* cancel-and-replace. Introduces a race window (the remainder may fill between cancel and replace), burns extra fees, and works against Kraken's native partial-fill accounting which already does the right thing.

3. **Source of truth for open orders: DB primary, exchange ultimate.**
   The engine reads `GridSlot` state from SQLite each tick and acts on that snapshot. At startup and every N ticks (default N=100, configurable), it reconciles against `ExchangePort.get_open_orders` — orders that exist on the exchange but not in our DB are imported; orders in our DB that no longer exist on the exchange are marked closed and trigger counter-action logic.
   *Reason:* restart resilience requires durable state, and the engine can crash between "send AddOrder" and "persist response." Reconciliation is the only convergent strategy under outages, region failovers, or process restarts.

4. **Order ID strategy: `GridSlot` is a derived view; only `GridState` (the anchor) is persisted.**
   `domain/grid.py` defines:
   ```python
   class GridSlot(BaseModel):
       symbol: Symbol
       side: OrderSide
       level_price: Decimal
       order_id: UUID | None  # None = empty slot, awaiting placement
   ```
   The grid is a *layout*; orders are *transient occupants* of slots. Separating the two lets `step()` reason about "what should exist" without coupling to "what currently exists."

   The only persisted entity is `GridState` (one row per symbol: `reference_price`, `spacing_percentage`, `levels_above`, `levels_below`, `created_at`). Each tick the engine reconstitutes `GridSlot`s by:
   1. `compute_grid_levels(grid_state)` to get the layout, then
   2. querying the existing `orders` table for open orders matching each level's price, to fill in `order_id`.

   *Rejected:* a separate `grid_slot` table with FKs to `orders.id`. Two tables would create FK consistency burden, a second source of truth for "is this slot occupied," and a place where the engine's view could disagree with the order table. Deriving slots from one table eliminates that class of bug, and the `Order` table is already authoritative for order state per ADR-005.

5. **Concurrency model: single asyncio task, per-coin `asyncio.Lock`.**
   For Stage 2.2 a single task steps each coin in turn. Each `Symbol` carries an `asyncio.Lock` so `step()` is re-entrant-safe if Stage 5 hardening later parallelizes per-coin tasks.
   *Rejected:* per-coin parallel tasks now. Adds real-time-ordering bugs that are hard to test deterministically and is unnecessary at the planned 5-second tick rate.

6. **Operational defaults (from the design doc's open questions).**
   - **Tick rate: 5 seconds.** Frequent enough to react to micro-grid moves, well below Kraken's rate limit. Revisit at Stage 2.3 against the live rate-limit budget.
   - **Order TTL: none.** Grid orders sit on the exchange until they fill, are canceled by the operator, or are pruned by reconciliation. Standard grid-bot practice — TTLs trade controlled idle for uncontrolled re-placement churn.
   - **Cycle reporting: log + persist per cycle, no batching.** A "cycle" (matched buy + sell at adjacent levels) is rare enough at micro scale that per-cycle logs are not noisy. Batching would obscure the operator's view of recent activity.

**Alternatives Considered:**
- **Trailing-stop or moving grid.** Folded into decision 1 (rejected). Mean-reversion strategies cannot also be trend-following without becoming a third, worse thing.
- **In-memory-only state with Kraken as truth.** Folded into decision 3 (rejected). Crashes mid-placement leave orphaned exchange state with no engine record.
- **One asyncio.Task per coin from day one.** Folded into decision 5 (rejected for now). Defer to Phase 5 if profiling shows master-task throughput is the bottleneck.

**Consequences:**
- **Positive:** Engine code is fully deterministic for a fixed price walk + initial state, making slice 2.2.5's integration test feasible.
- **Positive:** Stage 2.2 → Stage 2.3 transition is a one-line adapter swap in the wiring layer; the engine itself does not change.
- **Positive:** `GridSlot` separation makes "what cycles have completed" a derived query against `Order`/`Trade` tables — no parallel state machine.
- **Negative:** Stay-parked policy will produce extended idle stretches in trending markets. Acceptable — the operator chose a mean-reversion strategy.
- **Negative:** Reconciliation cadence (N=100) is a tuning knob with no theoretically optimal value. Acceptable — start at 100, surface the metric, adjust based on observed drift.

**Compliance:** Aligns with ADR-001 (hexagonal — engine depends only on `ExchangePort` + `StoragePort`), ADR-005 (Kraken-aligned domain models — `GridSlot.order_id` references the same `Order.id: UUID` already in use). No conflict with ADR-002/003/004; this ADR predates LLM advisory and Harvester involvement and does not constrain them.

**References:**
- [Stage 2.2 Design Doc](../planning/stage-2.2-design.md) — full design discussion, slicing plan, what-is-not-in-scope.

## ADR-007 — Advisor Architecture: Mixture of Experts + News Ingestion
**Status:** Accepted (planned for Phase 3 stages 3.2 - 3.4)
**Date:** 2026-05-14
**Context:** The original Phase 3 design (roadmap pre-2026-05-14) sketched the advisor as a single Ollama LLM consuming engine metrics and producing JSON recommendations. After Phase 2's close, the operator surfaced two scope expansions worth ratifying ahead of code:
1. **Mixture of Experts (MoE):** orchestrate multiple specialist LLMs (different models, different prompts) and aggregate their opinions, rather than a single monolithic advisor.
2. **News ingestion:** add a `NewsPort` so the advisor can consume external news feeds (CryptoPanic, Whale-alert, RSS, etc.) alongside engine metrics.

Both are extensions to the existing `AdvisorPort` contract — they don't change the engine, the safety caps, or any layer below the advisor. The hex architecture's port boundaries make this a composition exercise rather than a refactor.

**Decision:** Adopt the following architecture for Phase 3:

1. **`AdvisorPort` stays unchanged.** Single abstract method that returns a `Recommendation`. Engine remains advisor-implementation-agnostic — the same code path works for single-LLM, MoE, or any future advisor.

2. **MoE adapter is one possible `AdvisorPort` implementation.** `MoEAdvisorAdapter(experts, aggregator)` orchestrates 2-3 specialist LLMs. Each expert has:
   - A distinct base model — chosen for genuinely different training priors. **Mix freely between local Ollama and cloud APIs**: a `LocalOllamaExpert(model="deepseek-r1:7b")` for quant, an `AnthropicClaudeExpert(model="claude-sonnet-4-6")` for risk, a `GoogleGeminiExpert(model="gemini-2.0-flash")` for news. The `Expert` interface abstracts provider; the MoE adapter doesn't care where each expert runs.
   - A specialized system prompt (quant gets only metrics; news gets only news headlines; risk gets caps/balances).
   - A bounded inference budget (no expert reasons forever).
   - Graceful failure handling — if a cloud expert times out or returns an API error, the MoE adapter logs it and proceeds with the remaining experts' opinions. One vendor outage does not stop the advisor.

   Three aggregation strategies supported, all interchangeable behind the `Aggregator` abstraction:
   - **Voting** — discrete-direction proposals; majority wins.
   - **Weighted confidence average** — numeric proposals + self-reported confidence; combine via confidence-weighted mean.
   - **Arbitrator** — a fourth model reads the three opinions and produces the final call. Most expensive, most interpretable.

   **Discipline:** every expert's raw opinion MUST be logged alongside the aggregated recommendation. When the aggregated call surprises the operator, they need to see which expert pushed the bot which way. Black-box MoE without per-expert audit is worse than a single model.

3. **`NewsPort` is a separate, parallel abstraction** alongside `AdvisorPort` — not nested under it. The advisor consumes news; the news source is independent of the advisor implementation. First adapters (Phase 3.2.5):
   - **CryptoPanic API** (free tier, 50 req/min) — aggregated crypto news with sentiment scores baked in.
   - **Whale-alert API** (free tier) — on-chain large-transfer events; useful as a "trouble's coming" leading signal.
   - **RSS pollers** for CoinDesk / The Block / Decrypt — broad coverage, hours latency.

   Polling cadence: 15-30 minutes. Wobblebot's grid is slow-tick (5s); news cycle for trade-relevant signals is hours. Polling fits in tiny fractions of any free-tier rate limit. No streaming/firehose adapters in v1.

   Persisted to a `news_items` SQLite table: `(source, timestamp, headline, body, sentiment_score, mentioned_coins)`. The "news expert" in the MoE reads the last N items as context.

4. **News-derived recommendations never auto-apply.** Auto-tuning (Stage 3.4b) is gated to **metrics-driven** suggestions only — bounded spacing/size adjustments based on volatility / win-rate / drawdown signals. The news expert's input contributes to the aggregated reasoning but cannot drive an auto-applied parameter change. Reason: news LLMs hallucinate, react to noise, and confuse stale rehashes with novel signals. The operator reads news-derived suggestions; the bot doesn't act on them autonomously.

**Alternatives Considered:**

- **Stick with single LLM, no news.** Rejected: less interpretable as the model gets larger; no way to know whether a recommendation came from "the metrics look bad" or "the LLM is having a moment." MoE forces structural separation.
- **Single LLM with news as additional prompt context.** Rejected: indistinguishable opinions ("metrics says X, news says Y, so my answer is Z"). MoE keeps the inputs auditable per-source.
- **Vendor advisor API as the SOLE provider.** Rejected for the same reason Phase 1 chose local Ollama: removes operator independence, exposes data to a third party, fails when the vendor does. **However, vendor APIs are explicitly ALLOWED as individual experts in the MoE** (above) — the upside of mixing genuinely different priors (Anthropic Claude vs OpenAI GPT vs Google Gemini vs local DeepSeek) outweighs the per-call data exposure for a hobby trading bot. Operator chooses per-expert; the adapter abstracts the provider. Caveats acknowledged: each cloud expert sends some market state to its provider, vendor outages reduce the MoE to whichever experts remain reachable, and per-call cost (~$0.01) accumulates if the operator runs the advisor frequently. At advisor cadence (every N hours), pennies per month.
- **Streaming news firehose (Twitter/X, webhooks).** Rejected for v1: complex auth, rate limits, latency-vs-cost tradeoff doesn't justify itself for a 5s-tick grid bot. Polling 15-30 min covers the relevant signal cadence.
- **Auto-apply news-derived suggestions within bounds.** Rejected: news LLMs are too easily fooled by stale or noisy content. Keep the human in the loop for news-driven actions.

**Consequences:**

- **Positive:** Phase 3 becomes meaningfully more interesting. The MoE architecture gives operator-controllable transparency (which expert thinks what). The news ingestion makes the advisor situationally aware of regime changes (regulatory shock, exchange outage, hack) without giving it execution authority over those signals.
- **Positive:** Strengthens the "no LLM has execution authority" invariant by adding a sub-rule: news-derived recommendations are advisory-only even when the auto-tuning flag is on. Two layers of defense instead of one.
- **Positive:** `NewsPort` is reusable beyond the advisor — Phase 5's dashboard could surface "recent news" without going through any LLM.
- **Negative:** Phase 3 scope grows ~50% (extra slicing: 3.2.5, 3.4a, 3.4b instead of just 3.2 and 3.4). Time estimate: 4-6 evenings instead of 2-4.
- **Negative:** Per-expert latency multiplier on the advisor inference path. At advisor cadence (every N hours), this is operationally irrelevant.
- **Negative:** More moving parts to debug. Mitigated by the per-expert audit logging discipline above.

**Compliance:** Aligns with ADR-002 (LLM is advisory-only — MoE doesn't change that; the auto-tuning gate stays) and ADR-001 (hex architecture — `NewsPort` and `AdvisorPort` are abstract; concrete adapters are swappable). No conflict with any existing ADR.

**References:**
- [Phase 2 closing summary](../planning/phase-2-summary.md) — context for Phase 3's entry conditions.
- ADR-002 — LLM is advisory-only (the invariant this ADR refines).
- ADR-008 — Observer & Shadow Mode (the Phase 3 sandbox the MoE advisor will iterate against).

## ADR-008 — Observer & Shadow Mode (Phase 3 Sandbox)
**Status:** Accepted (planned for Phase 3 Stage 3.0)
**Date:** 2026-05-14
**Context:** Phase 3's advisor work (single LLM → MoE → news ingestion → bounded auto-tuning) needs a sandbox to iterate against. Iterating against `cli/live` is too expensive (real fees on every test cycle) and slow (real fills happen at market cadence, not test cadence). Iterating against pure `MockExchangeAdapter` is too synthetic — the engine's behavior against deterministic mock prices doesn't surface the regime shifts and price-action nuance that real markets produce. We need a third option: real market behavior, simulated execution.

The operator also wants a "lurker mode" — observe the market 24/7 without trading, build a dataset, watch the LLM commentate on real events as they unfold. This is broadly useful beyond Phase 3 (e.g. for backtesting, for building intuition about the bot's responses to specific market conditions, for collecting evidence to tune the safety caps).

**Decision:** Land two new entry points and one new adapter as Phase 3 Stage 3.0, before any of the advisor work begins.

1. **`cli/observe`** — pure data collection. Polls Kraken's public Ticker on a configurable interval (default 30s) for one or more symbols; periodically fetches `BalanceEx` via the read-only key. Persists to existing SQLite tables (`prices` would be new; `balance_snapshots` already exists). No engine, no LLM, no orders. Runs until killed via SIGINT. Useful for: building a multi-week price dataset for offline analysis, cheap continuous monitoring, baseline data the advisor's metrics layer can compute against.

2. **`ShadowExchangeAdapter`** — concrete `ExchangePort` implementation that composes a live `KrakenAdapter` (for `get_current_price`) with `MockExchangeAdapter`'s matching engine (for `place_order` / `get_open_orders` / `get_order_status` / `get_trade_history`). Uses a synthetic balance ledger initialized at construction time. Critically:
   - Maker vs taker fee assignment: when `place_order` is called, compare the limit price to current live market price. If the limit sits ON the book (BUY below market or SELL above), tag for maker fee (0.26%). If marketable (BUY above ask or SELL below bid), tag for taker fee (0.40%). This honestly models the fee schedule observed live in Phase 2's $0.08 receipt.
   - Live price ticks pump into the mock matcher; fills happen when the live tape crosses a shadow order's limit, with timestamps recorded at the moment of crossing (not at the next `get_order_status` poll, to keep the simulation honest).
   - Synthetic balance ledger tracks USD/BTC/ETH/etc. exactly as the mock does, but starting from operator-configured initial values (`--initial-shadow-usd 10000`, etc.) rather than real Kraken balances.

3. **`cli/shadow`** — same CLI surface as `cli/live`, but wires the engine to `ShadowExchangeAdapter` instead of `KrakenAdapter`. All other flags (`--symbols`, `--max-runtime-minutes`, `--max-session-loss-usd`, the SafetyConfig caps) work identically. Storage path defaults to `wobblebot-shadow.db` (separate from `wobblebot-live.db`).

4. **Rename `cli/grid` to `cli/live`.** The original name doesn't shout "this is real money." `cli/live` and `cli/shadow` make the distinction loud — muscle memory at 11pm should not be able to trip the operator into trading real funds when they meant to simulate. One-commit rename, all references updated.

5. **Defer Flavor B (`cli/lurker` = observer + advisor commentary, no trading).** Once Stage 3.2 lands the single-LLM advisor, `cli/lurker` becomes a thin wrapper: run `cli/observe`'s polling loop and periodically invoke the advisor against the collected metrics (and news, post-3.2.5). Defer until then because there's nothing to wrap yet.

**Alternatives Considered:**

- **Skip Stage 3.0 entirely; iterate on the advisor against `MockExchangeAdapter` with hand-crafted price scenarios.** Rejected: the mock's deterministic price walks won't surface the regime shifts that make the advisor useful (or break it). The advisor needs real market noise to be worth tuning against.
- **Add a `--shadow` flag to `cli/live` instead of a separate `cli/shadow` command.** Rejected: muscle-memory failure mode. A flag is too easy to forget; a separate command name forces the operator to consciously choose which mode they're invoking. The cost (one extra entry point) is trivial vs the safety upside.
- **Make `ShadowExchangeAdapter` use the operator's REAL Kraken balances as initial shadow balances.** Rejected: too easy to confuse "shadow has $99.92" with "real account has $99.92" in logs, especially when both processes are running side by side. Force operator to specify `--initial-shadow-usd` explicitly.
- **Defer Stage 3.0 to after Phase 3.5.** Rejected: by then the advisor work is done; the sandbox would have been most useful WHILE that work was happening. Land it first.

**Consequences:**

- **Positive:** Phase 3.1-3.5 gets a 24/7 sandbox to iterate against. The MoE advisor can be tested against real market behavior with synthetic execution costs.
- **Positive:** Operator gains a long-running "watch the market" tool independent of any trading work. Builds intuition; collects data.
- **Positive:** `ShadowExchangeAdapter` is a useful addition to the test seam family — integration tests can use it to assert engine behavior against deterministic-but-live-sourced price tapes, going beyond what the existing `MockExchangeAdapter` covers.
- **Positive:** The `cli/grid → cli/live` rename eliminates a real safety footgun (muscle memory at midnight typing `cli/grid` when meaning `cli/shadow`). Tiny up-front cost, permanent benefit.
- **Negative:** Stage 3.0 adds ~1 evening of work before the "real" Phase 3 stages start. Net schedule impact: minimal, since the sandbox saves time later.
- **Negative:** Maker/taker fee modeling in the shadow is approximate — real Kraken accounts can have volume-based fee tiers; the shadow uses fixed rates. Acceptable: at hobby trading volumes the operator stays at the lowest tier indefinitely, so fixed rates match reality.
- **Negative:** Shadow simulation cannot model order-book depth or partial fills due to thin liquidity. Acceptable for $10-$100 order sizes; would matter for larger.

**Compliance:** Aligns with ADR-001 (`ShadowExchangeAdapter` is just another `ExchangePort` impl; engine is unchanged) and the Phase 2 cleanup discipline (the shadow's `cli/shadow` inherits `cli/live`'s SIGINT cleanup + safety caps — same engine code, same finally block). Strengthens the "no LLM execution authority" invariant indirectly: the MoE advisor's auto-tuning behaviors can be validated against shadow traffic before being trusted with live traffic.

**References:**
- ADR-007 — MoE advisor + news ingestion (the Phase 3 work this sandbox supports).
- [Phase 2 closing summary](../planning/phase-2-summary.md) — `cli/grid` is the entry point being renamed to `cli/live` as part of this ADR.

## ADR-009 — Config Consolidation: YAML + Profiles + Prompt Files
**Status:** Accepted (planned for the config audit slated between Stage 3.0 and Stage 3.1)
**Date:** 2026-05-14
**Context:** Slice 2.2.1 built `WobbleBotConfig` + `load_config()` to read `grid` and `safety` sections from YAML. Then Stages 2.3, 2.4, and 3.0 shipped five operator CLIs (`cli/status`, `cli/preflight`, `cli/live`, `cli/shadow`, `cli/observe`) — every one of them takes its config via argparse with hardcoded defaults. **Nothing reads the YAML.** The two layers were built in different sessions and never connected. The operator surfaced this as a real architectural gap during the Stage 3.0 evening session.

Concurrently, ADR-007 introduced the Mixture-of-Experts advisor with provider-pluggable expert configurations (Ollama + cloud LLMs mixed) and per-expert prompt files. ADR-007 was specified at the architecture level; ADR-009 is where the YAML schema, profile system, and prompt-file format land.

**Decision:** Adopt the following config consolidation policy as the audit's deliverable:

1. **`config/settings.example.yml` is the operator-facing API.** Every operator-tunable value lives here with a comment explaining what it does. CLIs read this YAML at startup; argparse flags become *overrides* of YAML values, not the source of defaults.

2. **Per-CLI sections in YAML.** Each CLI has its own top-level section (`live`, `shadow`, `observe`, `validate`, `check`, `simulate`) holding only the knobs that CLI cares about. Engine knobs (`grid`, `safety`) stay shared across all CLIs that use the engine. Per-CLI sections are the most operator-readable structure: when an operator opens `settings.yml` looking for "what does cli/live do?", they find the `live:` block.

3. **Profiles as named overrides.** A top-level `profiles:` block holds named override blocks (e.g. `conservative`, `aggressive`, `cloud-only-moe`). Operator runs `cli/live --profile conservative` to deep-merge that block over the base config before CLI flags apply on top. Layering order: **base config → profile (if any) → CLI flags**. CLI flags always win.

4. **MoE advisor schema.** `advisor.type` is `single` or `moe`; when `moe`, an experts list with **min 3 entries** (Pydantic validator) and **no maximum** (operator can run 5+ experts if they want). Each expert specifies provider (`ollama|anthropic|openai|google`), model, role, prompt_file path, and inference_params. Aggregator is `voting | weighted_confidence | arbitrator`; arbitrator mode has its own dedicated config block. See ADR-007 for the architectural rationale.

5. **Prompt files use YAML frontmatter + Markdown body.** Same pattern as the project's OC memory files. Frontmatter holds structured metadata (output_schema, inference_params); body holds the prose system prompt. Files live at `config/prompts/{quant,risk,news,arbitrator}.md` shipped committed (operator edits in place; no `.example` variant for prompts since the operator's edits ARE the prompts they want to use).

6. **`python-frontmatter` dependency.** Mature (10+ years), MIT licensed, ~3KB. Handles only what it says on the tin. Acceptable per operator approval; alternative (rolling our own ~20 lines) rejected as not worth the maintenance.

7. **Schema-drift detection lives in tests.** `tests/config/test_schema_drift.py` checks that, IF the operator's local `settings.yml` exists, its key set + ordering matches `settings.example.yml`. Same for `.env` vs `.env.example`. **Initial strictness: medium** (keys + ordering, not comment parity); a `--strict` flag in the test enables full structural comparison and is available to flip if drift starts wandering. Comment parity is on the operator (and me) per the standing rule in feedback memory.

8. **`docker/env.example` → repo root `.env.example`.** More conventional location, easier for new contributors to find, single source of truth. Refresh content to match Phase 2.3 reality (`KRAKEN_TRADE_API_KEY`, drop stale `LLM_PROVIDER` / `OPENAI_API_KEY` placeholders).

**Alternatives Considered:**
- **(YAML sections) Shared "runtime" + per-CLI overrides instead of one section per CLI.** Rejected: less operator-readable. When an operator opens settings.yml looking for cli/live's tick rate, they want to find it in one place, not "well, the default tick rate is in `runtime`, but `live` overrides it sometimes."
- **(YAML sections) Flat top-level by concern (engine, storage, logging, live, shadow).** Rejected: more verbose, more cognitive load. Per-CLI is the operator-readable structure.
- **(Prompt format) Plain text or YAML-only files.** Rejected: plain text loses structured metadata; pure YAML makes prose editing miserable. Hybrid wins.
- **(Prompt format) Jinja2 templates.** Deferred to v2 if needed. Frontmatter pattern doesn't preclude adding Jinja2 substitution later.
- **(Profiles) Skip profiles entirely; let operators maintain multiple settings.yml files.** Rejected: operator workflow is harder; can't override one section while keeping others. Profiles are 30 extra lines of YAML and a small merge helper; worth it.
- **(MoE size) Cap at fixed N experts.** Rejected: operator preference is "min 3, no max." We trust operators to make their own choices about how many models to query per advisor cycle.
- **(Drift strictness) Default-strict with comment parity enforced mechanically.** Rejected: operator-hostile (whitespace tweaks fail tests). Medium default; strict toggle available.
- **(Dependency) Roll our own frontmatter parser.** Rejected per operator approval — `python-frontmatter` is small, mature, and saves the 20 lines of edge-case handling we'd rewrite.

**Consequences:**

- **Positive:** Five (six post-rename, if the simulate/check/validate naming proposal lands) CLIs share one config story. New operators read `settings.example.yml` once and understand the surface.
- **Positive:** Adding new CLI knobs becomes an example-file edit + a Pydantic field, not a per-CLI argparse change. Less opportunity to forget the YAML side.
- **Positive:** Profiles let operators flip between regimes without editing YAML each session.
- **Positive:** MoE config schema is operator-tunable, so swapping in cloud LLMs (Claude + GPT + Gemini for the "watch them argue" experiment) is a YAML change, not code.
- **Positive:** Prompt files are version-controllable — every prompt edit is a clean line-level git diff.
- **Positive:** Drift tests catch the "I edited one file but not the other" failure mode that the operator explicitly flagged as recurring.
- **Negative:** Audit is ~2-3 evenings of work touching every CLI. Measured against the cost of carrying the inconsistency forward (advisor work in Phase 3.2-3.4 would have to fight the same hardcoded-defaults pattern), worth the up-front pay.
- **Negative:** Profile deep-merge introduces edge cases (lists override vs append?). Decision: lists override entirely. Operator who wants to add experts to a profile re-lists all of them.
- **Negative:** `python-frontmatter` becomes a runtime dependency. Tiny.

**Compliance:** Aligns with ADR-001 (the YAML loader extends `WobbleBotConfig` which lives in `config/`; CLIs in `cli/` consume it; clean layer separation). Aligns with ADR-007 (advisor schema implements the MoE architecture decided there). Refines ADR-008 (cli/live/shadow/observe were named per ADR-008; this audit consolidates their config story).

**References:**
- ADR-007 — Advisor architecture (the schema this ADR encodes).
- ADR-008 — Observer + Shadow Mode (the CLIs this ADR audits).
- `feedback_keep_example_files_in_sync.md` — the operator's standing rule motivating the drift detection tests.

## ADR-010 — News Source Pivot: Paid (CryptoPanic + Whale-alert) → Free (RSS + CryptoCompare)
**Status:** Accepted
**Date:** 2026-05-15

**Context:** ADR-007's Mixture-of-Experts advisor architecture named CryptoPanic and Whale-alert as the news-ingestion sources for the dedicated news-role expert. Between ADR-007 (early 2026) and Stage 3.2.5 implementation (2026-05-15), both providers shifted to paid-only tiers:

- **CryptoPanic Pro:** ~$210/month (~$2,520/year). Was free at ADR-007 write time.
- **Whale-alert:** ~$25/month (~$300/year). Was free at ADR-007 write time.

For a hobby trading bot the operator has spent $0.08 of real money on across an entire phase, $2,820/year in news-data subscription is structurally wrong.

**Decision:** Pivot the Stage 3.2.5 news sources to free alternatives without changing the architecture:

1. **`NewsPort` stays abstract.** ADR-007's port definition is unchanged. The paid sources can plug in later as additional `NewsPort` implementations if the operator ever decides the marginal data is worth the cost.
2. **`RssNewsAdapter`** (one instance per feed) covers the news headline surface. Default feeds: CoinDesk, Decrypt, The Block (enabled by default in `settings.example.yml`); CoinTelegraph available but disabled (noisier signal). Library: `feedparser` (battle-tested, MIT, 15+ years).
3. **`CryptoCompareAdapter`** for cross-aggregator coverage. `/data/v2/news/` endpoint is free with an API key. `sentiment_score=None` is set explicitly — CryptoCompare's upvotes/downvotes aren't a reliable sentiment signal (verified empirically; the news expert derives tone from body text). Mentioned-coins from the structured `categories` field.
4. **90-day evaluation queued (2026-08-13).** CryptoCompare's source coverage overlaps substantially with RSS; re-evaluate whether the additional aggregation earns its place vs. just running more RSS feeds.

**Alternatives Considered:**
- **Pay for CryptoPanic + Whale-alert as originally specified.** Rejected: subscription cost out of proportion with project scope. The whale-watching signal Whale-alert provided was specifically called out in ADR-007, but at the actual real-money risk Phase 3 carries ($0.08 across the entire phase, advisor never executes per ADR-002), it's a poor cost-value trade.
- **Free tier of CryptoPanic only** (no Whale-alert). Rejected: CryptoPanic's free tier is now read-rate-limited to a level that doesn't sustain the advisor's polling cadence. Effectively no free option.
- **Roll our own scraper against each source's web UI.** Rejected: brittle, ToS-violating, ongoing maintenance.
- **Skip news ingestion entirely** until/unless the operator pays for it. Rejected: ADR-007's MoE architecture has the news expert as one of three; deferring breaks the architectural completeness for Stage 3.4a.

**Consequences:**
- **Positive:** Phase 3 ships with zero recurring cost beyond Ollama running locally (free).
- **Positive:** RSS+CryptoCompare deliver headline coverage and approximate mentioned-coin extraction; the news expert's job is "is the headline tone risk-on or risk-off?" — which doesn't need whale-alert-style on-chain flow signals to function. The aggregated MoE confidence catches the case where news disagrees with quant/risk anyway.
- **Positive:** Keeping `NewsPort` abstract preserves the upgrade path — adding `CryptoPanicAdapter` later is a single-file add.
- **Negative:** The whale-flow signal ADR-007 specifically valued isn't covered. We lose visibility into large-wallet movements that often precede major price action.
- **Negative:** RSS feeds carry latency (some publish 15-30 min after the source event) and dedup overhead (multiple outlets covering the same story). Mitigated by `news_items.UNIQUE(source, external_id)` and the advisor consuming a narrowed `NewsItemSummary` view (drops body, fetched_at).
- **Negative:** CryptoCompare adds a third-party API key the operator manages. Mitigated by the existing `.env.example` documentation pattern.

**Compliance:** Refines ADR-007 (same MoE architecture, different concrete news sources). No conflict with ADR-002 (advisor still cannot execute) or ADR-001 (NewsPort lives in `ports/`, adapters in `adapters/`, layer separation intact).

**References:**
- ADR-007 — original Mixture-of-Experts architecture naming CryptoPanic + Whale-alert.
- `tests/adapters/test_rss_news.py` and `tests/adapters/test_cryptocompare_news.py` for adapter contract tests.
- `config/settings.example.yml` `news:` section for the live default feed list.


## ADR-011 — MoE Composition Without an Expert Abstract Base Class
**Status:** Accepted
**Date:** 2026-05-15

**Context:** During Stage 3.4a implementation, the initial sketch introduced a new `Expert` ABC in `ports/expert.py` to formalize "things the MoE adapter can compose." The intent was to give `MoEAdvisorAdapter` a typed list of expert instances distinct from raw `AdvisorPort` instances.

The operator pushed back: "remind me again why we need ollamaexpert?" — recognizing premature abstraction. `AdvisorPort` already defines `get_recommendation(summary) -> AdvisorRecommendation`, which is exactly what the MoE adapter needs from each contributor. A separate `Expert` interface would have been a re-statement of the same contract under a new name.

**Decision:** Compose MoE experts directly as `AdvisorPort` instances. No `Expert` ABC.

1. **`MoEExpertEntry` is a frozen dataclass**, not a new port. It wraps `(name, role, advisor: AdvisorPort)` to carry the operator-chosen identifier (used in audit logs) and the canonical role (used by the auto-apply news-blocking rule) alongside the `AdvisorPort` instance.
2. **`MoEAdvisorAdapter` accepts `list[MoEExpertEntry]`** and dispatches each entry's `advisor.get_recommendation(summary)` via `asyncio.gather`. No `isinstance(expert, Expert)` check anywhere.
3. **`OllamaAdapter` plugs in directly.** Future cloud adapters (AnthropicAdapter, OpenAIAdapter, GoogleAdapter) will plug in the same way — they implement `AdvisorPort`, period.
4. **Arbitrator support uses a Protocol, not the ABC.** The arbitrator path needs `extra_context` injection beyond the base port; `services/aggregators.ArbitratorAdvisor` Protocol formalizes the structural type (any class with the right method shape satisfies it). OllamaAdapter gained an `extra_context: str = ""` kwarg keeping the base `AdvisorPort` interface unchanged.

**Alternatives Considered:**
- **Define an `Expert` ABC** matching `AdvisorPort` exactly. Rejected: same contract under a different name; pure abstraction churn.
- **Define an `Expert` ABC extending `AdvisorPort`** with `name` and `role` as abstract properties. Rejected: forces every adapter to grow getter properties for fields that are operator config, not adapter state. `MoEExpertEntry` carrying that data alongside the adapter is the simpler shape.
- **Make `extra_context` a method on `AdvisorPort`** so arbitrators can call it uniformly. Rejected: pollutes the single-LLM path which never needs the extra context; threads a meaningless kwarg through `cli/advise`. The structural Protocol is the right tool for "this concrete adapter has an extra capability."

**Consequences:**
- **Positive:** Less code, less indirection. New cloud adapter is "implement `AdvisorPort`" — no second interface to satisfy.
- **Positive:** Single-LLM (`cli/advise --profile <single>`) and MoE (`cli/advise --profile moe-advisor`) paths share the exact same `AdvisorPort` contract; `cli/advise._build_advisor` dispatches on `advisor.type` and returns one of two concrete shapes both typed as `AdvisorPort`.
- **Positive:** Tests stay light — no `MockExpert` separate from `MockAdvisor`.
- **Negative:** `MoEAdvisorAdapter` cannot statically guarantee that the wrapped `AdvisorPort` it receives is "real" (any AdvisorPort works, including a no-op stub). Operator-side validation in `AdvisorConfig` is the safety net (3-expert minimum, name uniqueness).
- **Negative:** Adding an MoE-only behavior to every adapter (e.g. "report your own model name") would now require an extension Protocol per behavior. Acceptable cost given how rarely such a feature is needed.

**Compliance:** Aligns with ADR-001 (no new layer; `MoEExpertEntry` is a service-layer dataclass; `MoEAdvisorAdapter` is in `adapters/`). Aligns with ADR-007 (same MoE architecture).

**References:**
- `src/wobblebot/adapters/moe_advisor.py` — implementation.
- `src/wobblebot/services/aggregators.py` — `ArbitratorAdvisor` Protocol pattern.
- The operator's literal pushback during the Stage 3.4a session.


## ADR-012 — Auto-Apply via `cli/apply --commit`, Not a Hot-Tune Daemon
**Status:** Accepted
**Date:** 2026-05-15

**Context:** Stage 3.4b implements bounded auto-tuning of the running grid config based on advisor suggestions. The question: which process applies the change?

Three plausible designs surfaced during the planning:

1. **`cli/advise` rewrites `settings.yml`** when a suggestion clears its own gate. Bundles emit + apply into one daemon.
2. **`cli/live` polls `advisor_suggestions` mid-run** on a new `schedules.auto_apply` cadence, hot-updates the in-memory `GridConfig`. Faster feedback loop.
3. **New `cli/apply` one-shot tool** gates the latest suggestion, prints a unified diff (dry-run), and with `--commit` rewrites `settings.yml` + persists an `AppliedSuggestion` audit row. Operator restarts `cli/live` to pick up the change.

The operator (and ADR-002 + ADR-007's "advisor is advisory-only" stance) clearly favored the most conservative, operator-in-the-loop posture available.

**Decision:** Build `cli/apply` as option 3.

1. **The `cli/apply` CLI is the only path by which advisor output mutates running config.** No daemon does it autonomously.
2. **Dry-run is the default.** `cli/apply` (no flag) reads the latest `AdvisorSuggestion`, runs it through the gate, and prints APPLIED / REJECTED with reasons. No file writes, no DB writes.
3. **`--commit` is opt-in.** Rewrites `settings.yml` via `ruamel.yaml` (atomic .tmp + rename, comment + numeric-style preservation) and persists an `AppliedSuggestion` audit row to `advise.db`. Stdouts the unified diff.
4. **`AutoApplyConfig.enabled=False` by default.** Even with `--commit`, the gate refuses every key unless the operator explicitly enables auto-apply in `settings.yml`. Defaults-off is the load-bearing safety property.
5. **`cli/live` is restarted by the operator** to pick up the new config. The engine does not poll for config changes mid-session.

**Alternatives Considered:**
- **Option 1: `cli/advise` rewrites settings.yml.** Rejected: bundles two concerns (think + apply) into the same daemon; if the apply path has a bug, the advisor daemon takes it down. Separation of concerns matters more here than convenience.
- **Option 2: hot-tune daemon polling advisor_suggestions.** Rejected: introduces shared-state coupling between `cli/advise` (writer) and `cli/live` (reader); complicates the engine's idempotency story; mid-run config changes confuse the audit trail ("which spacing was in effect at trade T?"). The operator-in-the-loop posture from ADR-002 + ADR-007 prefers explicit restart over silent in-flight adjustment.
- **No auto-apply at all** (operator hand-edits settings.yml after reading suggestions). Rejected: defeats the point of having the gate. The audit row + diff preview deliver the same operator visibility with less manual error potential.
- **`cli/apply` with a `--watch` mode** continuously applying suggestions as they arrive. Deferred: same problems as option 2 once you turn it on; can be added later as a wrapper script if the operator ever wants it.

**Consequences:**
- **Positive:** Sharp boundary between "produce recommendation" (cli/advise) and "apply recommendation" (cli/apply). One can fail without taking down the other.
- **Positive:** Operator always sees the diff before --commit. No silent config drift.
- **Positive:** `AppliedSuggestion` audit table provides full forensic history — operator can always answer "what changed when based on which model's recommendation?"
- **Positive:** News-role blanket-rejection (per ADR-007) is implemented inside the gate, so it applies to every code path that uses `evaluate_auto_apply` — not just `cli/apply`.
- **Negative:** Requires `cli/live` restart to pick up the change. For a long-running production deploy this is a real cost; for a hobby bot the operator is hands-on anyway.
- **Negative:** Two daemons (`cli/advise` writes suggestions; `cli/apply` reads them) plus the live engine adds operational complexity vs a single all-in-one daemon. Mitigated by the per-CLI `--help` + the shared `settings.yml` config surface.
- **Negative:** New `ruamel.yaml` runtime dep (PyYAML loses comments on round-trip; non-starter for the operator's heavily-commented file).

**Compliance:** Aligns with ADR-001 (cli/apply is an entry point; gate is in `services/`; rewriter is in `services/`; clean layer separation). Aligns with ADR-002 (advisor never executes; cli/apply is operator-triggered). Aligns with ADR-007 (news-role-never-auto-applies enforced in the gate).

**References:**
- `src/wobblebot/cli/apply.py` — the CLI.
- `src/wobblebot/services/auto_apply.py` — the gate.
- `src/wobblebot/services/settings_rewriter.py` — the ruamel.yaml-backed rewriter.
- `src/wobblebot/ports/advisor.py::AppliedSuggestion` — audit row schema.

## ADR-013 — Operator Interaction Engine (Discord + Conversational LLM)
**Status:** Accepted (planned for Phase 5)
**Date:** 2026-05-16

**Context:** Phase 4 closed with the engine running unattended but blind to the operator's day. Outbound visibility is "tail the logs"; inbound control is "SIGINT, edit settings.yml, restart cli/live." The original Phase 5 roadmap addressed this with two adjacent stages — 5.1.5 outbound webhook notifier (one-evening scope) and 5.2 structured slash-command control surface — both narrow.

Mid-kickoff the operator surfaced a broader vision: Discord isn't just a notifier — it's an interaction engine. The operator wants to converse with the bot (multi-turn, context-aware), get status answers in natural language, and issue commands by talking rather than memorizing slash-command syntax. This is a fundamentally different architectural commitment than "post embeds to a webhook," and it exposes a tension with ADR-002 (LLM is advisory-only; never executes): the LLM's role here is intent parsing, not execution, and the bridge from intent to action must keep the human in the loop.

Per operator decision 2026-05-16, the Operator Interaction Engine becomes the whole of Phase 5. The displaced stages reorganize into three downstream phases: **Phase 6 — Cloud LLM Integration** (cloud assistant + cloud advisor adapters, the long-standing `_build_advisor` placeholders, cost tracking, provider selection); **Phase 7 — Web UI / Dashboard** (FastAPI app at `src/wobblebot/web/`, balance / PnL / cycle / advisor / harvester surfaces); **Phase 8 — Hardening & v1.0 Release** (Reliability & Recovery, Background Maintenance Worker, Performance Tuning, soak test, v1.0 tag). This ADR ratifies the Phase 5 architecture; per-stage design docs (starting with `stage-5.1-design.md`) handle the slicing.

**Decision:** Adopt the following architectural commitments for Phase 5. Stable across stages 5.1–5.7; revisit only via a follow-on ADR.

1. **Two new ports, layered.**
   - **`OperatorPort`** — engine-side. Exposes the engine ops the operator can request (`pause_symbol`, `resume_symbol`, `cancel_all`, plus read-only query handlers for status / pnl / open orders / recent fills / last advisor suggestion / current harvester proposal). Lives in `ports/operator.py`. `cli/live` (and `cli/harvest`) consume this port via constructor DI.
   - **`AssistantPort`** — LLM-side. Converts a `ConversationContext` (operator's latest message + recent turn history + engine state snapshot) into a strictly-typed `OperatorIntent`. Lives in `ports/assistant.py`. Consumed by `cli/operator`.

2. **`OperatorIntent` is a strict typed sum, not free-form text.**
   `OperatorIntent = Command | Query | Conversational | Unparseable`. The LLM emits one as JSON; Pydantic discriminator validation runs before any downstream code touches it. Same wire-format discipline as `AdvisorRecommendation`.
   - `Command(name, args)` — state-mutating ops (pause, resume, cancel-all). **Always routes through confirm-before-execute.**
   - `Query(name, args)` — read-only ops (status, fills, suggestions). Executes immediately; no confirm.
   - `Conversational(reply_text)` — chat that doesn't resolve to an action ("thanks", "what can you do?"). Bot replies; engine untouched.
   - `Unparseable(reason)` — LLM's "I don't understand" signal. Bot asks for clarification.

   Each concrete `Command` and `Query` is its own Pydantic model with typed args (e.g. `PauseCommand(symbol: Symbol)`, `RecentFillsQuery(symbol: Symbol | None, lookback_hours: int)`). The exact catalog lands in `stage-5.1-design.md`; this ADR only pins the shape.

3. **ADR-002 preservation: confirm-before-execute is the firewall.** Every `Command` intent persists to `pending_commands` with status `awaiting_confirmation`. The bot posts a confirmation embed in Discord with ✅ / ❌ reaction buttons summarizing the parsed command. Only on ✅ does status transition to `approved`, after which `cli/live` polls the table on its tick and dispatches. ❌ marks `rejected`; inactivity past `confirm_ttl_seconds` (default 300) marks `expired`. **The LLM never reaches a code path that mutates engine state without an operator click.** This is the load-bearing safety property that keeps ADR-002 intact under the conversational layer.

4. **DB-mediated decoupling between `cli/operator` and `cli/live`.** A new `operator.db` carries three new tables:
   - `pending_commands` — `cli/operator` writes; `cli/live` polls approved rows; `cli/live` updates status to `dispatched` after execution.
   - `notifications` — `cli/live` and `cli/harvest` write outbound events; `cli/operator` reads and forwards to Discord.
   - `conversation_turns` — `cli/operator` stores chat history per `(channel_id, user_id)` for multi-turn context.

   Both daemons run independently. cli/operator down → cli/live keeps trading; notifications queue in the table (forwarded once cli/operator returns). cli/live down → operator can still chat (commands queue with status `awaiting_dispatch`; queries that need live state return graceful errors). Same shape as `cli/advise` ↔ `cli/apply` via `advise.db`.

5. **Multi-turn conversation state; pronoun resolution by prompt context.** The assistant receives the last N turns (default 10, configurable per `OperatorConfig.context_window_turns`) plus the current engine-state snapshot in each prompt. "Show me yesterday's fills" → "now filter to BTC" works because turn 2's prompt includes turn 1 verbatim. **No symbolic dereferencing of pronouns in code** — the LLM handles context resolution. Active-context TTL: 30 minutes of inactivity per `(channel_id, user_id)` resets the prompt window to "fresh conversation." Older turns remain persisted for forensic audit but stop being fed into prompts.

6. **User + channel allowlist authorization.** Inbound messages drop unless `(user_id, channel_id)` matches the allowlist in `OperatorConfig.authorization`. Channel IDs in `settings.yml` (not secret). User IDs in env (`DISCORD_OPERATOR_USER_IDS`, csv) — keeping user identities out of the committed config. No per-command role/permission system in v1; any allowlisted user has full authority. Defer per-command roles to a later phase if multi-user collaboration ever becomes a thing.

7. **Pluggable LLM provider via `AssistantPort`; Phase 5 ships Ollama-only.** v1 ships `OllamaAssistantAdapter` (reuses existing Ollama infrastructure). The port is provider-agnostic by construction so **Phase 6** can add cloud assistant adapters (anthropic / openai / google) alongside the corresponding cloud trading-advisor adapters in one cohesive integration phase. Provider is selectable per the same `provider:` config pattern as the trading advisor. **The assistant role is separate from MoE trading roles** — different prompt (`config/prompts/operator.md`), different model choices appropriate to the conversational task vs the analytical-recommendation task.

8. **Discord library: `discord.py`.** Battle-tested, MIT-licensed, primary maintained Python Discord library. The Gateway is the only realistic transport for inbound messages (webhooks are POST-only by protocol). Lock to a stable major version in `pyproject.toml`; Discord API changes do periodically break this lib, so dependabot will surface those.

9. **Outbound notifications use the same DB-mediated pipe.** `cli/live` and `cli/harvest` call a `NotifierPort` impl (`SqliteNotifierAdapter`) that writes to `notifications`. `cli/operator`'s notification forwarder reads new rows and posts to Discord. **`cli/live` is Discord-ignorant** — does not import `discord.py`, has no awareness of bot tokens or channels or embeds. The original Stage 5.1.5 outbound scope is now naturally part of the bidirectional system but architecturally subordinate to the same decoupling rule.

10. **The conversational LLM is not in the money path.** It can crash, hallucinate, get confused, time out, or refuse to respond — none of which can affect trading decisions. Worst case from a misbehaved assistant: bot says weird things in Discord; operator ignores or rephrases. The confirm-before-execute gate + the structured-output contract + the DB-mediated decoupling make this a hard guarantee, not a hope.

**Alternatives Considered:**
- **Outbound webhook + structured slash commands (original 5.1.5 + 5.2 split).** Rejected: the operator explicitly wants conversational interaction, not memorized command syntax. Slash commands serve when the operator already knows what they want; conversation serves when the operator is exploring or doesn't remember the exact arg shape. The structured-command surface remains useful as a power-user fast path — could be added as a complement later, but is not v1 scope.
- **Single supervisor process running both Discord client and engine.** Rejected: violates the daemons-per-CLI pattern established in Phase 3-4. A `discord.py` disconnect or Gateway bug inside the engine process is the failure mode this ADR exists to prevent.
- **LLM directly executes parsed commands (skip confirm).** Rejected: violates ADR-002. The confirm step is the firewall, full stop.
- **Free-form LLM output without structured-intent contract.** Rejected: string-parsing the LLM's response is brittle, hard to test, and every prompt-engineering tweak risks breaking the parser. Structured JSON output is the same pattern that works for the trading advisor (ADR-007).
- **Discord webhooks only (no Gateway bot).** Rejected: webhooks are outbound-only by protocol. Conversational requires inbound message reception, which requires Gateway.
- **In-process asyncio queues for cli/operator ↔ cli/live IPC.** Rejected: queues don't survive crashes, can't span processes, and force coupling. Same reasoning that drove the `cli/advise` ↔ `cli/apply` separation via `advise.db`.
- **Conversation state in OpenChronicle (OC) memory.** Considered. Decided against for v1: `operator.db` is the cheap project-local choice; OC is for cross-session synthesized memories, not raw per-turn chat history. Could route a synthesized OC memory ("operator's most-issued commands", "patterns of operator concern") later if useful, but raw turns belong in the project DB.
- **Per-command role-based authorization** (e.g. `/pause` for any allowlisted user, `/cancel-all` admin only). Deferred: v1 is single-operator; the complexity isn't justified yet. If multi-operator collaboration ever becomes real, this is a clean follow-on.
- **Voice transport** (the operator talks to a Discord voice channel; STT → assistant → TTS → reply). Out of scope; mentioned only to retire the question.

**Consequences:**
- **Positive:** Bidirectional bot delivers operator UX that flat-out doesn't exist today — status answers in natural language, conversation-resolved follow-ups, commands without memorized arg syntax, async visibility while away from the machine.
- **Positive:** ADR-002 stays intact under a much richer LLM interaction surface. Every state mutation is human-clicked. The audit trail (`pending_commands`) is forensically complete.
- **Positive:** DB-mediated decoupling means `cli/operator` is developable, deployable, restartable, even temporarily killable without touching `cli/live`. Same operational story as the rest of the daemon-per-CLI fleet.
- **Positive:** Engine code stays Discord-ignorant. New trading features don't have to know about chat transport; new chat features don't have to know about trading internals.
- **Positive:** Pluggable LLM provider — operator picks free-local (Ollama, default) or cloud (when those adapters land in 5.6) without code changes. Same pattern operators are already used to from the trading advisor.
- **Positive:** Conversational state and outbound notifications both forensically auditable via `operator.db`.
- **Negative:** Adds `discord.py` as a new runtime dep. Pin to a stable major version; Discord's API changes do break this lib periodically. Dependabot will surface required bumps.
- **Negative:** Adds another long-running daemon to operate (`cli/operator`). Mitigated by Phase 6's maintenance worker (process supervision is part of that stage's scope).
- **Negative:** Three new SQLite tables and a new `operator.db` file. Schema lifecycle now needs cross-DB coordination (`price_snapshots` in `observe.db`; `advisor_suggestions` in `advise.db`; `transfer_*` in `harvest.db`; operator tables in `operator.db`). Documented in `stage-5.1-design.md`.
- **Negative:** New secrets: `DISCORD_BOT_TOKEN`, `DISCORD_OPERATOR_USER_IDS` (csv of operator user IDs). Bot token is of similar sensitivity to a Kraken API key. Standard `.env` + gitignore + `.env.example` placeholder pattern; the bot token is the bot's identity — leak it and someone else can impersonate the bot.
- **Negative:** LLM latency on the conversational path. Ollama at ~3–30s per turn depending on model; can feel slow in chat. Mitigated by Discord typing-indicator while the bot thinks, and by streaming responses where the library supports it.
- **Negative:** Multi-turn prompts grow in length, potentially hitting model context limits. v1 caps at 10 turns; tunable. Past the cap, oldest turn drops out of the prompt window (still persisted for audit).
- **Negative:** Confirm-button drift — operator could lose track of which pending command they're approving if multiple are in flight. Mitigated by including the parsed command summary in the confirmation embed and by per-message confirm IDs.
- **Negative:** Discord platform dependency. If Discord is down, both inbound and outbound disappear; cli/live keeps trading. Acceptable for a hobby bot; flagged so it's not a surprise.

**Compliance:** Aligns with ADR-001 (new ports in `ports/`, adapters in `adapters/`, services in `services/`, CLI in `cli/`; layer discipline intact). Aligns with ADR-002 (LLM never executes — confirm-before-execute is the firewall; the conversational layer is intent parsing, not execution). Aligns with the daemon-per-CLI pattern established by Phase 3 (`cli/advise`, `cli/apply`, `cli/news`) and Phase 4 (`cli/harvest`). Aligns with the structured-JSON-output contract from ADR-007 and ADR-012 (advisor and apply gate both work with strictly-typed wire formats; operator intent extends the pattern). **Refines ADR-007** by introducing a second LLM role (operator assistant) alongside the trading-advisor roles (quant/risk/news/arbitrator) — same pluggable-provider machinery, different prompt, separate config block.

**References:**
- `docs/planning/stage-5.1-design.md` — first stage slicing (Domain & Ports; lands in same kickoff commit as this ADR).
- `docs/planning/roadmap.md` — Phase 5 stage list (5.1 Domain & Ports → 5.7 Integration Check), Phase 6 provisional.
- ADR-001 — Hexagonal architecture (the layer rules the new ports live under).
- ADR-002 — LLM is advisory-only (the firewall this ADR preserves under a richer interaction surface).
- ADR-007 — MoE advisor + news ingestion (precedent for pluggable LLM providers and structured-output contracts).
- ADR-012 — Operator-driven auto-apply gate (precedent for "LLM proposes, operator clicks").
- `discord.py` documentation: https://discordpy.readthedocs.io/ (Gateway client + Intents + reactions API).
