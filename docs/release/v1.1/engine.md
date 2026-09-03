# Engine — reliability and performance

*Engine-side improvements: safety nets, reconciliation extensions, real-time data paths, performance optimizations. Touches `services/grid_engine.py`, `services/reconciler.py`, and `KrakenAdapter`.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Bug: ADR-038 fee-drift check false-positives on dust trade fragments

**Confirmed 2026-08-28.** A DOGE/USD fill triggered `fee drift on DOGE/USD:
realized 0% matches neither maker 0.4% nor taker 0.8%`
(`_check_fee_drift`, `services/grid_engine.py:1338`) — read as a possible
Kraken fee-schedule change, but isn't one. Traced against the raw
`trades` rows: Kraken split the fill into two trade records 34ms apart
on the same order (`OFNBLB-XSSBM-5NH4LA`) — the real trade (68.97 DOGE,
$6.00 cost, $0.024 fee = exactly 0.4000%, correct) plus a dust remainder
(0.00000021 DOGE, $0.000000018 cost, $0 fee). The guard is `if
trade.cost <= 0: return` — the dust trade's cost is a tiny *positive*
number, so it slips past, and `0 / ~2e-8` computes to exactly 0%,
trivially outside the 5bps tolerance against either rate.

**No accounting impact** — the real trade's fee is correct and already
persisted correctly; this is a false-positive alarm, not a money bug.

