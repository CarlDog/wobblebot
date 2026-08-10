"""Engine startup reconciliation (Stage 8.1.C — ADR-018; extended by ADR-023).

When cli/live or cli/shadow boots, storage's view of open orders
and the exchange's view can disagree. Three scenarios produce
drift:

1. **Shutdown bug.** Shutdown loop called ``adapter.cancel_order``
   but didn't persist (fixed in Stage 8.1.B; reconciliation
   catches stragglers from prior buggy sessions).
2. **Out-of-band cancellation.** Kraken cancelled an order while
   the daemon was offline (expiry, manual cancel via Kraken Pro,
   exchange-side incident).
3. **Out-of-band placement.** Operator placed an order via
   Kraken Pro that storage doesn't track.

Per ADR-018, the exchange is authoritative. This module enforces
the policy:

- **Storage-only orders** (status="open" in storage, not on
  exchange) → resolved via :func:`_resolve_terminal_order` (ADR-023)
  rather than assumed canceled: a storage-only order that actually
  filled (fully, or partially before a cancel/expiry) while the
  daemon was down gets its Trade rows recovered and is flagged for
  a counter-order on the engine's first tick. A genuine clean
  cancel/expire (``filled_amount == 0``) is marked canceled as
  before.
- **Exchange-only orders** (on exchange, not in storage) → log
  ERROR + do NOT adopt. Operator must manually review.

Two-layer split following the Stage 2.2 pure-function-first
pattern:

- :func:`reconcile_open_orders` — pure function over the two
  input lists. Returns a :class:`ReconciliationPlan` enumerating
  what to do. No I/O.
- :func:`apply_reconciliation` — async orchestrator. Queries the
  adapter + storage, calls the pure function, resolves each
  storage-only order's true terminal state, logs the orphans,
  returns a :class:`ReconciliationReport` for the caller's
  session-start logging.

``_resolve_terminal_order`` is also used by ``GridEngine._detect_fills``
(the live per-tick path) — ADR-023 treats the startup and live cases as
one root state (an order that left the open set with ``filled_amount >
0`` regardless of whether it refreshed to ``closed`` or
``canceled``/``expired``) behind one shared resolver.

CLI wiring is one call from each daemon's ``_main_async`` between
storage open + engine first tick. See ``cli/live`` and
``cli/shadow``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Symbol, Timestamp, fmt_decimal
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger("wobblebot.services.reconciler")

# How many recent trades to pull per symbol when resolving storage-only
# orders at startup. Boot-time, not per-tick — a handful of extra calls
# (bounded by the number of distinct symbols with a storage-only order)
# is not the rate-limit-storm risk class the per-tick shared-fetch
# optimization (fleet-review #19 finding 8) guards against.
_RECONCILE_TRADE_HISTORY_LIMIT = 200


# --------------------------------------------------------------------- #
# Pure-function plan + orchestrator report                              #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReconciliationPlan:
    """What the pure reconciler decided to do.

    Attributes:
        storage_only: Orders that storage has as ``open`` but the
            exchange doesn't know about. These get marked canceled.
        exchange_only: Orders the exchange has that storage doesn't
            track. Logged at ERROR; NOT adopted. Operator handles
            manually.
    """

    storage_only: tuple[Order, ...] = ()
    exchange_only: tuple[Order, ...] = ()


@dataclass(frozen=True)
class ReconciliationReport:
    """What ``apply_reconciliation`` actually did.

    Attributes:
        storage_canceled_count: Number of storage rows transitioned
            to a clean ``canceled``/``expired`` terminal state
            (``filled_amount == 0``).
        storage_persistence_failures: How many transitions failed at
            the storage layer (logged + counted; reconciliation
            continues so one bad row doesn't block boot).
        orphan_count: Number of exchange-only orders detected.
        orphan_summaries: One short string per orphan for the
            caller's session-start summary log line.
        recovered_fill_count: Number of storage-only orders resolved
            with ``filled_amount > 0`` (ADR-023) — a real fill that
            would have been silently dropped as a clean cancel.
        needs_counter_order_ids: UUIDs of the recovered-fill orders
            above, each needing a counter-order on the engine's first
            tick. The reconciler never places orders itself (financial
            power fragmentation) — ``GridEngine`` consumes this list.
    """

    storage_canceled_count: int = 0
    storage_persistence_failures: int = 0
    orphan_count: int = 0
    orphan_summaries: tuple[str, ...] = ()
    recovered_fill_count: int = 0
    needs_counter_order_ids: tuple[UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TerminalOrderResolution:
    """The true terminal state of one order that left the open set (ADR-023).

    ``order`` is the refreshed order (correct ``status`` +
    ``filled_amount`` from ``get_order_status``). ``trades`` are the
    matched ``Trade`` rows to persist — non-empty only when
    ``filled_amount > 0``, regardless of whether the order refreshed to
    ``closed`` (full fill) or ``canceled``/``expired`` (partial fill
    before the cancel/expiry — the F1 case). ``needs_counter`` mirrors
    "there was a real fill here": the caller must place a counter sized
    to ``order.filled_amount``.
    """

    order: Order
    trades: tuple[Trade, ...] = ()
    needs_counter: bool = False


# --------------------------------------------------------------------- #
# Pure function                                                          #
# --------------------------------------------------------------------- #


def _match_key(order: Order) -> str:
    """Match storage orders to exchange orders by ``exchange_id``.

    Storage rows without an ``exchange_id`` are unsubmitted (status
    "pending") and don't participate in reconciliation — the engine
    hadn't actually placed them yet, so the exchange wouldn't know
    about them. ADR-018's reconciliation policy applies only to
    submitted (``open``) orders.
    """
    return order.exchange_id or ""


def reconcile_open_orders(
    *,
    exchange_open: list[Order],
    storage_open: list[Order],
    configured_symbols: frozenset[str] | None = None,
) -> ReconciliationPlan:
    """Diff exchange's open orders against storage's open orders.

    Per ADR-018 the exchange is authoritative. This pure function
    enumerates the two divergence classes:

    - **storage_only**: storage has the row as ``open`` but the
      exchange doesn't report it. These get marked canceled.
    - **exchange_only**: the exchange has the order but storage
      doesn't track it. These get logged + flagged for operator
      review.

    Args:
        exchange_open: Orders the adapter reports as currently open.
        storage_open: Storage rows with ``status="open"``.
        configured_symbols: Optional restriction on which symbol
            bases the engine actually trades. Exchange-only orders
            outside this set are SILENTLY SKIPPED (operator's
            non-engine orders on unrelated coins). Storage-only
            reconciliation still runs against ALL storage rows —
            stale rows in any symbol get cleared regardless. Pass
            ``None`` to disable filtering (consider every orphan).

    Returns:
        :class:`ReconciliationPlan` enumerating the two diff classes.
    """
    exchange_ids = {_match_key(o) for o in exchange_open if _match_key(o)}
    storage_ids = {_match_key(o) for o in storage_open if _match_key(o)}

    # Storage-only: rows storage has open that exchange doesn't.
    # Skip storage rows with empty exchange_id (unsubmitted; the
    # engine hadn't placed them yet — see _match_key docstring).
    storage_only = tuple(
        o for o in storage_open if _match_key(o) and _match_key(o) not in exchange_ids
    )

    # Exchange-only: orders exchange has that storage doesn't.
    exchange_only_raw = [
        o for o in exchange_open if _match_key(o) and _match_key(o) not in storage_ids
    ]

    if configured_symbols is not None:
        # Filter exchange-only orders to just the symbols we
        # actually trade. Orphans on other symbols are operator's
        # business, not the engine's.
        exchange_only = tuple(
            o for o in exchange_only_raw if o.symbol.base.upper() in configured_symbols
        )
    else:
        exchange_only = tuple(exchange_only_raw)

    return ReconciliationPlan(
        storage_only=storage_only,
        exchange_only=exchange_only,
    )


# --------------------------------------------------------------------- #
# Async orchestrator                                                    #
# --------------------------------------------------------------------- #


class _AdapterLike(Protocol):
    """The slice of ExchangePort apply_reconciliation needs."""

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[Order]: ...

    async def get_order_status(self, order: Order) -> Order: ...

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]: ...


async def _resolve_terminal_order(
    adapter: _AdapterLike,
    order: Order,
    trades_by_order: dict[str, list[Trade]],
) -> TerminalOrderResolution:
    """Resolve one order that's left the open set into its true terminal
    state, instead of assuming a clean cancel (ADR-023).

    Refreshes via ``get_order_status`` (Kraken QueryOrders already
    reports the real ``status`` + ``filled_amount`` for a
    partially-filled cancel/expire — see
    ``kraken_exchange._apply_kraken_order_update``). An order with
    ``filled_amount > 0`` had a real fill regardless of whether it
    refreshed to ``closed`` (full fill) or ``canceled``/``expired``
    (partial fill before the cancel/expiry, the F1 case): its matched
    ``Trade`` rows must be persisted and a counter placed. An order
    with ``filled_amount == 0`` is a genuine clean cancel/expire.

    ``trades_by_order`` is caller-supplied (keyed by ``exchange_id``)
    rather than fetched here, mirroring ``GridEngine._detect_fills``'s
    shared-per-tick-fetch pattern — this function never issues its own
    trade-history call.
    """
    refreshed = await adapter.get_order_status(order)
    if refreshed.filled_amount > 0 and refreshed.exchange_id:
        trades = tuple(trades_by_order.get(refreshed.exchange_id, []))
        return TerminalOrderResolution(order=refreshed, trades=trades, needs_counter=True)
    return TerminalOrderResolution(order=refreshed)


def _summarize_orphan(order: Order) -> str:
    """One short string per orphan for the session-start summary."""
    return (
        f"{order.symbol.base}/{order.symbol.quote} {order.side.value} "
        f"@ ${order.price.amount} (exchange_id={order.exchange_id or '?'})"
    )


async def apply_reconciliation(  # pylint: disable=too-many-locals
    # R0914 disable: every local is a distinct ADR-023 resolution-stage
    # signal (plan, per-symbol trade index, the four report counters,
    # the per-order resolution/exception locals in the loop below) --
    # splitting further would obscure the linear reconcile-then-persist
    # flow without removing complexity.
    adapter: _AdapterLike,
    storage: StoragePort,
    *,
    configured_symbols: frozenset[str] | None = None,
) -> ReconciliationReport:
    """Run startup reconciliation against the configured exchange.

    Queries both sides via a single call each (global ``get_open_orders``
    against the adapter; per ADR-018 decision 1, the adapter is
    authoritative). Computes the plan via :func:`reconcile_open_orders`,
    persists storage-only orders as ``canceled``, logs every orphan at
    ERROR level, and returns the report.

    Per ADR-018 decision 8 / stage-8.1-design.md decision 7 the helper
    inherits the adapter's timeout. If the adapter's ``get_open_orders``
    raises, the function propagates so the daemon refuses to start —
    booting against unreconciled state is what this stage exists to
    prevent.

    Args:
        adapter: ExchangePort-shaped object. Only ``get_open_orders``
            is consulted (the read side; reconciliation never places
            or cancels orders, only marks storage rows).
        storage: StoragePort. ``get_open_orders`` (storage side) +
            ``save_order`` for the canceled transitions.
        configured_symbols: Optional set of symbol bases (uppercase)
            the engine is configured for. Orphan orders on other
            symbols are silently skipped per stage-8.1-design.md
            decision 8.

    Returns:
        :class:`ReconciliationReport` with per-class counts + orphan
        summaries the caller can include in session-start logging.

    Raises:
        ExchangeError: If ``adapter.get_open_orders`` fails. Propagates
            so the daemon refuses to start.
        StorageError: If ``storage.get_open_orders`` fails. Propagates
            for the same reason. Per-row ``save_order`` failures inside
            the loop are logged + counted, not raised.
    """
    exchange_open = await adapter.get_open_orders()
    storage_open = await storage.get_open_orders()

    plan = reconcile_open_orders(
        exchange_open=exchange_open,
        storage_open=storage_open,
        configured_symbols=configured_symbols,
    )

    # ADR-023: resolve each storage-only order's true terminal state
    # (recovered fill vs clean cancel) instead of assuming cancel.
    # Trade history is fetched once per distinct symbol among the
    # storage-only orders (boot-time, not the per-tick hot path the
    # fleet-review #19 shared-fetch optimization guards).
    trades_by_symbol_order: dict[Symbol, dict[str, list[Trade]]] = {}
    for stale in plan.storage_only:
        if stale.symbol not in trades_by_symbol_order:
            recent_trades = await adapter.get_trade_history(
                symbol=stale.symbol, limit=_RECONCILE_TRADE_HISTORY_LIMIT
            )
            by_order: dict[str, list[Trade]] = {}
            for trade in recent_trades:
                by_order.setdefault(trade.order_id, []).append(trade)
            trades_by_symbol_order[stale.symbol] = by_order

    canceled_count = 0
    failures = 0
    recovered_fill_count = 0
    needs_counter_order_ids: list[UUID] = []
    now = Timestamp(dt=datetime.now(UTC))
    for stale in plan.storage_only:
        resolution = await _resolve_terminal_order(
            adapter, stale, trades_by_symbol_order[stale.symbol]
        )
        try:
            if resolution.needs_counter:
                for trade in resolution.trades:
                    await storage.save_trade(trade)
                await storage.save_order(resolution.order.model_copy(update={"updated_at": now}))
                recovered_fill_count += 1
                needs_counter_order_ids.append(stale.id)
                _LOGGER.warning(
                    "reconciler: %s %s (%s) recovered a real fill of %s "
                    "while the daemon was down; queuing a counter-order",
                    stale.symbol,
                    stale.side.value.upper(),
                    stale.exchange_id,
                    fmt_decimal(resolution.order.filled_amount),
                    extra={
                        "exchange_id": stale.exchange_id,
                        "symbol": str(stale.symbol),
                        "side": stale.side.value,
                        "status": resolution.order.status,
                        "filled_amount": str(resolution.order.filled_amount),
                        "trades_recovered": len(resolution.trades),
                    },
                )
            else:
                await storage.save_order(
                    resolution.order.model_copy(update={"status": "canceled", "updated_at": now})
                )
                canceled_count += 1
                _LOGGER.info(
                    "reconciler: storage-only %s %s @ %s (%s) marked canceled",
                    stale.symbol,
                    stale.side.value.upper(),
                    fmt_decimal(stale.price.amount),
                    stale.exchange_id,
                    extra={
                        "exchange_id": stale.exchange_id,
                        "symbol": str(stale.symbol),
                        "side": stale.side.value,
                        "reason": "not_on_exchange_at_startup",
                    },
                )
        except StorageError as exc:
            failures += 1
            _LOGGER.error(
                "reconciler: failed to persist terminal-order transition; row stays open "
                "(exchange_id=%s, symbol=%s): %s",
                stale.exchange_id,
                stale.symbol,
                exc,
                extra={
                    "exchange_id": stale.exchange_id,
                    "symbol": str(stale.symbol),
                    "error": str(exc),
                },
            )

    # Log orphans at ERROR level. Each orphan gets its own line so the
    # operator can grep for the exchange_id.
    summaries: list[str] = []
    for orphan in plan.exchange_only:
        summary = _summarize_orphan(orphan)
        summaries.append(summary)
        _LOGGER.error(
            "reconciler: exchange-only orphan order detected; engine will NOT adopt "
            "(exchange_id=%s, symbol=%s, side=%s, price=%s)",
            orphan.exchange_id,
            orphan.symbol,
            orphan.side.value,
            fmt_decimal(orphan.price.amount),
            extra={
                "exchange_id": orphan.exchange_id,
                "symbol": str(orphan.symbol),
                "side": orphan.side.value,
                "price": str(orphan.price.amount),
                "amount": str(orphan.amount.value),
                "status_at_exchange": orphan.status,
            },
        )

    if summaries:
        _LOGGER.error(
            "orphan orders detected at startup, %d total — "
            "review Kraken Pro and reconcile manually",
            len(summaries),
        )

    return ReconciliationReport(
        storage_canceled_count=canceled_count,
        storage_persistence_failures=failures,
        orphan_count=len(plan.exchange_only),
        orphan_summaries=tuple(summaries),
        recovered_fill_count=recovered_fill_count,
        needs_counter_order_ids=tuple(needs_counter_order_ids),
    )


__all__ = (
    "ReconciliationPlan",
    "ReconciliationReport",
    "TerminalOrderResolution",
    "apply_reconciliation",
    "reconcile_open_orders",
)
