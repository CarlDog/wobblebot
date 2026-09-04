"""Operational CLI — run the live grid engine against real Kraken.

Run as a module::

    python -m wobblebot.cli.live
    python -m wobblebot.cli.live --profile conservative
    python -m wobblebot.cli.live --symbols BTC/USD,ETH/USD --tick-seconds 10
    python -m wobblebot.cli.live --max-session-loss-usd 2 --max-runtime-minutes 30
    python -m wobblebot.cli.live --config /path/to/custom-settings.yml

**Real money trading.** Every tick may place, cancel, or refresh real
orders on Kraken. Use ``cli/preflight`` first to verify Kraken accepts
your config without spending anything; only then run this.

Configuration layering (per ADR-009):

1. **Base config** — ``config/settings.yml`` (or ``--config path``,
   or ``config/settings.example.yml`` as a last-resort fallback).
2. **Profile overrides** — if ``--profile name`` is passed, the named
   block from ``profiles:`` deep-merges over the base.
3. **CLI flag overrides** — explicit flags below win over both YAML
   and profile. Omitted flags inherit YAML values.

Multi-symbol since Stage 2.4: ``--symbols BTC/USD,ETH/USD,DOGE/USD``
or set ``live.symbols:`` in the YAML. Each tick steps every symbol in
series; per-symbol asyncio.Lock keeps them re-entrant-safe.

On shutdown (any reason): every open order for every configured
symbol is cancelled in the ``finally`` block. Exit codes: 0 clean
(signal/runtime), 1 loss-cap tripped, 2 missing credentials.

Loads trade credentials from ``KRAKEN_TRADER_API_KEY`` /
``KRAKEN_TRADER_API_SECRET``.
"""

# pylint: disable=too-many-lines
# One cohesive daemon loop (session lifecycle, per-tick fetch batching,
# dead man's switch, cool-down gate, shutdown cleanup); splitting it would
# fragment a single control flow across files for no organizational gain.

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

from wobblebot.adapters.kraken_exchange import (
    KrakenAdapter,
    is_permanent_auth_error,
    is_temporary_lockout,
)
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    CONFIG_LOAD_ERRORS,
    add_config_args,
    collect_overrides,
    config_load_exit,
    emit_engine_state,
    emit_heartbeat,
    identity,
    install_signal_handlers,
    load_operator_env,
    missing_section_exit,
    notify,
    parse_symbol_csv,
    partition_or_exit,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.config.cli import LiveConfig, ScreenerConfig
from wobblebot.config.grid import (
    KRAKEN_MAKER_FEE_RATE,
    KRAKEN_TAKER_FEE_RATE,
    GridConfig,
)
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.grid import compute_grid_levels
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Symbol, Ticker, Timestamp, fmt_decimal
from wobblebot.ports.exceptions import (
    ExchangeError,
    OperatorError,
    StorageError,
    WobbleBotPortError,
)
from wobblebot.ports.notification_events import (
    CommandResultEvent,
    FillEvent,
    LossCapEvent,
    SessionEndEvent,
    SessionStartEvent,
)
from wobblebot.ports.notifier import NotifierPort
from wobblebot.ports.operator import CommandResult, ExecuteProposalCommand, OperatorCommand
from wobblebot.ports.storage import StoragePort
from wobblebot.services.cool_down import check_cool_down
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService
from wobblebot.services.reconciler import apply_reconciliation
from wobblebot.services.screener import (
    ScreenerRanking,
    SymbolMetrics,
    compute_symbol_metrics,
    rank_candidates,
)
from wobblebot.services.symbol_priority import proximity_in_atr, sweep_order

_LOGGER = logging.getLogger("wobblebot.cli.live")

# How often (in consecutive ticks) to re-emit a "still not confirmed
# armed" WARNING for the dead man's switch, mirroring GridEngine's
# offside transition + heartbeat pattern -- never a WARNING every tick.
_DMS_UNCONFIRMED_SUMMARY_EVERY_TICKS = 240

# ADR-027: pace successive shutdown CancelOrder calls so the cleanup
# path itself can't re-trigger a Kraken rate-limit storm during the
# most safety-critical cleanup (DMS-armed shutdown).
_INTER_CANCEL_PACING_SECONDS = 0.2

# ADR-034: the command kinds THIS daemon may dispatch. Derived from the
# OperatorCommand union rather than hand-listed, so a new engine command
# is picked up automatically — while ExecuteProposalCommand, which lives
# outside that union, stays invisible to this poll. cli/harvest owns it.
_ENGINE_COMMAND_KINDS: tuple[str, ...] = tuple(
    variant.model_fields["kind"].default for variant in get_args(get_args(OperatorCommand)[0])
)


# ---------------------------------------------------------------------------
# Loop helpers — same shape as before, now consume LiveConfig directly
# ---------------------------------------------------------------------------


async def _cancel_all_open(  # pylint: disable=too-many-locals
    # R0914 disable: every local is a distinct stage signal of a linear
    # procedure (fetch both sides, cancel, resolve identity, persist-or-
    # defer) -- same rationale as GridEngine.cancel_open_orders's disable.
    adapter: KrakenAdapter,
    storage: StoragePort,
    symbols: tuple[Symbol, ...],
) -> tuple[int, int]:
    """Cancel every open order across all configured ``symbols``.

    Fetches the account's open orders with a SINGLE global ``OpenOrders``
    call (not one per symbol — that burst blew Kraken's private-API rate
    limit on a multi-coin shutdown, 2026-06-02), then cancels the orders
    whose symbol is configured. A fetch failure propagates so the caller
    marks the cancel unclean and LEAVES the dead-man's-switch armed
    (ADR-021) — Kraken's server-side timer becomes the backstop that
    sweeps whatever this cleanup couldn't.

    Persistence resolves each cancelled order back to its STORED
    identity via ``exchange_id`` before saving — mirrors the 2026-08-19
    ``GridEngine.cancel_open_orders`` fix. ``ExchangePort.
    get_open_orders`` deliberately constructs fresh-UUID ``Order``
    objects (see that method's docstring); saving one of those directly
    (as this function did before the 2026-08-19 fix) upserts an ORPHAN
    row (``save_order`` keys on ``id``) and leaves the real stored row
    ``status='open'`` forever — the next daemon start's reconciler then
    discovers that stale row as an apparent EXTERNAL cancel. An order
    the exchange reports that local storage never tracked (a manual
    Kraken-side order) is cancelled but not adopted, per ADR-018.

    A cancel that catches an order carrying a pre-existing partial fill
    (visible in the ``get_open_orders`` snapshot's ``filled_amount``
    before this loop starts cancelling — Kraken's own ``cancel_order``
    doesn't re-query, so it never refreshes this figure) is deliberately
    left with its stored row UNTOUCHED (``status`` stays ``"open"``)
    rather than persisted as canceled here. This function has no engine
    instance and the process is exiting right after — there's no tick
    left to place a counter-order on, unlike ``GridEngine.
    cancel_open_orders``'s ``_pending_counter_ids`` path. Leaving the
    row ``open`` lets the next daemon startup's ``apply_reconciliation``
    (ADR-023) discover it as storage-only, recover the matched trade(s),
    and queue the counter-order via ``ReconciliationReport.
    needs_counter_order_ids`` — the same recovery path a fill landing in
    the narrow fetch→cancel window already has to rely on regardless.

    Successive ``cancel_order`` calls are paced (ADR-027) — a short
    sleep between attempts, none before the first — so this cleanup
    path can't itself re-trigger the rate-limit storm the OpenOrders
    batching above already guards against. The underlying Kraken calls
    (``_public_get``/``_private_post``) additionally retry a rate-limit
    rejection with bounded backoff before it ever reaches this
    function as an ``ExchangeError``.

    Args:
        adapter: Trading-key exchange adapter.
        storage: Storage port.
        symbols: Configured symbols to restrict cancellation to.

    Returns:
        ``(cancelled, failed)`` counts — exchange-side cancel outcomes.
        A post-cancel persistence choice (resolve now, or deliberately
        defer to the reconciler) never moves either count: the
        cancellation itself already succeeded.
    """
    cancelled = 0
    failed = 0
    configured = set(symbols)
    opens = await adapter.get_open_orders()
    stored_by_exchange_id: dict[str, Order] = {
        o.exchange_id: o for o in await storage.get_open_orders() if o.exchange_id
    }
    attempted = 0
    for o in opens:
        if o.symbol not in configured:
            continue
        if attempted > 0:
            # ADR-027 inter-cancel pacing (see module constant docstring).
            await asyncio.sleep(_INTER_CANCEL_PACING_SECONDS)
        attempted += 1
        try:
            canceled_order = await adapter.cancel_order(o)
            cancelled += 1
            _LOGGER.info(
                "shutdown cancelled (symbol=%s, exchange_id=%s)",
                o.symbol,
                o.exchange_id,
                extra={"symbol": str(o.symbol), "exchange_id": o.exchange_id},
            )
        except WobbleBotPortError as exc:
            failed += 1
            _LOGGER.error(
                "shutdown cancel failed (symbol=%s, exchange_id=%s): %s",
                o.symbol,
                o.exchange_id,
                exc,
                extra={
                    "symbol": str(o.symbol),
                    "exchange_id": o.exchange_id,
                    "error": str(exc),
                },
            )
            continue

        if canceled_order.exchange_id is None:
            continue
        stored = stored_by_exchange_id.get(canceled_order.exchange_id)
        if stored is None:
            _LOGGER.info(
                "shutdown cancel: %s (%s) not tracked in local storage; not adopting",
                canceled_order.symbol,
                canceled_order.exchange_id,
                extra={
                    "symbol": str(canceled_order.symbol),
                    "exchange_id": canceled_order.exchange_id,
                },
            )
            continue

        if canceled_order.filled_amount > 0:
            # Deferred to the next startup's reconciler (see docstring)
            # -- leave the stored row status='open', don't touch it.
            _LOGGER.warning(
                "shutdown cancel: %s %s (%s) had a partial fill of %s before this "
                "cancel; recovery deferred to the next startup's reconciler",
                stored.symbol,
                stored.side.value.upper(),
                canceled_order.exchange_id,
                fmt_decimal(canceled_order.filled_amount),
                extra={
                    "symbol": str(stored.symbol),
                    "exchange_id": canceled_order.exchange_id,
                    "filled_amount": str(canceled_order.filled_amount),
                },
            )
            continue

        # Stage 8.1.B: persist the status transition onto the STORED
        # order's identity (2026-08-19 fix) so the storage view matches
        # what we just did to the exchange, without creating an orphan.
        resolved = stored.model_copy(
            update={
                "status": canceled_order.status,
                "filled_amount": canceled_order.filled_amount,
                "updated_at": canceled_order.updated_at,
            }
        )
        try:
            await storage.save_order(resolved)
        except StorageError as exc:
            _LOGGER.warning(
                "shutdown cancel persistence failed; reconciler will catch on next start "
                "(symbol=%s, exchange_id=%s): %s",
                resolved.symbol,
                resolved.exchange_id,
                exc,
                extra={
                    "symbol": str(resolved.symbol),
                    "exchange_id": resolved.exchange_id,
                    "error": str(exc),
                },
            )
    return cancelled, failed


