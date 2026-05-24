# Engine — reliability and performance

*Engine-side improvements: safety nets, reconciliation extensions, real-time data paths, performance optimizations. Touches `services/grid_engine.py`, `services/reconciler.py`, and `KrakenAdapter`.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Storage caching layer

**What:** an in-memory LRU cache for hot read paths (`get_open_orders`,
`get_grid_state`) keyed by `(method, params_tuple)` with a small
TTL (1-5s) and invalidation on write.

**Why deferred:** Stage 8.3's index audit confirmed every hot read
uses `SEARCH` (index access), not `SCAN`. On the harness against
1020 seeded rows, `get_open_orders` p50 0.26ms / p99 0.60ms — fast
enough that caching is speculative.

**Trigger:** soak shows engine ticks consistently >100ms with
storage dominating the profile, OR `tools/profile_storage.py` on
the Synology shows p99 >10ms for a hot read.

### Async query parallelism (asyncio.gather over symbols)

**What:** in `_run_one_tick`, replace the serial per-symbol loop
with `asyncio.gather(*[engine.step_one_symbol(s) for s in symbols])`.

**Why deferred:** Stage 2.4 ADR-006 decision 5 measured ~150ms
per-symbol latency vs the 5s tick budget. Even a 30-coin sweep
finishes in well under one tick. Parallelization here is premature
without measured bottleneck.

