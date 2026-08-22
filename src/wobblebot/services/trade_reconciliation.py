"""Kraken-vs-local trade history reconciliation (2026-08-22, post-incident).

Diffs Kraken's own ``TradesHistory`` against a symbol's locally
recorded trades, by trade id. Detection only — this module never
writes.

Born from two confirmed 2026-08-22 incidents where a real Kraken fill
never made it into ``live.db``: an SOL trade orphaned by a
non-atomic order-then-trade write (fixed separately by
``StoragePort.save_fill``), and 18 BTC trades from
``tools/first_real_trade.py`` runs (that script writes only to its
own JSONL log, never to storage, by design). Neither was detectable
from a balance comparison alone — SOL's fill fully closed and vanished
from Kraken's ``OpenOrders`` before any reconciliation pass ever
looked for it. A direct trade-history diff is the only signal that
catches both classes, and it is used here specifically because it is
a binary, dust-tolerance-free check: a Kraken trade either has a
matching local row or it doesn't, with no ambiguous middle ground the
way a quantity/balance comparison has (see PR #102's follow-up
comment on the ``total`` vs. ``available`` balance-field confusion
that an earlier, cruder comparison produced).

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

# Kraken's TradesHistory pagination tops out here (mirrors
# kraken_exchange._TRADES_HISTORY_MAX_PAGES x 50/page); a single
# symbol's history should never realistically approach this.
_TRADE_FETCH_LIMIT = 1000


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
    """

    symbol: Symbol
    kraken_trades: tuple[Trade, ...]
    local_trades: tuple[Trade, ...]
    missing_locally: tuple[Trade, ...]

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
    exchange: ExchangePort, storage: StoragePort, symbol: Symbol
) -> SymbolReconciliation:
    """Diff one symbol's Kraken trade history against local storage.

    Every Kraken-reported trade not present locally (matched by trade
    id) lands in ``missing_locally`` — the direct, unambiguous
    evidence of a silent persistence gap.

    Raises:
        WobbleBotPortError: If the Kraken or storage call fails.
            Callers decide how to handle a failed reconciliation pass
            (this function never swallows errors — a caller silently
            treating a failed check as "clean" would be worse than
            the gap it's meant to catch).
    """
    kraken_trades = await exchange.get_trade_history(symbol=symbol, limit=_TRADE_FETCH_LIMIT)
    local_trades = await storage.get_trades(symbol=symbol, limit=_TRADE_FETCH_LIMIT)
    local_ids = {t.id for t in local_trades}
    missing = tuple(t for t in kraken_trades if t.id not in local_ids)
    return SymbolReconciliation(
        symbol=symbol,
        kraken_trades=tuple(kraken_trades),
        local_trades=tuple(local_trades),
        missing_locally=missing,
    )


__all__ = ["SymbolReconciliation", "reconcile_symbol_trades"]
