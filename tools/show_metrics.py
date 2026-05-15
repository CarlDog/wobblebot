"""Read-only inspection of Stage 3.1 metrics against any wobblebot DB.

Run against the observe DB to see price-only metrics; against the
live or shadow DB to see cycle stats as well. The script
auto-detects which symbols are present and which data types are
available; it doesn't require a specific schema layout beyond the
``price_snapshots`` and ``trades`` tables that the project already
creates on connect.

Usage::

    python tools/show_metrics.py
    python tools/show_metrics.py --db-path data/wobblebot-observe.db
    python tools/show_metrics.py --lookback-hours 6 --symbol BTC/USD
    python tools/show_metrics.py --log-format json

**Safe to run against the live observe DB while ``cli/observe`` is
polling.** SQLite handles concurrent readers; no write surface is
exercised. This is the Stage 3.1 verification surface for
``services.metrics`` and the ``DataCollector v2`` read path.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.metrics import (
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)

_LOGGER = logging.getLogger("wobblebot.tools.show_metrics")
_DEFAULT_DB = Path("data") / "wobblebot-observe.db"


def _parse_symbol(value: str) -> Symbol:
    if "/" not in value:
        raise argparse.ArgumentTypeError(f"--symbol must look like BASE/QUOTE; got {value!r}")
    base, quote = value.split("/", 1)
    return Symbol(base=base.strip(), quote=quote.strip())


async def _discover_symbols(storage: SQLiteStorageAdapter) -> list[Symbol]:
    """List every symbol with at least one price snapshot in the DB."""
    conn = storage._require_conn()  # noqa: SLF001  pylint: disable=protected-access
    async with conn.execute(
        "SELECT DISTINCT symbol_base, symbol_quote "
        "FROM price_snapshots ORDER BY symbol_base, symbol_quote"
    ) as cursor:
        rows = await cursor.fetchall()
    return [Symbol(base=row["symbol_base"], quote=row["symbol_quote"]) for row in rows]


async def _compute_symbol_metrics(
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    lookback: timedelta,
) -> dict[str, Any]:
    """Compute every Stage 3.1 metric available for one symbol."""
    start_time = datetime.now(UTC) - lookback
    snapshots = await storage.get_price_snapshots(symbol=symbol, start_time=start_time)
    trades_desc = await storage.get_trades(symbol=symbol, start_time=start_time, limit=10000)
    trades_asc = list(reversed(trades_desc))
    prices = [s.price.amount for s in snapshots]
    cycle_stats = compute_cycle_stats(trades_asc)
    return {
        "symbol": str(symbol),
        "lookback_hours": lookback.total_seconds() / 3600,
        "snapshot_count": len(snapshots),
        "trade_count": len(trades_asc),
        "latest_price": str(snapshots[-1].price.amount) if snapshots else None,
        "latest_observed_at": (snapshots[-1].observed_at.dt.isoformat() if snapshots else None),
        "volatility": str(compute_volatility(prices)) if prices else "0",
        "max_drawdown": str(compute_max_drawdown(prices)) if prices else "0",
        "flatness": str(compute_flatness(prices)) if prices else "1",
        "cycle_count": cycle_stats.cycle_count,
        "win_count": cycle_stats.win_count,
        "win_rate": str(cycle_stats.win_rate),
        "total_pnl": str(cycle_stats.total_pnl),
        "avg_profit_per_cycle": str(cycle_stats.avg_profit_per_cycle),
    }


def _format_line(metrics: dict[str, Any]) -> str:
    """One-line human summary for stdout logging."""
    parts = [
        f"{metrics['symbol']}",
        f"snapshots={metrics['snapshot_count']}",
    ]
    if metrics["latest_price"] is not None:
        parts.append(f"latest={metrics['latest_price']}")
        parts.append(f"vol={Decimal(metrics['volatility']):.6f}")
        parts.append(f"dd={Decimal(metrics['max_drawdown']):.6f}")
        parts.append(f"flat={Decimal(metrics['flatness']):.4f}")
    if metrics["trade_count"] > 0:
        parts.append(f"cycles={metrics['cycle_count']}")
        parts.append(f"wins={metrics['win_count']}")
        parts.append(f"pnl={metrics['total_pnl']}")
    return " | ".join(parts)


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        _LOGGER.error("db not found", extra={"db_path": str(db_path)})
        return 2

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    try:
        if args.symbol is not None:
            symbols = [args.symbol]
        else:
            symbols = await _discover_symbols(storage)
            if not symbols:
                _LOGGER.error(
                    "no price snapshots found; specify --symbol to compute trade-only stats",
                    extra={"db_path": str(db_path)},
                )
                return 1

        lookback = timedelta(hours=args.lookback_hours)
        for symbol in symbols:
            metrics = await _compute_symbol_metrics(storage, symbol, lookback)
            _LOGGER.info(_format_line(metrics), extra=metrics)
    finally:
        await storage.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"SQLite DB to read (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--symbol",
        type=_parse_symbol,
        default=None,
        help="Restrict to one symbol (BASE/QUOTE). Default: every symbol "
        "with snapshots in the DB.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=24.0,
        help="Window for derived metrics, in hours (default: 24).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Output format. json emits one JSON record per symbol with "
        "all fields machine-readable.",
    )
    args = parser.parse_args()

    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
