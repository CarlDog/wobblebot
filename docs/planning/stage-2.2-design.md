# Stage 2.2 — Micro-Grid Engine: Design and Slicing

*Drafted 2026-05-14 at the close of Stage 2.1, before any 2.2 code was written. Living document — actual slicing may adjust during implementation, but the principles below are load-bearing and should not be relitigated without an ADR.*

## What "micro-grid" means here

Place evenly-spaced buy orders below and sell orders above a reference price.
As the market moves:

- Price drops → a buy order fills → place a sell order one level up (locking in a small gain when *that* fills).
- Price rises → a sell order fills → place a buy order one level down.

One **cycle** = one matched buy-fill + sell-fill pair, producing realized
P&L of roughly `level_spacing * order_size` minus fees.

"Micro" refers to **per-order size**, not grid spacing or cycle count.
Order sizes are configured small (USD-denominated, single-digit to
low-double-digit dollars) so that any single misfire has bounded blast radius.

## Critical separation: Stage 2.2 ≠ Stage 2.3

Stage 2.2 produces **intent** — the engine decides what orders should exist
and places them through `ExchangePort`. In 2.2 the port is wired to
`MockExchangeAdapter` (paper trading, no real money). In 2.3 it's wired to
`KrakenAdapter` (real money, tiny size).

The engine code is identical. Only the adapter swap differentiates the
two stages. This is the entire point of the hex architecture and the
load-bearing reason Phase 1 invested so heavily in port contracts.

**Do not conflate them.** Stage 2.2's success metric is "the engine
produces correct grid cycles against the mock under a synthetic price
walk." No real Kraken orders are placed in 2.2.

## What's already in place

- **Config skeleton.** `config/settings.example.yml` declares `grid:`
  (default + per-coin overrides) and `safety:` sections with the right
  fields. Slice 2.2.1 binds these to Pydantic schemas.
- **Port contracts.** `ExchangePort.place_order`,
  `ExchangePort.cancel_order`, `ExchangePort.get_order_status`,
  `ExchangePort.get_open_orders` are defined and implemented by
  `MockExchangeAdapter` with limit-order matching. The Kraken adapter
  stubs them out — to be filled in by Stage 2.3.
- **Storage.** `SQLiteStorageAdapter` persists `Order`/`Trade`/`Balance`
  records. Schema is stable and Stage 2.2's grid-slot state can be
  derived from these tables plus a new `grid_slots` table.

## Proposed slicing

| Slice | Scope | Estimated size |
|-------|-------|----------------|
| **2.2.1 — Config schemas** | Pydantic `GridConfig`, `SafetyConfig`. YAML loader. Validation tests covering edge cases (negative spacing, zero levels, missing required fields, per-coin overrides shadowing defaults). | ~1 hour |
| **2.2.2 — Pure grid math** | `domain/grid.py`. Functions: `compute_grid_levels(price, spacing, levels_above, levels_below) → list[GridLevel]`; `next_counter_action(fill, grid) → BuyAt(price) \| SellAt(price) \| NoOp`. No I/O — pure functions, fast tests. | ~1.5 hours |
| **2.2.3 — Grid engine service** | `services/grid_engine.py`. Wires the pure logic to `ExchangePort` (read price, place orders) + `StoragePort`. Persists only `GridState` (the per-symbol anchor: reference_price + grid params + created_at) — `GridSlot` state is derived each tick from the layout + the existing `orders` table. Exposes `GridEngine.step(symbol)` that advances one tick: read current price → load/initialize grid_state → reconstitute slots → check fills → place counters and fill empty slots (unless offside). | ~2-3 hours (largest) |
| **2.2.4 — Safety caps** | Per-coin and global cap enforcement *inside* the engine, before any `place_order` call. Daily-spend tracking persisted in a new `daily_spend` SQLite table. Refusal of orders that would exceed limits surfaces as a logged event, not an exception. | ~1.5 hours |
| **2.2.5 — End-to-end integration test** | Synthetic ~1000-tick price walk fed through `GridEngine` + `MockExchangeAdapter` + `SQLiteStorageAdapter`. Asserts: total filled cycles match expectation, realized P&L positive, no cap violations, restart-and-resume produces identical final state. | ~1.5 hours |
| **2.2.6 — CLI wrapper** *(optional)* | `cli/paper` (or extend `cli/sandbox`) to run the engine indefinitely against the mock with operator-visible logging. Honestly **defer this to Stage 2.3** — that's when the operator wants to actually watch it run. | — |

