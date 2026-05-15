"""Stage 2.1 integration check — wire KrakenAdapter + DataCollector end-to-end.

Run as a module::

    python -m wobblebot.cli.status
    python -m wobblebot.cli.status --symbol ETH/USD

Loads Kraken credentials from environment variables
(``KRAKEN_API_KEY`` / ``KRAKEN_API_SECRET``, picked up automatically
from a project-root ``.env`` file via ``python-dotenv``), builds a
``KrakenAdapter`` and wraps it in ``DataCollector``, then fetches:

- The current price for ``--symbol`` (default BTC/USD).
- All account balances.

Output goes through the project logger (``configure_logging``) — no
``print()`` calls. The ``--log-format json`` flag is useful for piping
into log aggregators in the Phase 2+ deployment.

This is the Stage 2.1 integration check: it proves the read-only path
through the whole hex stack works against real Kraken. It does not
place orders or move funds.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import LogFormat, configure_logging
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.data_collector import DataCollector


def _parse_symbol(raw: str) -> Symbol:
    """Parse ``BASE/QUOTE`` into a ``Symbol``. Raises ``ValueError`` on bad input."""
    parts = raw.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"--symbol must be in BASE/QUOTE form (e.g. BTC/USD); got {raw!r}")
    return Symbol(base=parts[0], quote=parts[1])


async def _run(symbol: Symbol, log_format: LogFormat) -> int:
    configure_logging(log_format=log_format)
    logger = logging.getLogger("wobblebot.cli.status")

    try:
        config = KrakenConfig.from_env()
    except ValueError as exc:
        logger.error("missing Kraken credentials", extra={"error": str(exc)})
        return 2

    adapter = KrakenAdapter(config=config)
    collector = DataCollector(exchange=adapter)
    try:
        snapshot = await collector.get_market_snapshot(symbol)
        balances = await collector.get_balances()
    except WobbleBotPortError as exc:
        logger.error(
            "stage 2.1 check failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    finally:
        await adapter.aclose()

    # The plain log format only renders the message string. We put the
    # operator-facing data inline (via lazy %s args so pylint W1203 is
    # happy); the ``extra`` dict still surfaces in the JSON format for
    # log aggregators.
    logger.info(
        "%s price: %s %s (fetched at %s)",
        snapshot.symbol,
        snapshot.price.amount,
        snapshot.price.currency,
        snapshot.timestamp.dt.isoformat(),
        extra={
            "symbol": str(snapshot.symbol),
            "price": str(snapshot.price.amount),
            "currency": snapshot.price.currency,
            "fetched_at": snapshot.timestamp.dt.isoformat(),
        },
    )
    if not balances:
        logger.info(
            "account balances: (empty)",
            extra={"balances": {}, "count": 0},
        )
    else:
        summary = ", ".join(
            f"{b.asset}={b.total} (avail {b.available} / locked {b.locked})" for b in balances
        )
        logger.info(
            "account balances (%d): %s",
            len(balances),
            summary,
            extra={
                "balances": {
                    b.asset: {
                        "total": str(b.total),
                        "available": str(b.available),
                        "locked": str(b.locked),
                    }
                    for b in balances
                },
                "count": len(balances),
            },
        )
    return 0


def main() -> int:
    # Load .env so KRAKEN_API_KEY / KRAKEN_API_SECRET are available even
    # when the user hasn't sourced the file into their shell. Idempotent
    # no-op if .env is absent or already loaded by something else.
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        default="BTC/USD",
        help="Trading pair in BASE/QUOTE form (default: BTC/USD).",
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

    return asyncio.run(_run(symbol, args.log_format))


if __name__ == "__main__":
    raise SystemExit(main())
