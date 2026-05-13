"""Phase 1.5 integration check — wire the layers together end-to-end.

Run as a module::

    python -m wobblebot.cli.simulate

Composes the MockExchangeAdapter + SQLiteStorageAdapter +
configure_logging stack and walks through one hard-coded buy-low /
sell-high cycle. Writes the DB to ``wobblebot-sim.db`` in the
working directory so the operator can inspect the persisted state
afterwards (``sqlite3 wobblebot-sim.db .schema`` then
``SELECT * FROM trades;``).

This is the Phase 1 integration check; not a paper-trading bot.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.simulator import run_buy_dip_sell_rebound_cycle


async def _run(db_path: str, log_format: str) -> int:
    configure_logging(format=log_format)  # type: ignore[arg-type]
    logger = logging.getLogger("wobblebot.cli.simulate")

    symbol = Symbol(base="BTC", quote="USD")
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("10000")},
        starting_prices={symbol: Decimal("50000")},
    )
    storage = SQLiteStorageAdapter(db_path)
    await storage.connect()
    try:
        result = await run_buy_dip_sell_rebound_cycle(
            exchange,
            storage,
            symbol=symbol,
            buy_price=Decimal("48000"),
            sell_price=Decimal("52000"),
            amount=Decimal("0.05"),
            price_walk=[
                Decimal("49000"),  # no fill, still above buy limit
                Decimal("47500"),  # buy fills; sell is placed at 52000
                Decimal("51000"),  # no fill, still below sell limit
                Decimal("52500"),  # sell fills
            ],
        )
    finally:
        await storage.close()

    logger.info(
        "simulation summary",
        extra={
            "db_path": db_path,
            "orders_placed": result.orders_placed,
            "trades_executed": result.trades_executed,
            "final_balances": {b.asset: str(b.total) for b in result.final_balances},
        },
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="wobblebot-sim.db",
        help="SQLite file to persist orders/trades into (default: wobblebot-sim.db).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Log output format (default: plain).",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.db, args.log_format))


if __name__ == "__main__":
    raise SystemExit(main())
