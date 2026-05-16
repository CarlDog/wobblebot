"""Harvest CLI — Phase 4 read-only treasury monitor (Stage 4.2).

Run as a module::

    python -m wobblebot.cli.harvest
    python -m wobblebot.cli.harvest --profile conservative

**Read-only against Kraken; log-only against the operator's
attention.** Polls Kraken USD balance on ``schedules.harvest`` cadence,
runs the Stage 4.1 ``propose_transfer()`` decision against the
operator's ``HarvesterConfig`` thresholds, and logs what *would* be
proposed. **No transfers, no DB writes** (the transfer-proposals
table is Stage 4.3's job once proposals become operator-reviewable).

Uses the existing read-only ``KRAKEN_API_KEY`` — the Harvester key
with Withdraw scope isn't needed until Stage 4.4.

Per ADR-003 + ADR-012, the operator-in-the-loop posture applies:
this daemon never moves money; it produces visibility into what
the threshold policy WOULD do given current balances. The operator
watches the proposal stream against real balance fluctuations to
calibrate the thresholds before flipping ``harvester.enabled``.

The ``today_total_withdrawn_usd`` parameter that feeds the day-cap
check flows in as 0 throughout Stage 4.2 — no withdrawals happen.
Stage 4.3+ wires a real history query.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from decimal import Decimal
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import ExchangeError, StorageError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.services.harvester import propose_transfer

_LOGGER = logging.getLogger("wobblebot.cli.harvest")


async def _read_usd_balance(adapter: ExchangePort) -> Decimal | None:
    """Read the operator's current Kraken USD balance.

    Returns ``None`` on transport / parse failure (logged); the
    daemon's outer loop treats this as a recoverable miss and tries
    again next tick. A real balance read of ``Decimal('0')`` (operator
    has no USD) returns ``0``, not ``None`` — the deficit branch in
    the decision logic handles it correctly.
    """
    try:
        balance = await adapter.get_balance("USD")
    except ExchangeError as exc:
        _LOGGER.error(
            "kraken balance read failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None
    if balance is None:
        return Decimal("0")
    return balance.total


async def _run_cycle(
    adapter: ExchangePort,
    *,
    config: WobbleBotConfig,
    storage: SQLiteStorageAdapter | None,
) -> bool:
    """One harvest tick: read balance → decide → log → persist if there's
    a proposal. Returns True on a successful read (proposal or no-op),
    False on a recoverable failure.

    Persistence (Stage 4.3): when a proposal fires AND ``storage`` is
    provided, the proposal lands in the ``transfer_proposals`` table.
    A storage write failure is logged but does NOT fail the tick —
    the daemon's main job is observation; missing one audit row is
    less bad than killing the loop.
    """
    assert config.harvester is not None  # caller-enforced
    balance_usd = await _read_usd_balance(adapter)
    if balance_usd is None:
        return False

    proposal = propose_transfer(
        balance_usd=balance_usd,
        config=config.harvester,
        today_total_withdrawn_usd=Decimal("0"),  # 4.3: no history yet (4.4's job)
    )

    if proposal is None:
        _LOGGER.info(
            "harvest tick: no proposal",
            extra={
                "balance_usd": str(balance_usd),
                "min_exchange_liquidity_usd": str(config.harvester.min_exchange_liquidity_usd),
                "topup_threshold_usd": str(config.harvester.topup_threshold_usd),
                "surplus_threshold_usd": str(config.harvester.surplus_threshold_usd),
                "band": _classify_band(balance_usd, config.harvester),
            },
        )
        return True

    _LOGGER.info(
        "harvest tick: HYPOTHETICAL proposal (no money moved)",
        extra={
            "proposal_id": proposal.proposal_id,
            "direction": proposal.direction,
            "asset": proposal.asset,
            "amount": str(proposal.amount),
            "current_exchange_balance": str(proposal.current_exchange_balance),
            "target_exchange_balance": str(proposal.target_exchange_balance),
            "rationale": proposal.rationale,
        },
    )

    if storage is not None:
        try:
            await storage.save_transfer_proposal(proposal)
        except StorageError as exc:
            # Log + continue: missing a row in the audit table is
            # worse than killing the loop. Operator will see the
            # error and can investigate.
            _LOGGER.error(
                "transfer proposal persistence failed",
                extra={
                    "proposal_id": proposal.proposal_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
    return True


def _classify_band(balance_usd: Decimal, harvester_config: Any) -> str:
    """Operator-facing label for the current band; sugar in the log."""
    if balance_usd < harvester_config.min_exchange_liquidity_usd:
        return "deficit"
    if balance_usd < harvester_config.topup_threshold_usd:
        return "topup_band"
    if balance_usd <= harvester_config.surplus_threshold_usd:
        return "hold_band"
    return "surplus"


async def _run_loop(
    *,
    adapter: ExchangePort,
    config: WobbleBotConfig,
    storage: SQLiteStorageAdapter | None,
    interval_seconds: float,
    stop_event: asyncio.Event,
) -> int:
    started_at = time.monotonic()
    ticks_run = 0
    ticks_succeeded = 0
    _LOGGER.info(
        "harvest session start",
        extra={
            "interval_seconds": interval_seconds,
            "harvester_enabled": config.harvester.enabled if config.harvester else False,
            "persistence_enabled": storage is not None,
        },
    )
    try:
        while not stop_event.is_set():
            ticks_run += 1
            ok = await _run_cycle(adapter, config=config, storage=storage)
            if ok:
                ticks_succeeded += 1
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        _LOGGER.info(
            "harvest session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "ticks_run": ticks_run,
                "ticks_succeeded": ticks_succeeded,
            },
        )
    return 0


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


async def _main_async(config: WobbleBotConfig) -> int:
    if config.harvester is None:
        _LOGGER.error("settings.yml is missing the `harvester:` section")
        return 2

    try:
        interval = config.schedules.get("harvest")
    except KeyError as exc:
        _LOGGER.error("missing schedule", extra={"error": str(exc)})
        return 2

    # Stage 4.4: load the Harvester key (Withdraw + Query Funds scopes).
    # Per ADR-003 this MUST be a different key from KRAKEN_TRADE_API_KEY —
    # operator-side discipline; we trust the .env config here. cli/harvest
    # always uses this key now; the Withdraw scope is dormant until
    # ``--execute`` lands in Slice 4.4c.
    try:
        kraken = KrakenConfig.from_env(
            key_var=config.harvester.api_key_env_var,
            secret_var=config.harvester.api_secret_env_var,
        )
    except (KeyError, ValueError) as exc:
        _LOGGER.error(
            "harvester kraken credentials missing",
            extra={
                "error": str(exc),
                "expected_key_var": config.harvester.api_key_env_var,
                "expected_secret_var": config.harvester.api_secret_env_var,
            },
        )
        return 2

    adapter = KrakenAdapter(kraken)

    # Stage 4.3: open storage for transfer-proposal persistence.
    # config.harvest may be None if the operator omitted the per-CLI
    # section — in that case skip persistence (operator gets the log
    # stream but no forensic table).
    storage: SQLiteStorageAdapter | None = None
    if config.harvest is not None:
        storage = SQLiteStorageAdapter(config.harvest.db)
        try:
            await storage.connect()
        except StorageError as exc:
            _LOGGER.error(
                "failed to open harvest db; persistence disabled",
                extra={"path": config.harvest.db, "error": str(exc)},
            )
            storage = None

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(
            adapter=adapter,
            config=config,
            storage=storage,
            interval_seconds=interval.total_seconds(),
            stop_event=stop_event,
        )
    finally:
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            await aclose()
        if storage is not None:
            await storage.close()


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "harvest",
        {
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
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

    log_format = (
        args.log_format
        if args.log_format is not None
        else (config.harvest.log_format if config.harvest else "plain")
    )
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
