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

**Amendment (2026-06-05 — structural news firewall; makes the news sub-rule enforced, not just stated):**
This ADR's news sub-rule ("news-derived recommendations are advisory-only even when
auto-tuning is on") was only ever enforced for a *standalone* news opinion: the Stage 3.4b
gate blocks `role == "news"` (`auto_apply.py::_BLOCKED_ROLES`). But the MoE arbitrator's
output is force-tagged `role == "aggregated"` (`moe_advisor.py:161`), which is **not**
blocked — so a news-driven *number* folded into the reconciled `recommendations` dict can
auto-apply, violating this ADR's stated intent. Inert today (`auto_apply.enabled = false`);
real once auto-apply is on. The 2026-06-04 MoE prompt review shipped a *prompt* mitigation
(`arbitrator.md` Rule 2: reconciled numbers must be justifiable from quant + risk alone),
but that trusts the arbitrator LLM — this ADR always intended the firewall to be
**structural**. Ratified fix (kept as an ADR-007 amendment, **not** a new ADR — the
decision already exists; this only makes it enforced): `MoEAdvisorAdapter` inspects the
per-expert `expert_opinions` provenance it already carries and tags the aggregated
suggestion with whether news *materially drove* the reconciled value; the gate blocks (or
at minimum flags) an aggregated suggestion so tagged. The prompt mitigation stays as
defense in depth. Tracked as P1 (`docs/release/v1.1/README.md`, the news-firewall row);
own test reproducing a news-driven aggregated number the structural gate must refuse.

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

8. **`docker/env.example` → repo root `.env.example`.** More conventional location, easier for new contributors to find, single source of truth. Refresh content to match Phase 2.3 reality (`KRAKEN_TRADER_API_KEY`, drop stale `LLM_PROVIDER` / `OPENAI_API_KEY` placeholders).

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

## ADR-014 — LLM Cost Caps
**Status:** Accepted (planned for Phase 6)
**Date:** 2026-05-17

**Context:** Phase 6 introduces cloud LLM providers (Anthropic, OpenAI, Google) for both the operator-assistant role (added in Phase 5) and the MoE trading-advisor roles (Phase 3 placeholder slots in `_build_advisor`). Unlike Ollama (free / local), cloud APIs charge per request — typically a fraction of a cent per turn, occasionally several cents on long thinking-mode responses. A stuck retry loop, an unbounded conversation context, a misconfigured polling cadence, or an over-eager MoE expert lineup can burn $10s–$100s of dollars in hours without anyone noticing until the provider's monthly invoice lands. The project's safety design has a strong precedent for bounding monetary blast radius (ADR-003 separates withdrawal authority; Phase 2's `SafetyConfig` caps trading exposure; Phase 4's `HarvesterConfig` caps daily withdrawal). The same discipline should apply before the first cloud-LLM API call goes out.

