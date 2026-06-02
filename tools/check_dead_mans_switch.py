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
the engine steps, ``_private_post`` raises on any Kraken error — and the
logs showed no arm failures). So this verifies the LIVE behaviour
directly, because ``set_dead_mans_switch`` discards Kraken's response —
the bot has never actually confirmed Kraken armed anything.

It calls ``CancelAllOrdersAfter`` and prints Kraken's ``currentTime`` and
``triggerTime``. If ``triggerTime`` is set roughly ``timeout`` seconds
past ``currentTime``, Kraken accepted the arm. Two modes:

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

_LOGGER = logging.getLogger("tools.check_dead_mans_switch")


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
        # set_dead_mans_switch() returns None (it discards Kraken's response),
        # so call the signed-POST helper directly to SEE what Kraken returns.
        result = await adapter._private_post(  # pylint: disable=protected-access
            "/0/private/CancelAllOrdersAfter", {"timeout": str(timeout)}
        )
        current = result.get("currentTime")
        trigger = result.get("triggerTime")
        # Kraken returns triggerTime="0" when disabled; a set timer is a
        # future timestamp distinct from currentTime.
        armed = bool(trigger) and str(trigger) not in ("0", str(current))
        _LOGGER.info(
            "CancelAllOrdersAfter response",
            extra={
                "requested_timeout_seconds": timeout,
                "currentTime": current,
                "triggerTime": trigger,
                "armed": armed,
            },
        )
        if armed:
            _LOGGER.info(
                "KRAKEN ACCEPTED THE ARM: triggerTime=%s is set ~%ss past currentTime=%s. "
                "The switch IS armed server-side.",
                trigger,
                timeout,
                current,
            )
        else:
            _LOGGER.error(
                "KRAKEN DID NOT ARM: triggerTime=%s (missing / zero / == currentTime). The "
                "dead man's switch is NOT functioning — this is the defect.",
                trigger,
            )

        if watch:
            _LOGGER.warning(
                "LEFT ARMED (not disarming). Keep a throwaway order open and watch Kraken "
                "Pro -> Orders: every open order should cancel by triggerTime=%s (~%ss from "
                "now). If they do NOT, the switch does not fire.",
                trigger,
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
