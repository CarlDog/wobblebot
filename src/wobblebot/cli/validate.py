"""Stage 2.3 diagnostic CLI — validate the grid against live Kraken without moving money.

Run as a module::

    python -m wobblebot.cli.validate
    python -m wobblebot.cli.validate --symbol ETH/USD
    python -m wobblebot.cli.validate --order-size 5 --spacing 0.5

Builds a ``GridEngine`` wired to ``KrakenAdapter(dry_run=True)`` plus
an in-memory SQLite, then runs **one** ``step(symbol)`` against live
Kraken. Every order the engine wants to place goes through Kraken's
``validate=true`` flag — the request is signed, sent, and validated
end-to-end (auth / pair / precision / balance / ordermin / costmin)
**without placing the order**.

Exits 0 if every order validated, non-zero on any failure. Operator
runs this before flipping to ``cli/grid`` (which actually trades).

Loads credentials from ``KRAKEN_TRADE_API_KEY`` /
``KRAKEN_TRADE_API_SECRET`` (separate from the read-only key per
ADR-003-style separation; loaded session-wide via ``python-dotenv`` so
the project's ``.env`` file works without manual sourcing).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import LogFormat, configure_logging
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.grid_engine import GridEngine


def _parse_symbol(raw: str) -> Symbol:
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"--symbol must be BASE/QUOTE (e.g. BTC/USD); got {raw!r}")
    return Symbol(base=parts[0], quote=parts[1])


def _build_grid_config(
    spacing_pct: Decimal,
    above: int,
    below: int,
    order_size_usd: Decimal,
) -> GridConfig:
    return GridConfig(
        default=GridLevels(
            spacing_percentage=spacing_pct,
            levels_above=above,
            levels_below=below,
            order_size_usd=order_size_usd,
        ),
    )


def _build_safety_config(max_total_usd: Decimal, max_orders: int) -> SafetyConfig:
    """Caps sized to comfortably permit the validate run.

    The diagnostic CLI is single-shot and exists to verify Kraken
    validation passes; tight caps would mask real validation failures
    behind safety refusals."""
    return SafetyConfig(
        max_total_exposure_usd=max_total_usd,
        max_daily_spend_usd=max_total_usd,
        max_per_coin_exposure_usd=max_total_usd,
        max_orders_per_coin=max_orders,
        emergency_stop=EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("20"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


async def _run(
    symbol: Symbol,
    spacing_pct: Decimal,
    above: int,
    below: int,
    order_size_usd: Decimal,
    log_format: LogFormat,
) -> int:
    configure_logging(log_format=log_format)
    logger = logging.getLogger("wobblebot.cli.validate")

    try:
        config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADE_API_KEY",
            secret_var="KRAKEN_TRADE_API_SECRET",
        )
    except ValueError as exc:
        logger.error(
            "missing trade credentials",
            extra={"error": str(exc), "expected": "KRAKEN_TRADE_API_KEY/KRAKEN_TRADE_API_SECRET"},
        )
        return 2

    grid_config = _build_grid_config(spacing_pct, above, below, order_size_usd)
    layout_count = above + below
    max_total = order_size_usd * layout_count
    safety_config = _build_safety_config(max_total_usd=max_total, max_orders=layout_count + 5)

    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    adapter = KrakenAdapter(config=config, dry_run=True)
    engine = GridEngine(adapter, storage, grid_config, safety_config)

    try:
        ref_price = (await adapter.get_current_price(symbol)).amount
        logger.info(
            "validate run starting",
            extra={
                "symbol": str(symbol),
                "reference_price_live": str(ref_price),
                "spacing_percentage": str(spacing_pct),
                "levels_above": above,
                "levels_below": below,
                "order_size_usd": str(order_size_usd),
                "expected_layout_orders": layout_count,
                "max_total_exposure_usd": str(max_total),
            },
        )

        result = await engine.step(symbol)

        if result.action != "initialized":
            logger.error(
                "expected first-tick initialization; got something else",
                extra={"action": result.action},
            )
            return 1

        if result.refusals:
            logger.error(
                "engine refused some placements via the safety cap layer",
                extra={
                    "placed": result.placed,
                    "refusals": result.refusals,
                    "expected": layout_count,
                },
            )
            return 1

        if result.placed != layout_count:
            logger.error(
                "placed count does not match expected layout",
                extra={"placed": result.placed, "expected": layout_count},
            )
            return 1

        opens = await storage.get_open_orders(symbol=symbol)
        non_dryrun = [
            o.exchange_id
            for o in opens
            if o.exchange_id and not o.exchange_id.startswith("DRYRUN-")
        ]
        if non_dryrun:
            logger.error(
                "dry-run produced non-DRYRUN exchange_ids — adapter not in dry_run mode?",
                extra={"non_dryrun_ids": non_dryrun},
            )
            return 1

        logger.info(
            "validate run completed successfully",
            extra={
                "symbol": str(symbol),
                "validated": result.placed,
                "expected": layout_count,
                "all_dry_run": True,
            },
        )
        return 0

    except WobbleBotPortError as exc:
        logger.error(
            "validate run failed against live Kraken",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    finally:
        await adapter.aclose()
        await storage.close()


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        default="BTC/USD",
        help="Trading pair in BASE/QUOTE form (default: BTC/USD).",
    )
    parser.add_argument(
        "--spacing",
        type=Decimal,
        default=Decimal("1.0"),
        help="Grid spacing as percentage of reference price (default: 1.0).",
    )
    parser.add_argument(
        "--above",
        type=int,
        default=3,
        help="Number of grid levels above the reference (default: 3).",
    )
    parser.add_argument(
        "--below",
        type=int,
        default=3,
        help="Number of grid levels below the reference (default: 3).",
    )
    parser.add_argument(
        "--order-size",
        type=Decimal,
        default=Decimal("10"),
        help="USD per order (default: 10.0).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Log output format (default: plain).",
    )
    args = parser.parse_args()

    try:
        symbol = _parse_symbol(args.symbol)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    return asyncio.run(
        _run(
            symbol=symbol,
            spacing_pct=args.spacing,
            above=args.above,
            below=args.below,
            order_size_usd=args.order_size,
            log_format=args.log_format,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