The existing real-money cost ledger is **$0.08** (one tiny Kraken round-trip during Phase 2's first-trade test). Phase 6 is the first project component that incurs ongoing per-use cost. The bookkeeping pattern (forensic SQLite table + structured logging + operator inspection tool) carries over cleanly from the trading and transfer history tables.

**Decision:** Adopt the following commitments for Phase 6's cloud-LLM cost discipline. Land in Stage 6.1; stable across 6.2–6.5; revisit only via follow-on ADR.

1. **USD-denominated caps, not token caps.** The operator thinks in dollars; provider pricing changes; token costs vary by model. Two caps:
   - `LLMCostConfig.max_spend_per_day_usd` (default **$1.00**). Sliding 24-hour window, computed at gate-check time from the `llm_calls` table.
   - `LLMCostConfig.max_spend_per_session_usd` (default **$0.50**). A "session" = one CLI invocation (one cli/advise tick, one cli/operator conversational turn, one cli/news cycle). In-memory tally, reset per invocation.
2. **Single shared pool across roles in v1.** Operator-assistant calls and trading-advisor calls draw from the same daily budget. Per-role budget split (e.g., $0.50/day for operator chat + $0.50/day for MoE experts) is deferred to a follow-on ADR if real usage shows starvation. Simpler v1 surface; one config knob; one cap to reason about.
3. **Enforcement layer: a new domain service, not the adapters.** `services/llm_cost_gate.py` exposes `check_budget(role, estimated_cost_usd) -> Allow | Deny(reason)`. The gate reads recent `llm_calls` rows from `StoragePort.get_llm_calls(since=...)`. Adapters are responsible for computing the per-call cost from token counts via the pricing table (item 6) and persisting it after each call. The CLI layer (`cli/advise`, `cli/operator`, future `cli/news` cloud-news fetch) calls `check_budget` before each cloud call. **Ollama calls bypass the gate entirely** — free / local; no need to instrument.
4. **Hard-stop on cap trip.** `check_budget` returns `Deny(reason)` when the cap is exceeded; the calling code raises `LLMCostCapExceeded` (a new domain exception). cli/operator catches and posts an embed asking the operator to bump the cap or wait. cli/advise catches and skips the tick (logs structured; advisor output is best-effort by ADR-002 anyway). Trading and harvester loops are not in the cloud-LLM call path so they continue unaffected.
5. **`llm_calls` SQLite table schema.** Columns: `id` UUID PK, `timestamp` ISO 8601 UTC, `role` (operator | quant | risk | news | arbitrator | single | unknown), `provider` (anthropic | openai | google), `model` (provider model id), `tokens_in` int, `tokens_out` int, `tokens_reasoning` int NULL (for o1/Claude thinking/Gemini thinking; provider-specific column), `cost_usd` Decimal(10,6), `request_id` TEXT NULL (provider correlation id), `success` BOOL, `error_kind` TEXT NULL (set on failed calls; cost still recorded — failed calls can incur charges on some providers). Two indexes: `(timestamp)` for daily totals; `(provider, model)` for cost-by-provider reports. Lives in **`operator.db`** alongside `pending_commands` / `notifications` / `conversation_turns` (cli/operator already owns that database, and cost data is operator-visible).
6. **Pricing table is code, not config.** `services/llm_pricing.py` carries a Pydantic-modeled static dict `{(provider, model): (input_usd_per_million_tokens, output_usd_per_million_tokens, reasoning_usd_per_million_tokens | None)}`. Each entry includes a comment with the provider's pricing-page URL + the date the price was verified. Operators don't edit pricing; it's a fact about reality that lives in the codebase. Periodic verification is a Phase 8 quarterly audit item.
7. **`tools/show_llm_costs.py` operator inspection.** Mirrors the existing `tools/show_proposals.py` / `tools/show_transfers.py` / `tools/show_pending.py` pattern. Reads `operator.db`'s `llm_calls`, prints recent N calls + per-day / per-provider / per-role rollups. Read-only; runs against the same database without lock contention.
8. **Caps are advisory in dry-run modes.** `cli/advise` running against `OllamaAdapter` (Phase 5 default) sees no gate checks because the gate only triggers on cloud providers (item 3). When `cli/advise` is wired to a cloud provider but with `LLMCostConfig.enforce=False` (operator-configurable kill switch — analogous to `auto_apply.enabled=False` from ADR-012), the gate records calls but never raises. Useful for an initial week of "see what this would have cost" before turning enforcement on.

**Alternatives Considered:**
- **Per-role budget split (operator vs advisor vs news).** Considered, deferred. Simpler v1 with one pool; if real usage shows the conversational assistant starving advisor cycles (or vice versa), a follow-on ADR splits the budget. The cost table already carries `role` so retroactive analysis is possible.
- **Token-based caps (max tokens/day instead of USD/day).** Rejected. Token prices vary 10×+ across models (input vs output, base vs reasoning, provider differences). Operators reason about dollars; tokens are an implementation detail.
- **Soft-stop with graceful degrade.** Considered (e.g., cap trip switches operator-assistant from Anthropic → Ollama). Rejected for v1 — silent model substitution is exactly what ADR-015 prohibits. Cap trip = explicit error, operator decides.
- **Trust the cloud provider's own dashboard / billing alerts.** Rejected. Provider billing alerts lag by hours-to-days, are usually per-account (not per-role), and don't give the operator the same forensic detail the project's other money paths get.
- **Enforcement at the adapter layer (each adapter checks before calling).** Considered. Rejected: scatters gate logic across N adapters; harder to audit; harder to extend to "what if this call would cost more than the remaining day budget" computations that need cross-adapter visibility. Centralized service is the same pattern that ADR-002 + ADR-003 establish for trading caps.
- **Provider-supplied cost field instead of computing locally.** Anthropic and OpenAI return token counts but not USD cost in the response. Google's `usage_metadata` is similar. Computing locally from token counts × pricing table is the only consistent option.
- **No cap, just monitoring.** Rejected. The whole point of monitoring is to act on it; an unactioned graph is just a graph.

**Consequences:**
- **Positive:** Bounded blast radius for stuck loops, runaway prompts, misconfigured cadences. The "$10 surprise" failure mode is structurally prevented.
- **Positive:** Forensic audit trail matches the project's other money-path discipline. Same operator-facing inspection-tool pattern.
- **Positive:** Pricing table lives in code, evolves with the codebase, has commit history, and can be unit-tested.
- **Positive:** `enforce=False` dry-run mode lets operators see real-world costs before flipping enforcement on. Same posture as `auto_apply.enabled=False` from ADR-012 — opt-in for the risky behavior.
- **Positive:** Trading / harvester loops are untouched by cap trips. Engine never goes down because of an LLM budget event.
- **Negative:** New SQLite table + new service + new config block + new domain exception + new pricing module + new inspection tool. Phase 6 has more carrying capacity than Phase 5's $0-cost equivalents.
- **Negative:** Pricing table needs periodic manual refresh when providers change rates. Quarterly audit item; Anthropic + OpenAI both publish stable price-per-million-tokens that change rarely (months to years between adjustments).
- **Negative:** Sliding 24-hour window requires `get_llm_calls(since=...)` with a timestamp filter at every gate check. With per-call costs and reasonable cadences (~1 call/sec worst case), the row count stays well under the index-scan budget; if it ever bites, add a denormalized running-total cache. Not a v1 concern.

**Compliance:** Aligns with ADR-001 (cost gate is a service in `services/`, pricing is a service, adapters compute cost but don't enforce). Aligns with ADR-002 (LLM advisory-only — cap trip stops advisory output but never blocks trading). Aligns with ADR-003-style "bound monetary blast radius via configured caps." Aligns with ADR-013 (`operator.db` already owns operator-facing state — `llm_calls` belongs there). **Refines ADR-007** by adding cost accounting to the LLM provider abstraction.

**References:**
- `docs/planning/stage-6.1-design.md` — Stage 6.1 slicing (where the table + gate + pricing land).
- `docs/planning/roadmap.md` — Phase 6 stage list.
- ADR-002 — LLM advisory-only (the unaffected invariant).
- ADR-003 — Cap-based safety design (precedent).
- ADR-012 — Operator-controlled gates default-off (precedent for `enforce=False` dry-run posture).
- ADR-013 — `operator.db` as the operator-state database.
- ADR-015 — Provider failover policy (retries draw from the same caps).

## ADR-015 — Cloud LLM Provider Failover Policy
**Status:** Accepted (planned for Phase 6)
**Date:** 2026-05-17

**Context:** Cloud LLM APIs fail. 429 rate limits during quota bursts, 5xx transient errors during provider-side incidents, network blips between Synology NAS and provider edge, full provider outages lasting minutes to hours. Phase 5's Ollama-only assistant didn't face this — local model, no provider-side outages, the only failure modes were "ollama process not running" and "model not pulled." Phase 6's cloud adapters need a clear policy: what happens when Anthropic returns 503 mid-conversational-turn? Mid-MoE-cycle?

The shape of this decision matters because the wrong answer has compounding implications. Silent failover to a different provider (or to local Ollama) means the trading advisor is silently using a different decision-maker — phi4:14b vs Claude Sonnet 4.6 are genuinely different judges, even given identical prompts. The operator deserves to know which model produced which recommendation. Failover-to-different-provider also has cost implications — provider prices vary 5–20× — which interacts with ADR-014's cost caps.

**Decision:** Adopt the following commitments for Phase 6 provider-failure handling. Land in Stage 6.1's retry/backoff helper; stable across 6.2–6.5.

1. **Default: fail loudly + retry on transient errors only.** Each cloud adapter classifies HTTP responses:
   - `429` (rate limit) → transient: retry with backoff.
   - `5xx` (server error) → transient: retry with backoff.
   - `4xx` other (auth, bad-request, content-policy) → permanent: fail immediately.
   - Network/connection errors (DNS, timeout, connection-refused) → transient: retry with backoff.
   Up to **3 retries** with exponential backoff (1s, 4s, 9s — `LLMRetryConfig.initial_backoff_seconds * backoff_multiplier ** attempt`). All retries exhausted → raise the relevant port's error type (`AssistantError` / `AdvisorError`).
2. **No cross-provider failover in v1.** When Anthropic exhausts retries, the call fails. The operator does NOT see the system silently fall back to OpenAI or Google. Cross-provider failover is deferred to a post-v1 stage because:
   - Different models = different decisions. A Phase-6 MoE cycle that started on Claude and finished on Gemini has mixed provenance that breaks the audit trail.
   - Provider prices vary widely; failover changes the cost math operators planned around.
   - Operators rarely configure all three providers at once; the v1 expectation is "pick one, configure it well."
   If real-world provider availability proves bad enough to justify cross-provider failover, the follow-on ADR adds it with explicit operator opt-in plus a logged "called Anthropic, fell back to OpenAI" trail in the `llm_calls` table.
3. **No silent failover to local Ollama.** Even when Ollama is installed and reachable, a cloud-to-Ollama auto-failover would silently change the model's decision shape. The operator picks "use Claude for the operator assistant" because Claude's conversational behavior differs from phi4's. Failover violates that choice without telling the operator.
4. **Retries count against ADR-014 cost caps.** Each retry that reaches the provider's API counts toward the daily and session budgets per ADR-014. A 429 typically doesn't bill (provider didn't process tokens), but a 200 retry after backoff is a full charge. The cost-gate check happens **before** the initial call, not before each retry — so a retried call that pushes over the cap is accepted (the retry doesn't get a fresh budget check). This keeps the gate logic simple and matches operator intuition ("I asked for one answer; one answer was billed for").
5. **Operator-facing notifications on permanent failure.** When all retries are exhausted:
   - **cli/operator**: posts a `level=error` notification ("LLM call failed: anthropic 503 after 3 retries"). The operator sees it via the Stage 5.5 forwarder.
   - **cli/advise**: logs structured (`logger.error` with `extra={"provider", "model", "role", "error_kind"}`) and skips the tick. The next scheduled cycle tries again — most provider outages resolve in minutes.
   - **cli/news (when Phase 6 wires cloud news fetch)**: same skip-and-log pattern.
   In all cases the engine (cli/live, cli/harvest) keeps running. Advisory output is best-effort per ADR-002.
6. **Per-provider auth lives in env, not config.** Each provider's API key gets its own env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) following the existing `KRAKEN_READER_API_KEY` / `KRAKEN_TRADER_API_KEY` / `DISCORD_BOT_TOKEN` convention. `.env.example` carries the placeholder names; operators copy to `.env` with real values. Adapter constructors fail fast (clear error, exit code 2) if the configured provider's key is missing — same posture as `cli/status` for missing Kraken creds.
7. **Retry config knobs (small, defaulted)**:
   - `LLMRetryConfig.max_retries` (default 3)
   - `LLMRetryConfig.initial_backoff_seconds` (default 1.0)
   - `LLMRetryConfig.backoff_multiplier` (default 2.0)
   Single shared `LLMRetryConfig` across all providers in v1. Per-provider override deferred unless real usage shows one provider needs different backoff.
8. **No circuit-breaker pattern in v1.** A circuit breaker (after N failures in M minutes, refuse calls for P minutes) is a real pattern but it adds state, requires reasoning about reset semantics, and its value depends on the actual failure cadence operators see. Defer to Phase 8's reliability stage if observation justifies it.

**Alternatives Considered:**
- **Cross-provider failover (Anthropic 503 → OpenAI fallback).** Rejected for v1. Model substitution is not transparent — different model, different decision shape, different cost. Operators deserve to know.
- **Cloud-to-Ollama failover.** Rejected. Silent behavior change is exactly what this ADR exists to prevent.
- **Exponential backoff with jitter.** Considered. v1 ships fixed exponential backoff for simplicity (deterministic, easy to reason about, easy to test). Thundering-herd is unlikely with a single-operator system; jitter is a one-line addition if it ever shows up.
- **Per-attempt cost-gate re-check (recheck budget before each retry).** Considered. Rejected: a retried call is one logical call from the operator's perspective; re-checking mid-retry creates the confusing case where attempt 1 was accepted but attempt 2 trips because someone else's call landed in between. Simpler invariant: one budget check per logical call.
- **Circuit breaker.** Deferred to Phase 8.
- **Provider-side rate-limit response respected (honor `Retry-After` header).** Considered. v1 ships fixed backoff; honoring `Retry-After` on 429 is a clean enhancement once a real adapter exists to extend. Note for Stage 6.2 — if implementing the Anthropic adapter, this is a single `httpx` response-header read.
- **Async-queue-then-batch.** Rejected — adds complexity for negligible benefit at this call cadence.

**Consequences:**
- **Positive:** Simple, predictable, fully under operator control. No surprise model substitution.
- **Positive:** Provenance of every advisor recommendation is single-provider, traceable to the configured model.
- **Positive:** Trading and harvester loops never block on LLM availability.
- **Positive:** Failure surface is small and well-defined — three retry parameters, two response classifications (transient vs permanent), one error path.
- **Negative:** A multi-hour provider outage means degraded advisory output until either the provider recovers or the operator manually edits `settings.yml` to switch providers. Hand-on-the-wheel posture; acceptable for a hobby bot.
- **Negative:** Recurring transient errors waste retry budget against ADR-014 caps. Mitigated by `LLMCostConfig.enforce=False` dry-run mode for the first week of cloud usage to see real failure cadence.
- **Negative:** No automatic graceful-degrade story (yet). Phase 8 reliability stage may add one.

**Compliance:** Aligns with ADR-002 (LLM advisory-only — failure stalls advisory; engine continues). Aligns with ADR-013 (operator gets clear visibility via the notification pipe). Compatible with ADR-014 (retries draw from the same cost pool; one budget check per logical call).

**References:**
- `docs/planning/stage-6.1-design.md` — where the retry helper lives.
- ADR-002 — LLM advisory-only (the invariant this preserves).
- ADR-014 — Cost caps (retries are budgeted against the same pool).
- ADR-013 — Operator notification pipe (where permanent failures surface).
- ADR-009 — Config consolidation (where the per-provider env vars are documented in `.env.example`).

## ADR-016 — Web UI Architectural Commitments
**Status:** Accepted (planned for Phase 7)
**Date:** 2026-05-17

**Context:** Phase 6 closed the cloud-LLM integration; the cost ledger
(`llm_calls`), the advisor history (`advisor_suggestions`), the
harvester history (`transfer_proposals` + `transfer_results`), the
news ingestion (`news_items`), the audit trail (`pending_commands`),
the notifications log, and the trade history (`trades`) are all
forensically complete in SQLite tables across observe.db / advise.db
/ harvest.db / operator.db. Inspection today happens via
``tools/show_*.py`` scripts — one per concern. That works but
requires SSH-to-the-NAS-and-run-a-command for every check; it's
neither at-a-glance nor browseable.

Phase 7 carves off the original-Phase-5-roadmap "Dashboard" stage
(per ADR-013's reorg) and brings it to life. The architectural
decisions below shape every Phase 7 stage; the auth model gets its
own ADR-017.

The non-obvious decisions for a single-operator observability surface
that also needs to support pause/resume mutations:

- **What stack?** FastAPI vs Flask vs Starlette vs plain WSGI. Going
  with FastAPI — async-native (matches every existing CLI's asyncio
  shape), typed (matches the project's pydantic-everywhere posture),
  dependency-injection-friendly (matches the hex-architecture
  port-DI pattern).
- **Templating shape?** Server-rendered Jinja2 + HTMX vs full SPA
  (React/Vue/Svelte). For single-operator scope, server-rendered
  HTML + targeted HTMX swaps is dramatically simpler than a SPA
  with build pipeline, frontend state, and CORS plumbing. No Node,
  no webpack, no separate frontend repo. Pages are full reloads
  with HTMX where partial updates pay off.
- **Read-only or include mutations?** Web UI mutations (operator
  decision 2026-05-17): pause/resume per symbol + emergency stop.
  These route through `pending_commands` (preserves the ADR-013
  confirm-before-execute firewall) with a UI-side two-click flow
  that mirrors Discord's ✅/❌ reaction pattern.
- **TLS termination?** Operator-managed reverse proxy (nginx,
  Caddy, Cloudflare Tunnel). Not bundled. FastAPI binds
  127.0.0.1:8000 by default; operator exposes via the proxy.
- **Real-time updates?** HTMX polling for the cost-ledger card +
  the open-orders card (every 10-30s). No SSE / WebSocket in v1.
- **Daemon shape?** New `cli/web` runs `uvicorn` — sibling to
  `cli/operator`, `cli/live`, `cli/harvest`. Same per-CLI-daemon
  pattern established by Phase 3-5.

**Decision:** Adopt the following commitments for Phase 7. Stable
across stages 7.1-7.5; revisit only via a follow-on ADR.

1. **FastAPI + Jinja2 + HTMX, no SPA.** ``src/wobblebot/web/`` is
   a sibling of ``src/wobblebot/cli/``. FastAPI app instance built
   via a factory function (``create_app(...)`` taking config + storage
   adapters) so tests can construct an isolated app per test.
   Jinja2 templates live under ``src/wobblebot/web/templates/``;
   static assets (HTMX, base CSS) under ``src/wobblebot/web/static/``.
   No build step. No Node. No frontend bundler. HTMX is a single
   ~14kb static file committed alongside the project's CSS.

2. **Routes consume ports via DI; no business logic in handlers.**
   FastAPI's dependency-injection system wires StoragePort /
   OperatorService / etc. into route handlers exactly the way
   constructor-DI wires them into ``cli/operator``. Handlers do:
   parse request → call port method → render template. They never
   compute metrics, aggregate data, or implement domain rules —
   that all lives in ``services/`` or ``domain/``. Same hex rule
   as every other presentation layer.

3. **Read-mostly + ADR-013-firewalled mutations.** Every state-
   mutating action (Pause / Resume / Stop in v1) creates a
   ``PendingCommand`` row in ``operator.db`` with status
   ``awaiting_confirmation``, exactly the same way ``cli/operator``
   does. The web UI never calls ``OperatorService.dispatch_command``
   directly — it routes through ``pending_commands`` so
   ``cli/live``'s existing ``WHERE status='approved'`` poll
   remains the only path from intent to engine. **The ADR-002
   firewall is preserved unchanged.**

4. **Two-click confirmation for mutations.** Clicking "Pause BTC"
   opens a confirmation page summarizing the parsed command;
   clicking "Confirm" transitions the row from
   ``awaiting_confirmation`` to ``approved`` (same state machine
   as Discord's ✅ reaction). The two-click flow keeps the
   operator-acknowledges-twice safety property symmetric with
   the Discord interaction layer. ❌-equivalent is a "Cancel"
   button that transitions the row to ``rejected``.

5. **v1 mutation catalog: Pause, Resume, Stop.** The most common
   operator interactions. Less-frequent commands (PauseAll,
   ResumeAll, CancelOpenOrders) stay on the Discord / CLI paths
   for now; they can be added to the UI catalog later without an
   ADR change since the mutation pattern is established.

6. **HTMX polling for live cards.** The cost ledger and open-orders
   panels refresh every 15s by default (configurable per
   ``WebConfig.htmx_poll_seconds``). Pages don't auto-refresh
   wholesale — only the cards that show fast-moving data poll.
   Static-ish pages (news headlines, audit logs, advisor history)
   are full-reload-on-navigation.

7. **127.0.0.1 by default, reverse-proxied for LAN access.** The
   FastAPI app binds to localhost only by default
   (``WebConfig.bind_host="127.0.0.1"``). Exposing to the LAN is
   the operator's choice and goes through their reverse proxy
   (which also handles TLS termination + any auth-front-side
   policies). Documented in ``.env.example`` + ``settings.example.yml``.

8. **`cli/web` daemon runs uvicorn.** New CLI entry point
   ``python -m wobblebot.cli.web``; same lifecycle shape as
   ``cli/operator`` (signal-handled clean shutdown, structured
   logging, ``--config`` / ``--profile`` flags via the shared
   _common.py helpers). The daemon loads WobbleBotConfig +
   opens the appropriate SQLite adapters (operator.db, advise.db,
   harvest.db, observe.db, news.db) + constructs the FastAPI
   app + runs ``uvicorn.run(app)``.

9. **Cross-DB queries via OperatorService graceful-degrade.**
   The web UI inherits Stage 5.6.C's pattern: if ``observe.db``
   isn't configured, the "current price" card just doesn't render
   (vs raising). Same shape as ``OperatorService.answer_query``.

10. **Phase 5 limitation carried forward.** ``cli/live``'s
    in-memory pause state isn't visible to the web UI for the
    same reason it wasn't visible to cli/operator in Stage 5.6 —
    pause state is per-session in-memory. The web UI shows the
    *audit log* of pause commands (pending_commands rows with
    ``command_kind='pause'``); the *live state* surfaces as
    "active" for all symbols until the deferred Phase 8 fix
    persists pause state to disk.

**Alternatives Considered:**

- **Django instead of FastAPI.** Heavier; brings an ORM that
  conflicts with the project's StoragePort + Pydantic-everywhere
  posture; sync-by-default is awkward against the asyncio-native
  ports. FastAPI is the natural fit.
- **Plain Starlette without FastAPI.** Leaner — FastAPI is mostly
  Starlette + auto-validation + OpenAPI. Rejected because
  FastAPI's DI system is the load-bearing piece for clean
  port-injection into routes; building it manually in Starlette
  is 50-100 LOC of fragile glue.
- **SPA front-end (React / Vue / Svelte).** Rejected on complexity
  budget: build pipeline, Node, CORS plumbing, frontend state
  management — all of it overhead for a single-operator
  observability surface. HTMX gets ~80% of "interactive web app"
  feel at 5% of the cost.
- **Read-only v1, no mutations.** Considered. Rejected by operator
  decision: pause/resume is the most-frequent operator
  interaction, and forcing it through Discord/CLI when the
  operator is already looking at the trading dashboard is
  workflow friction. Preserving ADR-013's firewall via the
  PendingCommand path + two-click confirmation gets the
  mutation safety without losing the dashboard's utility.
- **Mutations bypass the PendingCommand table** (i.e., the web
  UI calls dispatch_command directly). Rejected — violates
  ADR-013 decision 3. The firewall is load-bearing; every entry
  point honors it.
- **Single-click mutations (no confirmation page).** Considered.
  Rejected for the same reason Discord requires ✅ — the
  operator-clicks-twice safety property is cheap insurance against
  accidental fat-finger pauses during live trading.
- **WebSocket / SSE real-time updates.** Premature. HTMX polling
  at 15s gets the same UX with one fewer transport layer.
  Revisit if Phase 8 reliability ever shows the polling load
  costs more than the WebSocket setup.
- **Bundled TLS via uvicorn's ssl-keyfile/ssl-certfile.**
  Rejected — cert-rotation, ACME plumbing, and HTTP-to-HTTPS
  redirects are exactly what a reverse proxy is for. Don't
  reinvent.
- **Public-facing internet exposure (binding to 0.0.0.0 by default).**
  Rejected for the operator's security posture; the proxy /
  Cloudflare Tunnel layer is the right place to make that
  decision per-deployment, not bake into the daemon's defaults.

**Consequences:**

- **Positive:** At-a-glance observability across every Phase 1-6
  data store. Cost ledger visible. Recent fills + open orders
  visible. Pause / resume + emergency stop available without
  switching to Discord. The operator's most common workflows
  collapse into one tab.
- **Positive:** Architecture-discipline preserved. Routes consume
  ports. Mutations cross ``pending_commands``. No business logic
  in handlers. New layer, same hex rules.
- **Positive:** No build pipeline. Edit a template, refresh the
  browser. No Node, no npm, no webpack, no separate frontend
  repo to keep in sync.
- **Positive:** HTMX polling means a forgotten browser tab won't
  hammer the engine — polls are cheap reads against SQLite.
- **Positive:** Single-operator scope means no multi-user
  permission complexity; everyone with the password sees
  everything.
- **Negative:** Six new runtime deps (``fastapi``,
  ``uvicorn[standard]``, ``jinja2``, ``python-multipart``,
  ``bcrypt``, ``itsdangerous``). Biggest dep-add since Phase 5's
  ``discord.py``. Documented in pyproject.toml; the bcrypt +
  itsdangerous transitive surface is small + stable.
- **Negative:** Another long-running daemon to operate
  (``cli/web``). Mitigated by Phase 8's planned maintenance
  worker (process supervision is part of that stage's scope).
- **Negative:** Schema lifecycle now needs a fifth DB
  consideration: the web UI reads operator.db (pending_commands +
  notifications + conversation_turns + llm_calls + future users
  table) + advise.db + harvest.db + observe.db + news.db. Each
  read-only connection is independent; SQLite WAL mode handles
  the concurrent-readers + the occasional write from web mutation.
- **Negative:** Web UI is yet another writer to operator.db's
  ``pending_commands`` table (was just cli/operator pre-Phase 7).
  SQLite handles concurrent writers via WAL; both writers append
  rows (never UPDATE simultaneously) so contention is minimal.
- **Negative:** v1 pause-state limitation carried forward — the
  status dashboard can't show "is BTC currently paused" because
  that's cli/live's in-memory state. Shows the audit log
  instead. Fix path documented; lands in Phase 8 reliability.
- **Negative:** CSRF protection on POSTs requires a custom
  middleware (Starlette doesn't ship one). ~30 lines. Documented
  in the Stage 7.1 design doc; tested as part of the auth flow.

**Compliance:** Aligns with ADR-001 (web/ is a sibling presentation
layer; depends on ports, never on adapters directly). Aligns with
ADR-002 (LLM is advisory-only; the web UI doesn't surface LLM
decisions as actions — those go through the existing advisor →
auto-apply gate). Aligns with ADR-013 (the operator interaction
engine's confirm-before-execute firewall is honored; the web UI
is just another entry point that creates PendingCommand rows in
``awaiting_confirmation``). **Refines** ADR-013 by adding web as
a second writer to ``pending_commands`` alongside cli/operator —
both honor the same state machine and ADR-002 firewall.

**References:**
- ``docs/planning/stage-7.1-design.md`` — first stage slicing
  (web app skeleton + auth; no features).
- ``docs/planning/roadmap.md`` — Phase 7 stage list (7.1-7.5).
- ADR-001 — Hexagonal architecture (web/ as a sibling presentation
  layer).
- ADR-002 — LLM advisory-only (preserved; web UI surfaces advisor
  history but never executes from it).
- ADR-013 — Operator Interaction Engine (the confirm-before-execute
  firewall this ADR extends to the web entry point).
- ADR-017 — Web UI authentication (the auth-specific decisions).
- FastAPI: https://fastapi.tiangolo.com/
- HTMX: https://htmx.org/

## ADR-017 — Web UI Authentication
**Status:** Accepted (planned for Phase 7)
**Date:** 2026-05-17

**Context:** ADR-016 ratifies the Web UI architectural shape;
authentication is a load-bearing detail that warrants its own ADR.
The decisions below shape the login flow + session lifecycle +
password storage + CSRF protection, all of which surface in
Stage 7.1.

The constraints:

- **Single-operator, single-bot.** Multi-user collaboration is
  not in scope for v1 (matches ADR-013 decision 6's stance on
  the Discord allowlist). The operator may run multiple browser
  sessions but they all authenticate as the same identity.
- **Local-network deployment by default** with optional LAN /
  reverse-proxy exposure. The proxy may add its own auth layer
  (Cloudflare Access, oauth2_proxy, etc.); the FastAPI app's
  auth is the inner perimeter.
- **The operator already has Kraken trade keys + Discord bot
  tokens + cloud LLM API keys in `.env`.** Adding one more
  secret (a web password) is acceptable as long as the storage
  + handling discipline is comparable.
- **Mutation routes exist** (pause / resume / stop per ADR-016
  decision 5). CSRF protection is non-optional for forms.

**Decision:** Adopt the following commitments for Phase 7 auth.

1. **Session cookie + bcrypt-hashed-password.** The operator
   logs in via a form POST (``/login`` route); on success the
   server creates a signed session cookie via Starlette's
   ``SessionMiddleware`` (``itsdangerous``-signed under the hood).
   Subsequent requests carry the cookie; an auth dependency
   checks ``session["user_id"]`` is set and rejects with 302
   to ``/login`` if not. Logout is a POST to ``/logout`` that
   clears the session.

2. **Single ``users`` SQLite table in ``operator.db``.** Columns:
   ``id`` INTEGER PK AUTOINCREMENT, ``username`` TEXT UNIQUE NOT
   NULL, ``password_hash`` TEXT NOT NULL (bcrypt hash;
   ``$2b$``-prefixed), ``created_at`` TEXT NOT NULL,
   ``last_login_at`` TEXT NULL. v1 has one row in production
   but the table is keyed by username so multi-user could land
   later without schema work. CHECK constraint enforces
   ``length(password_hash) > 0``.

3. **Password seeded via ``cli/web --create-user``.** Operator
   creates the initial account via a one-shot CLI subcommand
   that prompts for username + password (twice for confirmation,
   matching unix conventions), bcrypts the password, persists
   the row. The bot daemon (``cli/web`` without that flag) refuses
   to start if no user exists — operator must seed before serving.

4. **Bcrypt cost factor 12.** Default for the ``bcrypt`` package.
   Cheap enough that login isn't slow (~50ms on modern hardware)
   + expensive enough that brute-force is impractical. Bumpable
   per ``settings.yml`` if Phase 8 hardening wants higher.

5. **Session lifetime: 7 days, sliding.** Cookie expires 7 days
   after last activity. Operator who hasn't touched the dashboard
   in a week re-logs in. ``SessionMiddleware`` handles the
   sliding-window math; we just set ``max_age``.

6. **Cookie attributes:** ``HttpOnly`` (no JS access),
   ``SameSite=lax`` (allow top-level navigation but block
   cross-site POSTs), ``Secure`` flag set conditionally based
   on ``X-Forwarded-Proto`` (so the reverse proxy can terminate
   TLS and the cookie still flags Secure for browsers). Cookie
   name: ``wobblebot_session``.

7. **CSRF protection via synchronizer token.** Every form GET
   includes a random token in the session + a hidden form input;
   POST handlers validate the form's token matches the session's.
   Mismatch → 403. ~30 lines of middleware + a Jinja2 macro for
   the form-input. Standard pattern; no library.

8. **Login form is rate-limited to 5 attempts / 60 seconds per
   IP.** Simple in-memory counter (per-IP bucket) reset on
   successful login. Brute-force defense against the local
   network. Restart-resets is acceptable — the operator isn't
   running a public-facing login form.

9. **Constant-time password comparison.** ``bcrypt.checkpw``
   handles this automatically; the auth code never short-circuits
   on length mismatch or first-character mismatch.

10. **No password reset flow in v1.** If the operator forgets
    the password, they delete the row via SQL and re-create via
    ``cli/web --create-user``. Multi-user password reset (email
    recovery, etc.) is out of scope.

**Alternatives Considered:**

- **HTTP Basic auth.** Browser-native popup; no login page; no
  CSRF needed (auth is in every request header). Rejected for
  UX — no logout, popup styling can't be customized, less
  obvious to the operator that they're logged in.
- **OAuth (Discord / GitHub / etc.).** Heavier; adds a callback
  flow, client-secret management, third-party dependency on the
  auth provider's uptime. For single-operator scope, password is
  simpler.
- **JWT in cookie instead of opaque session cookie.** JWT means
  the cookie carries claims; revoke-on-logout requires a JWT
  blocklist or short TTL + refresh dance. The signed-session
  approach (server-side state, opaque cookie) is simpler for
  single-operator scope.
- **Bcrypt vs argon2 vs scrypt.** Bcrypt is the conservative
  choice — battle-tested, well-supported in Python, no
  GPU-resistance properties for a non-public-facing form means
  argon2's advantages are mostly theoretical. Bcrypt + cost 12
  is plenty.
- **Password stored in settings.yml.** Considered. Rejected —
  settings.yml is operator-edited in plain text; a hash there
  reads as more "secret" than necessary and a typo during an
  edit could lock the operator out without a re-seed path.
  Storing in operator.db's users table matches the project's
  pattern of "operator-managed secrets live in DB or .env, not
  YAML".
- **Multi-user permission system.** Out of scope for v1 (same
  call as Discord allowlist). Schema is per-user-keyed so the
  follow-on is just role-checking middleware + a roles column.

**Consequences:**

- **Positive:** Standard, well-understood auth pattern. Session
  cookie + bcrypt password are the boring defaults; deviating
  from them needs a reason and we don't have one.
- **Positive:** Logout is a real action (clear session); operator
  can deliberately log out of a shared machine.
- **Positive:** CSRF protection on forms means the operator's
  authenticated session can't be tricked into a cross-site POST
  that pauses BTC via image-tag exploitation.
- **Positive:** Rate-limiting buys time against brute-force —
  combined with bcrypt cost 12, the local-network attacker would
  need years for an 8-char password.
- **Negative:** One more secret for the operator to remember /
  store. Mitigated by the seed-via-CLI flow giving the operator
  the option of using a long random password from a password
  manager (no in-UI password generation is provided; out of
  scope).
- **Negative:** Restart-reset rate-limit means a sufficiently
  patient attacker could restart the daemon (somehow) to clear
  the counter. Edge case for a non-public-facing dashboard;
  Phase 8 reliability may add persistent rate limit state if
  it ever becomes relevant.
- **Negative:** ``cli/web --create-user`` is an interactive
  subcommand (prompts on stdin); operators running in
  fully-headless setups need to think about how to seed (e.g.
  ``echo password | cli/web --create-user --stdin``). Document
  in the deprived-env walkthrough.

**Compliance:** Aligns with ADR-001 (auth state lives in
``operator.db`` next to the other operator-state tables; reads /
writes go through StoragePort like every other entity). Aligns with
ADR-013 (operator.db is the operator-state database; ``users`` is
just another table next to ``pending_commands`` /
``conversation_turns`` / ``notifications`` / ``llm_calls``).

**References:**
- ADR-016 — Web UI architectural commitments (the larger context
  this auth model fits inside).
- ``docs/planning/stage-7.1-design.md`` — Stage 7.1 slicing
  (where the users table + login form + middleware land).
- bcrypt: https://github.com/pyca/bcrypt
- Starlette SessionMiddleware:
  https://www.starlette.io/middleware/#sessionmiddleware

## ADR-018 — Engine Reconciliation Strategy

**Status:** Ratified 2026-05-18 at Stage 8.1 kickoff.

**Context:** WobbleBot maintains two views of "what orders exist":

1. **Storage** (live.db / shadow.db) — local SQLite tables
   populated by the engine as it places orders, observes fills,
   and processes cancellations.
2. **Exchange** (Kraken's order book, or the synthetic ledger
   inside ``ShadowExchangeAdapter``) — the canonical record of
   what's actually committed.

In normal operation the two stay in sync because every place /
fill / cancel goes through ``adapter.X(...)`` immediately followed
by ``storage.save_order(...)`` inside the same engine tick. But
they can drift in four scenarios:

1. **Shutdown drift.** ``cli/live`` / ``cli/shadow`` call
   ``adapter.cancel_order(o)`` in the shutdown finally-block, but
   don't write the resulting ``status="canceled"`` back to storage
   (concrete bug surfaced 2026-05-18 in a 60-minute shadow
   session: 3 BUYs cancelled per log, all 3 still
   ``status="open"`` in shadow.db at exit).
2. **Out-of-band exchange cancellation.** Kraken can cancel an
   order while the daemon is offline (expiry, manual cancel via
   Kraken Pro, exchange-side incident). Next startup, storage
   shows the row as open; Kraken doesn't have it.
3. **Out-of-band exchange placement.** Operator places an order
   manually via Kraken Pro (or runs a second bot against the same
   key). Storage doesn't have the row; Kraken does.
4. **Crash mid-tick.** Daemon exits between ``adapter.place_order``
   and ``storage.save_order`` (or between ``adapter.cancel_order``
   and the status update). One side has the change, the other
   doesn't.

Phase 8.1's charter is to make WobbleBot robust against all four.
Stage 8.1.B fixes scenario 1 (the concrete shutdown bug).
Stage 8.1.C handles scenarios 2-4 at startup by reconciling
storage against the exchange.

This ADR ratifies the reconciliation policy — *which side wins
when they disagree, and how each kind of drift is resolved*.

**Decisions:**

1. **Exchange is authoritative for "what orders exist."** When
   storage and exchange disagree about an order's existence,
   exchange wins. Storage gets updated to match.

   *Why:* Kraken's record is the canonical source — that's the
   ledger holding the actual money. Storage is our local
   bookkeeping that can drift on crash, shutdown bug, or network
   issue. Trusting storage over Kraken means trading on a fiction.

2. **Storage-only orders → mark canceled.** On engine startup,
   for every storage row with ``status="open"`` that is NOT
   present in ``adapter.get_open_orders()``, transition status to
   ``"canceled"`` with ``updated_at = now()``. Log the
   reconciliation with structured fields.

   *Why:* The order isn't on the exchange. Either the engine
   cancelled it and didn't persist (scenario 1), or Kraken
   cancelled it out-of-band (scenario 2). Either way, it's not
   active anymore. The status transition matches reality.

3. **Exchange-only orders → log loud error + DO NOT adopt.** On
   engine startup, for every order in ``adapter.get_open_orders()``
   that is NOT matched to a storage row (by ``exchange_id``), log
   an ERROR-level message with the order details. Continue
   startup; do not adopt the order into the engine's tracking.

   *Why:* WobbleBot's engine doesn't place orders outside its own
   ticks. An order on Kraken we don't track means one of three
   things:

   - Manual order via Kraken Pro UI (operator deliberate).
   - A second bot or instance running against the same key.
   - A serious bug (the engine placed it and lost track).

   None of those are "the engine should start managing this
   order". Adopting an orphan risks:

   - Doubling up positions if the operator's other tooling
     also manages it.
   - Cancelling the operator's deliberate manual order on next
     grid re-layout.
   - Treating a buggy place-without-persist as if it were
     correct, masking the underlying bug.

   Log + continue lets the operator notice and make the call.
   Alternative considered: refuse to start. Too disruptive — the
   operator may have legitimate non-engine orders (large hand-
   sized limit orders, OCO bracket, etc.) that should coexist
   with the engine's grid. Refusing to boot forces the operator
   to manually clear those before starting the bot. Hard-no.

4. **Reconciliation runs once at engine startup, not per tick.**
   The Stage 2.2 engine already reconciles fills + state on every
   tick via ``adapter.get_open_orders()`` + storage diff.
   Reconciliation handles the *startup drift gap* only — once the
   engine is running, the tick logic catches ongoing drift.

   *Why:* Avoid duplicating work. Tick logic + startup
   reconciliation are complementary; the tick handles the live
   case, the startup handles the recovery case.

5. **Same policy for cli/shadow.** Shadow's "exchange" is the
   synthetic ledger inside ``ShadowExchangeAdapter``. Same
   reconciliation logic applies: query
   ``shadow_adapter.get_open_orders()``, diff against shadow.db's
   storage, mark missing rows canceled, log orphans.

   *Why:* Consistency. One reconciliation policy across both
   CLIs is easier to reason about than two slightly different
   ones. Shadow's ledger is in-memory and dies with the process
   — but the storage view persists across runs, so reconciliation
   on shadow startup is genuinely useful for "I shadow-tested
   yesterday; today the engine should start clean".

6. **Harvester pending-transfer reconciliation deferred to v1.1.**
   ``transfer_results`` rows can also drift (``status="pending"``
   in storage; Kraken settled or failed it while daemon was
   down). In v1.0, the operator manually reconciles via Kraken
   Pro's withdrawal history. No automated reconciliation on
   cli/harvest startup.

   *Why:* Harvester transfers are infrequent (operator-approved,
   ad-hoc cadence — not the 5-second engine tick). The drift
   surface is small; the operator already inspects Kraken Pro
   for the actual settlement. Adding automated reconciliation
   requires the ``Kraken /0/private/WithdrawStatus`` API path
   plus a "what's the pending row's match in Kraken's history"
   mapping that doesn't fit the Stage 8.1 scope. Defer to
   v1.1 backlog; document the manual reconciliation procedure
   in v1.0 release notes.

7. **Reconciliation policy lives in a pure service.** The
   per-CLI wiring is thin: ``cli/live._main_async`` calls
   ``services.reconciler.reconcile_open_orders(adapter, storage)``
   after storage open + adapter construct but before engine first
   tick. Same call site in ``cli/shadow._main_async``. The
   reconciler is testable in isolation against mocked adapters
   + in-memory storage.

   *Why:* Stage 2.2's pure-function bias (``compute_grid_levels``,
   ``next_counter_action``, etc.) has paid off. Same shape:
   the policy is a pure function over (adapter open orders,
   storage open orders) returning a diff to apply. CLI wiring
   is one async helper call.

**Alternatives considered + rejected:**

- **Storage authoritative for cancelled orders, exchange for
  open orders** (hybrid). Asymmetric. Rejected because it
  doesn't simplify the implementation but does complicate the
  mental model. If storage says "open" and Kraken says
  "doesn't exist", the answer is "not open" — not "we need to
  go figure out whether storage was right".

- **Refuse to start on any reconciliation discrepancy.** Too
  disruptive (rejected in Decision 3 reasoning).

- **Adopt exchange-only orders into engine tracking.** Rejected
  in Decision 3 reasoning. Particularly bad because adoption
  would have the engine try to manage manual orders the
  operator didn't intend to delegate.

- **Per-tick reconciliation instead of startup-only.** Rejected
  in Decision 4 reasoning. The tick already does this for live
  drift; doing it again across tick boundaries is redundant.

- **Automated harvester reconciliation in v1.0.** Rejected in
  Decision 6 reasoning. Manual reconciliation suffices for the
  v1.0 surface.

**Consequences:**

- **Positive:** Engine startup is robust against shutdown bugs,
  out-of-band cancels, and crashes. The "Phase 8.1 reliability"
  promise becomes concrete.
- **Positive:** Operator's manual Kraken Pro orders coexist with
  the engine. The engine logs orphans but doesn't fight them.
- **Positive:** Single ``services.reconciler`` module is the one
  edit point for any future reconciliation refinement (matching
  Stage 8.0.C's "one edit point" payoff for the poll loop).
- **Negative:** No safety net for harvester pending transfers in
  v1.0. Operator must inspect Kraken Pro after every withdrawal.
  Acceptable given the operator-approved cadence; documented.
- **Negative:** Orphan-order detection logs an error but doesn't
  block startup. Operator must read the log to notice. Acceptable
  given the operator's existing log-monitoring discipline; the
  alternative (refuse to start) is worse.

**Compliance:**

- Aligns with ADR-001 (reconciler is a pure service consuming
  ``ExchangePort`` + ``StoragePort`` — no adapter-layer
  bypassing).
- Aligns with ADR-005 (status value is ``"canceled"``, American
  spelling per Kraken).
- Aligns with ADR-002 (no LLM involvement; reconciliation is
  engine-state operations only).
- Reinforces ADR-003's harvester-separation invariant by
  explicitly NOT touching harvester reconciliation in this ADR
  — the harvester key boundary stays clean.

**References:**

- ``docs/planning/stage-8.1-design.md`` — Stage 8.1 slicing
  (where the persistence-on-cancel fix + reconciler land).
- ``docs/planning/roadmap.md`` Stage 8.1 entry — the concrete
  shadow-session repro that surfaced the shutdown bug.

## ADR-019 — Advisor Purpose: Regime Reader + Guardrail, Not a Volatility Tuner

**Status:** Accepted
**Date:** 2026-05-30

**Context:** Stage 8.5 shipped the advisor as a *volatility → spacing* tuner: a
curve mapping realized per-tick volatility to an "ideal" grid spacing, plus four
override guards. The premise was "jumpier market → wider grid." After Stage 8.5
landed, a grid backtest over the local 2013–2025 Kraken 1m history (and a 2026 Q1
out-of-sample quarter) refuted that premise as the advisor's organizing principle.
The full account is `docs/reference/grid-strategy-research-synthesis-2026-05-30.md`;
the load-bearing findings:

1. **Trend, not volatility, decides win-vs-lose.** A grid's profitability over a
   full cycle is dominated by whether price trends through it (long-bias downtrend
   bleed is the dominant risk), not by how jumpy it is tick-to-tick. The advisor's
   curve keyed off the wrong variable.
2. **No single grid rules them all; every grid fails in some regime.** A static
   spacing — any spacing — loses to buy-and-hold over full cycles. ~3% is merely the
   least-bad *static default* (it survives every regime), not "the right spacing."
3. **A tight grid is not categorically wrong.** A 1% grid *chosen in chop and pulled
   before the trend resumes* genuinely works — demonstrated live in the two weeks
   before this ADR, and the regime-switch experiment's perfect-foresight oracle
   returned +164.6% (the only configuration in the entire backtest program to beat
   hold) precisely by selecting the regime-appropriate grid and pulling it on time.
   The losing pattern is a tight grid *held as a default* across a trend, not tight
   spacing per se.
4. **Calibration defect made it concrete.** Real BTC per-tick volatility sits ~2×
   *below* the floor of the shipped curve's domain, so on the now-3% live grid the
   advisor recommended TIGHTEN to the 0.65% floor on ~98% of windows — i.e. it
   actively recommended the single worst static setting, nearly every tick.

**Decision:** Ratify the advisor's purpose as a **regime reader + transparent
guardrail**, not a volatility-to-spacing tuner.

1. **The advisor reads what the market is doing and suggests a proportionate
   posture, showing its reasoning; the operator owns the call.** Being wrong
   sometimes is acceptable because it is advisory (ADR-002). This refines — does not
   replace — ADR-002 (LLM advisory-only) and ADR-007 (news-never-auto-applies).
2. **Regime-appropriate grid *selection and pulling* is the job of a future
   regime/Oracle engine, not of a static curve.** "Pick the tight grid in chop, pull
   it before the trend" requires real regime detection (the research showed heuristic
   detection is insufficient; it needs LLM-grade judgment). That engine is PARKED, not
   abandoned — revisit conditions in the synthesis doc §4. Until it exists, the live
   grid runs a single survival-optimized static default (~3%, ADR-006 park-when-offside
   unchanged).
3. **Posture output is advisory-only, never auto-applied — an invariant.** Even when
   the regime engine lands and emits a posture (harvest / cautious / defensive), that
   posture can never drive an autonomous money-moving or grid-rebuilding action. The
   backtest proved mechanical auto-de-risk (e.g. sell-to-cash on a drawdown trigger)
   is destructive under imperfect detection. Only bounded *spacing* stays
   auto-applicable, and only through the existing `cli/apply` operator gate
   (ADR-012) behind the ADR-002/007 firewall.
4. **The vol→spacing curve is demoted to a coarse static-default calibration, and its
   rework is deferred.** Recalibrating the curve to "rest at 3%, never tighten" was
   explicitly *rejected* here: it would bake in the false absolute "tight is always
   wrong" (decision-point 3 above) — something the regime engine will need to undo the
   moment it can correctly select a tight grid in chop. So the curve is left as-is for
   the pre-soak; the proper curve + judgment-battery rework moves onto the Oracle/regime
   track where it can be built against real detection. During the v1.0 soak the advisor
   is advisory-only with `auto_apply` off, so the mis-calibrated curve is harmless
   log-noise, not a live risk.

**Alternatives Considered:**
- **Recalibrate the curve now to rest at 3% (Stage 8.6 Slice A as originally drafted).**
  Rejected: (a) it encodes "never recommend below 3%," a false absolute per decision 3;
  (b) it would invalidate the blessed 20-fixture judgment battery
  (`tools/probe_advisor.py`, 5-agent-adjudicated 2026-05-29) which all encodes the
  refuted vol→spacing thesis, forcing a full battery rebuild under pre-soak time
  pressure; (c) it polishes a model we've already decided to replace. Deferred to the
  regime track instead.
- **Build the first-class regime classifier + posture now (the original Stage 8.6
  centerpiece).** Rejected/parked: heuristic regime detection does not beat hold or even
  a static grid (synthesis §3); shipping it as a feature isn't justified until there's an
  appetite to test an LLM-grade detector or capital growth changes the calculus.
- **Keep treating the advisor as a vol→spacing tuner and just fix the calibration.**
  Rejected: that doubles down on the variable the backtest showed doesn't decide
  outcomes.

**Consequences:**
- **Positive:** The advisor's charter now matches what the evidence supports; future
  advisor work (the Oracle/MoE regime engine) has a ratified purpose to build toward.
- **Positive:** No false absolute is baked into shipped config; the path to "tight grid
  in chop, pulled on time" stays open for the regime engine.
- **Positive:** Stage 8.6 closes as a small, safe, pre-soak hardening pass (grid widened
  to a survival default + lookback dormancy documented) without reworking a blessed
  artifact in a hurry.
- **Negative:** During the soak the heuristic advisor logs a mis-calibrated "tighten"
  recommendation on most windows. Acceptable — advisory-only, `auto_apply` off; the
  operator can also simply not run `cli/advise` during the soak (purely observational).
- **Negative:** The curve + 20-fixture battery rework is now carried as parked debt on
  the Oracle track rather than resolved. Tracked in the synthesis doc + the Stage 8.6
  design doc so it isn't lost.

**ADR-020 (regime classification as a first-class metric) is DEFERRED** — write it only
if/when the Oracle/MoE regime engine is greenlit (it would record the `RegimeSignal`
domain model + `compute_regime` metric design). Parked with the research track.

**Compliance:** Refines ADR-002 (advisory-only — unchanged; posture-advisory-only is a
new sub-rule) and ADR-007 (MoE + news-never-auto-applies — the regime engine, when built,
is an `AdvisorPort` composition under the same firewall). No conflict with ADR-006
(park-when-offside, static grid) or ADR-012 (`cli/apply` remains the only config-mutation
path).

**References:**
- `docs/reference/grid-strategy-research-synthesis-2026-05-30.md` — the research this ADR ratifies.
- `docs/planning/stage-8.6-advisor-regime-reorientation-design.md` — the (rescoped) stage that produced it.
- ADR-002, ADR-007 (the advisory-only invariants ADR-019 refines), ADR-006, ADR-012.
- Project memory: `project_advisor_philosophy`, `no-false-absolutes-from-backtests`, `project_oracle_naming`.

<!-- ADR-020 (regime as first-class metric) DEFERRED — see ADR-019. -->

## ADR-021 — Server-Side Dead Man's Switch (Kraken CancelAllOrdersAfter)

**Status:** Accepted
**Date:** 2026-06-01

**Context:** The engine's only order-cleanup mechanism at v1.0 is the `cli/live`
`finally`-block cancel (`e2b6cfc`, Stage 8.4 hotfix): on shutdown it iterates open
orders and cancels each via `ExchangePort.cancel_order`. That mechanism requires the
host to be *alive enough to run Python cleanup* AND *able to reach Kraken over the
network*. The 2026-05-19 soak outage defeated exactly that — a thunderstorm took out
DNS mid-`finally`, the cancel calls couldn't reach Kraken, and three open BUYs sat
overnight; one filled when BTC drifted into it. A safety net that depends on the failing
host cannot cover the failure mode where the host itself is gone.

Kraken exposes `/0/private/CancelAllOrdersAfter`: a call starts a countdown timer ON
KRAKEN'S SERVERS; if the client doesn't call again within `timeout` seconds, Kraken
cancels every open order on the account itself. The cancellation runs exchange-side, so
a power/network loss at our end is the *trigger* (no keepalive → timer lapses), not an
obstacle. Kraken's recommended pattern is a 60s timeout pinged every 15–30s; it requires
only "Create & modify orders" / "Cancel & close orders" scope — notably NOT Withdraw, so
it stays clear of the ADR-003 key split.

**Decision:** Add `ExchangePort.set_dead_mans_switch(timeout_seconds)` and pet it from
`cli/live`'s engine loop every tick, ON BY DEFAULT (`live.dead_mans_switch_seconds = 60`;
`null` disables). Specifics:

1. **Port-level capability, not Kraken-specific.** The method is abstract on
   `ExchangePort`. `KrakenAdapter` implements the real call (dry-run short-circuits, so a
   `validate=true` diagnostic never arms a real timer). Synthetic adapters
   (`MockExchangeAdapter`, `ShadowExchangeAdapter`) implement a documented no-op — they
   hold no real resting orders. **Shadow deliberately does NOT forward to its wrapped
   live adapter**: doing so would arm a REAL timer on the operator's real account during
   a paper-trade.
2. **Pet every tick, before any order is placed that iteration.** Log-and-continue on
   failure — never crash a tick over the safety net; a failed ping leaves the timer at
   its prior (still-protective) value until the next tick re-pings.
3. **Disarm only on a confirmed-clean shutdown cancel.** In the `finally`, set timeout 0
   ONLY when `_cancel_all_open` reported zero failures. If our own cancel failed or
   raised, the switch is deliberately LEFT armed so Kraken's timer sweeps the stragglers
   we couldn't — making the dead man's switch the backstop for a failed clean cancel too.
4. **Default ON.** A real-incident-motivated safety net that can only cancel orders —
   never place or move money — is enabled by default; the operator opts out with `null`.
5. **Validation.** `dead_mans_switch_seconds`, when set, must be `>= max(10, 2 × tick_seconds)`
   so a couple of slow ticks can't lapse the timer and falsely cancel everything.

**Alternatives considered:**
- **Keep only the `finally`-block cancel.** Rejected: it cannot cover host death / power
  loss / network partition — the exact 2026-05-19 failure. The switch is *strictly
  stronger* and complementary (the `finally` handles controlled stops + disarms the
  switch; the switch handles uncontrolled death).
- **Make it a Kraken-only concrete method (not on the port).** Rejected: the loop is
  unit-tested through `MockExchangeAdapter`, so the seam must be on the port; and an
  abstract method forces any future real adapter to consciously implement the safety net
  rather than silently inherit a no-op.
- **Auto-liquidate holdings on outage (stop-loss style).** Rejected — out of scope and an
  anti-pattern for the grid (see the standing "stop-loss declined" position). The switch
  cancels resting ORDERS only; it does not and should not touch inventory.
- **Opt-in (default `null`).** Rejected per operator decision: the safety upside is high,
  the downside (account-wide cancellation) is documented, and the feature can't move money.

**Consequences:**
- **Positive:** The 2026-05-19 orphaned-order scenario is bounded from "hours" to
  "≤ timeout seconds," and fires even when the host is fully dark.
- **Positive:** A failed clean-shutdown cancel now has a server-side backstop instead of
  leaving orders resting until the next-startup reconciler.
- **Negative / caveat:** Kraken's timer is **account-wide** — it cancels manually-placed
  orders on the same account too. Documented in `settings.example.yml` and the port
  docstring. The bot runs on a dedicated trading key, but keys share the account's order
  book.
- **Negative / caveat:** A residual exposure window of up to `timeout` seconds exists
  between the last ping and expiry; a resting order can fill in that gap. The
  `>= 2 × tick_seconds` floor trades a slightly larger window for false-trip immunity.
- **Negative:** One extra private POST per tick (~12/min at the 5s default) — negligible
  against Kraken's rate budget.

**Soak note:** This is v1.1 work on the `v1.1` branch and is NOT in the frozen v1.0 soak
image; it takes effect only when the operator deploys the v1.1 image post-tag and runs
`cli/live`.

**Compliance:** Adds an engine-level guardrail only; introduces no new money-mover and no
auto-execution path (financial-power fragmentation intact). Uses `ExchangePort` per
ADR-001/004; needs no Withdraw scope, so the ADR-003 key split is untouched. Independent
of ADR-002 (no LLM involvement).

**References:**
- `docs/release/v1.1/engine.md` — the backlog entry this ships.
- `docs/reference/kraken-api-reference.md` — the `CancelAllOrdersAfter` endpoint.
- Kraken REST docs: `POST /0/private/CancelAllOrdersAfter`.
- ADR-001 (hexagonal / `ExchangePort`), ADR-003/004 (key split + withdrawal API), and the
  Stage 8.4 `finally`-block hotfix (`e2b6cfc`) this complements.

## ADR-022 — Advisor Reorientation: Guards-Only Heuristic + LLM Free Judge

**Status:** Accepted
**Date:** 2026-06-04

**Context:** ADR-019 ratified the advisor as a regime reader, demoted the vol→spacing
curve to a "coarse static-default calibration," and **deferred** its rework to the future
Oracle track — explicitly *rejecting* a recalibrate-to-rest-at-3% as a false absolute. It
left the curve in place for the soak as "harmless log-noise." Picking that thread up on
the v1.1 track surfaced two new facts:

1. **The curve isn't harmless once the advisor is meant to be *tracked*.** Its 2.70%
   ceiling sits below the 3% live grid, so its first-order logic recommended TIGHTEN on
   ~every non-guard tick *and flagged those as clear matches* — so in `engine: cascade`
   the LLM was almost never consulted. The trackable advisory signal (the whole point of
   running the advisor through a soak) was the curve's mechanical tighten, not judgment.
2. **Clamping the recommendation would destroy the signal it's meant to produce.** The
   tempting fix — floor the *recommendation* at the configured spacing — was rejected: it
   launders a bad recommendation into a fake-good one, so per-suggestion accuracy tracking
   (and any future learned arbitrator trained on it) measures the clamp, not the advisor.
   The application-time floor (`8500226`, defense-in-depth under ADR-019/ADR-002) already
   guarantees nothing below the configured spacing ever *lands*; the recommendation itself
   must stay honest.

**Decision:** Retire the vol→spacing first-order logic entirely (do not recalibrate it —
consistent with ADR-019's rejection of baking in a "never below 3%" absolute) and
reorient the advisor to **deterministic guards + an LLM free judge**:

1. **The heuristic makes only the four clear guard calls** (`directional_runaway`,
   `defensive_drawdown`, `dont_fix_working`, `fee_floor_calm`). `_first_order` and
   `_is_ambiguous` are deleted; `hold_deadband` + the `escalation` band are removed from
   the spec. The vol curve survives **only** as the `defensive_drawdown` guard's widen
   floor.
2. **Every non-guard tick escalates to the LLM**, which judges the regime with no
   prescribed target (`config/prompts/quant.md` rewritten curve-follower → free judge). A
   genuine HOLD is a valid answer. The cascade already does this — escalate on
   `clear_match=False`, fall back to the heuristic's HOLD on LLM error / cost-cap — so it
   needs no code change.
3. **The escalation model is `gpt-5-mini`** (cpu-only profile), chosen over `o3` in a
   2026-06-04 bake-off: on the cases that actually reach the LLM post-reorientation,
   gpt-5-mini held the matched grids that o3 (and o3-mini, o4-mini, the Gemini flashes)
   compulsively tightened, never made a wrong-direction call, and costs ~⅓ of o3
   (~$0.10/day, gate-bounded by ADR-014). Recorded in
   `docs/reference/advisor-llm-models.md`.
4. **Advisory-only is unchanged (ADR-002).** The LLM has full rein to *recommend* — that
   honesty is what makes the signal trackable — but the auto-apply floor (`8500226`) and
   the `cli/apply` gate (ADR-012) bound what can be *applied*. gpt-5-mini's residual
   matched-grid over-tighten (a minority of escalate ticks) cannot land below the
   configured spacing.

**Alternatives considered:**
- **Recalibrate the curve to rest at 3%.** Rejected here exactly as in ADR-019 — it bakes
  in "tight is always wrong," which the regime research refuted. Retiring the curve
  removes the false absolute rather than re-encoding it.
- **Clamp the LLM/heuristic recommendation at the configured floor (a "Layer 1").** Built,
  then reverted: it destroys per-suggestion accuracy tracking by converting a sub-floor
  recommendation into a floor-equal one. The floor belongs at *application* only.
- **Keep o3.** Rejected: the bake-off showed o3 tightens matched grids 100% of runs (the
  exact pathology motivating this ADR) at 3× the cost.
- **o3-mini / o4-mini (same reasoning class, cheaper rate).** Rejected: measured ~5%
  cheaper per call than o3 (the weaker model burns more reasoning tokens) *and* worse
  judgment; o4-mini even recommended below the fee floor.

**Consequences:**
- **Positive:** The advisor now produces a genuine, trackable judgment signal on every
  non-guard tick — the precondition for the learned-arbitrator soak and any P4 work.
- **Positive:** Safety is unchanged: guards still catch the clear failure modes; the
  application floor + `cli/apply` gate still bound real changes.
- **Positive:** Cheaper than the o3 baseline and gate-bounded; full escalation can't run
  away (`llm.cost.enforce`).
- **Negative:** Real LLM spend rises from ~$0 (curve suppressed escalation) to ~$0.10/day
  at full escalation — the cost of an honest signal, bounded by the daily cap.
- **Negative:** The curve-keyed *heuristic* battery tests are retired; the
  `tools/probe_advisor.py` fixtures are repurposed as the LLM-grading oracle (not rebuilt,
  per ADR-019's reluctance to rework the blessed battery under pressure).

**Compliance:** Successor to **ADR-019** — executes its deferred curve rework by retiring
(not recalibrating) the curve. Refines nothing in ADR-002 (advisory-only intact; bounds
enforced at application, not by suppressing the recommendation), ADR-007, ADR-012, or
ADR-014 (cost gate unchanged and now load-bearing).

**Soak note:** v1.1 work on the `v1.1` branch; NOT in the frozen v1.0 soak image. Takes
effect only when the operator deploys the v1.1 image post-tag and updates the live
`config/settings.yml` cpu-only model to `gpt-5-mini`.

**References:**
- `docs/reference/advisor-llm-models.md` — the 2026-06-04 model bake-off + selection.
- `8500226` — the application-time spacing floor this ADR relies on.
- ADR-019 (the predecessor this executes), ADR-002/012 (advisory-only + apply gate),
  ADR-014 (cost gate).

## ADR-023 — Unified Terminal-Order Resolution (Fill-vs-Cancel + Partial-Fill Recovery)

**Status:** Accepted (P1, v1.1 — blueprint settled 2026-06-03; extends ADR-018)
**Date:** 2026-06-05

**Context:** One untreated order state — `Order.status in (canceled, expired) AND
filled_amount > 0` — corrupts the ledger at two sites:
1. **Live (`_detect_fills`, "F1"):** the engine saves a `Trade` + places a counter only
   when a refreshed order is `closed` (full fill). A partially-filled order that refreshes
   to `canceled`/`expired` with `filled_amount > 0` drops the matching `Trade` rows (storage
   under-records a real fill) and skips the counter — corrupting cycle-matcher/dashboard PnL
   and drifting base-inventory vs Kraken's real holdings. Now *more* likely: shutdown
   cancel-all and the ADR-021 dead-man's-switch both cancel partially-filled limits, and the
   2026-06-03 live ADA dust fill confirmed Kraken routinely fragments one ordermin-compliant
   grid order into sub-ordermin partials.
2. **Startup (`reconciler`):** marks a storage-only order `canceled` without checking
   whether it actually *filled* while the daemon was down — the 2026-05-19 orphaned-$10-BTC
   class (a buy filled overnight while `cli/live` was down; the reconciler never re-derived
   the dropped fill).

ADR-018 set the reconciliation strategy (diff open-order status at boot) but did not cover
re-deriving a *dropped partial Trade*; this ADR extends it.

**Decision:** Treat both as one root cause behind a single shared resolver
(`services/reconciler.py::_resolve_terminal_order`):

1. **Shared resolver.** Given a departed order, `get_order_status` (QueryOrders) →
   classify `closed` / `partial_cancel` / `clean_cancel`; for the first two, save the
   `Trade` rows + the terminal-status order. Both `_detect_fills` and the reconciler call
   it. Read-side helper, no new module.
2. **QueryOrders, not `ClosedOrders`.** `get_order_status` already exists on the port +
   adapter and targets a known `exchange_id`; a paged `ClosedOrders` scan is unnecessary.
   Widen the reconciler's `_AdapterLike` Protocol with `get_order_status`. (Supersedes the
   un-ratified `ClosedOrders` mention in the old soak deferral note.)
3. **Counter-replay is placed by the engine, never the reconciler.** The reconciler is
   constructed after the engine and is documented never to place/cancel (`reconciler.py`);
   a reconciler `place_order` would breach financial-power fragmentation AND run outside the
   engine's per-symbol lock. Instead the reconciler adds the order UUID to
   `ReconciliationReport.needs_counter_order_ids`; `GridEngine.__init__` takes
   `pending_counters` and places them on the first `_tick` inside the `if not offside:`
   block (recovery counters inherit offside suppression, ADR-006).
4. **Retry-on-failure (decisive).** A pending counter that fails placement **stays** in
   `pending_counters` and retries next tick. Discard-on-failure plus the auto-re-layout
   guard would re-place a full grid with no counter — *reproducing the very orphan this
   fixes*.
5. **Full safety caps at startup.** `_check_safety` reads storage live; there is no
   uninitialized session accumulator, so caps are correct at boot. No reduced-check mode.
6. **Idempotency.** The terminal-status `save_order` drops the order from `_detect_fills`'
   `status=open` candidate filter, so the tick never re-processes it; self-heals across a
   crash between reconcile and tick (one-boot delay, no double-counter).

**Alternatives considered:**
- **Patch F1 and the reconciler separately.** Rejected: same root state, two drifting fixes.
- **Reconciler places the recovery counter directly.** Rejected by the adversarial judge on
  safety grounds (power-fragmentation breach + runs outside the engine lock + a failed
  placement re-triggers the auto-re-layout → reproduces the orphan).
- **`ClosedOrders` paged scan.** Rejected: `get_order_status` is simpler, already exists,
  targets the exact txid.
- **A reduced safety-check mode at startup.** Rejected: a first-pass design got this wrong;
  the live storage read makes full caps correct at boot.

**Consequences:**
- **Positive:** The 2026-05-19 orphan class and the live partial-Trade-drop are both closed
  by one helper; ledger + base-inventory stay truthful.
- **Positive:** Recovery counters obey the same offside + caps + lock discipline as normal
  grid orders.
- **Negative / caveat:** `MockExchangeAdapter` can't currently produce canceled+partial; add
  an `inject_partial_cancel` control method (mirrors `_apply_kraken_order_update`'s
  field-assignment bypass). Tests per outcome at both sites + a no-double-counter case + the
  orphan-reproduction regression.

**Compliance:** Extends **ADR-018** (engine reconciliation). Upholds financial-power
fragmentation (only the engine places orders) and ADR-006 (offside suppression). No
LLM/withdrawal surface (ADR-002/003 untouched).

**Soak note:** P1, `v1.1` branch; NOT in the frozen v1.0 soak image. Merges to `main` at
soak-clear; takes effect when the operator deploys the v1.1 image post-tag.

**References:**
- `docs/release/v1.1/engine.md` — "Order-lifecycle fill-vs-cancel + partial-fill recovery
  (reconciler + F1) — blueprint" (2026-06-03).
- ADR-018 (reconciliation strategy this extends), ADR-006 (offside), ADR-021 (the DMS whose
  cancels make partials more frequent).

## ADR-024 — Session-Loss-Cap Cool-Down

**Status:** Accepted (P1, v1.1 — blueprint settled 2026-06-03)
**Date:** 2026-06-05

**Context:** When `cli/live` exits on the session loss cap (`exit_code = 1`), nothing stops
an immediate relaunch straight back into the losing condition. The soak's 4:22am loss-cap
trip (a too-low $5 cap meeting a mark-to-market drawdown) is exactly the scenario where a
knee-jerk restart — or a `restart: unless-stopped` policy — would re-enter the bleed.

**Decision:** After a loss-cap exit, refuse to start a new session for a configurable
cool-down window.

1. **Persist the trip in a new `live.db` table** (one row per loss-cap trip: `tripped_at`
   + `session_pnl`), written in `_run_loop`'s `finally` (own try/except) when
   `exit_code == 1`; a pre-loop gate in `_main_async` queries it. `StoragePort` gains
   `record_cap_trip` + `get_last_cap_trip_at`.
2. **New exit code 4** for a cool-down refusal — distinct from 2 (creds/config) so restart
   policies / a future `cli/up` can tell "give up" from "try again later."
3. **Fail-open** on a storage-read error at the gate (log WARNING, proceed) — fail-closed
   under `restart: unless-stopped` would crash-loop (the docker rule-6 lesson). The
   cool-down is a safety *feature*, not a safety-*critical* invariant.
4. **Scope:** only `exit_code == 1`; never shadow/sandbox (synthetic ledgers).
   `--ignore-cool-down` is terminal-only (not YAML-settable, so a Portainer redeploy can't
   standing-bypass it) and does NOT clear the record.
5. **Default window is the operator's risk call** (the two design passes split 30 vs 60 min
   from the same evidence) — a config knob set to taste. Gate logic lives in a small
   `services/cool_down.py` helper for testability.

**Alternatives considered:**
- **A state file.** Rejected: a second source of truth that drifts from the DB.
- **Parse the notifications table.** Rejected: `operator_db` is optional (no Discord → no
  persistence).
- **Reuse exit code 2.** Rejected: conflates "operator must fix config" with "wait and
  retry" — restart automation needs to tell them apart.
- **Fail-closed at the gate.** Rejected: crash-loops under `unless-stopped`.

**Consequences:**
- **Positive:** A loss-cap trip enforces a deliberate pause instead of an automated
  re-entry into the losing market.
- **Positive:** The terminal-only bypass + new exit code make the behavior legible to
  restart policies.
- **Negative:** One new table + exit code; `--ignore-cool-down` is the documented escape
  hatch for a deliberate operator restart.

**Compliance:** Engine-level safety gate; no money-mover, no LLM (ADR-002/003 untouched).
Honors the docker rule-6 crash-loop guard (fail-open per-feature).

**Soak note:** P1, `v1.1` branch; NOT in the frozen v1.0 soak image.

**References:**
- `docs/release/v1.1/engine.md` — "Session-loss-cap cool-down — blueprint" (2026-06-03).
- ADR-021 (DMS), and the soak's 4:22am cap-trip evidence.

## ADR-025 — Pre-Placement Spread Guard

**Status:** Accepted (P1, v1.1 — blueprint settled 2026-06-03)
**Date:** 2026-06-05

**Context:** The grid places resting limit orders regardless of current market quality. A
wide bid/ask spread — routine for thin alts off-hours, and a live concern once multi-asset
ships — means fills happen at dislocated prices, eroding the per-cycle edge. No
market-quality gate exists today.

**Decision:** Refuse to run a tick when the spread is too wide.

1. **`get_ticker`, not `get_order_book`.** Bid/ask (`a[0]`/`b[0]`) are already in the Kraken
   Ticker response the adapter fetches every tick (it reads only last/`c[0]` today). A new
   `get_ticker(symbol) -> Ticker` value object (last/bid/ask + `spread_percentage` + a
   `bid < ask` validator) extracts the spread at **zero extra API calls**. Chosen over
   widening `get_current_price` (9 callers, most need only last); `_step_unlocked` calls
   `get_ticker` in its place → net one read per tick.
2. **Pre-tick gate, not a 5th `_check_safety` arm.** Spread is a per-symbol market signal,
   not a per-order invariant; gating the whole tick (a new skip `StepAction`) avoids an
   N×/tick re-fetch.
3. **Config:** `max_spread_percentage` on `SafetyConfig` (default 1.0% — never fires on
   healthy BTC/ETH ~0.01–0.05%; None/0 disables). Per-coin override on `CoinGridConfig`
   deferred (YAGNI) until a thin alt needs it.
4. **Log-flood guard:** reuse the offside heartbeat cadence — a sustained wide spread
   otherwise floods at the 5s tick.

**Alternatives considered:**
- **`get_order_book` round-trip for depth.** Rejected: bid/ask top-of-book is free from the
  Ticker call already made; a depth fetch is an extra round-trip for no v1 benefit.
- **A per-order `_check_safety` arm.** Rejected: re-fetches N×/tick and mis-models spread as
  a per-order invariant.
- **Widen `get_current_price` to return bid/ask.** Rejected: 9 callers, most need only last.

**Consequences:**
- **Positive:** Dislocated-market ticks are skipped before any order rests; matters most for
  thin alts and multi-asset.
- **Positive:** Zero added API cost (rides the existing Ticker fetch).
- **Negative:** Mock adapter gains `set_spread` + a default tight spread so existing engine
  tests don't trip; Shadow forwards to live.

**Compliance:** Engine-level market-quality gate; no money-mover, no LLM. Uses
`ExchangePort` per ADR-001.

**Soak note:** P1, `v1.1` branch; NOT in the frozen v1.0 soak image. Higher priority once
multi-asset ships.

**References:**
- `docs/release/v1.1/engine.md` — "Slippage / spread guard — blueprint" (2026-06-03).

## ADR-026 — Harvester `--execute` Replay Guard

**Status:** Accepted (P1, v1.1; extends ADR-003/ADR-004)
**Date:** 2026-06-05

**Context:** `cli/harvest --execute <proposal_id>` runs gates 1–7
(enabled/lookup/direction/staleness/destination/balance/day-cap) then calls `withdraw()` —
with **no "already executed for this `proposal_id`" check**. A double-tap, a shell re-run,
or a retry-after-perceived-hang can withdraw twice; the rolling day-cap is the only
accidental backstop. The 2026-06-02 plan review flagged this as the highest-blast-radius
hole in the codebase. It also becomes a hard co-requisite of the P3 web-Execute button,
which multiplies the double-withdraw vectors (web → `pending_commands` → `cli/harvest`
poll).

**Decision:** Add a cheap idempotency layer before `withdraw()`, DB-enforced.

1. **A UNIQUE constraint on `transfer_results.proposal_id`** — concurrency-proof, the
   authoritative guard. The insert fails if a result already exists for the proposal.
2. **A "layer 0" pre-check:** `SELECT TransferResult WHERE proposal_id = ? AND status IN
   (pending, completed)` → refuse with a clear message before running the seven gates' side
   effects.
3. **Hard prerequisite of web-Execute.** Per the P3 judge (2026-06-03), do NOT ship the web
   Execute/Approve button without this guard — prefer the DB UNIQUE over an app-layer-only
   check because the web path adds concurrent dispatchers.

**Alternatives considered:**
- **App-layer check only.** Rejected as the *sole* guard: two near-simultaneous dispatchers
  (CLI + web-poll) can both pass the SELECT before either inserts. The DB UNIQUE closes the
  race.
- **Rely on the rolling day-cap.** Rejected: it bounds total daily outflow, not a duplicate
  of one specific proposal — an accidental backstop, not a guard.

**Consequences:**
- **Positive:** A withdrawal proposal can execute at most once, regardless of double-taps,
  retries, or a CLI+web race.
- **Positive:** Unblocks the P3 web-Execute button safely.
- **Negative:** A migration adding the UNIQUE constraint; the migration must verify existing
  rows are already unique on `proposal_id`.

**Compliance:** Strengthens **ADR-003** (Harvester is the sole transfer authority) and
**ADR-004** (withdrawal via `ExchangePort`) by making `--execute` idempotent; no new
money-mover. Withdraw scope stays on the Harvester key only.

**Soak note:** P1, `v1.1` branch; NOT in the frozen v1.0 soak image. (Not previously in
`harvester.md`.)

**References:**
- `docs/release/v1.1/README.md` — P1 "Harvester `--execute` replay guard" row + the P3
  web-Execute judge corrections (E/F).
- ADR-003 / ADR-004 (Harvester authority + withdrawal API).

## ADR-027 — Kraken Rate-Limit Backoff

**Status:** Accepted (P1, v1.1; reuses the ADR-015 retry shape)
**Date:** 2026-06-05

**Context:** The 2026-06-02 global-fetch fix cut OpenOrders call *count* (one batched call
per tick instead of one per symbol) but not the error *class*: `_unwrap_envelope` still
raises a generic `ExchangeError` on `EAPI:Rate limit exceeded`, with no transient
classification or backoff. Worse, shutdown still fires N `CancelOrder` calls back-to-back
with zero spacing — so a rate-limit storm can recur during the most safety-critical cleanup
(the DMS-armed shutdown). `retry-policy.md` G4 parked this under "perf"; the soak proved
it's *resilience*.

**Decision:** Classify and pace around Kraken rate limits.

1. **Classify `EAPI:Rate limit exceeded` as a transient error** (distinct from a permanent
   `ExchangeError`) and apply a **bounded** backoff-and-retry — reuse the cloud-LLM retry
   shape from **ADR-015** (capped attempts + jittered backoff) rather than inventing a new
   one.
2. **Inter-cancel pacing** in `_cancel_all_open`: space the shutdown `CancelOrder` calls so
   the cleanup path itself can't re-trigger the storm.
3. Keep it small and own-tested — a transient-classification unit test + a paced-cancel
   test.

**Alternatives considered:**
- **Leave it under "perf" (G4).** Rejected: the soak showed a rate-limit storm during
  shutdown is a *resilience* failure (orders may not get cancelled), not a latency nicety.
- **A bespoke backoff.** Rejected: ADR-015 already defines the project's retry/backoff shape
  for transient transport errors; reuse it for one implementation.
- **Unbounded retry.** Rejected: a wedged rate-limit must eventually surface, not spin
  forever during shutdown.

**Consequences:**
- **Positive:** A transient rate limit no longer aborts a tick or a shutdown cancel as a
  hard error; the safety-critical cleanup paces itself.
- **Positive:** One retry/backoff implementation shared with the cloud-LLM path.
- **Negative:** Bounded backoff adds a small worst-case latency to a rate-limited shutdown —
  acceptable against the alternative (uncancelled orders).

**Compliance:** Transport-resilience only; no money-mover, no LLM, no new authority. Uses
`ExchangePort` per ADR-001; mirrors ADR-015's retry policy.

**Soak note:** P1, `v1.1` branch; NOT in the frozen v1.0 soak image.

**References:**
- `docs/release/v1.1/README.md` — P1 "Kraken rate-limit backoff" row.
- `docs/architecture/retry-policy.md` — G4 (re-scoped from perf to resilience).
- ADR-015 (cloud-LLM failover/retry shape reused here).

<!-- ADR-027 is the last in this file; new ADRs append below. -->
<!-- Reserved (v1.1 P2/P3 — bodies written at build time): ADR-028 historical-replay
     auditor; ADR-029 configurable counter-order target (top_sell); ADR-030 engine-state
     visibility table; ADR-031 operator-initiated re-anchor command.
     See docs/release/v1.1/README.md. -->
<!-- ADR-020 (regime as first-class metric) DEFERRED — see ADR-019. -->

