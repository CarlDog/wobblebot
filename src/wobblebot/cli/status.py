"""Status CLI — read-only Kraken price + balance fetch.

Run as a module::

    python -m wobblebot.cli.status
    python -m wobblebot.cli.status --symbol ETH/USD
    python -m wobblebot.cli.status --config /path/to/custom-settings.yml

**Read-only.** Loads ``KRAKEN_API_KEY`` (the read-only key, not the
trade key), builds a ``KrakenAdapter`` + ``DataCollector``, and
prints:

- The current price for ``--symbol`` (default BTC/USD).
- All account balances.

Output goes through the project logger; ``--log-format json`` emits
one JSON object per record. Useful as a first sanity check after
configuring credentials, and for any "is the read path still working
end-to-end" verification.

Configuration layering (per ADR-009):
1. Base config — ``config/settings.yml`` (or ``--config`` /
   ``settings.example.yml`` fallback).
2. Profile overrides — ``--profile name`` (no operational
   distinction for status, but supported for consistency).
3. CLI flag overrides — explicit flags below.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.data_collector import DataCollector

_LOGGER = logging.getLogger("wobblebot.cli.status")


async def _run(config: WobbleBotConfig) -> int:
    if config.status is None:
        _LOGGER.error("settings.yml is missing the `status:` section")
        return 2

    try:
        kraken_config = KrakenConfig.from_env()
    except ValueError as exc:
        _LOGGER.error("missing Kraken credentials", extra={"error": str(exc)})
        return 2

    adapter = KrakenAdapter(config=kraken_config)
    # status is a read-only diagnostic — no historical metrics — but the
    # Stage 3.1 DataCollector requires a storage backing. An in-memory
    # adapter satisfies the contract without touching disk.
    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    collector = DataCollector(exchange=adapter, storage=storage)
    try:
        snapshot = await collector.get_market_snapshot(config.status.symbol)
        balances = await collector.get_balances()
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "status check failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    finally:
        await adapter.aclose()
        await storage.close()

    _LOGGER.info(
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
        _LOGGER.info(
            "account balances: (empty)",
            extra={"balances": {}, "count": 0},
        )
    else:
        summary = ", ".join(
            f"{b.asset}={b.total} (avail {b.available} / locked {b.locked})" for b in balances
        )
        _LOGGER.info(
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


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "status",
        {
            "symbol": ("symbol", identity),
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--symbol", default=None, help="Trading pair in BASE/QUOTE form (e.g. BTC/USD)."
    )
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

    log_format = config.status.log_format if config.status else "plain"
    configure_logging(log_format=log_format)

    return asyncio.run(_run(config))


if __name__ == "__main__":
    raise SystemExit(main())
