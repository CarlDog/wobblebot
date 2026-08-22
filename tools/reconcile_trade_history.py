"""One-shot read-only reconciliation: Kraken's own trade/ledger history
vs. what ``live.db``'s ``trades`` table actually recorded.

Diagnostic for the 2026-08-22 silent-fill-loss incident (receipt in
``docs/planning/roadmap.md``'s v1.1 digest). Never places or cancels an
order; uses the READER key (``KRAKEN_READER_API_KEY`` /
``KRAKEN_READER_API_SECRET``) since only read scopes are needed, and
opens ``live.db`` strictly read-only (``mode=ro``).

What one run does:

1. Fetches Kraken's account-wide ``TradesHistory`` ONCE (the endpoint
   has no pair filter; per-symbol fetches would re-walk identical
   pages) and diffs it per requested symbol against ``live.db``'s
   ``trades`` table by trade id, via the same
   ``services/trade_reconciliation.reconcile_symbol_trades`` the
   scheduled ``cli/maintenance`` reconcile task uses — one
   implementation, so the manual and automated checks cannot drift.
   A Kraken trade on a locally-OPEN order is reported as *deferred*
   (persistence legitimately pending), not missing.
2. Pulls Kraken's ``Ledgers`` endpoint per asset — non-trade balance
   moves (deposits, withdrawals, staking accrual, dust conversions)
   that a trade diff can't see. Response shape verified against a
   live response 2026-08-22 per api-integration.md's rule; SOL
   returned 36 entries, 11 of them ``type=staking``.
3. Reports a quantity reconciliation (Kraken net qty vs. local
   replayed qty) so a gap's size and direction is immediately
   visible, and dumps the full machine-readable report to JSON.

Ledgers calls are paced (``_CALL_DELAY_SECONDS``) — an earlier unpaced
run tripped ``EAPI:Rate limit exceeded`` after 2 of 5 symbols. The
human report goes through the project logger (stderr, per the
stdout/stderr contract); the JSON file is the machine artifact.

**Backfill runbook** (what to do when this tool — or the daily
``cli/maintenance`` reconcile alert — reports a real gap):

1. Re-run this tool for the affected symbol and confirm the missing
   trades are NOT ``deferred`` (a deferred trade resolves itself when
   its order closes; back off and re-check instead).
2. Cross-check each missing trade id against the JSON dump's ledger
   entries — a non-trade balance move is not backfillable as a trade.
3. Write a one-off backfill script from the JSON dump's
   ``missing_locally`` records (they carry every ``trades``-table
   column). Follow the reviewed 2026-08-22 pattern: assert the exact
   expected pre-state (trade count + replayed qty), dry-run first,
   insert via ``StoragePort.save_trade`` (idempotent INSERT OR
   REPLACE), then assert the exact expected post-state. See PR #102's
   thread for the reviewed script this pattern comes from.
4. Restart ``wobblebot-live`` afterward: the SellGuard caches each
   symbol's replayed basis in memory and only invalidates on a NEW
   fill, so it keeps using the stale average cost until restarted.
5. Re-run this tool and confirm the symbol reports clean.

Usage::

    python tools/reconcile_trade_history.py --symbols SOL,XRP
    python tools/reconcile_trade_history.py --symbols SOL --db data/wobblebot-live.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import configure_logging
from wobblebot.domain.cost_basis import replay_average_cost
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import OrderSide, Symbol
from wobblebot.ports.exceptions import StorageError, WobbleBotPortError
from wobblebot.services.trade_reconciliation import TRADE_FETCH_LIMIT, reconcile_symbol_trades

_LOGGER = logging.getLogger("wobblebot.tools.reconcile_trade_history")

_LEDGER_MAX_PAGES = 20  # mirrors kraken_exchange._TRADES_HISTORY_MAX_PAGES

# Kraken's private-API call counter is shared across every endpoint on
# the account. Paces this script's own Ledgers calls (between pages and
# between symbols); the single TradesHistory fetch paginates inside the
# adapter, which is production code this script doesn't reach into.
_CALL_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    kraken_trade_count: int
    local_trade_count: int
    missing_locally: list[Trade]
    deferred_open: list[Trade]
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
            # isfinite: json.loads parses NaN/Infinity by default and
            # int() on either raises — a bogus count degrades to "no
            # count" (the page cap still bounds the walk).
            if isinstance(raw_count, (int, float)) and math.isfinite(raw_count):
                total_count = int(raw_count)
        if total_count is not None and offset >= total_count:
            break
    entries.sort(key=lambda e: float(str(e.get("time", 0))), reverse=True)
    return entries


async def _reconcile_symbol(
    adapter: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    base: str,
    account_trades: list[Trade],
) -> SymbolReport:
    symbol = Symbol(base=base, quote="USD")
    # Shared with cli/maintenance's scheduled reconcile task
    # (services/trade_reconciliation.py) -- one implementation of the
    # actual diff, so the two can never drift apart. The account-wide
    # snapshot was fetched once by the caller.
    result = await reconcile_symbol_trades(adapter, storage, symbol, account_trades=account_trades)

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
        deferred_open=list(result.deferred_open),
        kraken_net_qty=kraken_net_qty,
        local_replayed_qty=local_basis.quantity,
        ledger_entries=ledger_entries,
    )


def _report_symbol(report: SymbolReport) -> None:
    """Operator-facing report, message-first through the project logger
    (plain format renders message-only; the JSON dump carries the
    machine copy)."""
    _LOGGER.info("=== %s ===", report.symbol)
    _LOGGER.info("Kraken TradesHistory: %d trades", report.kraken_trade_count)
    _LOGGER.info("local live.db trades: %d trades", report.local_trade_count)
    _LOGGER.info("Kraken net qty (buys - sells, all reported trades): %s", report.kraken_net_qty)
    _LOGGER.info(
        "local replayed qty (domain.cost_basis.replay_average_cost): %s",
        report.local_replayed_qty,
    )

    if report.deferred_open:
        _LOGGER.info(
            "%d Kraken trade(s) on still-open local orders — persistence pending, not a gap",
            len(report.deferred_open),
        )
    if report.missing_locally:
        _LOGGER.warning("!!! %d Kraken trade(s) MISSING from live.db:", len(report.missing_locally))
        for t in sorted(report.missing_locally, key=lambda t: t.executed_at.dt):
            _LOGGER.warning(
                "  id=%s order_id=%s side=%s amount=%s price=%s cost=%s fee=%s executed_at=%s",
                t.id,
                t.order_id,
                t.side.value,
                t.amount.value,
                t.price.amount,
                t.cost,
                t.fee,
                t.executed_at.dt.isoformat(),
            )
    else:
        _LOGGER.info("No Kraken trades missing from live.db — trade history matches.")

    non_trade = [e for e in report.ledger_entries if e.get("type") != "trade"]
    _LOGGER.info(
        "Ledgers: %d total entries, %d non-trade", len(report.ledger_entries), len(non_trade)
    )
    for e in non_trade[:20]:
        _LOGGER.info(
            "  ledger_id=%s type=%s subtype=%s amount=%s balance=%s time=%s",
            e.get("ledger_id"),
            e.get("type"),
            e.get("subtype"),
            e.get("amount"),
            e.get("balance"),
            e.get("time"),
        )
    if len(non_trade) > 20:
        _LOGGER.info("  ... and %d more (see the JSON dump)", len(non_trade) - 20)


async def _run(symbols: list[str], db_path: Path, out_path: Path) -> int:
    try:
        config = KrakenConfig.from_env(
            key_var="KRAKEN_READER_API_KEY",
            secret_var="KRAKEN_READER_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error("missing reader credentials: %s", exc)
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
            _LOGGER.error("could not open %s read-only: %s", db_path, exc)
            return 2
        # One account-wide fetch covers every symbol (the endpoint has
        # no pair filter; per-symbol fetches would re-walk the same
        # pages once per symbol against Kraken's account-wide limiter).
        try:
            account_trades = await adapter.get_trade_history(symbol=None, limit=TRADE_FETCH_LIMIT)
        except WobbleBotPortError as exc:
            _LOGGER.error("account trade-history fetch failed: %s", exc)
            return 1
        for base in symbols:
            try:
                report = await _reconcile_symbol(adapter, storage, base, account_trades)
            except WobbleBotPortError as exc:
                _LOGGER.error("error reconciling %s: %s", base, exc)
                failed.append(base)
                continue
            reports.append(report)
            _report_symbol(report)
    finally:
        await storage.close()
        await adapter.aclose()

    _LOGGER.info("=== SUMMARY: %d/%d symbols reconciled ===", len(reports), len(symbols))
    if failed:
        _LOGGER.warning(
            "NOT reconciled (see errors above, re-run for these): %s", ", ".join(failed)
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _trade_record(t: Trade) -> dict[str, str]:
        return {
            "id": t.id,
            "order_id": t.order_id,
            "side": t.side.value,
            "amount": str(t.amount.value),
            "price": str(t.price.amount),
            "cost": str(t.cost),
            "fee": str(t.fee),
            "executed_at": t.executed_at.dt.isoformat(),
        }

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "symbol": r.symbol,
                    "kraken_trade_count": r.kraken_trade_count,
                    "local_trade_count": r.local_trade_count,
                    "kraken_net_qty": str(r.kraken_net_qty),
                    "local_replayed_qty": str(r.local_replayed_qty),
                    "missing_locally": [_trade_record(t) for t in r.missing_locally],
                    "deferred_open": [_trade_record(t) for t in r.deferred_open],
                    "ledger_entries": r.ledger_entries,
                }
                for r in reports
            ],
            f,
            indent=2,
            default=str,
        )
    _LOGGER.info("Full report written to %s", out_path)
    return 0


def main() -> int:
    load_dotenv()
    configure_logging(log_format="plain")
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
