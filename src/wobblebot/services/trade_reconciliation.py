"""Kraken-vs-local trade history reconciliation (2026-08-22, post-incident).

Diffs Kraken's own ``TradesHistory`` against a symbol's locally
recorded trades, by trade id. Detection only — this module never
writes (callers open storage read-only; see
``SQLiteStorageAdapter(read_only=True)``).

Born from two confirmed 2026-08-22 incidents where a real Kraken fill
never made it into ``live.db``: an SOL trade orphaned by a
non-atomic order-then-trade write (fixed separately by
``StoragePort.save_fill``), and 18 BTC trades from
``tools/first_real_trade.py`` (that script writes only to its own
JSONL log, never to storage, by design). Neither was detectable
from a balance comparison alone — SOL's fill fully closed and vanished
from Kraken's ``OpenOrders`` before any reconciliation pass ever
looked for it. A direct trade-history diff is the only signal that
catches both classes, and it is used here specifically because it is
a binary, dust-tolerance-free check: a Kraken trade either has a
matching local row or it doesn't, with no ambiguous middle ground the
way a quantity/balance comparison has (see PR #102's follow-up
comment on the ``total`` vs. ``available`` balance-field confusion
that an earlier, cruder comparison produced).

**Open-order deferral (2026-08-22 review).** A Kraken trade whose
``order_id`` matches a locally-OPEN order is *deferred*, not missing:
the engine persists trades only when the order reaches terminal
status (ADR-023's resolution flow), so a partial fill on an order
still resting on the book legitimately sits in Kraken's history for
hours or days before the local row appears. The same exclusion also
covers the ~one-tick persist lag after a full fill and fills that
land while the daemon is down (the startup reconciler resolves those
from the still-open row). Without it, the daily check would page
CRITICAL for correct engine behavior. Deferred trades are reported
separately so a caller can log them without alerting.

Kraken's ``Ledgers`` endpoint (non-trade balance moves — staking,
dust conversions, transfers) is deliberately NOT part of this check:
those entries are expected noise (e.g. routine staking accrual), not
a correctness signal, and folding them in would make the automated
check alert-happy. ``tools/reconcile_trade_history.py`` still pulls
Ledgers for the deeper one-off manual diagnostic; this module is the
narrower, always-on subset wired into ``cli/maintenance``.
"""

from __future__ import annotations

from dataclasses import dataclass

from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.storage import StoragePort

# Upper bound on trades fetched per side of the diff. NB (2026-08-22
# review): Kraken's TradesHistory is ACCOUNT-WIDE with no pair filter —
# the adapter walks up to 20 pages (~1000 raw trades) and filters
# client-side — so on a busy account this window is the newest ~1000
# trades ACROSS ALL SYMBOLS, not 1000 per symbol. A standing gap older
# than that window ages out of this check's sight; the manual tool's
# JSON dump is the archaeology path for anything that old.
TRADE_FETCH_LIMIT = 1000


@dataclass(frozen=True)
class SymbolReconciliation:
    """One symbol's Kraken-vs-local trade diff.

    Carries the full ``kraken_trades``/``local_trades`` lists (not
    just counts) so a richer caller — e.g.
    ``tools/reconcile_trade_history.py``'s quantity/replayed-cost
    reporting — can derive additional stats from the SAME fetch
    rather than re-querying Kraken a second time. Callers that only
    care about the pass/fail signal (the scheduled maintenance check)
    can ignore these and use ``is_clean``/``missing_locally``.

    ``deferred_open`` holds Kraken trades absent locally whose order
    is still locally OPEN — legitimate persist-later cases (see module
    docstring), excluded from ``missing_locally`` and from
    ``is_clean``'s judgment.
    """

    symbol: Symbol
    kraken_trades: tuple[Trade, ...]
    local_trades: tuple[Trade, ...]
    missing_locally: tuple[Trade, ...]
    deferred_open: tuple[Trade, ...] = ()

    @property
    def kraken_trade_count(self) -> int:
        return len(self.kraken_trades)

    @property
    def local_trade_count(self) -> int:
        return len(self.local_trades)

    @property
    def is_clean(self) -> bool:
        return not self.missing_locally


async def reconcile_symbol_trades(
    exchange: ExchangePort,
    storage: StoragePort,
    symbol: Symbol,
    *,
    account_trades: list[Trade] | None = None,
) -> SymbolReconciliation:
    """Diff one symbol's Kraken trade history against local storage.

    Every Kraken-reported trade not present locally (matched by trade
    id) lands in ``missing_locally`` — the direct, unambiguous
    evidence of a silent persistence gap — unless its order is still
    locally open, in which case it lands in ``deferred_open`` instead
    (persistence is legitimately pending; see module docstring).

    ``account_trades``: an account-wide ``get_trade_history`` snapshot
    the caller fetched once, filtered to ``symbol`` client-side —
    mirroring ``GridEngine``'s shared ``exchange_trades`` snapshot
    pattern. Kraken's endpoint is account-wide anyway, so per-symbol
    fetches would re-walk the identical pages once per symbol (up to
    20 private calls each; the 2026-08-15→17 incident showed Kraken's
    limiter has account-wide blast radius). ``None`` falls back to a
    per-symbol fetch (the one-symbol manual tool path / tests).

    Raises:
        WobbleBotPortError: If the Kraken or storage call fails.
            Callers decide how to handle a failed reconciliation pass
            (this function never swallows errors — a caller silently
            treating a failed check as "clean" would be worse than
            the gap it's meant to catch).
    """
    if account_trades is None:
        kraken_trades = [
            t
            for t in await exchange.get_trade_history(symbol=symbol, limit=TRADE_FETCH_LIMIT)
            if t.symbol == symbol
        ]
    else:
        kraken_trades = [t for t in account_trades if t.symbol == symbol]
    local_trades = await storage.get_trades(symbol=symbol, limit=TRADE_FETCH_LIMIT)
    local_ids = {t.id for t in local_trades}
    absent = [t for t in kraken_trades if t.id not in local_ids]

    open_exchange_ids = {
        o.exchange_id for o in await storage.get_open_orders(symbol=symbol) if o.exchange_id
    }
    missing = tuple(t for t in absent if t.order_id not in open_exchange_ids)
    deferred = tuple(t for t in absent if t.order_id in open_exchange_ids)

    return SymbolReconciliation(
        symbol=symbol,
        kraken_trades=tuple(kraken_trades),
        local_trades=tuple(local_trades),
        missing_locally=missing,
        deferred_open=deferred,
    )


__all__ = ["SymbolReconciliation", "TRADE_FETCH_LIMIT", "reconcile_symbol_trades"]
