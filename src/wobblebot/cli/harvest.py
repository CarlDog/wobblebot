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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

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
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import ExchangeError, StorageError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.harvester import TransferResult
from wobblebot.ports.notifier import NotifierPort
from wobblebot.services.harvester import compute_today_total_withdrawn_usd, propose_transfer

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
        _LOGGER.warning(
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
                "harvest tick: today-total fetch failed; treating as 0",
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
        context={
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
            _LOGGER.warning(
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
    _LOGGER.info(
        "harvest session start",
        extra={
            "interval_seconds": interval_seconds,
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

    try:
        await run_poll_loop(
            _one_cycle,
            interval_seconds=interval_seconds,
            stop_event=stop_event,
        )
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


async def _execute_command(  # pylint: disable=too-many-return-statements,too-many-locals,too-many-branches,too-many-arguments
    *,
    adapter: ExchangePort,
    storage: SQLiteStorageAdapter,
    config: WobbleBotConfig,
    proposal_id: str,
    notifier: NotifierPort | None = None,
) -> int:
    """Operator-approved execution of a persisted TransferProposal.

    Mirrors the cli/apply --commit pattern: explicit per-call flag,
    multiple defense-in-depth checks, persists the outcome to a
    forensic table regardless of success/failure.

    Defense layers (any failure aborts with exit 1; no money moved
    unless we reach step 7):
    1. ``HarvesterConfig.enabled`` must be True (operator-side opt-in
       beyond the per-call flag).
    2. Proposal must exist in the harvest db.
    3. Proposal direction must be ``exchange_to_bank``. Deposits
       (``bank_to_exchange``) cannot be executed through Kraken's API
       — they're operator-pushed from the bank side using Kraken's
       deposit instructions. The harvester surfaces deposit proposals
       only as a signal that the operator should manually fund.
    4. Proposal must not be stale (≤ ``proposal_max_age_hours``).
    5. Destination label must resolve in
       ``HarvesterConfig.withdrawal_destinations[proposal.asset]``.
    6. Current exchange balance must cover the proposed amount.
    7. Day-cap must still have headroom — ``today_total_withdrawn_usd
       + proposal.amount ≤ max_withdrawal_per_day_usd``.

    After all checks pass, calls ``adapter.withdraw()`` and persists
    a TransferResult (``status="pending"`` on success; ``status="failed"``
    if Kraken returns an error after we cleared all our gates).
    """
    assert config.harvester is not None  # caller-enforced

    # 1. HarvesterConfig.enabled gate
    if not config.harvester.enabled:
        _LOGGER.error(
            "harvester.enabled=False — refusing execution. Flip the flag in "
            "settings.yml to opt in to live withdrawals."
        )
        return 1

    # 2. Proposal lookup
    proposals = await storage.get_transfer_proposals(limit=1000)
    proposal = next((p for p in proposals if p.proposal_id == proposal_id), None)
    if proposal is None:
        _LOGGER.error(
            "proposal not found in harvest db",
            extra={"proposal_id": proposal_id, "searched": len(proposals)},
        )
        return 1

    # 3. Direction gate (caught during the Stage 4.5 integration audit).
    # Kraken's /0/private/Withdraw is exchange→bank only. Deposits are
    # operator-pushed from the bank side using Kraken's deposit
    # instructions (account number + routing number visible in Kraken
    # Pro). There's no API path for "initiate ACH from bank to Kraken"
    # — refusing here prevents calling /Withdraw with the wrong
    # semantics and accidentally moving money in the opposite
    # direction.
    if proposal.direction != "exchange_to_bank":
        _LOGGER.error(
            "deposit proposals cannot be executed via the API; "
            "manually push funds to Kraken using the deposit instructions "
            "from Kraken Pro → Funding → Deposit",
            extra={
                "proposal_id": proposal_id,
                "direction": proposal.direction,
                "amount": str(proposal.amount),
                "asset": proposal.asset,
            },
        )
        return 1

    # 3. Staleness check
    now = datetime.now(UTC)
    age = now - proposal.created_at.dt
    max_age = timedelta(hours=config.harvester.proposal_max_age_hours)
    if age > max_age:
        _LOGGER.error(
            "proposal is stale; refusing — generate a fresh one before --execute",
            extra={
                "proposal_id": proposal_id,
                "age_hours": round(age.total_seconds() / 3600, 2),
                "max_age_hours": config.harvester.proposal_max_age_hours,
            },
        )
        return 1

    # 4. Destination label resolution
    destination = config.harvester.withdrawal_destinations.get(proposal.asset)
    if not destination:
        _LOGGER.error(
            "asset has no destination label in HarvesterConfig.withdrawal_destinations; "
            "operator must add a Kraken Pro destination label first",
            extra={
                "asset": proposal.asset,
                "configured_assets": sorted(config.harvester.withdrawal_destinations),
            },
        )
        return 1

    # 6. Current balance check. Step 3 already guaranteed
    # direction == "exchange_to_bank", so this fires unconditionally.
    current_balance = await _read_usd_balance(adapter)
    if current_balance is None:
        _LOGGER.error("could not read current balance; refusing execution")
        return 1
    if current_balance < proposal.amount:
        _LOGGER.error(
            "current exchange balance below proposed withdrawal amount; refusing",
            extra={
                "current_balance_usd": str(current_balance),
                "proposal_amount_usd": str(proposal.amount),
            },
        )
        return 1

    # 7. Day-cap fresh check
    today_total = await compute_today_total_withdrawn_usd(storage, asset=proposal.asset)
    if today_total + proposal.amount > config.harvester.max_withdrawal_per_day_usd:
        _LOGGER.error(
            "executing would push today's total over max_withdrawal_per_day_usd; refusing",
            extra={
                "today_total_usd": str(today_total),
                "proposal_amount_usd": str(proposal.amount),
                "max_withdrawal_per_day_usd": str(config.harvester.max_withdrawal_per_day_usd),
            },
        )
        return 1

    # 7. Execute via Kraken /Withdraw
    _LOGGER.info(
        "executing withdrawal via Kraken /Withdraw",
        extra={
            "proposal_id": proposal.proposal_id,
            "asset": proposal.asset,
            "amount": str(proposal.amount),
            "destination": destination,
        },
    )
    try:
        refid = await adapter.withdraw(
            asset=proposal.asset,
            amount=proposal.amount,
            destination=destination,
        )
    except ExchangeError as exc:
        _LOGGER.error(
            "kraken /Withdraw rejected the request",
            extra={
                "proposal_id": proposal.proposal_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        # Persist a failed TransferResult so the audit trail records
        # the attempt. transaction_id is synthetic (no Kraken refid
        # was issued); prefix lets show_transfers distinguish.
        try:
            await storage.save_transfer_result(
                TransferResult(
                    proposal_id=proposal.proposal_id,
                    transaction_id=f"failed-{uuid4()}",
                    status="failed",
                    executed_amount=proposal.amount,
                    direction=proposal.direction,
                    asset=proposal.asset,
                    timestamp=Timestamp(dt=datetime.now(UTC)),
                ),
            )
        except StorageError as persist_exc:
            _LOGGER.error(
                "failed to persist failure audit row",
                extra={"error": str(persist_exc)},
            )
        # Stage 5.5: surface the failure to the operator's Discord.
        await notify(
            notifier,
            level="error",
            title=f"Withdrawal failed: {proposal.amount} {proposal.asset}",
            message=(
                f"Kraken /Withdraw rejected proposal {proposal.proposal_id}: {exc}. "
                "No money moved."
            ),
            context={
                "proposal_id": proposal.proposal_id,
                "asset": proposal.asset,
                "amount": str(proposal.amount),
                "destination": destination,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return 1

    # 8. Persist success
    result = TransferResult(
        proposal_id=proposal.proposal_id,
        transaction_id=refid,
        status="pending",  # Kraken hasn't settled the wire/ACH yet
        executed_amount=proposal.amount,
        direction=proposal.direction,
        asset=proposal.asset,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    try:
        await storage.save_transfer_result(result)
    except StorageError as exc:
        # The withdrawal SUBMITTED at Kraken but our audit row didn't
        # persist. This is a bad state — flag it loudly. The Kraken
        # refid is in the log so the operator can reconcile manually
        # from Kraken Pro.
        _LOGGER.error(
            "WITHDRAWAL SUBMITTED but audit row persistence failed — "
            "reconcile manually from Kraken Pro using the refid below",
            extra={
                "refid": refid,
                "proposal_id": proposal.proposal_id,
                "error": str(exc),
            },
        )
        return 1

    _LOGGER.info(
        "WITHDRAWAL SUBMITTED — money moved",
        extra={
            "proposal_id": proposal.proposal_id,
            "transaction_id": refid,
            "asset": proposal.asset,
            "amount": str(proposal.amount),
            "destination": destination,
            "status": "pending",
        },
    )
    # Stage 5.5: surface the successful withdrawal to the operator's
    # Discord. Level "warning" not "info" because money moved — this is
    # the highest-value event the harvester emits and the operator
    # wants it surfaced loudly.
    await notify(
        notifier,
        level="warning",
        title=f"Withdrawal submitted: {proposal.amount} {proposal.asset}",
        message=(
            f"Kraken /Withdraw accepted proposal {proposal.proposal_id}. "
            f"refid={refid}, destination={destination}, status=pending. "
            "Money has left the exchange."
        ),
        context={
            "proposal_id": proposal.proposal_id,
            "transaction_id": refid,
            "asset": proposal.asset,
            "amount": str(proposal.amount),
            "destination": destination,
            "status": "pending",
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
            "could not verify Harvester key withdraw scope (transient); continuing",
            extra={"error": str(exc)},
        )
        can_withdraw = None
    if can_withdraw is False:
        _LOGGER.error(
            "Harvester key lacks Kraken Withdraw scope — refusing to start "
            "(ADR-003); mint a Harvester key with the Withdraw Funds permission",
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
                "failed to open harvest db",
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
                "failed to open operator db; notifications disabled",
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