**Trigger:** soak shows a tick budget violation (tick + 1 > next
tick's start) under multi-coin configuration.

### Batch APIs (`save_orders_bulk`, `save_trades_bulk`)

**What:** new StoragePort methods that take a list and do a single
multi-row INSERT instead of N transactions.

**Why deferred:** the engine places one order per fill per tick;
batching is irrelevant at that rate.

**Trigger:** a future workflow (e.g. backfill from Kraken's full
trade history) needs N >100 writes in a single operation.

### Engine tick latency budget alarming

**What:** the engine emits a notification (level=warning) when a
tick takes >X% of the configured tick interval; X defaults to 50.

**Why deferred:** Stage 8.3 ratified "no CI perf regression check"
(decision 8). The same logic applies to runtime alarming:
threshold tuning needs measurement data.

**Trigger:** the operator notices ticks dropping during the soak
and wants automated detection.

### Server-side dead man's switch (`CancelAllOrdersAfter`)

**What:** Kraken's `/0/private/CancelAllOrdersAfter` endpoint sets a
server-side timer; if the engine doesn't ping again within N
seconds, **Kraken auto-cancels every open order on the account**.
The engine would call it on every tick with a timeout (e.g. 30s),
keeping the timer rolling. When cli/live dies, the timer expires
and Kraken does the cleanup — no engine cooperation needed.

**Why high value:** the 2026-05-19 thunderstorm outage is the
exact scenario this prevents. cli/live crashed via uncaught
exception; couldn't cancel its 3 open buys; one filled overnight
when BTC drifted into it. With this endpoint set on every tick,
Kraken would have auto-cancelled all three within 30s of the
last tick — zero orphaned orders, zero unintended fills, no
manual recovery needed.

It's also a **strictly stronger safety net than the `e2b6cfc`
finally-block fix** because it doesn't depend on the engine being
alive enough to execute cleanup. Pure server-side, fires even
when the host has lost power entirely.

**Why deferred:** v1.0 ships with the finally-block fix as the
cleanup mechanism. The dead-man's switch is feature work and
v1.0 is in documentation freeze. Implementation is small (~1
new adapter method + 1 call per tick) but warrants its own slice
with proper tests.

**Trigger:** the 2026-05-19 outage already triggered it
conceptually; v1.1 should ship this early. Low engineering cost,
high safety upside.

### WebSocket real-time updates (private + public channels)

**What:** replace REST polling with Kraken's WebSocket API for
two channels: own-order updates (private channel) and ticker /
order-book updates (public channel). Engine reacts to fills the
moment Kraken signals them, not on the next 5s tick.

**Two payoffs:**
1. **Lower fill-detection latency** — counter-order placement
   could happen <1s after fill instead of up-to-5s. Tighter
   loop = more cycles in choppy markets.
2. **Lower REST rate-limit pressure** — useful at multi-symbol
   scale, useful for any future per-second polling scenarios
   (live dashboards, real-time alerts).

**Why deferred:** real architectural surface area. WebSocket is
a different transport (event-driven instead of request/response),
needs reconnection logic, heartbeat handling, message ordering.
Likely warrants its own ADR (event-driven adapter pattern) and
its own design doc. The current REST + 5s polling is sufficient
for soak-grade reliability.

**Trigger:** profile data from Stage 8.3's `tools/profile_storage.py`
showing storage latency is no longer the bottleneck AND multi-
symbol soak showing REST rate limits being approached.

### System status awareness (`/0/public/SystemStatus`)

**What:** Kraken publishes its operating mode at this endpoint:
`online` / `cancel_only` / `post_only` / `limit_only` /
`maintenance`. Engine checks at startup + periodically; pauses
cleanly during exchange-side issues instead of repeatedly
hitting orders-rejected errors.

**Why deferred:** today the engine logs and continues when
Kraken rejects orders for operational reasons. Inelegant but
not unsafe (rejections don't lose money). System-status
awareness is a graceful-degradation improvement, not a safety
gap.

**Trigger:** the next Kraken maintenance window during a soak,
where the operator notices noisy error logs that would have been
suppressed if the engine knew to park.

### Session-loss-cap cool-down period

**What:** mandatory operator-configurable cool-down after cli/live
exits with ``exit_code=1`` (loss cap tripped). For N minutes
after the last loss-cap shutdown, cli/live refuses to place new
orders on startup — either refuses to start entirely or starts
in a "monitoring only" mode where the engine ticks but
``_try_place`` short-circuits.

**Why high value:** Day 5 surfaced this implicitly. When the cap
tripped on the morning's BUY fill (math bug), the natural reflex
was "restart and resume." Same reflex would apply if the cap
trips *legitimately* on a real drawdown — and then the operator
would have bypassed the safety the cap was designed to provide.
A forced gap between cap trip and resumption is the difference
between "the safety worked" and "the safety became a speed bump."

**Implementation:** new ``live.cool_down_seconds`` config field
(default e.g. 3600 = 1h). cli/live on startup queries the most
recent ``session_end`` log row (or a new ``session_exits`` audit
table) and computes ``time_since_last_cap_trip``. If < cool_down,
either refuses to start (exit 2 with operator-readable message)
or starts in monitoring mode with placement disabled. Operator
override flag (``--ignore-cool-down``) for genuine "I fixed the
bug, restart immediately" scenarios.

**Why deferred:** behavior change for live mode; deserves an ADR
ratifying the cool-down semantics (refuse vs monitor, default
duration, operator-override discipline).

**Trigger:** post-v1.0. Real-world soak data may inform default
cool-down duration (longer for trend-following losses, shorter
for known-bug recoveries).

### Slippage / spread guard before placement

**What:** pre-tick check on the current bid-ask spread; refuse
placement for the tick if spread exceeds the operator-tuned
threshold (absolute width or N × rolling-mean multiplier).

**Why high value:** the engine places at fixed grid prices
computed from the anchor. It doesn't check live market spread.
During news events / volatility spikes / thin-book moments,
Kraken's bid-ask can widen 10×+ — a BUY at a grid level computed
days ago could fill at a price unreflective of "fair" market.
Grid bots specifically lose money when filling against
abnormally-wide spreads because the counter-SELL spread + fees
exceeds the cycle's notional. Real safety hole the current
``max_*`` caps don't address.

**Implementation:** new exchange method ``get_order_book(symbol)
-> OrderBookSnapshot`` (top-of-book bid + ask + bid_size +
ask_size); new safety check in ``GridEngine._try_place`` that
fetches snapshot + computes spread + compares to a config-driven
threshold; refuses + logs ``order refused: spread_too_wide``.
Threshold is config-driven (e.g.,
``safety.max_spread_bps: 50`` = 0.5%); rolling-baseline mode
where threshold is N × rolling-mean over last X ticks.

**Why deferred:** new exchange method required (don't have
get_order_book yet); also deserves ADR ratification because
"refuse placement when conditions look weird" is a behavior
change.

**Trigger:** post-v1.0. Higher priority once multi-asset / new
exchanges ship (order book quality varies wildly across venues +
symbols).

### SQLite concurrency stress test

**What:** synthetic workload test under
``tests/stress/test_operator_db_concurrent.py`` simulating 8
daemons hitting ``operator.db`` simultaneously at higher than
normal rates (pending_commands inserts + heartbeats + LLM call
logs + notifications). Measure latency percentiles + assert no
``database is locked`` errors at projected v1.1+ throughput.

**Why deferred:** today's volume is fine. Stress test exists to
**guard against** Phase 9 expansion + multi-asset throughput
where the concurrency picture changes.

**Trigger:** before Phase 9 starts adding multi-asset write
volume. Earlier triggers if anomaly detector surfaces lock
contention metrics post-launch.

### Reconciler fill-vs-cancel disambiguation

**What:** extend `services/reconciler.py`'s storage-only handling
to query Kraken's `/0/private/ClosedOrders` (and optionally
`/0/private/TradesHistory`) for each storage-only `exchange_id`.
If Kraken says the order filled rather than was canceled, replay
the engine's `_detect_fills` counter-placement logic at startup
— place the counter sell/buy that would have been placed live.

**Why deferred:** ADR-018 ratified the v1.0 reconciler as a
minimal "exchange authoritative" diff with two outcomes
(storage-only → canceled, exchange-only → log). Adding closed-
orders / trade-history queries introduces another Kraken API
dependency at startup and another decision tree (what if the
order partial-filled? what if the trade-history page is
paginated? what's the lookback window?). v1.0 chose simplicity;
v1.1 can re-investigate.

**Trigger:** surfaced during the v1.0 soak's first overnight
outage (2026-05-19) when cli/live was down while a buy filled.
Reconciler correctly marked the order canceled in storage but no
counter sell was placed, leaving $10 of BTC orphaned from the
strategy. Pattern would recur on every host-crash mid-fill, so
the v1.1 case for fixing this is real.

### Mid-session reconciliation

**What:** the reconciler runs on a schedule (e.g. every 5 minutes)
during a session, not just at startup.

**Why deferred:** ADR-018 ratified startup-only. Mid-session
reconciliation races the engine's own order-placement path; the
existing model is safer.

**Trigger:** the operator runs `cli/live` for weeks without
restart AND drift accumulates between Kraken and storage in ways
startup-only doesn't catch. Soak window is the first place this
would surface.

### `cli/reconcile` background worker — ledger-level diff

**What:** a new always-on daemon (sibling to cli/maintenance)
that periodically pulls Kraken's `/0/private/Ledgers` endpoint
and diffs every wallet movement against live.db's `trades` rows
by `refid` / `exchange_id`. Writes findings (missing-on-either-
side, amount drift > fee precision, unknown refids) to a new
`reconciliation_findings` table in operator.db, surfaced on the
web /audit page.

**Why this is distinct from the existing reconciler:**
`services/reconciler.py` reconciles **open orders only**, at
startup. It does not see closed-trade history. If Kraken executes
a fill that the bot's websocket / poll misses (e.g., transient
network loss during a fill notification), live.db diverges from
ground truth and there's currently no detector. Soak day 6
surfaced this concern when a USD-vs-BTC split looked off; the
gap turned out to be a display interpretation bug not a real
divergence, but the audit revealed we have no external-truth
ground for trades.

**Why deferred to v1.1:** v1.0 has no reported case of an
undetected fill divergence. The 2026-05-23 soak audit was a
false-alarm — the bot's ledger matched Kraken's `/Ledgers`
within fee precision once Kraken's "Available balance" was
correctly decomposed (USD-equivalent of unreserved assets, not
USD-only). Adding a daemon for a hypothetical drift the soak
hasn't actually shown is over-investment pre-v1.0.

**Sketch:** new `cli/reconcile` follows the cli/maintenance
pattern — `run_poll_loop` over a 1-hour cadence by default,
`KrakenAdapter.get_ledger(since=last_known_refid)` (new ~30
lines wrapping `/0/private/Ledgers`), diff against
`storage.get_trades(since=...)`, write findings.
`ReconciliationConfig` with `target_dbs`, `cadence_seconds`,
`finding_db`. Honors the same `safe_shutdown` pattern as the
other daemons.

**Why a separate daemon rather than extending the existing
reconciler:** ADR-018's startup-only invariant is load-bearing
for the open-order path (it races the engine's own placements).
A ledger-level checker is read-only against history and can't
race anything, so it's safe to run continuously and benefits
from its own cadence + own audit log.

**Companion fix that lands in v1.0:** the cycle_matcher was
mispairing BUYs and SELLs by FIFO-cheapest rather than by
counter-amount equality; this caused Today's PnL to read
+$0.0035 when the engine's actual realized cycles netted
+$0.10. Fixed 2026-05-23 by switching the matcher's primary
heuristic to amount-equality (engine's ADR-006 decision 2
invariant) with FIFO-cheapest as a fallback for pre-engine /
manual fills.

### Graceful-shutdown timeout for daemons (`cli/web` et al.) — ✅ shipped in v1.0 (2026-05-23)

**Status:** ✅ Promoted to v1.0 and shipped 2026-05-23. The entry
was originally classified v1.1.A (first post-tag candidate) but the
operator made the call during soak day 6 to bump it into v1.0 since
the workaround discipline (`Stop-Process -Force`) was the only
observed friction during the entire soak window. Commits:

- `49e53a7` — `wobblebot.cli._common.safe_shutdown` helper + 7
  unit tests
- `34c9619` — wired 5 poll-loop daemons (observe / news / advise /
  harvest / operator) through safe_shutdown with named phases
- `a998b71` — wired cli/maintenance
- `8a85cbd` — wired cli/web + added uvicorn
  `timeout_graceful_shutdown=5` (caps in-flight-request waiting)
- `516f4f8` — wired cli/live's OUTER finally (resource close);
  the INNER finally with `cancel_all_open` is intentionally NOT
  routed through safe_shutdown because Kraken cancellation is
  the most safety-critical cleanup and a hard wall-clock cap
  could exit before all cancels complete

Combined budget: uvicorn's 5s graceful shutdown + safe_shutdown's
10s on the resource-close finally = ~15s worst case vs the soak's
observed 3+ minutes. If the resource-close phase hangs beyond 10s,
`os._exit(1)` releases the terminal with a WARNING naming the
stuck phase.

The original entry body is retained below for historical context
in case future contributors want to understand the decision trail.

---

**What:** every `cli/*` daemon's SIGINT/SIGTERM handler logs an
"exiting clean" line BEFORE the actual cleanup runs. If any
cleanup step hangs — `aiosqlite` close on a busy WAL, uvicorn
worker shutdown blocked on an in-flight request, a non-daemon
thread that never gets joined — the process stays alive
indefinitely. Add a wall-clock timeout (proposal: 10 seconds)
around the cleanup phase; on `TimeoutError`, log
`shutdown hung beyond 10s in phase=<x>; forcing exit` at WARNING
and call `os._exit(1)` to release the terminal.

**Why deferred:** soak Day 3 (2026-05-20) surfaced the failure
mode after the operator bounced `cli/web` to pick up consecutive
commits. The exit-clean log had already fired but the process
held PID 13420 alive for 3+ minutes, locking the operator's
PowerShell window. Force-killing recovered. The pattern is
shared across every `cli/*` daemon — `cli/live`, `cli/harvest`,
`cli/maintenance`, etc. all have the same exit shape, just
haven't tripped the failure mode visibly yet. v1.0 documentation
freeze + the workaround being a single `Stop-Process -Id <pid>
-Force` makes this a defer-with-paper-trail rather than a
must-fix.

**How:** factor each daemon's shutdown into named phases (close
storage, stop uvicorn, drain background tasks, etc.) and wrap
the orchestration in `asyncio.wait_for(_shutdown_all(),
timeout=10)`. The shared `cli/_common.run_poll_loop()` helper
(Stage 8.0.C) already brackets shutdown discipline for five
daemons — this would extend it. `cli/web` and `cli/live` don't
use the shared poll loop and need separate per-daemon wiring.
Add a soak-runbook note that "exiting clean log without process
death within 10s" is a known failure to expect occasionally
during v1.0 and that `Stop-Process -Force` is the documented
recovery.

**Trigger:** ORIGINAL: "any further soak-period bounce that
exhibits the pattern." UPDATE 2026-05-21 16:55: fourth observed
instance (Day 3 evening + Day 4 midday + Day 4 afternoon). The
case is now made — promote from "watch" to **v1.1.A** (first
candidate to land post-v1.0 tag). Operator-facing impact:
every cli/web bounce during soak risks a hung process holding
the terminal until ``Stop-Process -Force`` is run. Workaround
is documented; experience cost is real, repeated, and
predictable.

### `cli/preflight` ADR-003 key-scope verification

**What:** extend `cli/preflight` to verify ADR-003's load-bearing
invariant programmatically — the trade key must NOT have Withdraw
scope, and the harvest key (when configured) MUST have Withdraw
scope but NOT Trade scope. Today the separation is documented in
`.env.example` and the operator-handoff section of `CLAUDE.md`,
enforced operator-side at Kraken key-creation time. The 2026-05-23
security audit flagged this as a v1.1 L3 gap.

**Why deferred from v1.0:** Kraken's REST API doesn't expose an
endpoint that returns "this API key has these scopes" directly.
The audit agent's first-pass suggestion was a test-withdrawal
attempt with `validate=true` — **rejected as dangerous**: that's
a real signed `/0/private/Withdraw` request, even with
`validate=true` it surfaces audit-log noise on the operator's
Kraken account and risks key disabling under Kraken's API-abuse
heuristics.

**Safer paths to investigate:**

1. **`/0/private/GetWebSocketsToken`** — returns a token + metadata
   that includes the key's permission set on some Kraken API
   versions. Verify whether the current API surface still includes
   permission info here.
2. **Negative-probe at an idempotent read endpoint** — call
   `/0/private/ClosedOrders` with the trade key (should succeed
   because the trade key has `Query Open + Closed`) and a
   theoretically-Withdraw-only read with the harvest key. Inferred
   scope detection from which calls return `EAPI:Invalid permissions`.
3. **Operator-side documented check at `cli/preflight` startup** —
   surface the expected scope set + tell the operator to verify
   against pro.kraken.com/app/settings/api before proceeding.
   Cheapest path; no API call but no programmatic enforcement
   either.

**Why high-value:** ADR-003's "trade key can't move money, harvest
key can't trade" is the load-bearing safety property the whole
financial-power-fragmentation design rests on. Today an operator
who accidentally enables Withdraw on the trade key wouldn't notice
until something bad happened. A preflight check makes the
mis-configuration loud at startup time, not at incident time.

**Implementation outline:**
- Investigate which Kraken endpoint surfaces scope info (Path 1
  research first, fall back to Path 2 or Path 3 based on what's
  actually available)
- Add `services/kraken_key_audit.py` with one async function per
  key role (verify_read_only_key, verify_trade_key,
  verify_harvest_key)
- `cli/preflight` calls the appropriate audit functions; refuses
  to exit 0 if any key's actual scope set diverges from its
  expected set
- Friendly error output identifying which key has the wrong
  scopes + a pointer to the operator-side fix on pro.kraken.com

**Trigger:** v1.1 hardening. Operator-flagged 2026-05-23 during
the financial-grade security audit as a queue-not-skip item.

### Partial-grid placement: WARN → INFO with degraded-state context

**What:** when the engine lays out a fresh grid and Kraken
refuses one or more orders due to insufficient balance, the
current log line is:

```
[WARNING] wobblebot.services.grid_engine: order refused by exchange: insufficient balance
```

That reads as a scary edge case — operator sees the WARN, assumes
something's broken. In practice it's the expected response when
BTC inventory is below the full SELL-layout target (or USD below
the BUY target). The engine handles it correctly: places what it
can, moves on, no retry loop. The log just over-states the
severity.

**Proposed:** demote to INFO, include the degraded-but-correct
state in the message::

```
[INFO] partial grid placed: 3 BUYs + 2 SELLs of 3 target;
       BTC inventory below full SELL layout target (need
       0.000126 BTC for SELL @ $79,180; have 0.000011)
```

Operator immediately sees what got placed, what didn't, and why.
No alarm, just a status line. The current WARN-level alarm
should be reserved for genuine refusals (rate limit, auth, bad
parameters).

**Implementation outline:**
- In `services/grid_engine.py`, where the order-place loop
  catches Kraken's `EOrder:Insufficient funds`, count per-side
  placed-vs-target.
- After the layout completes, emit ONE summary INFO line if
  any leg was skipped, with the placed/target counts + the
  inferred insufficiency (BTC for skipped SELLs, USD for
  skipped BUYs).
- Genuine errors (rate limit, auth, validation) keep the
  per-order WARN.

**Why deferred to v1.1:** operator-flagged 2026-05-23 immediately
after a fresh cli/live restart where 1 SELL got skipped due to
short BTC inventory. The behavior is correct; only the messaging
is misleading. Doesn't block v1.0; ships as a quality-of-life
refinement.

**Companion concern:** the duplicate-storage-row issue surfaced
during the same restart (5 storage-only reconciliations where
only 3 unique exchange_ids existed; some orders had two rows at
different precisions). Cleaner v1.1 entry pending — likely a
reconciler edge case worth tracing before fixing the partial-
grid messaging, since both affect the operator's restart
experience.