def _log_dms_confirmation(
    trigger_at: datetime | None, requested_timeout_seconds: int, unconfirmed_ticks: int
) -> int:
    """Log the dead man's switch arm confirmation and return the updated
    consecutive-unconfirmed-ticks counter.

    Confirmed (``trigger_at`` set): DEBUG, matching the per-tick log
    level convention, and the counter resets to 0. Unconfirmed (the
    2026-06-02 soak lesson — Kraken's response carried no real future
    trigger despite the call not raising): transition + heartbeat
    WARNING logging, mirroring GridEngine's offside pattern, rather
    than a WARNING every tick.
    """
    if trigger_at is not None:
        _LOGGER.debug(
            "dead man's switch confirmed armed (trigger_at=%s)",
            trigger_at.isoformat(),
            extra={"trigger_at": trigger_at.isoformat()},
        )
        return 0
    unconfirmed_ticks += 1
    if unconfirmed_ticks == 1:
        _LOGGER.warning(
            "dead man's switch arm not confirmed by Kraken's response "
            "(requested_timeout_seconds=%s)",
            requested_timeout_seconds,
            extra={"requested_timeout_seconds": requested_timeout_seconds},
        )
    elif unconfirmed_ticks % _DMS_UNCONFIRMED_SUMMARY_EVERY_TICKS == 0:
        _LOGGER.warning(
            "dead man's switch still not confirmed armed (consecutive_unconfirmed_ticks=%s)",
            unconfirmed_ticks,
            extra={"consecutive_unconfirmed_ticks": unconfirmed_ticks},
        )
    return unconfirmed_ticks


async def _fetch_session_fee_rates(
    adapter: KrakenAdapter, symbols: Sequence[Symbol]
) -> tuple[Decimal, Decimal]:
    """ADR-038: the account's actual (maker, taker) for this session.

    One TradeVolume call per symbol at session start; the MAX of each
    rate across symbols is returned (conservative for the sell guard).
    Any failure falls back to the code constants with a WARNING — the
    documented fallback, not silent.
    """
    makers: list[Decimal] = []
    takers: list[Decimal] = []
    for symbol in symbols:
        try:
            rates = await adapter.get_fee_rates(symbol)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "fee-rate fetch failed for %s; falling back to code constants "
                "(maker %s%%, taker %s%%): %s",
                symbol,
                fmt_decimal(KRAKEN_MAKER_FEE_RATE * 100),
                fmt_decimal(KRAKEN_TAKER_FEE_RATE * 100),
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            return KRAKEN_MAKER_FEE_RATE, KRAKEN_TAKER_FEE_RATE
        makers.append(rates.maker)
        takers.append(rates.taker)
        _LOGGER.info(
            "session fee rates for %s: maker %s%% / taker %s%% (live TradeVolume)",
            symbol,
            fmt_decimal(rates.maker * 100),
            fmt_decimal(rates.taker * 100),
            extra={
                "symbol": str(symbol),
                "maker_rate": str(rates.maker),
                "taker_rate": str(rates.taker),
            },
        )
    if not makers:
        return KRAKEN_MAKER_FEE_RATE, KRAKEN_TAKER_FEE_RATE
    return max(makers), max(takers)


def _warn_if_spacing_below_fee_floor(grid: GridConfig, maker_rate: Decimal) -> None:
    """ADR-038: re-check spacing-vs-fees against the LIVE maker rate.

    The config-load validator enforces the floor with the fallback
    constants; if the account's real rate is higher, a spacing that
    validated can still be unprofitable. Advisory (WARNING, never
    fatal) — the operator decides.
    """
    floor_pct = maker_rate * Decimal("2") * Decimal("100")
    for label, levels in [
        ("grid.default", grid.default),
        *[(f"grid.coins.{name}", cfg) for name, cfg in grid.coins.items() if cfg.enabled],
    ]:
        if levels.spacing_percentage <= floor_pct:
            _LOGGER.warning(
                "%s spacing %s%% is at or below the LIVE fee floor %s%% "
                "(2 x maker %s%%) — completed cycles cannot profit at current fees",
                label,
                fmt_decimal(levels.spacing_percentage),
                fmt_decimal(floor_pct),
                fmt_decimal(maker_rate * 100),
                extra={
                    "label": label,
                    "spacing_percentage": str(levels.spacing_percentage),
                    "live_floor_percentage": str(floor_pct),
                },
            )


# ADR-037 decision 3: lockout backoff window bounds. 30s doubling to a
# 10-minute cap — retrying a locked account at tick cadence adds
# pressure to exactly the counter that is locked (10,500+ lockout
# errors in one day's log during the 2026-08-15→17 incident).
_LOCKOUT_BACKOFF_INITIAL_SECONDS = 30.0
_LOCKOUT_BACKOFF_MAX_SECONDS = 600.0
# ADR-037 decisions 1 + 6: consecutive permanent-auth failures before
# the trader key is declared dead and placement pauses.
_PERMANENT_AUTH_STRIKES = 3
# ADR-037 decision 3: consecutive DMS reset failures before the
# "your book is about to die server-side" critical page.
_DMS_FAILURE_STREAK_ALERT = 3

_DMS_CALM_FRAMING_FRACTION = 0.5
"""How much of the dead-man's-switch window an in-progress failure streak must
have consumed before a book vanish is framed as Kraken's own timer firing.

Measured against the 2026-09-03 incident: the streak ran 07:01:13 -> 07:02:38,
85s of a 120s window, so the purge landed at 0.71 of the window. The confirmed-
deadline check alone missed it (the purge preceded the client-side deadline by
about 18s) and the operator got "investigate before resuming" for the safety net
working as designed. 0.5 catches that with margin; a threshold above ~0.70 would
not have. Raising it buys margin against Kraken firing EARLY in the window at the
cost of reverting to the alarming framing.

This is elapsed WALL CLOCK, not a failure count. The count was rejected as a
proxy for good reason (see ``dms_trigger_at``) — at a 5s tick even the alert
threshold of 3 is ~15s — but that objection is about counts, not duration, and
each failed ping already blocks for the full request timeout."""


class _AuthEscalation:  # pylint: disable=too-many-instance-attributes
    """ADR-037 per-session escalation state for cli/live's private calls.

    Tracks three independent signals the 2026-08-15→17 incident proved
    need different handling: Kraken temporary lockout (back off hard),
    permanent trader-key auth death (pause all placement, decision 6),
    and a dead-man's-switch reset failure streak (page the operator
    before Kraken retires the book). All state is per-session; fixing a
    credential requires a container recreate anyway, so a restart
    clearing this is the designed recovery path.

    The 8 attributes are 4 independently-meaningful pairs (lockout
    backoff, DMS failure-streak + alert latch, DMS deadline + per-tick
    expiry evidence, permanent-auth strikes + pause flag) — splitting
    this into sub-objects would fragment one cohesive escalation state
    machine for no organizational gain.
    """

    def __init__(self) -> None:
        self.backoff_until = 0.0
        self.backoff_seconds = 0.0
        self.dms_failure_streak = 0
        self.dms_alerted = False
        # Kraken's own promised auto-cancel deadline, from the last
        # CONFIRMED (trigger_at is not None) successful DMS ping. This is
        # ground truth for "could the server-side timer have lapsed by
        # now" — a failure COUNT is not: at the default 5s tick, a streak
        # of even 3 is only ~15s, far short of the (default 60s) timeout,
        # so "streak > 0" falsely blamed the timer for a same-window
        # external cancel. Only ever moves forward on a confirmed ping;
        # untouched by failures, so it still reflects the true deadline
        # while it's being missed.
        self.dms_trigger_at: datetime | None = None
        # When the CURRENT unbroken run of DMS reset failures began. Wall
        # clock, so it survives ticks the loop skips entirely (``in_backoff``
        # returns before the OpenOrders fetch, and a skipped tick would
        # destroy a per-tick counter). ``None`` whenever the switch is
        # healthy. Added 2026-09-03 for the calm-framing predicate.
        self.dms_streak_started_at: datetime | None = None
        # Per-TICK evidence (not per-session) that ``dms_trigger_at`` had
        # already passed as of the START of this tick's DMS ping — i.e.
        # BEFORE a same-tick success could push it back out. Snapshotting
        # pre-ping is what lets a vanish discovered on the recovery tick
        # still see "the timer had lapsed," instead of the just-renewed
        # future deadline hiding the failure episode that preceded it.
        # The caller (``_run_loop``) resets this to False unconditionally
        # at the top of every iteration, before the DMS block — so it can
        # never carry a stale True into a tick where the block is skipped
        # (DMS disabled, or ``auth_paused``) and falsely reassure on a
        # vanish that has no DMS evidence backing it this tick.
        self.dms_timer_expired_this_tick = False
        # Companion per-tick snapshot: the fraction of the DMS window the
        # in-progress failure streak had consumed as of the START of this
        # tick. Reset unconditionally each iteration, same as the flag above.
        self.dms_degraded_fraction_this_tick = 0.0
        self.permanent_auth_strikes = 0
        self.auth_paused = False

    def note_success(self) -> bool:
        """Record a successful private call OTHER than a DMS reset (e.g.
        the per-tick OpenOrders fetch). Resets lockout backoff and
        permanent-auth strikes — account-wide signals a single working
        endpoint disproves — but deliberately leaves ``dms_failure_streak``
        untouched.

        2026-08-20 incident: a ~6-minute Kraken outage failed
        ``CancelAllOrdersAfter`` (the DMS reset) specifically, ~40 times
        back to back, while OpenOrders kept succeeding every tick. This
        method used to reset ``dms_failure_streak`` on ANY private-call
        success, so the streak was wiped back to 0 every tick right after
        climbing to 1 — it could never reach the alert threshold and the
        "DMS resets failing" critical never fired. DMS health is now
        tracked exclusively via ``note_dms_failure`` / ``note_dms_success``.

        Returns True exactly when this ends a lockout/permanent-auth
        escalation episode.
        """
        recovered = self.permanent_auth_strikes > 0 or self.backoff_seconds > 0
        self.permanent_auth_strikes = 0
        self.backoff_seconds = 0.0
        self.backoff_until = 0.0
        return recovered

    def note_lockout(self) -> float:
        """Extend the lockout backoff window. Returns the window chosen."""
        if self.backoff_seconds:
            self.backoff_seconds = min(_LOCKOUT_BACKOFF_MAX_SECONDS, self.backoff_seconds * 2)
        else:
            self.backoff_seconds = _LOCKOUT_BACKOFF_INITIAL_SECONDS
        self.backoff_until = time.monotonic() + self.backoff_seconds
        return self.backoff_seconds

    def in_backoff(self) -> bool:
        return time.monotonic() < self.backoff_until

    def note_permanent_auth(self) -> bool:
        """Count a permanent-auth failure. Returns True exactly once —
        on the strike that trips decision 6's all-placement pause."""
        self.permanent_auth_strikes += 1
        if self.permanent_auth_strikes >= _PERMANENT_AUTH_STRIKES and not self.auth_paused:
            self.auth_paused = True
            return True
        return False

    def note_dms_failure(self) -> bool:
        """Count a DMS reset failure specifically. Returns True exactly
        once per failure episode — when the streak first reaches (or,
        defensively, remains at/above) the alert threshold.

        ``>=`` plus the ``dms_alerted`` latch (rather than a bare ``==``)
        is defense in depth: as long as ``note_dms_success`` is the only
        thing that zeroes the streak, it will always pass through exactly
        3 on the way up and ``==`` alone would suffice — but the latch
        means a future change that increments by more than one, or a
        reset that lands the streak somewhere other than 0, still can't
        skip the alert or double-fire it.
        """
        self.dms_failure_streak += 1
        if self.dms_streak_started_at is None:
            self.dms_streak_started_at = datetime.now(UTC)
        if self.dms_failure_streak >= _DMS_FAILURE_STREAK_ALERT and not self.dms_alerted:
            self.dms_alerted = True
            return True
        return False

    def dms_deadline_note(self, now: datetime) -> str:
        """Where Kraken's last CONFIRMED auto-cancel deadline sits relative
        to ``now``. The one fact the 2026-09-03 post-mortem could not
        recover, because the confirmed ``trigger_at`` was DEBUG-only."""
        if self.dms_trigger_at is None:
            return "no confirmed auto-cancel deadline this session"
        remaining = (self.dms_trigger_at - now).total_seconds()
        stamp = self.dms_trigger_at.strftime("%H:%M:%SZ")
        if remaining >= 0:
            return f"last confirmed auto-cancel deadline {stamp} ({remaining:.0f}s from now)"
        return f"last confirmed auto-cancel deadline {stamp} (passed {-remaining:.0f}s ago)"

    def dms_degraded_fraction(self, now: datetime, window_seconds: int | None) -> float:
        """How much of the DMS window the in-progress failure streak has eaten.

        ``0.0`` when the switch is healthy or disabled. Values at or above
        :data:`_DMS_CALM_FRAMING_FRACTION` mean Kraken's server-side timer
        plausibly fired on its own, which is what the book-vanish page frames
        on. Uncapped deliberately: a value above 1.0 says the window was
        exceeded outright and reads that way in the log.
        """
        if self.dms_streak_started_at is None or not window_seconds:
            return 0.0
        return (now - self.dms_streak_started_at).total_seconds() / window_seconds

    def note_dms_success(self) -> bool:
        """Record a successful DMS reset call specifically (the
        ``CancelAllOrdersAfter`` ping in the main loop, NOT the generic
        per-tick OpenOrders fetch — see ``note_success``). Resets the
        DMS-failure streak and, since a DMS reset is itself a private
        call, also clears lockout/permanent-auth state. Returns True
        exactly when this ends a DMS-failure alert episode."""
        recovered = self.dms_alerted
        self.dms_failure_streak = 0
        self.dms_streak_started_at = None
        self.dms_alerted = False
        self.permanent_auth_strikes = 0
        self.backoff_seconds = 0.0
        self.backoff_until = 0.0
        return recovered


