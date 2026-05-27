# Standing rules

These rules survive every version boundary; they are NOT v1.1 candidate features. They constrain what work is in-scope for future versions.

## Standing rule: operator-experience gates on margin / futures

On 2026-05-20 (soak Day 2) the operator stated they have no
prior experience with margin or futures trading and asked Claude
to act as a guardrail. This rule formalizes that ask.

**Margin trading (v1.2+) and futures trading (v1.3+) entries
below have explicit multi-gate triggers** that require the
operator to demonstrate spot-grid experience, paper-trade margin
separately, read post-mortems, and make explicit financial
decisions BEFORE these features are considered in-scope.

If the operator asks for margin or futures before the relevant
gates are clear, Claude pushes back. The soak going well is NOT
a sufficient signal — those features have failure modes that
spot trading cannot teach. Bypassing the gates would defeat the
purpose of having them.

This rule survives v1.0 tag and is intended to remain in force
across all future versions until the operator explicitly states
they have gained the relevant experience.

## Standing position: third-party Kraken SDK adoption

The project decided in Stage 2.1 to roll its own HMAC signing on
top of `httpx` rather than adopt `python-kraken-sdk` (or any
other community Kraken library). The original rationale: the SDK's
only abstractions over httpx were signing + nonce + WebSocket;
the REST interface was generic enough that the manual parsing
burden was identical with or without the SDK. ~20 lines of
crypto, gold-cased against Kraken's published example signature.

**The position has been re-evaluated on 2026-05-20** in light of
the equities expansion conversation and stands unchanged.
Equities support landed on Kraken's REST API via an additive
`asset_class` parameter on existing endpoints — exactly the
kind of incremental change DIY ownership handles cleanly without
SDK gating. Adopting an SDK would not have accelerated equities
support and would have introduced a dependency on the SDK
maintainers' release cadence.

**Trigger to re-evaluate:** ONLY if a future capability we want
provides **genuine substantive benefit** that DIY can't trivially
match. Examples that would count:

- FIX 4.4 protocol support — protocols outside HTTP+WebSocket
  where wire-format reuse has real value
- Mature WebSocket reconnect / heartbeat / message-ordering
  logic that we'd otherwise reimplement and get subtly wrong
- Endpoint-specific complex transformations (margin liquidation
  state machines, futures position lifecycle, etc.) where the
  SDK's abstraction captures non-trivial domain logic

Examples that would NOT count (and should not prompt
re-evaluation):

- "Less code" / aesthetic preference
- Generic "modernization" arguments
- Equities support landing in the SDK before we get around to it
  (we control the adapter; "after we get around to it" is
  the rate-limiting step regardless)
- One specific endpoint shape being slightly cleaner with the SDK

This standing position is not a v1.x candidate — it's a posture
preserved so future work doesn't have to re-derive the analysis.
If a contributor (human or LLM) proposes adopting an SDK without
naming a substantive benefit from the first list, point them at
this section.

## Standing position: declining Kraken UI upsell nudges

Kraken's Pro UI and account-management surfaces algorithmically
nudge active accounts toward higher-fee products: stop-loss /
take-profit orders, conditional orders, margin trading, futures
trading, staking, "earn" yield products, etc. The operator first
hit a wave of these nudges on 2026-05-26 (soak Day 9), prompting
this entry.

Each item is declined for a specific architectural or strategic
reason — NOT because Claude is risk-averse in general. The
reasons are durable: re-evaluate only if the underlying strategy
changes, not if Kraken nudges harder.

### Stop-loss orders — declined (anti-pattern for grid)

The grid strategy assumes price oscillation; it keeps buying as
price falls (DCA-into-dip). A stop-loss says "exit everything if
price drops past X" — the direct opposite of grid premise. Adding
stop-loss order placement at the engine level means:

- New `OrderType` in the domain + `KrakenAdapter.add_order` paths
- New state-machine logic (stop-loss orders sit in a
  conditional-pending state on Kraken; they don't appear in
  `OpenOrders` until triggered)
- The cycle matcher gains a fourth pairing dimension it doesn't
  currently model
- Safety-cap arithmetic gains a new event class

All of that surface area to add behavior that *fights the
strategy*. Operators wanting a personal stop-loss on long-term BTC
holdings should set it manually in Kraken Pro UI — different
account scope from the bot's grid.

### Take-profit orders — declined (redundant with the grid)

The grid IS the take-profit mechanism: every BUY at level N has a
counter-SELL at N+1 (per ADR-006 decision 2). Profit-taking is
baked in at the spacing-percentage granularity. A separate
take-profit order would compete with the grid's counter-SELLs,
double-book the same inventory, and confuse both the engine's
order accounting and the cycle matcher.

If the operator wants larger profit captures than the spacing
allows, the right knob is `grid.btcusd.spacing_percentage` (or
the equivalent per-coin override) — wider spacing = bigger profit
per cycle, lower frequency. That's the existing tuning surface;
take-profit orders would be a parallel path with no additional
expressive power.

### Margin trading — gated (see operator-experience rule above)

Subject to the 4-gate margin sequence from the standing rule on
operator-experience gates. The Kraken margin UI nudge is not a
sufficient signal to start. The gates are gates regardless of how
loud the UI gets.

### Futures trading — gated (see operator-experience rule above)

Subject to the 7-gate futures sequence (all 4 margin gates plus 3
more). Same posture as margin.

### Staking / "Earn" yield products — declined (custody risk)

Kraken Earn products take custody of staked funds and pay yield
from Kraken's pool. Two architectural conflicts:

- ADR-002's confirm-before-execute firewall exists because
  WobbleBot moves the operator's money on the bot's schedule.
  Staking adds a *third* schedule (unstaking lock-up periods,
  unbond delays) that WobbleBot has no current concept of.
- Funds in Earn are not available for the harvester to withdraw.
  If a cap-trip needs cash, staked funds don't help. The bot's
  liquidity model assumes spot-USD is the buffer.

The operator gets ~0-4% APY in exchange for taking custody risk
and adding a liquidity constraint the bot's safety design doesn't
model. Operator-personal staking outside the bot's scope is
fine; bot-managed staking is out.

### Conditional / OCO / iceberg / hidden orders — declined (complexity)

Kraken's order-type catalog is wider than the bot uses (limit +
market on the spot side, period). Each additional order type is
~50 lines of `KrakenAdapter` surface, new domain modeling, new
test surface, and a new failure mode. None of them are anti-
strategy like stop-loss; they're just unused expressive power
that earns no return.

Re-evaluate only if a specific strategic capability needs one of
these (e.g., if a future "buy a large position without moving
the market" feature lands and iceberg orders become genuinely
useful). The current grid + DCA strategy doesn't.

### How to respond to a Kraken nudge

When the operator sees a Kraken UI nudge:

1. Identify which item from the list above it's pushing.
2. Decline based on the architectural reason, not "no thanks."
3. If the item isn't on the list above (Kraken keeps inventing
   products), evaluate against the same lens: does it serve the
   spot-grid strategy, or is it adjacent expressive power that
   adds engine surface without changing what the bot does well?
4. Update this entry if a new product type becomes a recurring
   nudge.

This standing position is not a v1.x candidate — it's a posture
that prevents re-litigating each nudge as a fresh question. The
strategic decision (spot grid, no leverage, no custody hand-off)
was made at project inception and is reaffirmed here for the
Kraken-UI surface area specifically.
