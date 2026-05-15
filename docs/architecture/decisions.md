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