async def _note_private_call_failure(
    exc: WobbleBotPortError,
    escalation: _AuthEscalation,
    engine: GridEngine,
    live: LiveConfig,
    notifier: NotifierPort | None,
) -> None:
    """Classify a failed private Kraken call per ADR-037 and escalate.

    Temporary lockout → extend the backoff window (decision 3).
    Permanent auth → count a strike; on the third, pause ALL placement
    (decision 6) and page the operator. Anything else is left to the
    caller's existing per-site warning log.
    """
    if not isinstance(exc, ExchangeError):
        return
    if is_temporary_lockout(exc):
        window = escalation.note_lockout()
        _LOGGER.warning(
            "Kraken temporary lockout; backing off private calls %.0fs (DMS ping continues)",
            window,
            extra={"backoff_seconds": window},
        )
        return
    if is_permanent_auth_error(exc) and escalation.note_permanent_auth():
        for symbol in live.symbols:
            engine.pause_symbol(symbol)
        _LOGGER.error(
            "trader key permanent auth failure x%d — ALL placement paused (ADR-037 decision 6); "
            "fix the key and redeploy",
            _PERMANENT_AUTH_STRIKES,
            extra={"strikes": _PERMANENT_AUTH_STRIKES},
        )
        await notify(
            notifier,
            level="critical",
            title="Trader key dead — all placement paused",
            message=(
                f"{_PERMANENT_AUTH_STRIKES} consecutive permanent auth failures on the "
                "trader key. All symbol placement is paused and private calls stop; "
                "Kraken's dead-man's-switch will retire any open orders server-side. "
                "Fix KRAKEN_TRADER_API_KEY/_SECRET and redeploy the stack."
            ),
            context={"strikes": _PERMANENT_AUTH_STRIKES, "reason": "auth_failure"},
        )


async def _check_held_reminder(
    engine: GridEngine,
    live: LiveConfig,
    notifier: NotifierPort | None,
    tick: int,
    last_reminder_at: float | None,
) -> float | None:
    """ADR-037 decision 2 follow-up: a book-vanish hold pages once when
    it starts (by design, so the page can't spam — see the call site in
    ``_run_one_tick``) and nothing previously reminded the operator it
    was STILL held. Production went ~18h with 5 symbols held and only
    the five initial alerts to notice by (2026-08-20 incident).

    Called once per loop iteration, after this tick's steps. Returns the
    updated ``last_reminder_at`` for the caller to carry into the next
    iteration. A single aggregate reminder covers every currently-paused
    symbol — never one per symbol — so this stays the anti-spam
    complement to the one-time page, not a second source of spam.

    Filters on ``engine.is_paused(symbol)``, not ``hold_reason(symbol)
    == "book_vanish"``. ``hold_reason`` is in-memory only (ADR-030's
    ``engine_state`` persists ``paused: bool`` but never the reason) —
    ``_restore_engine_state`` calls ``pause_symbol`` on restart with
    no reason to restore, so a book-vanish hold that survives a restart
    would read as ``hold_reason() is None`` and silently drop out of a
    narrower filter, defeating this reminder for exactly the case that
    most needs it. The tradeoff: a deliberate long-standing operator
    pause now also gets nagged on this cadence — an operator who wants
    a symbol parked quietly for longer than ``held_reminder_seconds``
    sets it to ``null``.

    ``live.held_reminder_seconds is None`` disables the reminder
    entirely (returns ``None`` unconditionally).
    """
    if live.held_reminder_seconds is None:
        return None
    held = [symbol for symbol in live.symbols if engine.is_paused(symbol)]
    if not held:
        # Nothing paused right now -- clear the clock so the NEXT pause
        # episode gets a full window before its first reminder, rather
        # than inheriting a stale start time from a prior episode.
        return None
    now = time.monotonic()
    if last_reminder_at is None:
        return now
    if now - last_reminder_at < live.held_reminder_seconds:
        return last_reminder_at
    await notify(
        notifier,
        level="warning",
        title=f"Still paused: {len(held)} symbol(s) not trading",
        message=(
            f"{len(held)} symbol(s) remain paused and are NOT trading: "
            f"{', '.join(str(symbol) for symbol in held)}. Resume via Discord "
            "('resume <symbol>') if this wasn't deliberate."
        ),
        context={"symbols": [str(symbol) for symbol in held], "tick": tick},
    )
    return now


async def _session_usd_balance(adapter: KrakenAdapter) -> Decimal:
    bal = await adapter.get_balance("USD")
    return bal.total if bal else Decimal("0")


async def _session_portfolio_value_usd(
    adapter: KrakenAdapter,
    symbols: tuple[Symbol, ...],
    tickers: dict[Symbol, Ticker] | None = None,
) -> Decimal:
    """USD-denominated mark-to-market portfolio value: USD balance plus
    each configured symbol's base asset valued at its current price.

    Why this exists (Stage 8.4 soak hotfix, 2026-05-22): a BUY fill drops
    USD by the order size, but that's an asset conversion (USD → base),
    not a loss. The session-loss cap used to be checked against
    USD balance alone, which tripped on the first BUY of any session
    whose order_size_usd > max_session_loss_usd. Using mark-to-market
    portfolio value captures realized + unrealized PnL honestly.

    ``tickers``: an optional pre-fetched ``{symbol: Ticker}`` map (v1.1
    backlog "per-tick price-fetch dedup") — when a symbol is present,
    its ``last`` price is used instead of this function issuing its own
    ``get_current_price`` call. Lets ``_run_one_tick``, which already
    fetches a ``Ticker`` per symbol for the engine step, reuse it here
    rather than doubling the per-tick ``/0/public/Ticker`` GETs. ``None``
    (the default) falls back to fetching here — the session-start /
    session-end call sites in ``_run_loop`` run outside any tick's
    pre-fetch and have nothing to share.
    """
    balances = await adapter.get_balances()
    by_asset = {b.asset: b.total for b in balances}
    total = by_asset.get("USD", Decimal("0"))
    bases_seen: set[str] = set()
    for symbol in symbols:
        if symbol.base in bases_seen:
            continue
        bases_seen.add(symbol.base)
        if symbol.quote != "USD":
            # v1: cap is in USD; non-USD quotes would need a cross-rate
            # conversion we don't ship yet. Skip and leave a breadcrumb.
            _LOGGER.warning(
                "skipping non-USD-quoted symbol in portfolio value (symbol=%s)",
                symbol,
                extra={"symbol": str(symbol)},
            )
            continue
        base_balance = by_asset.get(symbol.base, Decimal("0"))
        if base_balance <= 0:
            continue
        cached = tickers.get(symbol) if tickers is not None else None
        if cached is not None:
            price_amount = cached.last
        else:
            price = await adapter.get_current_price(symbol)
            price_amount = price.amount
        total += base_balance * price_amount
    return total


async def _prefetch_trades(
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    tick: int,
    tick_open_orders: list[Order] | None,
) -> list[Trade] | None:
    """One global TradesHistory fetch per tick, shared across every symbol
    that has a fill candidate this tick (fleet-review #19 finding 8
    follow-up).

    TradesHistory is paginated (up to 20 pages) to find a thin symbol's
    trades among heavy volume on others; without this consolidation, a
    tick where several symbols fill simultaneously would page that same
    account-wide history once per filling symbol — the same rate-limit-
    storm shape the OpenOrders consolidation already fixed once
    (2026-06-02). Checking candidates is pure storage plus the
    already-fetched open-orders snapshot, no network call, so this costs
    nothing on the (typical) no-fill tick.

    ``None`` — from a failed fetch, an absent open-orders snapshot, or no
    fill candidates — falls each symbol back to its own per-symbol fetch,
    exactly as before this consolidation existed.
    """
    if tick_open_orders is None:
        return None
    for symbol in live.symbols:
        if await engine.has_pending_fill_candidates(symbol, tick_open_orders):
            break
    else:
        return None
    try:
        return await adapter.get_trade_history(limit=200 * len(live.symbols))
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "tick trade-history fetch failed; falling back to per-symbol fetch (tick=%s): %s: %s",
            tick,
            type(exc).__name__,
            exc,
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        return None


async def _prefetch_tickers(
    adapter: KrakenAdapter, live: LiveConfig, tick: int
) -> dict[Symbol, Ticker]:
    """One ticker fetch per symbol per tick, shared between the engine
    step (spread guard + current price, ADR-025) and the post-tick
    loss-cap mark-to-market check (v1.1 backlog "per-tick price-fetch
    dedup") — both used to independently call ``/0/public/Ticker`` (or
    ``get_current_price``) for the same symbol in the same tick.

    A per-symbol fetch failure just leaves that symbol out of the dict;
    both call sites already fall back to fetching for themselves when a
    symbol has no cached entry.
    """
    tickers: dict[Symbol, Ticker] = {}
    for symbol in live.symbols:
        try:
            tickers[symbol] = await adapter.get_ticker(symbol)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "tick ticker fetch failed; symbol will fetch its own price (tick=%s, symbol=%s): "
                "%s: %s",
                tick,
                symbol,
                type(exc).__name__,
                exc,
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
    return tickers


