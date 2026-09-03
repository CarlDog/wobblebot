# Post-2.0.4 backlog plan

**Written 2026-09-03**, after the six remaining register items were each given a
design assessment against the real code plus an adversarial risk pass (13 agents;
full transcript in the session's workflow journal). This document is the build
order and the scope decisions. Per-item detail stays in
[`docs/release/v1.1/`](../release/v1.1/README.md); status stays in
[`roadmap.md`](roadmap.md). This is a plan, not a status file — when a group
ships, its receipt goes in the roadmap and this doc gets a one-line pointer.

## What the assessment changed

Three of the six were **undersized** by the risk pass, and two register entries
describe shapes that are outright wrong:

| Item | Filed as | Really | Why |
|---|---|---|---|
| `hide-symbol` | M | **L**, or M with the scope cut below | The register's `hidden_symbols`-on-`user_preferences` sketch is a silent-data-loss bug; hiding also removes *controls*, not just information |
| `fastpath-signal` | S | **M** | The Discord footer still names the model for regex-decided messages; `parse_fast` becomes dead production code |
| `starved-log-noise` | S (logging only) | **L**, and not logging-only | It necessarily fixes a latent unreachable heartbeat, and the stale-anchor WARN fires per retry too |
| `dms-framing` | S | **M** | The obvious implementation reintroduces the same stale-read bug class it is fixing |
| `offside-since` | M | M | Sound, but the naive shape asserts a confident wrong date on first deploy |
| `sell-side-offside` | M + ADR | **L + ADR**, park | No retirement policy; see the closing section |

Two claimed dependencies are **discharged** and should not be carried forward:
`grid_engine.py`'s comment calling the operator re-anchor "a separate,
not-yet-built P3 item" is stale (it shipped), and `operator-ux.md`'s "ships with
the fast-path entry above" is historical (both shipped in 2.0.4).

## Ordering principle

Risk-ascending, so the new pre-deploy adversarial review gets exercised on cheap
changes first; recurring costs paid down early; the money item last and gated.
One deploy bounce per group, because every deploy restarts the live trader.

This reorders the assessment's own proposal, which put starvation noise third.
It moves to second here: it is schema-free, money-free, and costs roughly 2,700
WARN lines a day right now, while the item it was sequenced behind is parked
anyway.

---

## Group 0 — Three looks, no code

Not a release. Next time the dashboard is open, confirm what nobody has:

1. BABY/USD renders **without** an anchor button (the 2.0.4 route guard).
2. A `reanchor <symbol>` Discord message actually queues a row in production.
3. The offside popover's live appearance on a real offside card — which also
   settles the two 2026-09-03 rendering findings that were refuted by reading
   CSS rather than by looking, and therefore are not settled.

## Group 1 — Operator legibility

**Items:** `fastpath-signal`, `dms-framing`. No ADR, no schema, no money.

Together because they share a review lens rather than files: *when the daemon
decides something, is the operator told what actually happened?* Both are the
"a silent path is indistinguishable from a bug" failure.

Scope decisions that came out of the assessment:

- **Fix the Discord footer, or park it in writing.** `cli/operator.py:988`
  stamps `parsed by {assistant_model_name}` on query answers, and `status` is a
  fast-path pattern. The item's whole premise is "the operator can tell what
  decided"; a container-log-only fix leaves the surface the operator actually
  reads naming the wrong parser.
- **Add a fourth reason, `symbol_ambiguous`.** `resolve_symbol` returns `None`
  for both "unknown base" and "ambiguous bare base"; a single "not one of the N
  active symbols" wording is false in the second case.
- **Decide `parse_fast`'s fate.** Repointing the call site at a new
  `classify_fast` leaves it with zero production callsites and a 166-line test
  table that has become circular. Keep it as the pure core and have
  `classify_fast` wrap it, or inline it — but decide, don't leave both.
- **`dms-framing` must not call `dms_deadline_note()` live.** `_run_loop`
  assigns `dms_trigger_at` before the message is built, so reusing it on a
  recovery tick reports the *post*-ping deadline and contradicts its own
  message. That is precisely the stale-read class this item exists to fix.
  Snapshot a pre-ping value, and prefer a latch over a single float because
  `in_backoff()` skips the fetch entirely for a tick.
- The `auth_paused=True` half of the proposed framing test is unwritable — that
  path early-returns before any engine step.
- Split `docs/release/v1.1/README.md`'s single follow-ups row into three
  sub-rows here, once, so later groups edit only their own line.

## Group 2 — Starvation log noise

**Item:** `starved-log-noise`, alone (it owns the `grid_engine.py` starvation
region that Group 4 also needs). No ADR, no schema, no money.

- **It is not "logging only" — say so in the commit and to the reviewer.** It
  necessarily repairs a latent bug: the "still starved" heartbeat's tick
  arithmetic is unreachable and has never fired in production.
- **The stale-anchor WARNING is in scope.** It sits *inside* the starvation
  retry gate, so it fires once per 60-tick retry: another ~288 WARN/day forever
  for a symbol starved by construction. Its "fix flow doesn't exist yet" comment
  is stale — re-anchor shipped — so demote it and correct the comment together.
- **Buy-side cap reasons only; no `_try_place` signature change.** The sell path
  discards its assessment and returns a bare `"sell_deferred"`, so a reason adds
  nothing over the count already in the entry WARNING. Threading the sell
  reasons out reshapes exactly the signature Group 4 needs — **this decision is
  what keeps Groups 2 and 4 separable.** Note in the slice that sell-side
  reasons ride along with Group 4.
- **Clear the new state at both existing sites**, and fix the resume-into-silence
  bug this shape introduces: `resume_symbol` clears hold reasons but not starved
  state, and pause short-circuits before the tick, so starve → pause → resume
  would yield no entry WARNING and a stale reason set. That is exactly how the
  XRP incident ended.
- **Gate the heartbeat on retries only** — the layout-outcome hook is also called
  by re-anchor and by init, so otherwise an operator action advances a counter
  that then reports "still starved after N retries."
- **Budget one extraction:** `services/grid_starvation.py`, mirroring how the
  sell guard already lives outside the engine. Realistic add is 100–140 lines
  against a file already at 1,664.

## Group 3 — Persisted per-symbol state

**Items:** `offside-since`, `hide-symbol`. No ADR. **One migration carrying
both.** No money.

Together because they genuinely co-edit: both add port methods, adapter
implementations and DDL, and both touch the card's badge/header cluster. The
hidden-symbols summary row must reproduce the OFFSIDE badge that `offside-since`
is changing; built apart, the second one rebuilds the first across a deploy.

- **`hide-symbol` refuses to hide configured symbols.** This is the plan's most
  consequential scope call. The motivating case is BABY/USD, never traded,
  surfacing from a dust balance. Restricting hiding to symbols outside
  `live.symbols` solves 100% of the stated need and dissolves the control-removal
  defect entirely: pause, resume and re-anchor exist *only* inside the card loop,
  so hiding a configured symbol would silently un-ship the anchor button added in
  2.0.4. It also pulls the item from L back toward M.
- **Storage is a new table, not a `user_preferences` column.** The register's
  sketch is a silent-data-loss bug: preference updates are a full-row upsert
  rebuilt from the timezone form alone, so every timezone save would blank the
  hidden set. Classic full-replace trap, invisible to a naive test.
- **Never filter upstream of the template.** The symbol union feeds price
  fetches and sparklines, and balances feed account value, realized P&L and the
  fills tables. A view preference that moves reported P&L is the worst outcome
  this item can produce; keeping the hidden set in a field only the template
  reads makes that structurally impossible rather than merely careful.
- **`offside_since` is never written independently of the offside flag**, and it
  is cleared where the flag is computed — not in an `else:` inside the tick,
  which four early-return paths defeat.
- **Decide the already-parked symbols first** (see open questions). Shipping the
  naive stamp-on-first-observation makes the popover assert a confident wrong
  date about BTC and ETH, which have been parked since the 2026-08-19 anchor.
  That is the same class of defect 2.0.4 just fixed.
- **`tests/services/test_data_collector.py` defines a concrete `StoragePort`
  subclass** implementing every abstract method. New abstract methods break it
  at instantiation. It is in the plan, not a test-time surprise.
- Accept in writing that the port and adapter files grow again; do not attempt a
  split here.

## Group 4 — Sell-side-only extension while offside-high

**Item:** `sell-side-offside`, alone. **ADR-006 amendment. Real money. Parked.**

My recommendation is stronger than "do it fourth": **write the ADR, and do not
schedule the code until the ADR solves the lifecycle problem.** The assessment
and its risk pass between them found four design holes, one of which is not a
detail:

**Nothing cancels resting extension sells when price falls back into the band.**
"At most N per episode" reads as a bound and is not one — each new offside-high
episode adds up to N more orders that sit above the band indefinitely, each
billing a *full* order size against the exposure caps regardless of its actual
size. It compounds silently, across restarts, on a money path, and it presents
weeks later as "the grid stopped placing orders" with nothing pointing back at
this feature. On 2026-09-03, four symbols were offside-high simultaneously.

The other three: the profit predicate rests on a cost basis with a **known
desync** (the BTC gap of 0.00053613 / ~2.8%); the sell guard it depends on is an
**operator toggle** that can be switched off, taking the eligibility machinery
with it; and the proposed "discard band-illegal pending counters" rule **breaks
ADR-031**, dropping legitimate recovery counters on an unrelated path.

Gates, in order:

1. A clean `tools/reconcile_trade_history.py` bill across all six symbols. Until
   that clears, the profit predicate is unsafe by construction.
2. The ADR-006 amendment, ratified before any code, stating a **lifecycle, not a
   placement rule**: the asymmetry (sells may extend, buys never), an explicit
   retirement trigger, and a hard ceiling on simultaneous extension orders per
   symbol that is independent of episode count.
3. Replay validation via `tools/auditor.py` over a real **multi-episode** rally
   window. Shadow mode is not a substitute, because you cannot choose the window
   at live-tape cadence.
4. Ship behind a config gate defaulting **off**, enabled for one symbol first.

Pin the lifecycle with a test that runs two consecutive offside-high episodes
with a return-to-band between them and asserts the open-order count returns to
the band ladder's. Its mutation: remove the retirement trigger, and the second
episode's count must go red.

Also un-grepped and silently divergent once the engine can trade outside the
band: the backtest tools' own inlined offside checks, the sweep-order price
helper (a symbol with a live extension ladder would rank as far-from-fill and get
de-prioritised for funding exactly while it is the one trading), and the capital
reporter.

---

## Open questions — operator decisions that block

1. **`hide-symbol` scope.** Restrict hiding to symbols outside `live.symbols`
   (recommended, and assumed above), or allow any symbol and accept that the
   summary row must then carry pause/resume/re-anchor controls? The second grows
   the item past L and moves it behind Group 2.
2. **`offside-since` for the already-parked symbols.** BTC and ETH have been
   offside since 2026-08-19. Either add a floor flag and render "parked at least
   Xh", or leave the column NULL and keep 2.0.4's tick sentence until a genuine
   transition is observed. Related: seed those two rows by hand from the engine's
   own first-transition WARNING, or let them fill in naturally?
3. **`dms-framing` threshold.** How much of the dead-man's-switch window must a
   failure streak consume before the calm framing is used? Kraken fired at ~0.85
   of the window on the one observed event; anything below that fixes
   2026-09-03, higher values buy margin against an earlier fire.
4. **`sell-side-offside`.** Confirm the reconcile gate, and name the net margin
   per extension sell you actually want. Note the fee accounting is already net
   of both legs, so a threshold quoted "on top of the round-trip fee" would
   double-count.

## Cross-cutting

- Every slice doc lists, per test, the exact mutation that turns it red. Both
  risk passes found proposed tests that pin the *absence* of code and are
  therefore unrevertable padding.
- File-size budget per group, in writing. One extraction is budgeted, in Group 2.
- Four groups, four deploy bounces, four adversarial reviews.
