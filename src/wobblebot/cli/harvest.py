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

Uses the existing read-only ``KRAKEN_READER_API_KEY`` — the Harvester key
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
import os
import sys
import time
from decimal import Decimal
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    emit_heartbeat,
    identity,
    install_signal_handlers,
    load_operator_env,
    notify,
    run_poll_loop,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.cli.harvest_execute import (
    _COMMAND_POLL_SECONDS,
    _execute_command,
    _process_pending_commands,
    _read_usd_balance,
)
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import ExchangeError, StorageError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.notification_events import HarvestProposalEvent
from wobblebot.ports.notifier import NotifierPort
from wobblebot.services.harvester import compute_today_total_withdrawn_usd, propose_transfer

_LOGGER = logging.getLogger("wobblebot.cli.harvest")


async def _run_cycle(
    adapter: ExchangePort,
    *,
    config: WobbleBotConfig,
    storage: SQLiteStorageAdapter | None,
    notifier: NotifierPort | None = None,
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

    # Stage 4.4b: real rolling-24h history feeds the day-cap. Pre-4.4b
    # this was always Decimal("0"); the gate effectively had no
    # day-cap because there was no history to subtract from. With
    # storage wired in, the gate now refuses proposals that would
    # push today's total past max_withdrawal_per_day_usd.
    #
    # Stage 8.4 hotfix #3 (2026-05-20): wrap the storage read in
    # try/except. A transient StorageError (disk full, WAL contention,
    # schema corruption) propagated from here used to kill the
    # daemon — exact same shape as the live/shadow balance-fetch
    # crash. Fail-soft: treat as Decimal("0") (the pre-4.4b default;
    # gate behaves as "no recorded history" not "no proposal"),
    # log a warning, continue the tick. The propose_transfer logic
    # below remains safe because today_total=0 only relaxes the
    # day-cap (never tightens it).
    today_total = Decimal("0")
    if storage is not None:
        try:
            today_total = await compute_today_total_withdrawn_usd(storage, asset="USD")
        except StorageError as exc:
            _LOGGER.warning(
                "harvest tick: today-total fetch failed (%s: %s); treating as $0 — "
                "the day-cap gate runs on an optimistic number this tick",
                type(exc).__name__,
                exc,
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )

    proposal = propose_transfer(
        balance_usd=balance_usd,
        config=config.harvester,
        today_total_withdrawn_usd=today_total,
    )

    if proposal is None:
        _LOGGER.debug(
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

    # Stage 5.5: emit a notification on every proposal so the operator
    # sees treasury suggestions in Discord without tailing logs. The
    # proposal is still HYPOTHETICAL — operator must run cli/harvest
    # --execute to actually move money.
    await notify(
        notifier,
        level="info",
        title=f"Harvester proposal: {proposal.direction} {proposal.amount} {proposal.asset}",
        message=(
            f"Proposal {proposal.proposal_id}: {proposal.direction} "
            f"{proposal.amount} {proposal.asset}. "
            f"{proposal.rationale} "
            f"Run `cli/harvest --execute {proposal.proposal_id}` to act."
        ),
        event=HarvestProposalEvent(
            proposal_id=proposal.proposal_id,
            direction=proposal.direction,
            asset=proposal.asset,
            amount=proposal.amount,
            current_exchange_balance=proposal.current_exchange_balance,
            target_exchange_balance=proposal.target_exchange_balance,
            rationale=proposal.rationale,
        ),
    )

    if storage is not None:
        try:
            await storage.save_transfer_proposal(proposal)
        except StorageError as exc:
            # Log + continue: missing a row in the audit table is
            # worse than killing the loop. Operator will see the
            # error and can investigate.
            _LOGGER.warning(
                "transfer proposal %s failed to persist (%s: %s)",
                proposal.proposal_id,
                type(exc).__name__,
                exc,
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


async def _run_loop(  # pylint: disable=too-many-arguments
    *,
    adapter: ExchangePort,
    config: WobbleBotConfig,
    storage: SQLiteStorageAdapter | None,
    interval_seconds: float,
    stop_event: asyncio.Event,
    notifier: NotifierPort | None = None,
    operator_storage: SQLiteStorageAdapter | None = None,
) -> int:
    started_at = time.monotonic()
    ticks_run = 0
    ticks_succeeded = 0
    commands_run = 0
    _LOGGER.info(
        "harvest session start",
        extra={
            "interval_seconds": interval_seconds,
            "command_poll_seconds": _COMMAND_POLL_SECONDS,
            "harvester_enabled": config.harvester.enabled if config.harvester else False,
            "persistence_enabled": storage is not None,
        },
    )

    async def _one_cycle() -> None:
        nonlocal ticks_run, ticks_succeeded
        # Stage 8.4.E follow-up — heartbeat at the top of each poll
        # so the /health page can prove cli/harvest is alive even
        # when no proposal is generated (the common case at hold-band
        # balances).
        await emit_heartbeat(operator_storage, "cli/harvest")
        ticks_run += 1
        ok = await _run_cycle(adapter, config=config, storage=storage, notifier=notifier)
        if ok:
            ticks_succeeded += 1

    async def _one_command_cycle() -> None:
        nonlocal commands_run
        commands_run += await _process_pending_commands(
            adapter=adapter,
            storage=storage,
            operator_storage=operator_storage,
            config=config,
            notifier=notifier,
        )

    try:
        # Two independent cadences (ADR-034): the proposal cycle runs on
        # the operator's configured schedule (hours), while approved
        # commands poll every few seconds because a human is watching a
        # modal spinner and the approval TTL is 10 minutes. Separate
        # loops rather than one fast loop — re-proposing every 15s would
        # spam Kraken and the proposal table for no benefit.
        await asyncio.gather(
            run_poll_loop(
                _one_cycle,
                interval_seconds=interval_seconds,
                stop_event=stop_event,
            ),
            run_poll_loop(
                _one_command_cycle,
                interval_seconds=_COMMAND_POLL_SECONDS,
                stop_event=stop_event,
            ),
        )
    finally:
        _LOGGER.info(
            "harvest session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "ticks_run": ticks_run,
                "ticks_succeeded": ticks_succeeded,
                "commands_processed": commands_run,
            },
        )
    return 0


# ADR-003 financial-power-fragmentation: the Harvester key (and ONLY it)
# may withdraw, and it must be a SEPARATE secret from the trade key. cli/live
# loads the trade key from this fixed env var (see cli/live).
_TRADE_KEY_ENV_VAR = "KRAKEN_TRADER_API_KEY"


async def _verify_harvester_key(adapter: KrakenAdapter, config: WobbleBotConfig) -> int | None:
    """Verify the ADR-003 invariants for the Harvester key at startup.

    Defense-in-depth on top of the operator-side .env discipline (the
    seven per-execute layers still apply regardless):

    1. **Withdraw scope present.** The Harvester key's whole job is to
       withdraw; a definitive ``has_withdraw_scope() == False`` means the
       wrong/misconfigured key — refuse.
    2. **Distinct from the trade key.** If ``KRAKEN_TRADER_API_KEY`` is in
       this process's env AND equals the Harvester key, financial-power
       fragmentation has collapsed (one secret can trade AND withdraw) —
       refuse.

    Fails SOFT on a transient probe error (an ``ExchangeError`` that is NOT
    a definitive permission-denied): logs + continues rather than
    crash-looping the daemon under ``restart: unless-stopped`` during a
    Kraken blip. Returns ``3`` on a definitive violation, ``None`` to proceed.
    """
    assert config.harvester is not None  # caller checked

    try:
        can_withdraw: bool | None = await adapter.has_withdraw_scope()
    except ExchangeError as exc:
        _LOGGER.warning(
            "could not verify Harvester key withdraw scope (transient): %s — continuing",
            exc,
            extra={"error": str(exc)},
        )
        can_withdraw = None
    if can_withdraw is False:
        _LOGGER.error(
            "Harvester key in %s lacks Kraken Withdraw scope — refusing to start "
            "(ADR-003); mint a key with the Withdraw Funds permission",
            config.harvester.api_key_env_var,
            extra={"key_env_var": config.harvester.api_key_env_var},
        )
        return 3

    harvest_key = os.environ.get(config.harvester.api_key_env_var)
    trade_key = os.environ.get(_TRADE_KEY_ENV_VAR)
    if harvest_key is not None and trade_key is not None and harvest_key == trade_key:
        _LOGGER.error(
            "Harvester key is identical to the trade key — refusing to start "
            "(ADR-003 financial-power-fragmentation); the Harvester key MUST be "
            "a separate secret with Withdraw scope",
            extra={
                "harvester_key_env_var": config.harvester.api_key_env_var,
                "trade_key_env_var": _TRADE_KEY_ENV_VAR,
            },
        )
        return 3
    if trade_key is None:
        _LOGGER.info(
            "trade key not present in this process's env — key distinctness not "
            "byte-verified; relying on deployment-level key separation",
            extra={"trade_key_env_var": _TRADE_KEY_ENV_VAR},
        )
    return None


async def _main_async(  # pylint: disable=too-many-return-statements,too-many-branches
    config: WobbleBotConfig,
    *,
    execute_proposal_id: str | None = None,
) -> int:
    if config.harvester is None:
        _LOGGER.error("settings.yml is missing the `harvester:` section")
        return 2

    # Stage 4.4: load the Harvester key (Withdraw + Query Funds scopes).
    # Per ADR-003 this MUST be a different key from KRAKEN_TRADER_API_KEY —
    # operator-side discipline; we trust the .env config here.
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

    # Open storage. For --execute mode this is REQUIRED (we read the
    # proposal from here); for daemon mode it's optional (persistence
    # gracefully degrades to log-only).
    storage: SQLiteStorageAdapter | None = None
    if config.harvest is not None:
        storage = SQLiteStorageAdapter(config.harvest.db)
        try:
            await storage.connect()
        except StorageError as exc:
            _LOGGER.error(
                "failed to open harvest db at %s: %s",
                config.harvest.db,
                exc,
                extra={"path": config.harvest.db, "error": str(exc)},
            )
            storage = None

    # Stage 5.5: optional operator-notification wiring. When
    # harvest.operator_db is set, open it as a second StoragePort and
    # wrap with SqliteNotifierAdapter; cli/harvest events emit
    # Notification rows that cli/operator forwards to Discord.
    operator_storage: SQLiteStorageAdapter | None = None
    notifier: SqliteNotifierAdapter | None = None
    if config.harvest is not None and config.harvest.operator_db is not None:
        operator_storage = SQLiteStorageAdapter(config.harvest.operator_db)
        try:
            await operator_storage.connect()
            notifier = SqliteNotifierAdapter(operator_storage)
            _LOGGER.info(
                "operator notifications enabled",
                extra={"operator_db": config.harvest.operator_db},
            )
        except StorageError as exc:
            _LOGGER.error(
                "failed to open operator db at %s: %s — notifications disabled",
                config.harvest.operator_db,
                exc,
                extra={"path": config.harvest.operator_db, "error": str(exc)},
            )
            operator_storage = None
            notifier = None

    try:
        # ADR-003 startup invariants for the Harvester key (withdraw scope
        # present + distinct from the trade key). Defense-in-depth; a
        # definitive violation refuses (exit 3) and the finally below cleans
        # up the adapter + any opened storage.
        verify_exit = await _verify_harvester_key(adapter, config)
        if verify_exit is not None:
            return verify_exit

        if execute_proposal_id is not None:
            # Stage 4.4c: one-shot operator-approved execution.
            if storage is None:
                _LOGGER.error(
                    "--execute requires the harvest db to be open; "
                    "configure harvest.db or remove --execute"
                )
                return 2
            return await _execute_command(
                adapter=adapter,
                storage=storage,
                config=config,
                proposal_id=execute_proposal_id,
                notifier=notifier,
            )

        # Daemon mode (read-only observation + proposal persistence).
        try:
            interval = config.schedules.get("harvest")
        except KeyError as exc:
            _LOGGER.error("missing schedule", extra={"error": str(exc)})
            return 2

        stop_event = asyncio.Event()
        install_signal_handlers(asyncio.get_running_loop(), stop_event, logger=_LOGGER)
        return await _run_loop(
            adapter=adapter,
            config=config,
            storage=storage,
            interval_seconds=interval.total_seconds(),
            stop_event=stop_event,
            notifier=notifier,
            operator_storage=operator_storage,
        )
    finally:

        async def _close_adapter() -> None:
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()

        phases: list[tuple[str, Any]] = [("close_kraken_adapter", _close_adapter)]
        if storage is not None:
            phases.append(("close_harvest_storage", storage.close))
        if operator_storage is not None:
            phases.append(("close_operator_storage", operator_storage.close))
        await safe_shutdown(phases, logger=_LOGGER)


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
    parser.add_argument(
        "--execute",
        default=None,
        metavar="PROPOSAL_ID",
        help=(
            "Operator-approved one-shot execution of a persisted "
            "TransferProposal. Defends behind multiple checks: "
            "harvester.enabled, proposal staleness, destination label "
            "resolution, current balance sufficient, day-cap headroom. "
            "Without this flag the daemon runs in read-only mode."
        ),
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

    log_format = (
        args.log_format
        if args.log_format is not None
        else (config.harvest.log_format if config.harvest else "plain")
    )
    log_file_path = config.harvest.log_file_path if config.harvest else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)

    run_with_clean_exit(
        _main_async(config, execute_proposal_id=args.execute),
        logger=_LOGGER,
    )


if __name__ == "__main__":
    raise SystemExit(main())