async def _run_one_tick(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    tick: int,
    started_value_usd: Decimal,
    notifier: NotifierPort | None = None,
    sweep: list[Symbol] | None = None,
    escalation: _AuthEscalation | None = None,
    fee_alerted: set[Symbol] | None = None,
) -> bool:
    """One tick across every configured symbol + post-tick loss cap
    check. Returns True when the loss cap tripped (caller stops).

    ``sweep`` is the ORDER to step symbols in (live.symbol_priority).
    ``None`` means config order, which is both the default strategy and
    the right fallback for callers that don't care. Only the stepping
    loop is ordered — the ticker and trade-history prefetches above are
    order-independent by construction.

    ``escalation`` (ADR-037) carries the session's auth-failure state;
    ``None`` (tests / callers that don't care) disables escalation.
    """
    # ADR-037 decisions 3 + 6: inside a lockout backoff window, or with
    # the trader key declared dead, skip this tick's private-call work
    # entirely. The DMS ping in _run_loop is governed separately.
    if escalation is not None and (escalation.auth_paused or escalation.in_backoff()):
        _LOGGER.debug(
            "tick %s skipped (auth_paused=%s, in_backoff=%s)",
            tick,
            escalation.auth_paused,
            escalation.in_backoff(),
            extra={"tick": tick, "auth_paused": escalation.auth_paused},
        )
        return False
    # One global OpenOrders fetch per tick, shared across every symbol.
    # Kraken's OpenOrders returns the whole account in a single call; the
    # engine used to fetch per-symbol, so a 5-coin tick fired it 5x and blew
    # the private-API rate limit (EAPI:Rate limit exceeded, 2026-06-02). On a
    # fetch failure, skip this tick's steps rather than let each symbol fall
    # back to its own fetch (which would worsen the rate limit) — the next
    # tick retries; the loss-cap check + DMS ping below still run.
    tick_open_orders: list[Order] | None
    try:
        tick_open_orders = await adapter.get_open_orders()
        if escalation is not None and escalation.note_success():
            await notify(
                notifier,
                level="info",
                title="Kraken API recovered",
                message="Private API calls are succeeding again after a failure episode.",
                context={"tick": tick},
            )
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "tick open-orders fetch failed; skipping this tick's steps (tick=%s): %s: %s",
            tick,
            type(exc).__name__,
            exc,
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        if escalation is not None:
            await _note_private_call_failure(exc, escalation, engine, live, notifier)
        tick_open_orders = None

    tick_trades = await _prefetch_trades(adapter, engine, live, tick, tick_open_orders)
    tick_tickers = await _prefetch_tickers(adapter, live, tick)

    for symbol in sweep if sweep is not None else live.symbols:
        if tick_open_orders is None:
            break
        try:
            result = await engine.step(
                symbol,
                exchange_open_orders=tick_open_orders,
                exchange_trades=tick_trades,
                ticker=tick_tickers.get(symbol),
            )
            # Per-symbol per-tick output is DEBUG so the operator's
            # terminal doesn't flood at the 5s default cadence. The
            # actually-interesting events (fills, cap trips, session
            # end) emit at INFO via their own log lines below + the
            # notification pipeline. To re-enable per-tick visibility
            # temporarily, run with WOBBLEBOT_LOG_LEVEL=DEBUG.
            _LOGGER.debug(
                "tick complete (tick=%s, symbol=%s, action=%s, fills=%s)",
                tick,
                symbol,
                result.action,
                result.fills,
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "action": result.action,
                    "fills": result.fills,
                    "counters_placed": result.counters_placed,
                    "placed": result.placed,
                    "refusals": result.refusals,
                    "offside": result.offside,
                },
            )
            # ADR-037 decision 2: the engine held this symbol because
            # its book vanished externally. The engine returns this
            # action exactly once (subsequent ticks read skipped_paused)
            # so this page cannot spam.
            if result.action == "held_book_vanish":
                # 2026-08-20 incident: a real Kraken outage failed DMS
                # resets for ~6 minutes, Kraken's own 60s server-side
                # timer lapsed and auto-cancelled every open order (the
                # safety net working exactly as designed), and this page
                # read identically alarming as a genuinely unexplained
                # external cancel — "it sounded a LOT like ... everything
                # went tits up" per the operator.
                #
                # The framing predicate is `dms_timer_expired_this_tick`
                # (whether Kraken's own PROMISED auto-cancel deadline had
                # already passed as of the start of this tick's DMS ping —
                # see `_AuthEscalation`), NOT a raw failure-streak count.
                # A streak count is a poor proxy: at the default 5s tick,
                # even the streak-alert threshold of 3 is only ~15s —
                # nowhere near a 60s timeout — so "streak > 0" falsely
                # blamed the timer for a same-window external cancel that
                # had nothing to do with it. The deadline check also
                # survives a same-tick recovery (DMS ping succeeds THIS
                # tick, right before this vanish is detected): the
                # snapshot is taken before the ping call updates the
                # deadline, so it still reflects the failure episode that
                # just ended rather than the freshly-renewed future one.
                # Either way the symbol still HOLDs; only the message's
                # certainty changes.
                dms_streak = escalation.dms_failure_streak if escalation is not None else 0
                dms_timer_expired = (
                    escalation.dms_timer_expired_this_tick if escalation is not None else False
                )
                dms_degraded_fraction = (
                    escalation.dms_degraded_fraction_this_tick if escalation is not None else 0.0
                )
                # Either predicate is enough. The confirmed-deadline check
                # answers "could the timer have fired by now"; it missed the
                # 2026-09-03 purge, which landed ~18s BEFORE the client-side
                # deadline, so the operator got the alarming framing for the
                # safety net working exactly as designed. The elapsed-window
                # fraction catches that case: 85s of a 120s window had already
                # been eaten by the failure streak when the book went.
                dms_window_degraded = dms_degraded_fraction >= _DMS_CALM_FRAMING_FRACTION
                if dms_timer_expired or dms_window_degraded:
                    window = live.dead_mans_switch_seconds
                    message = (
                        f"Kraken's dead-man's-switch reset was failing ({dms_streak} "
                        f"consecutive failures, {dms_degraded_fraction:.0%} of the "
                        f"{window}s window) immediately before this — orders were "
                        "most likely auto-cancelled by Kraken's own safety timer during "
                        f"an API disruption, not an external action. Placement is HELD "
                        f"for {symbol} until you resume it (Discord: 'resume {symbol}'). "
                        "Resume when ready; if Kraken's own order history disagrees with "
                        "these numbers, treat it as an external cancel instead."
                    )
                else:
                    message = (
                        f"{symbol}'s open orders left the exchange without the engine "
                        "cancelling them (DMS purge or manual cancel). Placement is "
                        f"HELD for {symbol} until you resume it (Discord: "
                        f"'resume {symbol}'). Investigate before resuming."
                    )
                await notify(
                    notifier,
                    level="critical",
                    title=f"Book vanished: {symbol} — trading held",
                    message=message,
                    context={
                        "symbol": str(symbol),
                        "reason": "book_vanish",
                        "tick": tick,
                        "dms_failure_streak": dms_streak,
                        "dms_timer_expired": dms_timer_expired,
                        "dms_degraded_fraction": round(dms_degraded_fraction, 3),
                        "dms_window_degraded": dms_window_degraded,
                    },
                )
            # ADR-038 fee-drift tripwire: page once per symbol per
            # session the first time a fill's fee rate matches neither
            # believed rate. Would have paged on 2026-07-13 — the
            # first fill after Kraken's fee doubling.
            if (
                fee_alerted is not None
                and result.fills > 0
                and symbol not in fee_alerted
                and engine.fee_anomaly_count(symbol) > 0
            ):
                fee_alerted.add(symbol)
                await notify(
                    notifier,
                    level="warning",
                    title=f"Fee drift: {symbol}",
                    message=(
                        f"A {symbol} fill's fee rate matches neither the maker nor "
                        "taker rate this session believes — the exchange fee "
                        "schedule may have changed. Check the fee-drift WARNINGs "
                        "in the live log and Kraken's fee page."
                    ),
                    context={"symbol": str(symbol), "anomalies": engine.fee_anomaly_count(symbol)},
                )
            # Stage 5.5: emit a notification on fills so the operator
            # sees activity in Discord without tailing logs.
            if result.fills > 0:
                await notify(
                    notifier,
                    level="info",
                    title=f"Fills: {symbol} ({result.fills})",
                    message=(
                        f"{result.fills} order(s) filled on {symbol}; "
                        f"{result.counters_placed} counter(s) placed."
                    ),
                    event=FillEvent(
                        symbol=str(symbol),
                        fills=result.fills,
                        counters_placed=result.counters_placed,
                        tick=tick,
                    ),
                )
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "symbol step failed; continuing other symbols (tick=%s, symbol=%s): %s: %s",
                tick,
                symbol,
                type(exc).__name__,
                exc,
                extra={
                    "tick": tick,
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    # Stage 8.4 hotfix #2 (2026-05-20): wrap the per-tick balance
    # fetch in try/except. e2b6cfc's earlier fix protected the
    # finally-block call site only; this is the OTHER call site —
    # the post-tick loss-cap evaluator. A transient httpx.ReadTimeout
    # to /0/private/BalanceEx should NOT kill the daemon. Skip the
    # cap check for this tick (next tick will retry), log a warning,
    # and treat as "no cap trip" so the loop continues.
    try:
        current_value_usd = await _session_portfolio_value_usd(
            adapter, tuple(live.symbols), tick_tickers
        )
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "post-tick portfolio-value fetch failed; skipping loss-cap check this tick (tick=%s): "
            "%s: %s",
            tick,
            type(exc).__name__,
            exc,
            extra={"tick": tick, "error": str(exc), "error_type": type(exc).__name__},
        )
        if escalation is not None:
            await _note_private_call_failure(exc, escalation, engine, live, notifier)
        return False  # No cap trip; loop continues.
    session_pnl = current_value_usd - started_value_usd
    if session_pnl < -live.max_session_loss_usd:
        _LOGGER.error(
            "session loss cap exceeded; stopping (session_pnl_usd=%s, limit=%s, tick=%s)",
            fmt_decimal(session_pnl),
            fmt_decimal(live.max_session_loss_usd),
            tick,
            extra={
                "session_pnl_usd": str(session_pnl),
                "limit": str(live.max_session_loss_usd),
                "tick": tick,
            },
        )
        await notify(
            notifier,
            level="error",
            title="Loss cap tripped — session ending",
            message=(
                f"Session PnL {session_pnl} exceeded cap "
                f"-{live.max_session_loss_usd} USD; cli/live stopping."
            ),
            event=LossCapEvent(
                session_pnl_usd=session_pnl,
                limit_usd=live.max_session_loss_usd,
                tick=tick,
            ),
        )
        return True
    return False


