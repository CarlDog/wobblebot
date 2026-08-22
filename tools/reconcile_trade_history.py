"""One-shot read-only reconciliation: Kraken's own trade/ledger history
vs. what ``live.db``'s ``trades`` table actually recorded.

Diagnostic for the 2026-08-22 replay-vs-balance drift investigation
(SOL confirmed real via a locked=0 balance snapshot — see
docs/planning/roadmap.md). Never places or cancels an order; uses the
READER key (``KRAKEN_READER_API_KEY`` / ``KRAKEN_READER_API_SECRET``)
by default since only read scopes are needed.

For each requested symbol:

1. Pulls Kraken's own ``TradesHistory`` (via the same adapter method
   the engine uses, so pagination is identical) and diffs it against
   ``live.db``'s ``trades`` table by trade id. Any Kraken trade absent
   locally is the direct cause of a replay/balance mismatch.
2. Pulls Kraken's ``Ledgers`` endpoint for the asset, paginated the
   same way ``TradesHistory`` is. ``TradesHistory`` only reports
   matched trades; ``Ledgers`` also reports non-trade balance moves
   (deposits, withdrawals, staking/earn transfers, dust conversions,
   adjustments) that a pure trade diff can't see. Response shape
   (``result.ledger`` dict keyed by ledger id, ``result.count`` for
   pagination) verified against a live response 2026-08-22 — Kraken's
   docs weren't consulted for this, per api-integration.md's
   "verify field names against a live response" rule; SOL returned 36
   entries, 11 of them ``type=staking``.
3. Prints a quantity reconciliation: Kraken-reported net trade
   quantity vs. local replayed quantity vs. local ``trades`` row
   count, so the size and direction of any gap is immediately visible.

Every private call is paced (``_CALL_DELAY_SECONDS`` between pages and
between symbols) — an earlier unpaced run against 5 symbols tripped
Kraken's private-API rate limiter after 2 symbols (``EAPI:Rate limit
exceeded``), silently covering only 40% of the requested symbols. A
per-symbol failure is reported explicitly, not swallowed into a
falsely-complete "done" — see the printed count vs. requested count.

Usage::

    python tools/reconcile_trade_history.py --symbols SOL,XRP
    python tools/reconcile_trade_history.py --symbols SOL --db data/wobblebot-live.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.cost_basis import replay_average_cost
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import OrderSide, Symbol
from wobblebot.ports.exceptions import StorageError, WobbleBotPortError
from wobblebot.services.trade_reconciliation import reconcile_symbol_trades

_LEDGER_MAX_PAGES = 20  # mirrors kraken_exchange._TRADES_HISTORY_MAX_PAGES

# Kraken's private-API call counter is shared across every endpoint on
# the account; this script and get_trade_history's own pagination both
# draw from it. An earlier unpaced run against 5 symbols (each up to 2
# top-level calls here, plus get_trade_history's internal pagination)
# tripped "EAPI:Rate limit exceeded" after 2 symbols. This delay is
# between this script's own top-level calls only -- it can't pace
# get_trade_history's internal page loop, which is production code.
_CALL_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    kraken_trade_count: int
    local_trade_count: int
    missing_locally: list[Trade]
    kraken_net_qty: Decimal
    local_replayed_qty: Decimal
    ledger_entries: list[dict[str, object]]


async def _fetch_ledger_entries(adapter: KrakenAdapter, asset: str) -> list[dict[str, object]]:
    """Raw ``/0/private/Ledgers`` pull for one asset, paginated like TradesHistory.

    Not exposed on ``ExchangePort`` (wobblebot has no Ledgers caller
    anywhere) — calls the adapter's private helper directly since this
    is a diagnostic script, not production code.
    """
    entries: list[dict[str, object]] = []
    offset = 0
    total_count: int | None = None
    for page in range(_LEDGER_MAX_PAGES):
        if page > 0:
            await asyncio.sleep(_CALL_DELAY_SECONDS)
        result = await adapter._private_post(  # pylint: disable=protected-access
            "/0/private/Ledgers", {"asset": asset, "ofs": offset}
        )
        ledger_map = result.get("ledger", {})
        if not isinstance(ledger_map, dict) or not ledger_map:
            break
        for ledger_id, entry in ledger_map.items():
            entries.append({"ledger_id": ledger_id, **entry})
        offset += len(ledger_map)
        if total_count is None:
            raw_count = result.get("count")
            if isinstance(raw_count, (int, float)):
                total_count = int(raw_count)
        if total_count is not None and offset >= total_count:
            break
    entries.sort(key=lambda e: float(str(e.get("time", 0))), reverse=True)
    return entries


async def _reconcile_symbol(
    adapter: KrakenAdapter, storage: SQLiteStorageAdapter, base: str
) -> SymbolReport:
    symbol = Symbol(base=base, quote="USD")
    # Shared with cli/maintenance's scheduled reconcile task
    # (services/trade_reconciliation.py) -- one implementation of the
    # actual diff, so the two can never drift apart.
    result = await reconcile_symbol_trades(adapter, storage, symbol)

    kraken_net_qty = sum(
        (
            t.amount.value if t.side is OrderSide.BUY else -t.amount.value
            for t in result.kraken_trades
        ),
        Decimal("0"),
    )
    local_basis = replay_average_cost(list(result.local_trades))

    await asyncio.sleep(_CALL_DELAY_SECONDS)
    ledger_entries = await _fetch_ledger_entries(adapter, base)

    return SymbolReport(
        symbol=str(symbol),
        kraken_trade_count=result.kraken_trade_count,
        local_trade_count=result.local_trade_count,
        missing_locally=list(result.missing_locally),
        kraken_net_qty=kraken_net_qty,
        local_replayed_qty=local_basis.quantity,
        ledger_entries=ledger_entries,
    )


def _print_report(report: SymbolReport) -> None:
    print(f"\n=== {report.symbol} ===")
    print(f"Kraken TradesHistory: {report.kraken_trade_count} trades")
    print(f"local live.db trades: {report.local_trade_count} trades")
    print(f"Kraken net qty (buys - sells, all reported trades): {report.kraken_net_qty}")
    print(
        f"local replayed qty (domain.cost_basis.replay_average_cost): "
        f"{report.local_replayed_qty}"
    )

    if report.missing_locally:
        print(f"\n!!! {len(report.missing_locally)} Kraken trade(s) MISSING from live.db:")
        for t in sorted(report.missing_locally, key=lambda t: t.executed_at.dt):
            print(
                f"  id={t.id} order_id={t.order_id} side={t.side.value} "
                f"amount={t.amount.value} price={t.price.amount} "
                f"cost={t.cost} fee={t.fee} executed_at={t.executed_at.dt.isoformat()}"
            )
    else:
        print("\nNo Kraken trades missing from live.db — trade history matches exactly.")

    non_trade = [e for e in report.ledger_entries if e.get("type") != "trade"]
    print(f"\nLedgers: {len(report.ledger_entries)} total entries, {len(non_trade)} non-trade")
    for e in non_trade[:20]:
        print(
            f"  ledger_id={e.get('ledger_id')} type={e.get('type')} "
            f"subtype={e.get('subtype')} amount={e.get('amount')} "
            f"balance={e.get('balance')} time={e.get('time')}"
        )
    if len(non_trade) > 20:
        print(f"  ... and {len(non_trade) - 20} more (see the JSON dump)")


async def _run(symbols: list[str], db_path: Path, out_path: Path) -> int:
    try:
        config = KrakenConfig.from_env(
            key_var="KRAKEN_READER_API_KEY",
            secret_var="KRAKEN_READER_API_SECRET",
        )
    except ValueError as exc:
        print(f"error: missing reader credentials: {exc}")
        return 2

    adapter = KrakenAdapter(config=config)
    # read_only=True -- this script only ever reads live.db; it must
    # never open a write-capable connection against a file cli/live
    # writes fills/orders to concurrently (2026-08-22 review fix).
    # mode=ro also means a missing/typo'd path fails loudly here rather
    # than silently creating an empty DB that would report every
    # Kraken trade as "missing".
    storage = SQLiteStorageAdapter(str(db_path), read_only=True)
    reports: list[SymbolReport] = []
    failed: list[str] = []
    try:
        try:
            await storage.connect()
        except StorageError as exc:
            print(f"error: could not open {db_path} read-only: {exc}")
            return 2
        for i, base in enumerate(symbols):
            if i > 0:
                await asyncio.sleep(_CALL_DELAY_SECONDS)
            try:
                report = await _reconcile_symbol(adapter, storage, base)
            except WobbleBotPortError as exc:
                print(f"error reconciling {base}: {exc}")
                failed.append(base)
                continue
            reports.append(report)
            _print_report(report)
    finally:
        await storage.close()
        await adapter.aclose()

    print(f"\n=== SUMMARY: {len(reports)}/{len(symbols)} symbols reconciled ===")
    if failed:
        print(f"NOT reconciled (see errors above, re-run for these): {', '.join(failed)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "symbol": r.symbol,
                    "kraken_trade_count": r.kraken_trade_count,
                    "local_trade_count": r.local_trade_count,
                    "kraken_net_qty": str(r.kraken_net_qty),
                    "local_replayed_qty": str(r.local_replayed_qty),
                    "missing_locally": [
                        {
                            "id": t.id,
                            "order_id": t.order_id,
                            "side": t.side.value,
                            "amount": str(t.amount.value),
                            "price": str(t.price.amount),
                            "cost": str(t.cost),
                            "fee": str(t.fee),
                            "executed_at": t.executed_at.dt.isoformat(),
                        }
                        for t in r.missing_locally
                    ],
                    "ledger_entries": r.ledger_entries,
                }
                for r in reports
            ],
            f,
            indent=2,
            default=str,
        )
    print(f"\nFull report written to {out_path}")
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", required=True, help="Comma-separated base assets, e.g. SOL,XRP"
    )
    parser.add_argument("--db", default="data/wobblebot-live.db", help="Path to live.db")
    parser.add_argument(
        "--out",
        default=None,
        help="Where to write the full JSON report (default: data/reconcile_<timestamp>.json)",
    )
    args = parser.parse_args()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out) if args.out else Path("data") / f"reconcile_{timestamp}.json"

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(_run(symbols, Path(args.db), out_path))


if __name__ == "__main__":
    raise SystemExit(main())