**Total: ~6-10 hours of focused implementation.** Not a single-session
chunk. Daylight and rest matter for money-touching code.

## Design decisions to ratify before writing code

These should land as **ADR-006: Grid Engine Architecture** at the start of
slice 2.2.1 or earlier. They will get re-litigated otherwise.

### 1. Grid re-centering policy

**Decision: stay parked.** If price moves outside the initial grid window
(drops below the lowest buy level), keep the grid where it is and wait
for price to return.

**Reason:** the grid is a mean-reversion bet. Re-centering chases the trend
and defeats the strategy. A "trending out of the grid" regime is a signal
the strategy is wrong for current conditions, not a signal to chase.

**Implication:** the engine needs a "grid is offside" log signal so the
operator can intervene. Maybe a per-coin pause when offside for N ticks.

### 2. Partial fill handling

**Decision: leave the remainder open; place the counter-order for the
filled portion.** No cancel-and-replace dance.

**Reason:** Kraken's order accounting handles partial fills cleanly, and
this matches the natural order lifecycle. Cancel-and-replace introduces
race-condition surface area (the remainder could fill between cancel and
replace) and burns fees.

### 3. Source of truth for open orders

**Decision: DB as primary, Kraken as ultimate truth.** Engine reads its
grid-slot state from SQLite each tick. At startup and every N ticks
(suggest N=100, configurable), reconcile against Kraken's actual
`get_open_orders` response — orders that exist on the exchange but not in
our DB are imported; orders in our DB that no longer exist on the
exchange are marked closed and trigger counter-action logic.

**Reason:** restart resilience requires durable state. Kraken can lose an
order entry (rare but real — outages, region failovers) or the engine can
crash between "send AddOrder" and "persist response." Reconciling both
sources is the only way to converge.

### 4. Order ID strategy

**Decision:** introduce a `GridSlot` model in `domain/grid.py`:

```python
class GridSlot(BaseModel):
    symbol: Symbol
    side: OrderSide
    level_price: Decimal  # the grid level this slot represents
    order_id: UUID | None  # Order currently occupying this slot, if any
```

A slot is "empty" when `order_id is None`; the engine fills empty slots
on the next step (subject to safety caps).

**Reason:** the grid is a *layout* concept and orders are *transient*
occupants of that layout. Separating them lets `step()` reason about
"what should exist" without coupling to "what currently exists."

### 5. Concurrency model

**Decision:** single asyncio task stepping all coins in turn for Stage
2.2. One mutex per coin (asyncio.Lock per Symbol) to make `step()`
re-entrant-safe in case Stage 5 later parallelizes.

**Reason:** Stage 2.2 doesn't need parallelism. Per-coin tasks add
real-time-ordering bugs that are hard to test deterministically. Stage 5
hardening can move to per-coin tasks if the master-task throughput
turns out to be a bottleneck (it won't, at 1-second tick rates).

## What's NOT in scope for Stage 2.2

- **Real Kraken orders.** That's Stage 2.3.
- **LLM-driven parameter tuning.** That's Phase 3.
- **Withdrawals or fund movement.** That's Phase 4.
- **Multi-asset coordination.** Stage 2.4 — Stage 2.2 builds the
  per-coin engine; Stage 2.4 wires it up to run across the
  whitelist.
- **Dashboard / metrics UI.** Phase 5.

## Open questions to resolve at slice 2.2.1 kickoff

- **Tick rate.** How often does the engine call `step()`? Suggest **5
  seconds** for Stage 2.2 (paper); revisit for Stage 2.3 based on Kraken
  rate-limit budgets.
- **Order TTL.** Should grid orders expire after N hours of no fill?
  Standard practice: no — let them sit until they fill or the operator
  cancels. Mention in ADR.
- **Cycle reporting cadence.** When the engine completes a cycle (buy +
  sell pair), do we log immediately, batch into 1-minute summaries, or
  persist only? Suggest: log + persist, no batching (cycles are rare
  enough at micro scale that per-cycle logs aren't noisy).

## How to use this document

When Stage 2.2 starts:

1. Re-read this doc.
2. Verify the decisions above still match operator intent.
3. Write `docs/architecture/decisions.md` ADR-006 capturing the
   ratified decisions (1-5 above plus tick rate / TTL / cadence).
4. Begin slice 2.2.1.

If you find yourself wanting to redesign anything substantial mid-slice,
stop and update this doc + the ADR first. Mid-slice redesigns are how
the grid engine acquires hidden bugs.
