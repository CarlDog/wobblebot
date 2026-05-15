"""Sandbox CLI — Phase 1 mock-only paper cycle, no Kraken contact.

Run as a module::

    python -m wobblebot.cli.sandbox
    python -m wobblebot.cli.sandbox --db /tmp/wobblebot-sim.db

Composes the MockExchangeAdapter + SQLiteStorageAdapter +
configure_logging stack and walks through one hard-coded buy-low /
sell-high cycle. Writes the DB to ``data/wobblebot-sim.db`` (or
``--db path``) so the operator can inspect persisted state afterwards
(``sqlite3 wobblebot-sim.db .schema`` then ``SELECT * FROM trades;``).

This is the Phase 1 integration check. No Kraken credentials needed;
no network contact. Useful for verifying the hex layers wire up
after a fresh checkout.

Configuration layering (per ADR-009):
1. Base config — ``config/settings.yml`` (or fallback to example).
2. Profile overrides — ``--profile name`` (no-op for sandbox; profiles
   typically don't touch sandbox).
3. CLI flag overrides — ``--db``, ``--log-format``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from typing import Any

from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.simulator import run_buy_dip_sell_rebound_cycle

_LOGGER = logging.getLogger("wobblebot.cli.sandbox")


async def _run(config: WobbleBotConfig) -> int:
    if config.sandbox is None:
        _LOGGER.error("settings.yml is missing the `sandbox:` section")
        return 2

    symbol = Symbol(base="BTC", quote="USD")
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("10000")},
        starting_prices={symbol: Decimal("50000")},
    )
    storage = SQLiteStorageAdapter(config.sandbox.db)
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
                Decimal("47500"),  # buy fills; sell placed at 52000
                Decimal("51000"),  # no fill, still below sell limit
                Decimal("52500"),  # sell fills
            ],
        )
    finally:
        await storage.close()

    _LOGGER.info(
        "simulation summary",
        extra={
            "db_path": config.sandbox.db,
            "orders_placed": result.orders_placed,
            "trades_executed": result.trades_executed,
            "final_balances": {b.asset: str(b.total) for b in result.final_balances},
        },
    )
    return 0


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "sandbox",
        {
            "db": ("db", identity),
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument("--db", default=None, help="SQLite file to persist orders/trades into.")
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = config.sandbox.log_format if config.sandbox else "plain"
    configure_logging(log_format=log_format)

    return asyncio.run(_run(config))


if __name__ == "__main__":
    raise SystemExit(main())