async def _process_pending_commands(
    operator_service: OperatorService,
    operator_storage: StoragePort,
    notifier: NotifierPort | None,
) -> int:
    """Drain approved ``pending_commands`` rows; dispatch + mark each.

    **ADR-002 firewall.** This is the only path from a ``PendingCommand``
    to the engine. The ``status='approved'`` filter on the SELECT is the
    confirm-before-execute gate — rows without operator ✅ never reach
    ``OperatorService.dispatch_command``. Per-row failures (engine
    refusal, ``OperatorError``) mark the row ``failed`` and record the
    error message in the result; the loop continues so one bad command
    doesn't starve the others. Returns the number of rows processed.

    Each dispatch also echoes its ``CommandResult`` back through
    ``notify()`` (P3 renderers slice, from the 2026-08-09 re-anchor e2e
    finding): the operator's ✅ used to get silence — which hid a
    "placed 0/6" re-anchor outcome — because the result landed only in
    this table. Best-effort like every notification.

    The poll is **kind-scoped** (ADR-034): ``pending_commands`` is now
    shared with ``cli/harvest``, which owns ``execute_proposal`` rows.
    Without the filter this loop would claim a withdrawal row, hand it
    to ``OperatorService`` (which has no dispatcher for it), and mark
    the operator's approved transfer ``failed`` — using a key that
    cannot withdraw in the first place (ADR-003).
    """
    approved = await operator_storage.get_pending_commands(
        status="approved",
        kinds=_ENGINE_COMMAND_KINDS,
    )
    if not approved:
        _LOGGER.debug("no approved pending_commands to process")
        return 0
    processed = 0
    for pending in approved:
        command = pending.command
        if isinstance(command, ExecuteProposalCommand):
            # Unreachable through the kind-scoped SELECT above. Kept as a
            # hard refusal rather than a type-ignore: if the filter is
            # ever widened by accident, this loop must NOT touch a
            # withdrawal row — leaving it 'approved' lets cli/harvest,
            # the only module with transfer authority, still pick it up
            # (ADR-003). Skipping is the safe failure here; dispatching
            # or marking it failed are both worse.
            _LOGGER.error(
                "refusing to dispatch execute_proposal %s — cli/harvest owns this kind",
                pending.id,
                extra={"pending_id": str(pending.id), "command_kind": command.kind},
            )
            continue
        try:
            cmd_result = await operator_service.dispatch_command(command)
            updated = pending.model_copy(
                update={
                    "status": "dispatched",
                    "dispatched_at": Timestamp(dt=datetime.now(UTC)),
                    "result": cmd_result,
                }
            )
        except OperatorError as exc:
            _LOGGER.error(
                "operator command dispatch failed (pending_id=%s, command_kind=%s): %s",
                pending.id,
                command.kind,
                exc,
                extra={
                    "pending_id": str(pending.id),
                    "command_kind": command.kind,
                    "error": str(exc),
                },
            )
            updated = pending.model_copy(
                update={
                    "status": "failed",
                    "dispatched_at": Timestamp(dt=datetime.now(UTC)),
                    "result": CommandResult(
                        success=False,
                        command_kind=command.kind,
                        message=f"OperatorError: {exc}",
                        executed_at=Timestamp(dt=datetime.now(UTC)),
                    ),
                }
            )
        try:
            await operator_storage.save_pending_command(updated)
        except WobbleBotPortError as exc:
            # Persistence failure here is bad — the operator's confirm
            # already happened, the engine action already ran, but we
            # can't record the outcome. Log and continue; the row stays
            # in 'approved' status and will be re-dispatched next tick.
            # That's an idempotency hazard for non-idempotent commands;
            # acceptable v1 trade-off given how rare DB failures are.
            _LOGGER.warning(
                "failed to persist dispatched pending_command (pending_id=%s): %s",
                pending.id,
                exc,
                extra={"pending_id": str(pending.id), "error": str(exc)},
            )
        processed += 1
        # The ✅'s receipt: echo the result to Discord via the forwarder.
        result = updated.result
        if result is not None:
            symbol = getattr(command, "symbol", None)
            await notify(
                notifier,
                level="info" if result.success else "error",
                title=(f"Command {'executed' if result.success else 'failed'}: {command.kind}"),
                message=result.message,
                event=CommandResultEvent(
                    command_kind=command.kind,
                    symbol=str(symbol) if symbol is not None else None,
                    success=result.success,
                    message=result.message,
                ),
            )
    return processed


#: How often the screener ranking is rebuilt. Its inputs are 60m bars, so
#: anything faster spends storage reads to produce an identical answer —
#: and an order that wobbles tick-to-tick would make "why did SOL get
#: funded and ADA not" unanswerable after the fact.
_SWEEP_REFRESH_SECONDS = 3600.0


async def _current_price(adapter: KrakenAdapter, symbol: Symbol) -> Decimal | None:
    """Last trade price, or ``None`` when Kraken won't say.

    One call per symbol per REFRESH (hourly), against the ~720 per symbol
    the tick loop already makes in that hour — the cost is noise, and a
    price fetched at ranking time is more honest than a cached one. A
    failure costs this symbol its tiebreak, never its rank.
    """
    try:
        return (await adapter.get_ticker(symbol)).last
    except WobbleBotPortError:
        return None


async def _grid_level_prices(storage: StoragePort, symbol: Symbol) -> list[Decimal]:
    """The symbol's current ladder prices; empty when it has no anchor.

    Empty is the honest answer for a symbol the engine has never stepped:
    :func:`proximity_in_atr` turns it into ``inf``, which sorts last —
    "we don't know" must never read as "about to fill".
    """
    try:
        grid_state = await storage.get_grid_state(symbol)
    except WobbleBotPortError:
        return []
    if grid_state is None:
        return []
    return [
        level.price
        for level in compute_grid_levels(
            reference_price=grid_state.reference_price,
            spacing_percentage=grid_state.spacing_percentage,
            levels_above=grid_state.levels_above,
            levels_below=grid_state.levels_below,
        )
    ]


async def _build_screener_inputs(
    symbols: list[Symbol],
    storage: StoragePort,
    observe_storage: StoragePort,
    adapter: KrakenAdapter,
    screener: ScreenerConfig,
) -> tuple[list[ScreenerRanking], dict[Symbol, float]]:
    """Rank the cohort and measure each symbol's distance to a fill.

    Every read degrades to "no opinion" rather than raising. This runs on
    the trading path, and an ordering preference must never be able to
    stop a tick — a symbol whose bars are thin simply goes unranked and
    sorts last; one whose price won't fetch keeps its composite rank and
    loses only the tiebreak.

    ``screener.db`` is deliberately ignored: bars are read through
    ``live.observe_db``, which the caller already opened. Only the
    lookback / interval / band-center knobs are borrowed, so ``cli/live``
    and ``cli/screener`` score suitability by one definition rather than
    two that can drift apart.
    """
    since = datetime.now(UTC) - timedelta(days=screener.lookback_days)
    metrics: list[SymbolMetrics] = []
    atr_abs: dict[Symbol, float] = {}
    for symbol in symbols:
        try:
            bars = await observe_storage.get_ohlc_bars(
                symbol, screener.interval_minutes, start_time=since
            )
        except WobbleBotPortError:
            continue
        computed = compute_symbol_metrics(bars)
        if computed is None:
            continue  # too few bars / no ATR -> unranked, sorts last
        metrics.append(computed)
        # atr_pct is a percentage OF THE LATEST CLOSE, so converting it
        # back with that same close is exact and needs no extra fetch.
        latest_close = float(bars[-1].close)
        if latest_close > 0:
            atr_abs[symbol] = latest_close * computed.atr_pct / 100.0

    rankings = rank_candidates(
        metrics,
        vol_band_center=screener.vol_band_center,
        atr_band_center_pct=screener.atr_band_center_pct,
    )
    proximity = {
        symbol: proximity_in_atr(
            await _current_price(adapter, symbol),
            await _grid_level_prices(storage, symbol),
            atr_abs.get(symbol),
        )
        for symbol in symbols
    }
    return rankings, proximity


def format_sweep(
    order: Sequence[Symbol],
    rankings: Sequence[ScreenerRanking],
    proximity: dict[Symbol, float],
) -> str:
    """Render the sweep as ``SYMBOL(composite|proximity)``, order preserved.

    **Why the numbers belong in the message.** Measured 2026-08-15, the
    tradeable order changes about 13% of hours. Logging the order ALONE
    made each change unexplainable from the log: reconstructing why one
    2026-08-15 reorder happened took copies of two production DBs and a
    replay script, when two consecutive log lines should have shown it.
    Diffing successive lines now names the symbol whose score moved.

    Both keys are shown because they answer different questions. The
    composite is the primary sort and drove every reorder measured — it is
    a mean of three integer ranks, so its resolution is 1/3 and adjacent
    symbols sit exactly 1/3 apart, which is why one new hourly bar
    crossing one rank boundary can move a symbol a whole place. Proximity
    only decides exact composite ties, which are common in a small cohort;
    printing it means a tie-broken reorder is legible too, instead of
    looking like an unexplained swap between equal scores.

    An unranked symbol (too few bars, no ATR) shows ``n/a`` rather than a
    number — it sorted last because it is unknown, and a fabricated score
    would hide that.
    """
    composites = {r.metrics.symbol: r.composite for r in rankings}
    parts = []
    for symbol in order:
        composite = composites.get(symbol)
        rendered = "n/a" if composite is None else f"{composite:.2f}"
        parts.append(f"{symbol}({rendered}|{proximity.get(symbol, math.inf):.1f})")
    return " > ".join(parts)


