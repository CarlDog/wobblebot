"""Preflight CLI — validate the grid against live Kraken without moving money.

Run as a module::

    python -m wobblebot.cli.preflight
    python -m wobblebot.cli.preflight --symbol ETH/USD
    python -m wobblebot.cli.preflight --order-size 5 --spacing 0.5
    python -m wobblebot.cli.preflight --profile conservative

Builds a ``GridEngine`` wired to ``KrakenAdapter(dry_run=True)`` plus
an in-memory SQLite, then runs **one** ``step(symbol)`` against live
Kraken. Every order the engine wants to place goes through Kraken's
``validate=true`` flag — the request is signed, sent, and validated
end-to-end (auth / pair / precision / balance / ordermin / costmin)
**without placing the order**.

Exits 0 if every order validated, non-zero on any failure. Operator
runs this before flipping to ``cli/live`` (which actually trades).

Configuration layering (per ADR-009):
1. Base config — ``config/settings.yml`` (or fallback).
2. Profile overrides — ``--profile name``.
3. CLI flag overrides — explicit flags below.

Reads grid + safety from the YAML; preflight uses generous internal
caps (matched to the layout's expected exposure) regardless of the
``safety:`` block, since its job is to verify Kraken validation
passes not to enforce trading caps.

Loads credentials from ``KRAKEN_TRADER_API_KEY`` /
``KRAKEN_TRADER_API_SECRET`` (separate from the read-only key per
ADR-003-style separation).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.services.grid_engine import GridEngine, StepResult

_LOGGER = logging.getLogger("wobblebot.cli.preflight")


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


def _check_step_result(result: StepResult, expected_layout: int) -> int | None:
    """Validate one StepResult. Returns an error exit code or None."""
    if result.action != "initialized":
        _LOGGER.error(
            "expected first-tick initialization; got something else",
            extra={"action": result.action},
        )
        return 1
    if result.refusals:
        _LOGGER.error(
            "engine refused some placements via the safety cap layer",
            extra={
                "placed": result.placed,
                "refusals": result.refusals,
                "expected": expected_layout,
            },
        )
        return 1
    if result.placed != expected_layout:
        _LOGGER.error(
            "placed count does not match expected layout",
            extra={"placed": result.placed, "expected": expected_layout},
        )
        return 1
    return None


async def _run(config: WobbleBotConfig) -> int:
    if config.preflight is None:
        _LOGGER.error("settings.yml is missing the `preflight:` section")
        return 2

    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADER_API_KEY",
            secret_var="KRAKEN_TRADER_API_SECRET",
        )
    except ValueError as exc:
        _LOGGER.error(
            "missing trade credentials",
            extra={"error": str(exc), "expected": "KRAKEN_TRADER_API_KEY/KRAKEN_TRADER_API_SECRET"},
        )
        return 2

    grid_config = config.grid
    layout_count = grid_config.default.levels_above + grid_config.default.levels_below
    max_total = grid_config.default.order_size_usd * layout_count
    safety_config = _build_safety_config(max_total_usd=max_total, max_orders=layout_count + 5)

    storage = SQLiteStorageAdapter(":memory:")
    await storage.connect()
    adapter = KrakenAdapter(config=kraken_config, dry_run=True)
    engine = GridEngine(adapter, storage, grid_config, safety_config)

    try:
        ref_price = (await adapter.get_current_price(config.preflight.symbol)).amount
        _LOGGER.info(
            "validate run starting",
            extra={
                "symbol": str(config.preflight.symbol),
                "reference_price_live": str(ref_price),
                "spacing_percentage": str(grid_config.default.spacing_percentage),
                "levels_above": grid_config.default.levels_above,
                "levels_below": grid_config.default.levels_below,
                "order_size_usd": str(grid_config.default.order_size_usd),
                "expected_layout_orders": layout_count,
                "max_total_exposure_usd": str(max_total),
            },
        )

        result = await engine.step(config.preflight.symbol)
        bad = _check_step_result(result, layout_count)
        if bad is not None:
            return bad

        opens = await storage.get_open_orders(symbol=config.preflight.symbol)
        non_dryrun = [
            o.exchange_id
            for o in opens
            if o.exchange_id and not o.exchange_id.startswith("DRYRUN-")
        ]
        if non_dryrun:
            _LOGGER.error(
                "dry-run produced non-DRYRUN exchange_ids — adapter not in dry_run mode?",
                extra={"non_dryrun_ids": non_dryrun},
            )
            return 1

        _LOGGER.info(
            "validate run completed successfully",
            extra={
                "symbol": str(config.preflight.symbol),
                "validated": result.placed,
                "expected": layout_count,
                "all_dry_run": True,
            },
        )
        return 0

    except WobbleBotPortError as exc:
        _LOGGER.error(
            "validate run failed against live Kraken",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    finally:
        await adapter.aclose()
        await storage.close()


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    preflight_overrides = collect_overrides(
        args,
        "preflight",
        {
            "symbol": ("symbol", identity),
            "log_format": ("log_format", identity),
        },
    )

    # grid.default overrides — built manually because nested
    grid_default: dict[str, Any] = {}
    if args.spacing is not None:
        grid_default["spacing_percentage"] = args.spacing
    if args.above is not None:
        grid_default["levels_above"] = args.above
    if args.below is not None:
        grid_default["levels_below"] = args.below
    if args.order_size is not None:
        grid_default["order_size_usd"] = args.order_size

    merged: dict[str, Any] = dict(preflight_overrides)
    if grid_default:
        merged["grid"] = {"default": grid_default}
    return merged


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--symbol", default=None, help="Trading pair in BASE/QUOTE form (e.g. BTC/USD)."
    )
    parser.add_argument("--spacing", type=Decimal, default=None)
    parser.add_argument("--above", type=int, default=None)
    parser.add_argument("--below", type=int, default=None)
    parser.add_argument("--order-size", type=Decimal, default=None)
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

    log_format = config.preflight.log_format if config.preflight else "plain"
    configure_logging(log_format=log_format)

    return asyncio.run(_run(config))


if __name__ == "__main__":
    raise SystemExit(main())
