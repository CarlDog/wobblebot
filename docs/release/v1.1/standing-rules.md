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
