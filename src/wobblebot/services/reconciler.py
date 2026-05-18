"""Engine startup reconciliation (Stage 8.1.C — ADR-018).

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
  exchange) → mark canceled with updated_at = now().
- **Exchange-only orders** (on exchange, not in storage) → log
  ERROR + do NOT adopt. Operator must manually review.

Two-layer split following the Stage 2.2 pure-function-first
pattern:

- :func:`reconcile_open_orders` — pure function over the two
  input lists. Returns a :class:`ReconciliationPlan` enumerating
  what to do. No I/O.
- :func:`apply_reconciliation` — async orchestrator. Queries the
  adapter + storage, calls the pure function, writes the
  transitions, logs the orphans, returns a
  :class:`ReconciliationReport` for the caller's session-start
  logging.

CLI wiring is one call from each daemon's ``_main_async`` between
storage open + engine first tick. See ``cli/live`` and
``cli/shadow``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger("wobblebot.services.reconciler")


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
            from ``open`` to ``canceled``.
        storage_persistence_failures: How many transitions failed at
            the storage layer (logged + counted; reconciliation
            continues so one bad row doesn't block boot).
        orphan_count: Number of exchange-only orders detected.
        orphan_summaries: One short string per orphan for the
            caller's session-start summary log line.
    """

    storage_canceled_count: int = 0
    storage_persistence_failures: int = 0
    orphan_count: int = 0
    orphan_summaries: tuple[str, ...] = ()


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


def _summarize_orphan(order: Order) -> str:
    """One short string per orphan for the session-start summary."""
    return (
        f"{order.symbol.base}/{order.symbol.quote} {order.side.value} "
        f"@ ${order.price.amount} (exchange_id={order.exchange_id or '?'})"
    )


async def apply_reconciliation(
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

    # Persist storage-only transitions.
    canceled_count = 0
    failures = 0
    now = Timestamp(dt=datetime.now(UTC))
    for stale in plan.storage_only:
        try:
            await storage.save_order(
                stale.model_copy(update={"status": "canceled", "updated_at": now})
            )
            canceled_count += 1
            _LOGGER.info(
                "reconciler: storage-only order marked canceled",
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
                "reconciler: failed to persist canceled transition; row stays open",
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
            "reconciler: exchange-only orphan order detected; engine will NOT adopt",
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
    )


__all__ = (
    "ReconciliationPlan",
    "ReconciliationReport",
    "apply_reconciliation",
    "reconcile_open_orders",
)
