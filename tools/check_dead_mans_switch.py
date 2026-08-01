"""One-shot diagnostic: does Kraken actually arm the dead man's switch?

ADR-021 wires cli/live to Kraken's server-side dead man's switch
(``/0/private/CancelAllOrdersAfter``): every tick it calls
``set_dead_mans_switch(dead_mans_switch_seconds)``, and if cli/live goes
silent past that timeout Kraken is supposed to cancel ALL open orders on
the account.

The 2026-06-02 multi-coin soak incident contradicted that: with
``dead_mans_switch_seconds=120`` configured, ~15 orders sat open for ~10
minutes while cli/live was down and the switch never swept them. The
arming code audits clean statically (config valid, the ping runs before
the engine steps, the adapter raises on any Kraken error — and the logs
showed no arm failures). ``set_dead_mans_switch`` used to discard
Kraken's response entirely, so the bot had never actually confirmed
Kraken armed anything; it now returns the confirmed ``triggerTime`` (or
``None``) for exactly this reason -- ``cli/live``'s per-tick ping logs
the same confirmation this tool prints.

It calls ``set_dead_mans_switch`` and prints the confirmed
``triggerTime``. If it's set (not ``None``), Kraken accepted the arm.
Two modes:

    # arm for 60s, read triggerTime, then DISARM (safe — nothing cancels):
    python -m tools.check_dead_mans_switch

    # arm for 30s and LEAVE armed: place a throwaway order first, then
    # watch Kraken Pro cancel it within the window (proves it FIRES):
    python -m tools.check_dead_mans_switch --watch --timeout 30

Run with the stack DOWN — cli/live's per-tick pings would otherwise keep
resetting the timer. Uses ``KRAKEN_TRADER_API_KEY`` /
``KRAKEN_TRADER_API_SECRET`` from ``.env`` (the same key cli/live uses;
``CancelAllOrdersAfter`` needs only order create/cancel scope, never
Withdraw — it stays clear of the ADR-003 key split).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import configure_logging
from wobblebot.ports.exceptions import WobbleBotPortError

_LOGGER = logging.getLogger("wobblebot.tools.check_dead_mans_switch")


async def _run(timeout: int, watch: bool) -> int:
    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADER_API_KEY",
            secret_var="KRAKEN_TRADER_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error(
            "missing trade credentials",
            extra={"error": str(exc), "expected": "KRAKEN_TRADER_API_KEY/SECRET"},
        )
        return 2

    adapter = KrakenAdapter(config=kraken_config, dry_run=False)
    try:
        trigger_at = await adapter.set_dead_mans_switch(timeout)
        armed = trigger_at is not None
        _LOGGER.info(
            "set_dead_mans_switch response",
            extra={
                "requested_timeout_seconds": timeout,
                "trigger_at": trigger_at.isoformat() if trigger_at else None,
                "armed": armed,
            },
        )
        if armed:
            _LOGGER.info(
                "KRAKEN ACCEPTED THE ARM: confirmed trigger_at=%s (~%ss from now). "
                "The switch IS armed server-side.",
                trigger_at,
                timeout,
            )
        else:
            _LOGGER.error(
                "KRAKEN DID NOT ARM: no confirmed trigger time in the response. The "
                "dead man's switch is NOT functioning — this is the defect."
            )

        if watch:
            _LOGGER.warning(
                "LEFT ARMED (not disarming). Keep a throwaway order open and watch Kraken "
                "Pro -> Orders: every open order should cancel by trigger_at=%s (~%ss from "
                "now). If they do NOT, the switch does not fire.",
                trigger_at,
                timeout,
            )
        else:
            await adapter.set_dead_mans_switch(0)
            _LOGGER.info("disarmed (timeout=0); this run will cancel nothing.")
        return 0 if armed else 1
    except WobbleBotPortError as exc:
        _LOGGER.error(
            "CancelAllOrdersAfter call failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    finally:
        await adapter.aclose()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Verify Kraken arms the dead man's switch.")
    parser.add_argument(
        "--timeout", type=int, default=60, help="DMS timeout (seconds) to arm. Default 60."
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Leave the switch armed (don't disarm) so you can watch Kraken cancel an order.",
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()
    if args.timeout < 0:
        parser.error("--timeout must be >= 0")
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args.timeout, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