**Fix:** give the guard a materiality floor (e.g. skip trades below the
pair's `costmin`, not just `> 0`) instead of a bare positivity check.
Small, well-scoped, low-risk — a one-line guard change plus a test
fixture reproducing a real+dust trade pair from the same order.

**Trigger:** next time `services/grid_engine.py` is touched, or if the
false-positive recurs enough to be noisy. Not urgent — it's a rare
Kraken-side split, not a routine occurrence (the last several DOGE fills
before this one were clean single-trade fills).

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

### Server-side dead man's switch (`CancelAllOrdersAfter`) — ✅ SHIPPED 2026-06-01 (v1.1, ADR-021)

**✅ Shipped 2026-06-01** (ADR-021), **fast-forwarded into `main` and now DEPLOYED** in the
multi-coin soak: `ExchangePort.set_dead_mans_switch` + per-tick pet in `cli/live`'s loop, ON by
default (`live.dead_mans_switch_seconds`, `null` disables; soak config 120s), disarmed only on a
confirmed-clean shutdown cancel (left armed otherwise so Kraken sweeps stragglers). Synthetic
adapters no-op; shadow deliberately does not arm a real timer.

**Validated + hardened on the soak (2026-06-02):** the arm itself works — verified live via
`tools/check_dead_mans_switch.py` (Kraken `triggerTime` = `currentTime` + timeout). But the
multi-coin restart exposed a real defect: a rate-limited shutdown couldn't fetch open orders, the
old `_cancel_all_open` swallowed that as `(0,0)` → the caller read `cancel_clean=True` → it
**disarmed the switch**, leaving ~15 orders open AND unprotected ~10 min. **Fixed in `abf3aa6`**
(the global fetch now *propagates* on failure → `cancel_clean=False` → the switch stays armed),
regression test `8b25feb`. Remaining v1.1 hardening (P1, defense-in-depth): confirm the arm
in-loop by logging Kraken's `triggerTime`, and optionally refuse to place orders when the switch
isn't confirmed-armed. See ADR-021 + `docs/reference/kraken-api-reference.md`. Original backlog
rationale preserved below.

**Follow-up (2026-09-03 incident — a second real DMS purge):** six
consecutive `CancelAllOrdersAfter` transport failures (07:01:13Z →
07:02:37Z; Kraken public endpoints and the cloud LLM calls were fine
throughout, and status.kraken.com listed nothing) let the 120s timer
purge the remaining SOL/DOGE book at ~07:02:38Z. All three
2026-08-20 follow-ups fired as designed (3-strike critical, recovered
notice, 4-hourly held-symbol reminder). One did not: the book-vanish
page's calmer "most likely Kraken's own timer" framing —
`dms_timer_expired_this_tick` read `False` because the purge landed
≥18s before the client-side `dms_trigger_at` deadline (NAS clock skew
ruled out, ≤2s against a synced desktop). Two small hardening items:
(1) log the last CONFIRMED `trigger_at` at WARNING when a failure
streak starts — today it is DEBUG-only (`_log_dms_confirmation`), so
a post-mortem cannot compare it to the vanish time; (2) base the
framing on the wall-clock length of the in-progress failure streak
(first failure of the streak → now) relative to
`dead_mans_switch_seconds`, not solely on the confirmed-deadline
check — a streak whose first failure was ≥ ~half the timeout ago plus
a whole-book vanish is a DMS purge by any honest reading. (The
existing code comment's "streak COUNT is a poor proxy" objection was
about counts at 5s ticks; it does not apply to elapsed time, and each
failed ping already blocks for the full 10s request timeout.) Receipt
in the roadmap's 2026-09-03 entry.

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

### Capital utilization — added capital and realized profit never reach the trading envelope

> **⚠️ Priority escalated (2026-08-26) — Sub-question 2 stopped being
> theoretical.** `cli/maintenance`'s Capital Reporter (ADR-040) had been
> flagging SOL/USD and ADA/USD daily since 2026-08-23: `order_size_usd`
> couldn't clear Kraken's per-symbol ordermin at current prices (SOL needs
> ≥$6.18, had $5; ADA needs ≥$5.36 at 3 of 6 levels, had $5). Confirmed via
> `wobblebot-live`'s own log: SOL placed **0/6** orders on every re-layout
> attempt for 3+ hours straight — not degraded, completely inert. Applied
> a **stopgap** the same day (per-coin `order_size_usd` overrides, SOL
> $8 / ADA $7, mirroring the existing DOGE precedent below) — confirmed
> live, SOL immediately went to 3/6 placed. **This is a static fix and
> will drift back out of range if either coin rallies enough** — it does
> not touch the underlying gap. Sub-question 2 / Candidate direction 3
> below (balance- and ordermin-aware sizing) is the real fix and should
> be prioritized ahead of the other two sub-questions the next time this
> file's items are picked up — a coin going fully inert is a harder
> failure than a daily-spend cap binding early.

**What:** three linked questions raised by the operator 2026-08-22,
filed together because they share one root: **every USD-denominated
knob is a static config number, and nothing in the running system
ever revises it.** Capital added to the Kraken account, and profit
the bot itself realizes, sit idle unless the operator manually reruns
`cli/recalibrate`.

**The observation that prompted it:** advisor output read
`Risk's high-confidence constraint dominates: daily spend is 98.4%
consumed ($118.02 of $120.00)` against an account that started at
~$250, has since taken a ~$100 operator top-up and realized ~$25 of
trading profit — yet is still cycling on roughly $150 of working
capital, the rest being either stale inventory or headroom the engine
cannot see.

**Sub-question 1 — is `max_daily_spend_usd` too conservative, and
should it recover as cycles complete?**

Verified behavior (`services/exposure.py:111`): `daily_spend_usd()`
sums `price × amount` over BUY orders with `created_after` = midnight
UTC and status in `SPEND_COMMITTED_STATUSES = {open, pending,
closed}`. Consequences:

- It is a **ratchet, not a balance.** A completed round-trip — BUY
  fills, counter-SELL fills, capital returns plus profit — leaves the
  BUY at `closed`, so it keeps consuming the cap for the rest of the
  UTC day. A grid that cycles *well* is penalized: the more
  round-trips it completes, the sooner it locks itself out. The cap
  resets only at midnight UTC.
- Only `canceled` and `expired` release headroom — precisely the
  orders that never moved money in the first place.
- **Fees and LLM costs are not in it.** It is pure notional; the LLM
  budget is a separate gate (`services/llm_cost_gate`) and Kraken
  fees are never added. So the figure is not "money spent today" in
  any ledger sense either — it is "BUY notional committed today,"
  which double-counts capital that has already come back.

The design intent is defensible (it bounds how much fresh exposure
can be opened per day, and it is trivially auditable), but
"committed" and "spent" have quietly diverged from each other and
from the operator's mental model. Worth deciding explicitly: is the
cap meant to bound *new risk opened per day* (current behavior,
ratchet correct) or *net capital deployed* (would need to net out
completed cycles)? Those are different safety properties and only the
first is implemented.

**Sub-question 2 — should order size scale with account balance? Is
that part of re-anchoring?**

No, on both counts. `_place_level` reads `coin_cfg.order_size_usd`
straight from config (`services/grid_engine.py:1595`); it is never
derived from a live balance. Re-anchoring (`request_reanchor` /
`_reanchor_unlocked`, `services/grid_engine.py:744`) recomputes the
**reference price** and re-lays the grid around it — cancelling and
replacing orders at new price *levels*, at the same *size*. Nothing
in the engine resizes an order for any reason.

The lever that does exist is `cli/recalibrate --target-balance N`,
which scales ten config sections proportionally
(`services/calibrator.py`) — per-coin `order_size_usd`, all three
exposure caps, `max_daily_spend_usd`, the session loss cap, and the
four harvester thresholds — and rewrites `settings.yml` under
`--commit`. It is operator-initiated per ADR-012's auto-tuning gate.

Whether it has ever been run here is **not determinable after the
fact**: `services/settings_rewriter.apply_dotted_overrides` writes
atomically (temp file + rename) and leaves no backup, no audit row,
and no log marker — a past run is indistinguishable from an operator
hand-editing the same values. (The `.bak-YYYYMMDD` files on the NAS
are made by hand during config sessions, so they are not evidence
either.) Worth noting as its own small gap: a config-mutating
operator tool with no trace is hard to reason about later. What is
certain is that `settings.yml`'s own sizing comments still describe
the original ~$242 soak, so nothing has re-scaled them since.

The mechanism is built; the gap is that nothing tells the operator
when it is needed, and nothing runs it.

**Sub-question 3 — should realized profit be redeployed?**

Same gap, sharper. Realized profit lands in the Kraken USD balance
and is invisible to the engine: exposure caps are computed from
*orders* (`total_exposure_usd`) and inventory from *trades at cost
basis* (`coin_inventory_cost_usd`, ADR-039) — never from the
account's free USD. Profit therefore neither raises headroom nor gets
reinvested; it accumulates as idle quote currency until the
Harvester's surplus threshold sweeps it out or the operator
recalibrates by hand.

**Candidate directions (not a decision):**

1. **A drift detector, not an auto-tuner.** Cheapest, and most in
   keeping with ADR-002 / ADR-012: a daily check comparing live free
   USD + inventory-at-cost against the balance the config was
   calibrated for, alerting when the ratio drifts past a threshold
   (say ±25%). Surfaces "you have ~$100 of unused capital; consider
   `cli/recalibrate --target-balance 350`" and leaves the call to the
   operator. Could ride `cli/maintenance` alongside the reconcile
   task.
2. **Make the daily cap net of completed cycles** — change
   `SPEND_COMMITTED_STATUSES` semantics so a BUY whose counter-SELL
   has filled stops consuming the cap. Needs an ADR: it materially
   loosens a safety cap, and `closed` was included deliberately after
   an incident (see the `services/exposure.py` module docstring).
3. **Balance-aware sizing** — derive `order_size_usd` from a fraction
   of free balance at layout time rather than from config. Rejected
   on first pass as an over-reach: it makes order size
   non-deterministic, breaks the schema-drift / example-config
   contract, and makes backtests non-reproducible. Recorded only so a
   later reader knows it was considered.

**Why deferred:** all three sub-questions touch safety caps or money
sizing, so none is a "while we're in here" change. (1) is the only
one that could ship without an ADR — it is advisory output, no
behavior change. (2) and (3) both loosen guardrails and need
ratification.

**Trigger:** review alongside the P4 advisor-feedback work, or sooner
if the daily-spend cap starts short-circuiting cycles regularly (the
98.4% reading above is the first observation of it binding) — **or
sooner still**, since sub-question 2's trigger already fired
2026-08-26 (see the escalation note above): a coin going fully inert
on its ordermin is the concrete case, not a hypothetical one anymore.
Running `cli/recalibrate` in its default dry-run mode is worth doing
now regardless — it costs nothing and writes nothing.

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

### operator.db SQLite write-contention: busy_timeout + retry-on-lock

**What:** tune the SQLite connection's `busy_timeout` higher than
the aiosqlite default (effectively 5s via Python `sqlite3.connect`
default), AND wrap the small handful of operator.db writers
(emit_heartbeat, save_notification, save_pending_command) in a
short retry-on-`database is locked` loop. Three retries with
50ms backoff would absorb almost all real contention.

**Why high value:** the 2026-05-25 emit_heartbeat-visibility fix
(commit `4868613`) surfaced its first real ``database is locked``
event the same afternoon:

```
2026-05-25 17:34:05  WARNING wobblebot.cli.heartbeat:
  heartbeat emit for cli/operator failed:
  StorageError: Failed to upsert heartbeat for 'cli/operator':
  database is locked; continuing
```

Both cli/live (heartbeats every 5s) and cli/operator (heartbeats
every 2s via forwarder cadence) write to ``operator.db``'s
``daemon_heartbeats`` table. WAL mode allows concurrent readers
but serializes writers; a write that can't get the lock inside
``busy_timeout`` falls through to ``SQLITE_BUSY``. The
heartbeat's `except WobbleBotPortError` catches it, logs, and
continues -- correct safety behavior, but it means the
``/health`` view's freshness threshold can briefly flap to STALE
for whichever daemon lost the race.

**What's missing:**

- An explicit ``PRAGMA busy_timeout = 30000`` (or similar) call in
  ``SQLiteStorageAdapter.connect()`` after WAL setup. The
  aiosqlite default isn't documented to track Python `sqlite3`'s
  5s default, and a longer wait is cheap insurance for the
  hot-write daemons.
- A small `services/storage_retry.py` helper:
  ``async def retry_on_locked(callable, attempts=3, backoff=0.05)``.
  Used by the three known concurrent writers (emit_heartbeat,
  save_notification, save_pending_command). Doesn't generalize to
  every storage method -- only the ones with documented contention.
- *(deep-scan 2026-06-02)* The same `busy_timeout` floor is **missing
  entirely** from the raw synchronous `sqlite3.connect` calls in
  `services/maintenance.py:70` (VACUUM / prune) and
  `services/backuper.py:115-117` — these bypass
  `SQLiteStorageAdapter.connect()`, so they inherit no adapter PRAGMA
  at all (only Python's implicit 5s default). Pass `timeout=30` to
  those three connects when the adapter PRAGMA above lands. The
  adapter half of this fix is the well-covered part; this raw-connect
  sliver is the net-new piece the cross-reference surfaced.

**Why deferred:** the failure mode is benign today (heartbeat
warnings, no data loss). The fix is small but the verification
that "we didn't introduce a deadlock" requires the SQLite
concurrency stress test above to land first. They pair naturally.

**Trigger:** ship after the SQLite concurrency stress test; or
earlier if ``database is locked`` warnings cross a frequency
threshold the operator finds annoying.

**Cross-references:** pairs with the **SQLite concurrency stress
test** entry above; the stress test provides the verification
harness, this entry is the concrete fix it would validate.

### Order-lifecycle fill-vs-cancel + partial-fill recovery (reconciler + F1) — blueprint

**What:** one root cause — the untreated state `Order.status in
(canceled, expired) AND filled_amount > 0` — bites at two sites: the
per-tick `_detect_fills` gate (**F1**: drops the partial's `Trade` rows +
skips the counter) and the startup **reconciler** (marks a storage-only
order `canceled` without checking whether it actually *filled* — the
2026-05-19 orphaned-$10-BTC class). Fix both behind one shared helper.

**Blueprint — settled 2026-06-03** (two independent feature-dev architecture
passes + an adversarial judge on the contested fork):

- **Shared resolver** in `services/reconciler.py` (`_resolve_terminal_order`):
  given a departed order, `get_order_status` → classify
  `closed`/`partial_cancel`/`clean_cancel`; for the first two, save the
  `Trade` rows + the terminal-status order. Both `_detect_fills` and the
  reconciler call it (read-side helper; no new module).
- **QueryOrders, not `ClosedOrders`** — *simplifies the original plan.*
  `get_order_status` (QueryOrders) already exists on the port + adapter and
  targets a known `exchange_id`; a paged `ClosedOrders` scan is unnecessary.
  Widen the reconciler's `_AdapterLike` Protocol to add `get_order_status`.
  (The `ClosedOrders` mention in the old deferral note was a rough soak note,
  not ratified — this supersedes it.)
- **Counter-replay = the engine places it, not the reconciler** — settled by
  the adversarial judge on *safety* grounds: the reconciler is constructed
  *after* the engine (`live.py:664` vs `:700`) and is *documented* never to
  place/cancel (`reconciler.py:226`); a reconciler `place_order` would breach
  the power-fragmentation design **and** runs outside the engine's per-symbol
  lock. Instead the reconciler adds the order UUID to
  `ReconciliationReport.needs_counter_order_ids`; `GridEngine.__init__` takes
  `pending_counters`; the first `_tick` places them inside the
  `if not offside:` block (recovery counters inherit the offside suppression).
- **Retry-on-failure (the judge's correction):** a pending counter that fails
  placement **stays** in `pending_counters` and retries next tick — do NOT
  discard on failure. Discard-on-failure + the auto-re-layout guard
  (`grid_engine.py:407-423`) would re-place a full grid with no counter →
  **reproduce the very orphan bug** this fixes. Decisive point.
- **Full safety caps at startup** — `_check_safety` reads storage live; there
  is no uninitialized session accumulator, so the caps are correct at boot.
  No reduced-check mode (a first-pass design got this wrong; verified).
- **No new domain method** — reading `filled_amount` after the refresh
  suffices; `_apply_kraken_order_update` already produces the canceled+partial
  state.
- **Idempotency:** the terminal-status `save_order` drops the order from
  `_detect_fills`' `status=open` candidate filter (`grid_engine.py:497-504`),
  so the tick never re-processes it. Self-heals across a
  crash-between-reconcile-and-tick (next boot re-flags the still-terminal
  order; cost = one-boot delay, no double-counter).
- **Test infra:** the `MockExchangeAdapter` can't produce canceled+partial —
  add an `inject_partial_cancel` control method (mirrors
  `_apply_kraken_order_update`'s field-assignment bypass); reconciler unit
  tests use a purpose-built fake adapter. Tests per outcome at both sites +
  the no-double-counter case.
- **Build order:** ADR → mock fixture → shared resolver + tests → Site 1
  (`_detect_fills` gate) → Site 2 (reconciler + `needs_counter_order_ids`) →
  engine `pending_counters` + `_process_pending_counters` → wire `cli/live` +
  `cli/shadow`.

**Trigger:** the 2026-05-19 overnight outage (cli/live down while a buy filled
→ orphaned $10 BTC) + the 2026-06-03 live ADA dust-fill confirming Kraken
fragments orders into sub-ordermin partials. Recurs on every host-crash
mid-fill.

### Slippage / spread guard — blueprint

**What:** refuse to place grid orders when the bid/ask spread is too wide (a
market-quality gate). New ADR.

**Blueprint (2026-06-03):**

- **`get_ticker`, not `get_order_book`** — *simplifies the original plan.*
  Bid/ask (`a[0]`/`b[0]`) are already in the Kraken Ticker response the adapter
  fetches every tick (it reads only `c[0]`/last today). A new
  `get_ticker(symbol) -> Ticker` value object (last/bid/ask + `spread_percentage`
  + a `bid<ask` validator) extracts the spread at **zero extra API calls**;
  chosen over widening `get_current_price` (9 callers, most need only last).
  `_step_unlocked` calls `get_ticker` in place of `get_current_price` → net one
  read per tick.
- **Pre-tick gate** in `_step_unlocked` (skip the whole tick if
  `spread > limit`) — *not* a 5th `_check_safety` arm: spread is a per-symbol
  market signal, not a per-order invariant, and per-order would re-fetch N×/tick.
  New `StepAction` skip-variant.
- **Config:** `max_spread_percentage` on `SafetyConfig` (default 1.0% — never
  fires on healthy BTC/ETH ~0.01–0.05%; None/0 disables). Per-coin override on
  `CoinGridConfig` deferred (YAGNI) until a thin alt needs it.
- **Log-flood guard:** reuse the offside heartbeat (`_OFFSIDE_SUMMARY_EVERY_TICKS`)
  — a sustained wide spread otherwise floods at 5s cadence.
- Adapters: Kraken reads a/b/c; Mock gets `set_spread` + a default tight spread
  (so existing engine tests don't trip); Shadow forwards to live.

**Trigger:** higher priority once multi-asset ships (thin alts dislocate off-hours).

### Session-loss-cap cool-down — blueprint

**What:** after `cli/live` exits on the loss cap (`exit_code=1`), refuse to start
a new session for a cool-down window; `--ignore-cool-down` bypasses. New ADR.

**Blueprint (2026-06-03):**

- **Persist in a new `live.db` table** (one row per loss-cap trip: `tripped_at`
  + `session_pnl`), written in `_run_loop`'s `finally` (own try/except,
  resilient) when `exit_code==1`; queried by a pre-loop gate in `_main_async`.
  Rejected: a state file (second source of truth) and parsing the notifications
  table (`operator_db` is optional → no persistence without Discord). StoragePort
  gains `record_cap_trip` + `get_last_cap_trip_at`.
- **New exit code 4** for a cool-down refusal — distinct from 2 (creds/config)
  so restart policies / a future `cli/up` can tell "give up" from "try again
  later."
- **Fail-open** on a storage-read error at the gate (log WARNING, proceed) —
  fail-closed + `restart: unless-stopped` would crash-loop (the docker G1
  lesson). The cool-down is a safety *feature*, not a safety-*critical* invariant.
- **Scope:** only `exit_code=1`; not shadow/sandbox (synthetic ledgers).
  `--ignore-cool-down` is terminal-only (not YAML-settable) so a Portainer
  redeploy can't standing-bypass it, and it does NOT clear the record.
- **Default window = operator's risk-tolerance call** (the two design passes
  split 30 vs 60 min from the same soak evidence) — it's a config knob, set to
  taste. The gate lives in a small `services/cool_down.py` helper for testability.

**Trigger:** the soak's 4:22am loss-cap trip (a too-low $5 cap → MTM drawdown) —
a knee-jerk relaunch would have dropped straight back into the losing condition.

**ADR numbering (RESOLVED 2026-06-05):** the three blueprints above are assigned, in the
global v1.1 sequence, as **ADR-023** (reconciler/F1 terminal-order resolution),
**ADR-024** (session-loss-cap cool-down), and **ADR-025** (pre-placement spread guard) —
bodies written in `docs/architecture/decisions.md`. (Not all "ADR-022", which is the
shipped advisor-reorientation ADR.)

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

### Zero-order layout starvation: back-off / re-park instead of per-tick retry — ✅ SHIPPED 2026-08-09 (P3 slice 11)

> Shipped to this design with the tick-based back-off variant: starved
> state on placed == 0, one WARNING with the counts, retry every 60
> ticks (about 5 min) with the standard heartbeat, any placement
> clears. All three layout sites participate, including
> `request_reanchor` — the original incident's 0/6 now backs off
> immediately.

**What:** when a layout places **zero** of its target orders, the
symbol ends the tick onside with no open orders — so the engine's
no-orders self-heal (`no open orders detected; re-laying out grid
at existing anchor`) fires again next tick, and every tick after,
indefinitely. Each cycle re-attempts the full layout and re-fails
the same way.

**Observed live (2026-08-09, ADR-031 re-anchor e2e test):** the
first operator re-anchor moved BTC/USD from a stale offside anchor
(74,769.80) to current price (65,193.50) and placed `0/6` — 3 BUYs
refused (free USD ≈ $2.81; ETH's 3 open BUYs reserved $15 of the
$17.81 balance) and 3 SELLs deferred by the cost-basis sell guard.
The engine then looped the re-layout **every 5s tick for ~11
minutes** until the operator paused the symbol. Roughly one balance
round-trip per tick plus two INFO lines — not dangerous, but a
busy-wait with no exit condition and no operator signal.

**Why it's silent:** the insufficient-balance refusal was
(correctly) demoted to DEBUG in v1.1 (`e05a931` — it's an expected
per-tick condition), and the sell-guard deferral logs once, not per
retry. So the only INFO evidence is the re-layout pair itself,
which doesn't carry the symbol or the placed-count in the message
string (see the logging-quality audit item, `observability.md`).

**Proposed:** after a layout completes with `placed == 0`, treat
the symbol as **starved**: skip the self-heal re-layout for a
backoff window (e.g. reuse the offside park shape — check
cheaply each tick whether conditions changed: free quote balance
above one order's notional, or a sell no longer below basis —
and only then re-attempt), and emit ONE WARN with the refusal/
deferral breakdown so the operator learns about it once. A
partial layout (`placed >= 1`) keeps today's behavior — the
standing orders make the next tick's no-orders check moot.

**Relation to the entry above:** the partial-grid item is the
*messaging* for the degraded-but-working case; this is the
*behavior* for the fully-starved case, which the "places what it
can, moves on, no retry loop" claim above turns out not to cover —
with zero placed there IS a retry loop, via the self-heal.

**Why deferred:** surfaced 2026-08-09 by the first live re-anchor;
self-resolves when funds free or price recovers, and `pause` is a
clean manual stop. Queue behind the P3 re-anchor chain work.

**Follow-up (2026-09-03, observed on XRP/USD):** the back-off works
as shipped — retry every 60 ticks — but each retry still emits the
per-level `refused by safety cap` WARNINGs from `_try_place`
(safety-cap refusals were never demoted alongside the
insufficient-balance ones), so a symbol that is starved *by
construction* logs ~10 lines every ~6 minutes, indefinitely. XRP/USD
after the 2026-09-02 15:57Z re-anchor to 1.33471 is the canonical
case: three BUYs refused by `max_per_coin_inventory_usd` and all
three SELLs deferred by the cost-basis guard (average cost 1.471
against a band top of 1.4548) — nothing can ever place at that
anchor, and the log carried ~2,400 lines of it per day for the 23
hours until the operator acted. Proposed (logging half only, no
behavior change): log the cap-refusal breakdown ONCE on entering the
starved state (it is the same information every retry), demote the
per-retry cap-refusal lines to DEBUG while starved, and add a "still
starved after N retries" WARNING carrying the same breakdown so a
permanently-starved symbol stays visible without being noisy.

### Sell-side-only extension while offside-high — take profit into strength (operator question 2026-09-03; ADR-006 amendment)

**The question, verbatim:** "When the selling price is favorable to our
held inventory, can offside clear one side of activity and hold the
rest?"

**Today: no.** Offside is per-symbol and all-or-nothing — `_tick` does
no re-layout and places no counters while `offside`, and leaves
whatever is already resting. On the way UP the sell side clears itself:
the ladder's sells fill as price climbs through them (DOGE/USD's 0.0885
sell filled at 15:52Z on 2026-09-03, right before the symbol went
offside above its band). Once price is above the top level there is
nothing left to sell into — the engine creates no new sell levels above
price, and a fill detected while offside gets no counter. Held
inventory that would now sell at a profit just sits. The buys below
keep resting, which is correct.

**Proposed:** while a symbol is offside ABOVE its band and the cost-basis
guard says a sell at the current price would be profitable, lay fresh
SELL levels at +1, +2, +3 spacings above the current price (a trailing
sell ladder) and leave the buy side parked exactly as ADR-006 requires.
Nothing about the buy side changes — no re-centering, no fresh buys on
the way down — so the trend-chasing scenario ADR-006 rejects is
untouched; this is taking profit into strength, the opposite trade. The
cost-basis guard (ADR-032) already vetoes loss sells, and the inventory
caps (ADR-039) are relieved, not strained, by every fill.

**Why it needs an ADR, not a slice:** ADR-006 decision 1 ("stay parked;
rejected: automatic re-centering") is ratified, and this is a partial,
one-sided re-centering. The amendment should state the asymmetry
explicitly — sells may extend, buys never do — and pin it with a test
that an offside-LOW symbol places nothing (the mirror case is exactly
the loss-averaging the stranded-capital HOLD ruling forbids). It
interacts with ADR-031: a manual re-anchor is today's equivalent
(SOL/USD's 2026-09-03 re-anchor at 104.29 produced three sells above
price and buys the caps refused); the proposal automates that sell half
only, and leaves the anchor where it is so a return to onside finds the
original grid intact.

**Open questions for the ADR:**
1. Cadence while it keeps rallying — extend once per band exit, or
   ratchet upward as each +1 fills?
2. Size — the standard `order_size_usd` per level, capped by held
   inventory above cost.
3. Anchor — unchanged (the extension is a ladder ABOVE the band, not a
   new grid).
4. Dashboard — the OFFSIDE badge should say the extension is live
   ("3 sells trailing above"), which the offside-popover entry in
   `operator-ux.md` can carry.

**Trigger:** operator question 2026-09-03, on a day BTC, ETH, DOGE and XRP
all sat offside ABOVE their bands with sellable inventory. Medium (M) +
ADR.
