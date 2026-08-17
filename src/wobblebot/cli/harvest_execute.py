"""The Harvester money path — every route to ``adapter.withdraw()``.

Split out of ``cli/harvest.py`` (2026-08-09, ADR-034) when the approved-
command poll pushed that module past the 1000-line gate. The seam is
deliberate rather than mechanical: this module holds **everything that
can move money**, so the seven defense layers, the echo validation, and
the two entry points that reach them sit in one auditable file, while
``cli/harvest.py`` keeps the daemon loop, the proposal cycle, key
verification, and CLI wiring.

Two entry points, one implementation (:func:`_execute_proposal`):

- :func:`_execute_command` — the operator at a terminal
  (``--execute <id>``), mapping the outcome to an exit code.
- :func:`_process_pending_commands` — the operator in the web UI, via
  an approved ``pending_commands`` row (ADR-034). Kind-scoped so
  ``cli/live`` never claims a withdrawal row and this never claims an
  engine command.

Per ADR-003 the Harvester is the sole module with transfer authority;
nothing here is reachable from the LLM assistant (see
``ExecuteProposalCommand``'s deliberate absence from ``OperatorCommand``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import PermanentAuthHalt, notify
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.domain.value_objects import Timestamp, fmt_decimal
from wobblebot.ports.exceptions import ExchangeError, StorageError, WobbleBotPortError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.harvester import TransferResult
from wobblebot.ports.notification_events import (
    CommandResultEvent,
    WithdrawalFailedEvent,
    WithdrawalSubmittedEvent,
)
from wobblebot.ports.notifier import NotifierPort
from wobblebot.ports.operator import CommandResult, ExecuteProposalCommand, PendingCommand
from wobblebot.services.harvester import compute_today_total_withdrawn_usd

_LOGGER = logging.getLogger("wobblebot.cli.harvest")


# ADR-034: the only command kind this daemon dispatches. cli/live polls
# the engine kinds; the two sets are disjoint, so both daemons can share
# operator.db's pending_commands without ever claiming each other's rows.
_HARVEST_COMMAND_KINDS: tuple[str, ...] = ("execute_proposal",)

# How often the approved-command poll runs. Deliberately faster than the
# proposal cycle (hours): the operator is watching a modal spinner while
# this waits, and an approval has a 10-minute TTL.
_COMMAND_POLL_SECONDS = 15.0


@dataclass(frozen=True)
class ExecuteOutcome:
    """Structured result of an execution attempt — the money path's return.

    Exists so the two callers share ONE implementation of the defense
    layers: ``--execute`` maps it to an exit code, the approved-command
    poll maps it to a ``CommandResult`` the web modal displays. Every
    refusal carries an operator-facing ``message`` explaining which gate
    stopped it, so a rejected withdrawal is never a bare "failed".
    """

    success: bool
    message: str


async def _read_usd_balance(
    adapter: ExchangePort,
    halt: PermanentAuthHalt | None = None,
    notifier: NotifierPort | None = None,
) -> Decimal | None:
    """Read the operator's current Kraken USD balance.

    Returns ``None`` on transport / parse failure (logged); the
    daemon's outer loop treats this as a recoverable miss and tries
    again next tick. A real balance read of ``Decimal('0')`` (operator
    has no USD) returns ``0``, not ``None`` — the deficit branch in
    the decision logic handles it correctly.

    ``halt`` (ADR-037 decision 1): the DAEMON cycle passes its 3-strike
    halt so a dead harvester key stops being retried hourly and pages
    the operator once. The ``--execute`` money path passes ``None`` —
    a one-shot has no retry loop to halt, and it already refuses on a
    failed read.

    Lives here rather than in ``cli/harvest`` because it is an input to
    defense layer 6 and the dependency runs one way (harvest imports
    harvest_execute, never the reverse).
    """
    if halt is not None and halt.halted:
        _LOGGER.debug("balance read halted (permanent auth failure); skipping")
        return None
    try:
        balance = await adapter.get_balance("USD")
    except ExchangeError as exc:
        _LOGGER.warning(
            "kraken balance read failed: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        if halt is not None and halt.note_failure(exc):
            _LOGGER.error(
                "harvest balance read HALTED after %d consecutive permanent auth failures; "
                "fix KRAKEN_HARVESTER_API_KEY/_SECRET and redeploy",
                halt.STRIKES,
                extra={"strikes": halt.STRIKES},
            )
            await notify(
                notifier,
                level="critical",
                title="Harvester key dead — balance reads halted",
                message=(
                    f"{halt.STRIKES} consecutive permanent auth failures on the "
                    "harvester key. The hourly harvest cycle is halted; approved "
                    "withdrawals will refuse until the key works. Fix "
                    "KRAKEN_HARVESTER_API_KEY/_SECRET and redeploy."
                ),
                context={"strikes": halt.STRIKES, "task": halt.task_name},
            )
        return None
    if halt is not None:
        halt.note_success()
    if balance is None:
        return Decimal("0")
    return balance.total


async def _execute_proposal(  # pylint: disable=too-many-return-statements,too-many-locals,too-many-branches,too-many-arguments,too-many-statements
    # too-many-statements: the seven defense layers are a deliberately
    # LINEAR gate chain over a money path. Splitting it to satisfy the
    # counter would scatter the layers across helpers and make "does
    # every path still refuse?" harder to answer by reading — the one
    # question this function exists to make answerable.
    *,
    adapter: ExchangePort,
    storage: SQLiteStorageAdapter,
    config: WobbleBotConfig,
    proposal_id: str,
    notifier: NotifierPort | None = None,
    expect_amount: Decimal | None = None,
    expect_destination: str | None = None,
) -> ExecuteOutcome:
    """Operator-approved execution of a persisted TransferProposal.

    Mirrors the cli/apply --commit pattern: explicit per-call flag,
    multiple defense-in-depth checks, persists the outcome to a
    forensic table regardless of success/failure.

    **The single implementation of the money path.** Both entry points
    reach Kraken through here — ``--execute <id>`` (operator at a
    terminal) and the approved-command poll (operator in the web UI,
    ADR-034) — so the gates below can never drift between them.

    Defense layers (any failure aborts; no money moved unless every
    gate passes and we reach the ``adapter.withdraw()`` call at step 8):
    1. ``HarvesterConfig.enabled`` must be True (operator-side opt-in
       beyond the per-call flag).
    2. Proposal must exist in the harvest db.
    2a. When the caller supplies ``expect_amount`` / ``expect_destination``
       (the queued path always does), they must match the proposal as
       loaded NOW. This is the "confirm on a human-readable value, not
       just an opaque id" gate: the operator approved a specific dollar
       amount to a specific destination in the modal, and if the stored
       proposal disagrees — id reuse, an edited row, a regenerated
       proposal — the approval no longer describes this transfer, so we
       refuse rather than move a number nobody agreed to.
    2b. No prior ``pending``/``completed`` TransferResult may exist for
       the proposal (idempotency guard — a repeat ``--execute`` must
       not double-withdraw; a prior ``failed`` row may be retried).
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
        return ExecuteOutcome(
            False,
            "Refused: harvester.enabled is False. Opt in to live withdrawals "
            "in settings.yml first.",
        )

    # 2. Proposal lookup
    proposals = await storage.get_transfer_proposals(limit=1000)
    proposal = next((p for p in proposals if p.proposal_id == proposal_id), None)
    if proposal is None:
        _LOGGER.error(
            "proposal %s not found in harvest db (searched %d)",
            proposal_id,
            len(proposals),
            extra={"proposal_id": proposal_id, "searched": len(proposals)},
        )
        return ExecuteOutcome(False, f"Refused: proposal {proposal_id} not found in harvest db.")

    # 2a. Echo validation — the approval must describe THIS proposal.
    if expect_amount is not None and expect_amount != proposal.amount:
        _LOGGER.error(
            "refusing %s: approved $%s but the stored proposal is $%s",
            proposal_id,
            fmt_decimal(expect_amount),
            fmt_decimal(proposal.amount),
            extra={
                "proposal_id": proposal_id,
                "approved_amount": str(expect_amount),
                "proposal_amount": str(proposal.amount),
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: you approved ${expect_amount} but proposal {proposal_id} "
            f"is now ${proposal.amount}. Re-issue from a fresh proposal.",
        )

    # 2b. Idempotency guard (issue #12): refuse a repeat withdrawal for a
    # proposal that was already submitted. Every gate below re-passes on a
    # second --execute (balance + day-cap still have headroom once the first
    # wire clears), so a duplicate ``--execute <id>`` would double-submit to
    # Kraken /Withdraw. A prior ``failed`` row does NOT block — Kraken rejected
    # it, no money moved, so a retry is legitimate; a ``pending``/``completed``
    # row means funds are already in flight, so we refuse. Withdrawals are rare,
    # so scope by asset and filter in Python rather than widen the storage port.
    prior_results = await storage.get_transfer_results(asset=proposal.asset)
    already_submitted = next(
        (r for r in prior_results if r.proposal_id == proposal_id and r.status != "failed"),
        None,
    )
    if already_submitted is not None:
        _LOGGER.error(
            "refusing %s: already executed as %s (status %s) — would double-withdraw",
            proposal_id,
            already_submitted.transaction_id,
            already_submitted.status,
            extra={
                "proposal_id": proposal_id,
                "prior_transaction_id": already_submitted.transaction_id,
                "prior_status": already_submitted.status,
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: proposal {proposal_id} was already executed "
            f"(refid {already_submitted.transaction_id}, status "
            f"{already_submitted.status}). Refusing to double-withdraw.",
        )

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
            "refusing %s (%s $%s %s): deposits cannot be executed via the API — "
            "push funds from your bank using Kraken Pro -> Funding -> Deposit",
            proposal_id,
            proposal.direction,
            fmt_decimal(proposal.amount),
            proposal.asset,
            extra={
                "proposal_id": proposal_id,
                "direction": proposal.direction,
                "amount": str(proposal.amount),
                "asset": proposal.asset,
            },
        )
        return ExecuteOutcome(
            False,
            "Refused: deposit proposals cannot be executed via the API. Push "
            "funds from your bank using Kraken Pro → Funding → Deposit.",
        )

    # 4. Staleness check
    now = datetime.now(UTC)
    age = now - proposal.created_at.dt
    max_age = timedelta(hours=config.harvester.proposal_max_age_hours)
    if age > max_age:
        _LOGGER.error(
            "refusing %s: %.2fh old exceeds the %dh limit — generate a fresh proposal",
            proposal_id,
            round(age.total_seconds() / 3600, 2),
            config.harvester.proposal_max_age_hours,
            extra={
                "proposal_id": proposal_id,
                "age_hours": round(age.total_seconds() / 3600, 2),
                "max_age_hours": config.harvester.proposal_max_age_hours,
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: proposal is {round(age.total_seconds() / 3600, 1)}h old "
            f"(max {config.harvester.proposal_max_age_hours}h). Generate a fresh one.",
        )

    # 5. Destination label resolution
    destination = config.harvester.withdrawal_destinations.get(proposal.asset)
    if not destination:
        _LOGGER.error(
            "refusing: no withdrawal destination configured for %s (configured: %s) — "
            "add a Kraken Pro destination label to harvester.withdrawal_destinations",
            proposal.asset,
            sorted(config.harvester.withdrawal_destinations),
            extra={
                "asset": proposal.asset,
                "configured_assets": sorted(config.harvester.withdrawal_destinations),
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: no withdrawal destination configured for {proposal.asset}. "
            "Add a Kraken Pro destination label to harvester.withdrawal_destinations.",
        )

    # 5a. Destination echo — same rationale as the amount echo in 2a,
    # checked here because the label only resolves at step 5. The
    # operator approved a transfer to a NAMED destination; if config
    # now resolves the asset elsewhere, the approval is stale.
    if expect_destination is not None and expect_destination != destination:
        _LOGGER.error(
            "refusing %s: approved destination %r but %s now resolves to %r",
            proposal_id,
            expect_destination,
            proposal.asset,
            destination,
            extra={
                "proposal_id": proposal_id,
                "approved_destination": expect_destination,
                "configured_destination": destination,
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: you approved a transfer to '{expect_destination}' but "
            f"{proposal.asset} now resolves to '{destination}'.",
        )

    # 6. Current balance check. Step 3 already guaranteed
    # direction == "exchange_to_bank", so this fires unconditionally.
    current_balance = await _read_usd_balance(adapter)
    if current_balance is None:
        _LOGGER.error("could not read current balance; refusing execution")
        return ExecuteOutcome(
            False, "Refused: could not read the current exchange balance from Kraken."
        )
    if current_balance < proposal.amount:
        _LOGGER.error(
            "refusing %s: exchange balance $%s is below the proposed $%s",
            proposal_id,
            fmt_decimal(current_balance),
            fmt_decimal(proposal.amount),
            extra={
                "current_balance_usd": str(current_balance),
                "proposal_amount_usd": str(proposal.amount),
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: exchange balance ${current_balance} is below the "
            f"proposed ${proposal.amount}.",
        )

    # 7. Day-cap fresh check
    today_total = await compute_today_total_withdrawn_usd(storage, asset=proposal.asset)
    if today_total + proposal.amount > config.harvester.max_withdrawal_per_day_usd:
        _LOGGER.error(
            "refusing %s: $%s would push today's withdrawals ($%s) past the $%s daily cap",
            proposal_id,
            fmt_decimal(proposal.amount),
            fmt_decimal(today_total),
            fmt_decimal(config.harvester.max_withdrawal_per_day_usd),
            extra={
                "today_total_usd": str(today_total),
                "proposal_amount_usd": str(proposal.amount),
                "max_withdrawal_per_day_usd": str(config.harvester.max_withdrawal_per_day_usd),
            },
        )
        return ExecuteOutcome(
            False,
            f"Refused: ${proposal.amount} would push today's withdrawals "
            f"(${today_total}) past the ${config.harvester.max_withdrawal_per_day_usd} "
            "daily cap.",
        )

    # 8. Execute via Kraken /Withdraw
    _LOGGER.info(
        "executing withdrawal via Kraken /Withdraw (proposal_id=%s, asset=%s, amount=%s, "
        "destination=%s)",
        proposal.proposal_id,
        proposal.asset,
        fmt_decimal(proposal.amount),
        destination,
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
            "kraken /Withdraw REJECTED %s ($%s %s): %s: %s — no money moved",
            proposal.proposal_id,
            fmt_decimal(proposal.amount),
            proposal.asset,
            type(exc).__name__,
            exc,
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
                "failed to persist the failure audit row for %s: %s",
                proposal.proposal_id,
                persist_exc,
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
            event=WithdrawalFailedEvent(
                proposal_id=proposal.proposal_id,
                asset=proposal.asset,
                amount=proposal.amount,
                destination=destination,
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        return ExecuteOutcome(False, f"Kraken rejected the withdrawal: {exc}. No money moved.")

    # 9. Persist success
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
            "WITHDRAWAL SUBMITTED (refid %s, proposal %s) but the audit row failed to "
            "persist: %s — reconcile manually from Kraken Pro",
            refid,
            proposal.proposal_id,
            exc,
            extra={
                "refid": refid,
                "proposal_id": proposal.proposal_id,
                "error": str(exc),
            },
        )
        # success=False is deliberate even though money DID move: the
        # operator must be told to reconcile, and a green "executed" in
        # the modal would bury that. The refid is in the message so the
        # reconciliation can happen from Kraken Pro.
        return ExecuteOutcome(
            False,
            f"WITHDRAWAL SUBMITTED (refid {refid}) but the audit row failed to "
            f"persist: {exc}. Reconcile manually from Kraken Pro.",
        )

    _LOGGER.info(
        "WITHDRAWAL SUBMITTED — money moved (proposal_id=%s, transaction_id=%s, asset=%s, "
        "amount=%s)",
        proposal.proposal_id,
        refid,
        proposal.asset,
        fmt_decimal(proposal.amount),
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
        event=WithdrawalSubmittedEvent(
            proposal_id=proposal.proposal_id,
            transaction_id=refid,
            asset=proposal.asset,
            amount=proposal.amount,
            destination=destination,
            status="pending",
        ),
    )
    return ExecuteOutcome(
        True,
        f"Withdrawal submitted: ${proposal.amount} {proposal.asset} → "
        f"{destination} (refid {refid}, status pending). Money has left the exchange.",
    )


async def _dispatch_one_command(
    *,
    pending: PendingCommand,
    adapter: ExchangePort,
    storage: SQLiteStorageAdapter | None,
    config: WobbleBotConfig,
    notifier: NotifierPort | None,
) -> CommandResult:
    """Run one approved ``execute_proposal`` row through the money path."""
    command = pending.command
    now = Timestamp(dt=datetime.now(UTC))
    if not isinstance(command, ExecuteProposalCommand):
        # Unreachable via the kind-scoped poll; a belt-and-braces guard so
        # a future kind added to _HARVEST_COMMAND_KINDS can't silently
        # fall through to the withdrawal path.
        _LOGGER.error(
            "harvest poll received a command kind it cannot dispatch: %s",
            command.kind,
            extra={"pending_id": str(pending.id), "command_kind": command.kind},
        )
        return CommandResult(
            success=False,
            command_kind=command.kind,
            message=f"cli/harvest cannot dispatch '{command.kind}'.",
            executed_at=now,
        )
    if storage is None or config.harvester is None:
        _LOGGER.error(
            "cannot execute approved proposal %s: harvest storage or config missing",
            command.proposal_id,
            extra={"pending_id": str(pending.id)},
        )
        return CommandResult(
            success=False,
            command_kind=command.kind,
            message=(
                "Refused: the harvest daemon has no harvest.db or harvester config "
                "wired; nothing was executed."
            ),
            executed_at=now,
        )
    outcome = await _execute_proposal(
        adapter=adapter,
        storage=storage,
        config=config,
        proposal_id=command.proposal_id,
        notifier=notifier,
        expect_amount=command.amount_usd,
        expect_destination=command.destination,
    )
    return CommandResult(
        success=outcome.success,
        command_kind=command.kind,
        message=outcome.message,
        executed_at=Timestamp(dt=datetime.now(UTC)),
    )


async def _process_pending_commands(
    *,
    adapter: ExchangePort,
    storage: SQLiteStorageAdapter | None,
    operator_storage: SQLiteStorageAdapter | None,
    config: WobbleBotConfig,
    notifier: NotifierPort | None = None,
) -> int:
    """Drain approved ``execute_proposal`` rows; execute + mark each (ADR-034).

    The Harvester half of the ADR-013 firewall, and the second path to
    real money alongside ``--execute``. The ``status='approved'`` +
    ``kinds=('execute_proposal',)`` SELECT is the gate: a row the
    operator has not approved in the web UI never reaches
    :func:`_execute_proposal`, and no other daemon polls this kind
    (ADR-003 — only the Harvester holds a withdraw-scoped key).

    Per-row failures mark the row ``failed`` with the refusing gate's
    message and continue, so one stale proposal can't block the queue.
    Returns the number of rows processed.
    """
    if operator_storage is None:
        return 0
    try:
        approved = await operator_storage.get_pending_commands(
            status="approved",
            kinds=_HARVEST_COMMAND_KINDS,
        )
    except WobbleBotPortError as exc:
        _LOGGER.warning("failed to poll approved commands: %s", exc)
        return 0
    if not approved:
        _LOGGER.debug("no approved execute_proposal commands to process")
        return 0
    for pending in approved:
        result = await _dispatch_one_command(
            pending=pending,
            adapter=adapter,
            storage=storage,
            config=config,
            notifier=notifier,
        )
        updated = pending.model_copy(
            update={
                "status": "dispatched" if result.success else "failed",
                "dispatched_at": result.executed_at,
                "result": result,
            }
        )
        try:
            await operator_storage.save_pending_command(updated)
        except WobbleBotPortError as exc:
            # Same hazard cli/live documents, but sharper here: the row
            # stays 'approved' and WILL be re-polled. The idempotency
            # guard (layer 2b) is what stops a re-poll from double-
            # withdrawing — it sees the pending TransferResult this run
            # already persisted and refuses.
            _LOGGER.warning(
                "failed to persist dispatched execute_proposal row %s: %s",
                pending.id,
                exc,
                extra={"pending_id": str(pending.id)},
            )
        await notify(
            notifier,
            level="warning" if result.success else "error",
            title=f"Proposal execution {'succeeded' if result.success else 'refused'}",
            message=result.message,
            event=CommandResultEvent(
                command_kind=result.command_kind,
                symbol=None,
                success=result.success,
                message=result.message,
            ),
        )
    return len(approved)


async def _execute_command(
    *,
    adapter: ExchangePort,
    storage: SQLiteStorageAdapter,
    config: WobbleBotConfig,
    proposal_id: str,
    notifier: NotifierPort | None = None,
) -> int:
    """``--execute <id>`` entry point — exit-code wrapper over the money path.

    Kept as a separate name so the CLI's contract (0 = money moved,
    1 = refused/failed) stays obvious at the callsite; all logic and
    every defense layer lives in :func:`_execute_proposal`.
    """
    outcome = await _execute_proposal(
        adapter=adapter,
        storage=storage,
        config=config,
        proposal_id=proposal_id,
        notifier=notifier,
    )
    return 0 if outcome.success else 1