async def _refresh_sweep_order(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    live: LiveConfig,
    screener: ScreenerConfig,
    storage: StoragePort,
    observe_storage: StoragePort | None,
    adapter: KrakenAdapter,
    tick: int,
    current: list[Symbol] | None,
    computed_at: float | None,
) -> tuple[list[Symbol] | None, float | None]:
    """Return ``(sweep_order, computed_at)`` for this tick.

    ``config_order`` returns ``None`` — the caller falls back to
    ``live.symbols``, so the DEFAULT path allocates nothing and behaves
    exactly as it did before this function existed.

    ``round_robin`` recomputes every tick; that IS the strategy, and it
    costs one list rotation.

    ``screener`` rebuilds at most hourly. On any failure it keeps the
    previous order (or config order) and logs — ordering is a preference,
    never a reason to stop trading.
    """
    strategy = live.symbol_priority
    if strategy == "config_order":
        return None, None
    if strategy == "round_robin":
        return sweep_order("round_robin", list(live.symbols), tick=tick), None

    now = time.monotonic()
    if computed_at is not None and (now - computed_at) < _SWEEP_REFRESH_SECONDS:
        return current, computed_at
    if observe_storage is None:
        # Config validation forbids this pairing, so reaching it means the
        # DB failed to OPEN. Say so (hourly, not per tick) and keep trading.
        _LOGGER.warning(
            "symbol_priority=screener but the observe DB is unavailable; sweeping config order"
        )
        return None, now
    try:
        rankings, proximity = await _build_screener_inputs(
            list(live.symbols), storage, observe_storage, adapter, screener
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Deliberately broad, same posture as GridEngine.cancel_open_orders:
        # the port errors are already handled per-symbol inside, so anything
        # reaching here is a bug in the scoring math — and a bug in a
        # PREFERENCE must not take down a real-money loop. Keep the previous
        # order and retry at the next refresh.
        _LOGGER.warning(
            "sweep-order refresh failed; keeping the previous order: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return current, now
    order = sweep_order("screener", list(live.symbols), rankings=rankings, proximity=proximity)
    if order != current:
        _LOGGER.info(
            "sweep order updated (composite|proximity): %s",
            format_sweep(order, rankings, proximity),
            extra={
                "sweep_order": [str(s) for s in order],
                "strategy": strategy,
                "composites": {str(r.metrics.symbol): r.composite for r in rankings},
                "proximity": {str(s): proximity.get(s, math.inf) for s in order},
            },
        )
    return order, now


async def _restore_engine_state(
    engine: GridEngine,
    symbols: list[Symbol],
    operator_storage: StoragePort | None,
    grid_config: GridConfig | None = None,
) -> None:
    """Re-apply pauses AND offside episodes from ``engine_state`` at startup.

    Pause lived only in ``GridEngine._paused_symbols`` — process memory —
    so every restart silently resumed trading on a symbol the operator had
    deliberately stopped. The state was already being WRITTEN to disk every
    tick for dashboard visibility (ADR-030); nothing ever read it back.
    That is the whole fix: one read, at startup.

    Group 3 added the offside half. ``offside_since`` exists so a duration
    can be wall-clock truth instead of "since cli/live last started"; that
    only works if a restart re-seeds the running episode, because otherwise
    the first tick reads as a fresh transition and stamps the boot time.

    Unlike the pause, the offside seed is falsifiable — but only for a coin
    whose tick actually runs. ``_step_unlocked`` returns before the
    ``is_offside`` recompute for a DISABLED coin, and ``enabled: false`` is
    the documented way to stop a coin while leaving it in ``live.symbols``,
    so seeding one would leave a permanent OFFSIDE badge over a growing
    "Parked since" duration that nothing can ever re-check or clear. Those
    are skipped here. A paused symbol also skips the recompute, but its
    badge renders PAUSED and no duration reaches the operator.

    **No freshness guard, deliberately.** ``get_engine_states`` returns
    rows regardless of age and leaves the guard to each consumer, because
    the dashboard must not render a dead engine's rows as live. Restore
    wants the opposite: a pause is operator INTENT, not a cache entry, and
    expiring it silently resumes trading — precisely the failure being
    fixed. A pause set a week ago is still a pause.

    Only currently-configured symbols are restored. A row for a symbol
    since dropped from ``live.symbols`` is logged and left alone rather
    than deleted: it costs nothing, and it is evidence if that symbol ever
    comes back.
    """
    if operator_storage is None:
        return
    try:
        rows = await operator_storage.get_engine_states()
    except WobbleBotPortError as exc:
        # Never block a trading start on a visibility read — but say so
        # loudly, because the cost of silence here is resumed trading.
        _LOGGER.error(
            "could not read engine_state to restore pauses; symbols start ACTIVE: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return

    configured = set(symbols)
    restored: list[str] = []
    restored_offside: list[str] = []
    for row in rows:
        if row.symbol not in configured:
            if row.paused:
                _LOGGER.info(
                    "engine_state has a paused row for %s, which is not in live.symbols; ignoring",
                    row.symbol,
                    extra={"symbol": str(row.symbol), "reason": "not_configured"},
                )
            continue
        # Offside first, and outside the paused branch: a symbol can be
        # offside without being paused, and that is the common case. The
        # seed is what stops the first tick from looking like a fresh
        # onside->offside transition and stamping `since` with the boot
        # time — which would turn a symbol parked for weeks into one
        # parked for seconds on every deploy.
        enabled = grid_config is None or grid_config.for_coin(row.symbol.base).enabled
        if row.offside and enabled:
            engine.restore_offside(row.symbol, row.offside_ticks, row.offside_since)
            restored_offside.append(
                f"{row.symbol} (since {row.offside_since.isoformat()})"
                if row.offside_since is not None
                else f"{row.symbol} (start unknown)"
            )
        if not row.paused:
            continue
        engine.pause_symbol(row.symbol)
        age_h = (datetime.now(UTC) - row.updated_at).total_seconds() / 3600
        restored.append(f"{row.symbol} (paused as of {age_h:.1f}h ago)")

    if restored_offside:
        # INFO, not WARNING: an offside symbol is parked by design (ADR-006),
        # unlike a restored pause, which means trading is stopped and the
        # operator needs to know it stayed stopped.
        _LOGGER.info(
            "restored %d offside episode(s) from engine_state: %s",
            len(restored_offside),
            ", ".join(restored_offside),
            extra={"restored_offside": restored_offside},
        )

    if restored:
        _LOGGER.warning(
            "restored %d paused symbol(s) from engine_state: %s — these will NOT trade "
            "until resumed",
            len(restored),
            ", ".join(restored),
            extra={"restored_paused": restored},
        )


async def _emit_engine_states(
    engine: GridEngine,
    symbols: list[Symbol],
    storage: StoragePort,
    operator_storage: StoragePort | None,
) -> None:
    """Publish one engine_state row per symbol (ADR-030). Best-effort.

    Reads paused/offside from the ENGINE accessors, never from
    StepResult (whose ``offside`` is False on every non-"stepped"
    action — a paused symbol would misreport as onside).
    reference_price/anchored_at come from the persisted GridState;
    a failed grid-state read degrades those two fields to None
    rather than skipping the row — paused/offside visibility is the
    load-bearing part. No-op when operator_db is unwired (skips the
    grid-state reads too).
    """
    if operator_storage is None:
        return
    now = datetime.now(UTC)
    for symbol in symbols:
        reference_price = None
        anchored_at = None
        try:
            grid_state = await storage.get_grid_state(symbol)
        except WobbleBotPortError:
            grid_state = None  # visibility degrade, never a tick-breaker
        if grid_state is not None:
            reference_price = grid_state.reference_price
            anchored_at = grid_state.created_at.dt
        ticks = engine.offside_ticks(symbol)
        await emit_engine_state(
            operator_storage,
            EngineStateRow(
                symbol=symbol,
                paused=engine.is_paused(symbol),
                offside=ticks > 0,
                offside_ticks=ticks,
                reference_price=reference_price,
                anchored_at=anchored_at,
                # None whenever the engine never watched this episode start.
                # It is written through unchanged rather than defaulted, so
                # a restart cannot invent a start time for a symbol it found
                # already parked.
                offside_since=engine.offside_since(symbol),
                updated_at=now,
            ),
        )


async def _run_loop(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements,too-many-branches
    adapter: KrakenAdapter,
    engine: GridEngine,
    live: LiveConfig,
    storage: StoragePort,
    stop_event: asyncio.Event,
    *,
    operator_service: OperatorService | None = None,
    operator_storage: StoragePort | None = None,
    observe_storage: StoragePort | None = None,
    screener: ScreenerConfig | None = None,
    notifier: NotifierPort | None = None,
) -> int:
    """Run the engine loop. Returns the process exit code.

    When ``operator_service`` and ``operator_storage`` are provided,
    each iteration polls ``pending_commands WHERE status='approved'``
    before stepping the engine and exits cleanly when
    ``engine.is_stop_requested`` is set. When ``notifier`` is provided,
    session-start / session-end / fill / cap-trip events emit
    ``Notification`` rows for the cli/operator forwarder (Stage 5.6).

    ``observe_storage`` + ``screener`` back ``live.symbol_priority:
    screener`` — the only sweep strategy that needs OHLC history. Both
    are unused (and legitimately ``None``) under the other two
    strategies, so the default path opens no second DB.
    """
    screener_config = screener if screener is not None else ScreenerConfig()
    started_usd = await _session_usd_balance(adapter)
    started_value_usd = await _session_portfolio_value_usd(adapter, tuple(live.symbols))
    started_at = time.monotonic()
    # ``None`` means "no runtime cap" — operator opts into indefinite
    # mode. SIGINT/SIGTERM and the session-loss cap still apply, so
    # this isn't a way to bypass safety.
    max_runtime_seconds = (
        live.max_runtime_minutes * 60.0 if live.max_runtime_minutes is not None else None
    )
    _LOGGER.info(
        "session start (symbols=%s, tick_seconds=%s, max_runtime_seconds=%s, "
        "max_session_loss_usd=%s)",
        [str(s) for s in live.symbols],
        live.tick_seconds,
        max_runtime_seconds,
        fmt_decimal(live.max_session_loss_usd),
        extra={
            "symbols": [str(s) for s in live.symbols],
            "tick_seconds": live.tick_seconds,
            "max_runtime_seconds": max_runtime_seconds,  # None == unlimited
            "max_session_loss_usd": str(live.max_session_loss_usd),
            "starting_usd": str(started_usd),
            "starting_value_usd": str(started_value_usd),
        },
    )
    await notify(
        notifier,
        level="info",
        title="Live session started",
        message=(
            f"Trading {len(live.symbols)} symbol(s): "
            f"{', '.join(str(s) for s in live.symbols)}. "
            f"Starting portfolio value={started_value_usd} USD "
            f"(USD balance={started_usd})."
        ),
        event=SessionStartEvent(
            symbols=tuple(str(s) for s in live.symbols),
            tick_seconds=live.tick_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_session_loss_usd=live.max_session_loss_usd,
            starting_usd=started_usd,
            starting_value_usd=started_value_usd,
        ),
    )

    exit_code = 0
    tick = 0
    dms_unconfirmed_ticks = 0
    # ADR-037: per-session auth-failure escalation state (lockout
    # backoff, permanent-auth strikes, DMS-failure streak).
    escalation = _AuthEscalation()
    # ADR-038: symbols already paged for fee drift this session.
    fee_alerted: set[Symbol] = set()
    # Terminal-visible periodic heartbeat (separate from the operator.db
    # daemon_heartbeats row). After the 2026-05-23 logging-audit demoted
    # per-tick "tick complete" from INFO to DEBUG, a long quiet period
    # left the terminal looking dead. Initialize so the FIRST heartbeat
    # fires `terminal_heartbeat_seconds` after session-start, not right
    # at boot (where it'd duplicate the session-start INFO line).
    last_terminal_heartbeat_at = time.monotonic()
    # 2026-08-20 incident follow-up: last time the aggregate "symbol(s)
    # still held" reminder fired (see the check after each tick below).
    # None means "no held symbols currently being tracked" -- set on the
    # first tick any symbol is found held, cleared once none remain, so a
    # fresh hold episode always gets a full `held_reminder_seconds`
    # window before its first reminder rather than firing immediately.
    last_held_reminder_at: float | None = None
    # Sweep order (live.symbol_priority). Recomputed on a SLOW cadence
    # because the screener's inputs are hourly bars: re-ranking every tick
    # would spend storage reads to produce an identical answer, and an
    # order that wobbles tick-to-tick makes "why did SOL get funded and
    # ADA not" unanswerable after the fact. None => config order.
    sweep: list[Symbol] | None = None
    sweep_computed_at: float | None = None
    try:
        while not stop_event.is_set():
            # Stage 8.4.E follow-up — emit a heartbeat at the top of
            # each loop iteration so the /health page can prove the
            # daemon's event loop is processing. No-op when
            # operator_storage isn't wired.
            await emit_heartbeat(operator_storage, "cli/live")

            # ADR-021: pet Kraken's server-side dead man's switch every
            # tick (before any order is placed this iteration). If this
            # loop dies — crash, power loss, network partition — Kraken
            # auto-cancels all open orders once the timer lapses, the
            # safety net the finally-block cancel can't provide when the
            # host is gone. Log-and-continue: never crash a tick over the
            # switch, and a failed ping just leaves the timer at its prior
            # (still-protective) value until the next tick re-pings.
            # ADR-037 decision 6: with the trader key declared dead the
            # DMS ping stops too — every ping would be another invalid
            # auth attempt feeding the lockout counter, and the switch
            # retiring the book server-side is the designed outcome.
            # During a LOCKOUT backoff the ping deliberately continues:
            # its success is worth more than the marginal lockout-
            # extension risk, since failing it for 120s costs the book.
            #
            # 2026-08-20 incident follow-up: reset the per-tick DMS-timer-
            # expiry evidence UNCONDITIONALLY, before the block below (and
            # its early-exit conditions) can skip setting it. A book-vanish
            # message must never see stale True evidence from a prior tick.
            escalation.dms_timer_expired_this_tick = False
            # Same snapshot-before-the-ping discipline as the deadline check
            # below, and for the same reason: a same-tick recovery must not
            # erase evidence of the episode that just ended.
            escalation.dms_degraded_fraction_this_tick = escalation.dms_degraded_fraction(
                datetime.now(UTC), live.dead_mans_switch_seconds
            )
            if live.dead_mans_switch_seconds is not None and not escalation.auth_paused:
                # Snapshot BEFORE this tick's ping call: if Kraken's last
                # CONFIRMED promise has already passed, the server-side
                # timer could have fired regardless of whether THIS ping
                # now succeeds — a same-tick recovery must not erase that.
                if (
                    escalation.dms_trigger_at is not None
                    and datetime.now(UTC) >= escalation.dms_trigger_at
                ):
                    escalation.dms_timer_expired_this_tick = True
                try:
                    trigger_at = await adapter.set_dead_mans_switch(live.dead_mans_switch_seconds)
                    dms_unconfirmed_ticks = _log_dms_confirmation(
                        trigger_at, live.dead_mans_switch_seconds, dms_unconfirmed_ticks
                    )
                    if trigger_at is not None:
                        escalation.dms_trigger_at = trigger_at
                    if escalation.note_dms_success():
                        await notify(
                            notifier,
                            level="info",
                            title="Kraken API recovered",
                            message=(
                                "Dead-man's-switch resets are succeeding again after a "
                                "failure episode."
                            ),
                            context={"tick": tick},
                        )
                except WobbleBotPortError as exc:
                    _LOGGER.warning(
                        "dead man's switch reset failed; continuing (timer retains prior value): "
                        "%s: %s",
                        type(exc).__name__,
                        exc,
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )
                    await _note_private_call_failure(exc, escalation, engine, live, notifier)
                    alert_now = escalation.note_dms_failure()
                    if escalation.dms_failure_streak == 1:
                        # 2026-09-03 follow-up: name the deadline once per
                        # episode so a post-mortem can compare it against
                        # the moment the book vanished.
                        deadline_note = escalation.dms_deadline_note(datetime.now(UTC))
                        _LOGGER.warning(
                            "dead man's switch failure streak started; %s",
                            deadline_note,
                            extra={"dms_deadline_note": deadline_note},
                        )
                    if alert_now:
                        await notify(
                            notifier,
                            level="critical",
                            title="Dead-man's-switch resets failing",
                            message=(
                                f"{_DMS_FAILURE_STREAK_ALERT} consecutive DMS reset failures. "
                                f"If this persists past {live.dead_mans_switch_seconds}s, Kraken "
                                "cancels ALL open orders server-side. Likely causes: account "
                                "lockout (check for another daemon retrying a bad key) or "
                                "network partition."
                            ),
                            context={
                                "streak": escalation.dms_failure_streak,
                                "dms_seconds": live.dead_mans_switch_seconds,
                                "last_confirmed_trigger_at": (
                                    escalation.dms_trigger_at.isoformat()
                                    if escalation.dms_trigger_at is not None
                                    else None
                                ),
                            },
                        )

            elapsed = time.monotonic() - started_at
            if max_runtime_seconds is not None and elapsed >= max_runtime_seconds:
                _LOGGER.info(
                    "max runtime reached; stopping (elapsed_seconds=%s)",
                    round(elapsed, 1),
                    extra={"elapsed_seconds": round(elapsed, 1)},
                )
                break

            # Operator interaction poll (Stage 5.4): drain approved
            # pending_commands BEFORE the engine tick so an operator
            # PauseCommand takes effect on the current tick.
            if operator_service is not None and operator_storage is not None:
                try:
                    await _process_pending_commands(operator_service, operator_storage, notifier)
                except WobbleBotPortError as exc:
                    _LOGGER.warning(
                        "pending_commands poll failed; continuing: %s",
                        exc,
                        extra={"error": str(exc), "error_type": type(exc).__name__},
                    )

            # Soft-stop honored after the poll so a StopCommand processed
            # this tick exits cleanly without one more engine step.
            if engine.is_stop_requested:
                _LOGGER.info("engine soft stop requested; exiting cleanly")
                break

            tick += 1
            sweep, sweep_computed_at = await _refresh_sweep_order(
                live=live,
                screener=screener_config,
                storage=storage,
                observe_storage=observe_storage,
                adapter=adapter,
                tick=tick,
                current=sweep,
                computed_at=sweep_computed_at,
            )
            if await _run_one_tick(
                adapter,
                engine,
                live,
                tick,
                started_value_usd,
                notifier,
                sweep=sweep,
                escalation=escalation,
                fee_alerted=fee_alerted,
            ):
                exit_code = 1
                break

            # ADR-030: publish per-symbol engine visibility AFTER the
            # tick so the row reflects this tick's paused/offside
            # outcome. Best-effort — never breaks the loop.
            await _emit_engine_states(engine, list(live.symbols), storage, operator_storage)

            last_held_reminder_at = await _check_held_reminder(
                engine, live, notifier, tick, last_held_reminder_at
            )

            # Periodic terminal-visible heartbeat. Cheap (just a log
            # line) — no Kraken/Storage calls. Proves the loop is
            # alive without flooding the terminal at tick cadence.
            now = time.monotonic()
            since_heartbeat = now - last_terminal_heartbeat_at
            if since_heartbeat >= live.terminal_heartbeat_seconds:
                session_elapsed_seconds = now - started_at
                _LOGGER.info(
                    "periodic heartbeat: tick %d, elapsed %dh %02dm, symbols %s",
                    tick,
                    int(session_elapsed_seconds // 3600),
                    int((session_elapsed_seconds % 3600) // 60),
                    ",".join(str(s) for s in live.symbols),
                    extra={
                        "tick": tick,
                        "elapsed_seconds": round(session_elapsed_seconds, 1),
                        "symbols": [str(s) for s in live.symbols],
                    },
                )
                last_terminal_heartbeat_at = now

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=live.tick_seconds)
            except asyncio.TimeoutError:
                pass  # normal — tick interval elapsed
    finally:
        # Stage 8.4 hotfix: each cleanup step gets its own try/except so a
        # transient failure in one (e.g. DNS down during a power outage
        # firing _session_usd_balance) can't skip the others. Order
        # cancellation is the most safety-critical cleanup; it must run
        # even if the balance fetch craters. Per the runbook §"Hard stop":
        # "cli/live shutdown leaves orders open on Kraken" is a hard stop
        # — surfaced during the 2026-05-19 soak outage where DNS failed
        # mid-finally and three open BUYs were never cancelled.
        try:
            ended_usd = await _session_usd_balance(adapter)
            ended_value_usd = await _session_portfolio_value_usd(adapter, tuple(live.symbols))
            ended_known = True
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "session_end balance fetch failed; PnL unavailable: %s",
                exc,
                extra={"error": str(exc)},
            )
            ended_usd = started_usd
            ended_value_usd = started_value_usd
            ended_known = False
        try:
            cancelled, failed = await _cancel_all_open(adapter, storage, tuple(live.symbols))
            cancel_clean = failed == 0
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "session_end cancel_all_open raised; reconciler will catch stragglers: %s",
                exc,
                extra={"error": str(exc)},
            )
            cancelled, failed = 0, 0
            cancel_clean = False
        # ADR-021: disarm the dead man's switch ONLY on a confirmed-clean
        # cancel. If our own cancellation failed or raised, deliberately
        # LEAVE it armed so Kraken's timer becomes the backstop that sweeps
        # the stragglers we couldn't — exactly the failure mode it exists
        # for. Disarm == set timeout 0.
        if live.dead_mans_switch_seconds is not None and cancel_clean:
            try:
                await adapter.set_dead_mans_switch(0)
            except WobbleBotPortError as exc:
                _LOGGER.warning(
                    "dead man's switch disarm failed; Kraken timer will lapse harmlessly: %s",
                    exc,
                    extra={"error": str(exc)},
                )
        session_pnl = ended_value_usd - started_value_usd if ended_known else Decimal("0")
        if exit_code == 1:
            # ADR-024: record the trip for the next session's cool-down
            # gate. Own try/except -- a storage failure here must not
            # mask the cap-trip session-end logging/notification below,
            # and the gate itself fails open on a read error, so a
            # failed write here just means the NEXT session isn't gated
            # (same fail-soft posture, not a crash-loop risk).
            try:
                await storage.record_cap_trip(Timestamp(dt=datetime.now(UTC)), session_pnl)
            except StorageError as exc:
                _LOGGER.warning(
                    "failed to record cap trip for cool-down gate: %s",
                    exc,
                    extra={"error": str(exc)},
                )
        duration_seconds = round(time.monotonic() - started_at, 1)
        ending_usd_str = str(ended_usd) if ended_known else "unknown"
        ending_value_str = str(ended_value_usd) if ended_known else "unknown"
        session_pnl_str = str(session_pnl) if ended_known else "unknown"
        _LOGGER.info(
            "session end (ticks=%s, duration_seconds=%s, starting_usd=%s, ending_usd=%s)",
            tick,
            duration_seconds,
            fmt_decimal(started_usd),
            ending_usd_str,
            extra={
                "ticks": tick,
                "duration_seconds": duration_seconds,
                "starting_usd": str(started_usd),
                "ending_usd": ending_usd_str,
                "starting_value_usd": str(started_value_usd),
                "ending_value_usd": ending_value_str,
                "session_pnl_usd": session_pnl_str,
                "open_orders_cancelled": cancelled,
                "open_orders_cancel_failed": failed,
                "exit_code": exit_code,
            },
        )
        await notify(
            notifier,
            level="error" if exit_code != 0 else "info",
            title=f"Live session ended (exit {exit_code})",
            message=(
                f"{tick} tick(s), {duration_seconds}s runtime. "
                f"PnL {session_pnl_str} USD "
                f"(value {started_value_usd} -> {ending_value_str}; "
                f"USD {started_usd} -> {ending_usd_str}). "
                f"Cancelled {cancelled} open order(s); {failed} cancel failure(s)."
            ),
            event=SessionEndEvent(
                ticks=tick,
                duration_seconds=duration_seconds,
                starting_usd=started_usd,
                ending_usd=ended_usd if ended_known else None,
                starting_value_usd=started_value_usd,
                ending_value_usd=ended_value_usd if ended_known else None,
                session_pnl_usd=session_pnl if ended_known else None,
                open_orders_cancelled=cancelled,
                open_orders_cancel_failed=failed,
                exit_code=exit_code,
            ),
        )
    return exit_code


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def _open_observe_storage(observe_db: str | None) -> SQLiteStorageAdapter | None:
    """Open the observe DB for ``symbol_priority: screener``, or ``None``.

    The screener strategy ranks from ``ohlc_bars``, which live in the
    observe DB — a THIRD adapter on the trading path, opened only when
    the operator selected that strategy (config validation pairs the two,
    so a ``None`` path here means they didn't).

    A failure to open degrades the sweep to config order rather than
    refusing to trade. Ordering is a preference; the money path must not
    depend on a data-collection DB being reachable.
    """
    if observe_db is None:
        return None
    storage = SQLiteStorageAdapter(observe_db)
    try:
        await storage.connect()
    except (StorageError, OSError) as exc:
        _LOGGER.error(
            "could not open observe_db for sweep ordering; sweeping config order "
            "(observe_db=%s): %s: %s",
            observe_db,
            type(exc).__name__,
            exc,
            extra={
                "observe_db": observe_db,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return None
    return storage


async def _main_async(  # pylint: disable=too-many-locals,too-many-statements
    config: WobbleBotConfig, *, ignore_cool_down: bool = False
) -> int:
    if config.live is None:
        return missing_section_exit(_LOGGER, "live")

    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADER_API_KEY",
            secret_var="KRAKEN_TRADER_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error(
            "missing trade credentials: %s",
            exc,
            extra={"error": str(exc)},
        )
        return 2

    storage = SQLiteStorageAdapter(config.live.db)
    await storage.connect()

    # ADR-024: refuse to start a new session for a configurable window
    # after the last session-loss-cap exit (exit_code=1) -- an
    # immediate restart (operator knee-jerk, or a `restart:
    # unless-stopped` policy) would otherwise re-enter the same losing
    # condition (the soak's 4:22am cap-trip-then-restart incident).
    # `--ignore-cool-down` is the terminal-only, one-time bypass; it
    # does not clear the record. Fail-OPEN on a storage-read error
    # (log + proceed) -- this is a safety *feature*, not a safety-
    # *critical* invariant, and failing closed would crash-loop under
    # `restart: unless-stopped` (docker rule 6).
    if not ignore_cool_down:
        try:
            last_trip_at = await storage.get_last_cap_trip_at()
        except StorageError as exc:
            _LOGGER.warning(
                "cool-down check failed to read cap_trips; proceeding: %s",
                exc,
                extra={"error": str(exc)},
            )
            last_trip_at = None
        status = check_cool_down(
            last_trip_at, now=datetime.now(UTC), window_minutes=config.live.cool_down_minutes
        )
        if status.active:
            _LOGGER.error(
                "session-loss-cap cool-down in effect; refusing to start (resumes_at=%s)",
                status.resumes_at.isoformat() if status.resumes_at else None,
                extra={
                    "resumes_at": status.resumes_at.isoformat() if status.resumes_at else None,
                },
            )
            await storage.close()
            return 4

    adapter = KrakenAdapter(config=kraken_config)

    exit_code = await partition_or_exit(
        adapter,
        config.live.symbols,
        logger=_LOGGER,
        cleanups=[
            ("close_kraken_adapter", adapter.aclose),
            ("close_live_storage", storage.close),
        ],
    )
    if exit_code is not None:
        return exit_code

    # Stage 8.1.C: startup reconciliation per ADR-018, extended by
    # ADR-023. Run between storage open + adapter construct and engine
    # first tick — refuses to start if the adapter is unreachable
    # (booting against unreconciled state is what this stage exists to
    # prevent), and now BEFORE engine construction so a recovered fill's
    # order UUID (ADR-023's needs_counter_order_ids) can be threaded
    # into GridEngine as pending_counters. The configured-symbols filter
    # narrows orphan logging to the engine's actual trade set (operator
    # manual orders on other coins stay silent per stage-8.1-design.md
    # decision 8).
    configured_symbols = frozenset(s.base.upper() for s in config.live.symbols)
    try:
        report = await apply_reconciliation(adapter, storage, configured_symbols=configured_symbols)
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "startup reconciliation failed; refusing to start: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    if report.storage_canceled_count or report.orphan_count or report.recovered_fill_count:
        _LOGGER.info(
            "startup reconciliation complete (storage_canceled=%s, "
            "storage_persistence_failures=%s, orphan_count=%s, recovered_fill_count=%s)",
            report.storage_canceled_count,
            report.storage_persistence_failures,
            report.orphan_count,
            report.recovered_fill_count,
            extra={
                "storage_canceled": report.storage_canceled_count,
                "storage_persistence_failures": report.storage_persistence_failures,
                "orphan_count": report.orphan_count,
                "recovered_fill_count": report.recovered_fill_count,
            },
        )

    # ADR-038: fetch the account's ACTUAL fee rates from Kraken at
    # session start instead of trusting the code constants — the
    # 2026-07-09 fee doubling hid for five weeks behind a schedule
    # copy. Conservative reduction: the MAX rate across symbols feeds
    # the sell guard (overestimating a fee only defers more sells,
    # never approves a worse one). Fetch failure falls back to the
    # constants with a WARNING; the session-start receipt logs which.
    maker_rate, taker_rate = await _fetch_session_fee_rates(adapter, config.live.symbols)
    _warn_if_spacing_below_fee_floor(config.grid, maker_rate)

    engine = GridEngine(
        adapter,
        storage,
        config.grid,
        config.safety,
        pending_counters=list(report.needs_counter_order_ids),
        maker_fee_rate=maker_rate,
        taker_fee_rate=taker_rate,
    )

    # Stage 5.4: optional operator-interaction wiring. When operator_db
    # is set in settings.yml, open it as a second storage adapter and
    # construct an OperatorService; cli/live's loop will poll it.
    # Stage 5.5: same operator_db backs the SqliteNotifierAdapter that
    # cli/live writes outbound events to (cli/operator forwards them
    # to Discord). Both share the same StoragePort connection.
    operator_storage: SQLiteStorageAdapter | None = None
    operator_service: OperatorService | None = None
    notifier: SqliteNotifierAdapter | None = None
    if config.live.operator_db is not None:
        operator_storage = SQLiteStorageAdapter(config.live.operator_db)
        await operator_storage.connect()
        operator_service = OperatorService(
            engine=engine,
            storage=storage,
            active_symbols=tuple(config.live.symbols),
            grid_config=config.grid,
            session_started_at=Timestamp(dt=datetime.now(UTC)),
        )
        notifier = SqliteNotifierAdapter(operator_storage)
        _LOGGER.info(
            "operator interaction enabled (operator_db=%s)",
            config.live.operator_db,
            extra={"operator_db": config.live.operator_db},
        )
        # Before the first tick: re-apply pauses the operator set in a
        # previous session. Until 2026-08-12 a restart silently resumed
        # trading on every paused symbol.
        await _restore_engine_state(
            engine, list(config.live.symbols), operator_storage, config.grid
        )

    observe_storage = await _open_observe_storage(config.live.observe_db)

    stop_event = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop_event, logger=_LOGGER)

    try:
        return await _run_loop(
            adapter=adapter,
            engine=engine,
            live=config.live,
            storage=storage,
            stop_event=stop_event,
            operator_service=operator_service,
            operator_storage=operator_storage,
            observe_storage=observe_storage,
            screener=config.screener,
            notifier=notifier,
        )
    finally:
        # NOTE: this is the OUTER finally — resource close after
        # _run_loop's INNER finally has already done session-end work
        # (balance fetch, cancel_all_open, session-end log, session-end
        # notification). The inner finally is intentionally NOT routed
        # through safe_shutdown because cancel_all_open is the most
        # safety-critical cleanup in the codebase and is already
        # hardened with per-step try/except (e2b6cfc, Stage 8.4 hotfix).
        # Forcing a hard wall-clock cap on it could exit before all
        # Kraken cancels complete — worse than the original problem.
        # Resource closes here are safe to timeout-bound because by the
        # time we reach this point, orders have already been cancelled.
        phases: list[tuple[str, Any]] = [
            ("close_kraken_adapter", adapter.aclose),
            ("close_live_storage", storage.close),
        ]
        if operator_storage is not None:
            phases.append(("close_operator_storage", operator_storage.close))
        if observe_storage is not None:
            phases.append(("close_observe_storage", observe_storage.close))
        await safe_shutdown(phases, logger=_LOGGER)


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Translate explicit CLI flags into a YAML override dict."""
    live_overrides = collect_overrides(
        args,
        "live",
        {
            "symbols": ("symbols", parse_symbol_csv),
            "db": ("db", identity),
            "tick_seconds": ("tick_seconds", identity),
            "max_runtime_minutes": ("max_runtime_minutes", identity),
            "max_session_loss_usd": ("max_session_loss_usd", identity),
            "log_format": ("log_format", identity),
        },
    )

    # grid.default is nested — build manually
    grid_default: dict[str, Any] = {}
    if args.spacing is not None:
        grid_default["spacing_percentage"] = args.spacing
    if args.above is not None:
        grid_default["levels_above"] = args.above
    if args.below is not None:
        grid_default["levels_below"] = args.below
    if args.order_size is not None:
        grid_default["order_size_usd"] = args.order_size
    grid_overrides = {"grid": {"default": grid_default}} if grid_default else {}

    safety_overrides = collect_overrides(
        args,
        "safety",
        {
            "max_total_exposure_usd": ("max_total_exposure_usd", identity),
            "max_per_coin_exposure_usd": ("max_per_coin_exposure_usd", identity),
            "max_orders_per_coin": ("max_orders_per_coin", identity),
            "max_daily_spend_usd": ("max_daily_spend_usd", identity),
        },
    )

    # Merge all three top-level overlays
    merged: dict[str, Any] = {}
    for layer in (live_overrides, grid_overrides, safety_overrides):
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)

    # All flag defaults are None — explicit-pass detection drives
    # whether the value overrides YAML or inherits.
    parser.add_argument(
        "--symbols", default=None, help="Comma-separated trading pairs (e.g. BTC/USD,ETH/USD)."
    )
    parser.add_argument("--spacing", type=Decimal, default=None)
    parser.add_argument("--above", type=int, default=None)
    parser.add_argument("--below", type=int, default=None)
    parser.add_argument("--order-size", type=Decimal, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--tick-seconds", type=float, default=None)
    parser.add_argument("--max-runtime-minutes", type=float, default=None)
    parser.add_argument("--max-session-loss-usd", type=Decimal, default=None)
    parser.add_argument("--max-total-exposure-usd", type=Decimal, default=None)
    parser.add_argument("--max-per-coin-exposure-usd", type=Decimal, default=None)
    parser.add_argument("--max-orders-per-coin", type=int, default=None)
    parser.add_argument("--max-daily-spend-usd", type=Decimal, default=None)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    # ADR-024: terminal-only bypass for one deliberate restart during an
    # active cool-down window. Not YAML-settable -- a Portainer redeploy
    # or a `restart: unless-stopped` policy can't standing-bypass it,
    # and it does NOT clear the recorded trip.
    parser.add_argument(
        "--ignore-cool-down",
        action="store_true",
        help="Bypass the session-loss-cap cool-down window for this one restart (ADR-024).",
    )
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except CONFIG_LOAD_ERRORS as exc:
        return config_load_exit(exc)

    log_format = config.live.log_format if config.live else "plain"
    log_file_path = config.live.log_file_path if config.live else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)

    run_with_clean_exit(_main_async(config, ignore_cool_down=args.ignore_cool_down), logger=_LOGGER)


if __name__ == "__main__":
    raise SystemExit(main())
