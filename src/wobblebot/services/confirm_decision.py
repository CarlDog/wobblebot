"""Apply an operator's approve/reject decision to a ``PendingCommand``.

Extracted from ``cli/operator._handle_reaction`` (P3, buttons-over-
reactions slice) so the Discord *button* path and any other confirming
surface share ONE implementation of the transition rules. The rules are
subtle enough that two copies would drift:

- **TTL wins over the click.** A decision that lands after
  ``ttl_expires_at`` — between ``_ttl_expirer_loop`` sweeps — must
  expire the row rather than act on a stale approval. Mirrors
  ``_expire_stale_pending_commands``: status only, no
  ``confirming_user_id``/``confirmed_at``, because nothing was confirmed.
- **Idempotency.** A row that already left ``awaiting_confirmation``
  (double-click, a parallel decision from the web UI) is a no-op, not an
  overwrite.
- **Firewall.** This function only ever moves a row to ``approved`` /
  ``rejected`` / ``expired``. It never dispatches — the daemons'
  ``status='approved'`` polls remain the sole path to the engine
  (ADR-002/ADR-013).

Returns a :class:`ConfirmOutcome` whose ``message`` is operator-facing:
the button path renders it back into Discord, so a refusal explains
itself instead of silently doing nothing (the reaction path's behavior).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.storage import StoragePort

ConfirmDecision = Literal["approve", "reject"]

ConfirmResult = Literal[
    "approved",
    "rejected",
    "expired",
    "already_decided",
    "not_found",
    "error",
]


@dataclass(frozen=True)
class ConfirmOutcome:
    """What happened to the row, plus a line the operator can read."""

    result: ConfirmResult
    message: str

    @property
    def decided(self) -> bool:
        """True when this call is what moved the row to approved/rejected."""
        return self.result in ("approved", "rejected")


async def apply_confirm_decision(  # pylint: disable=too-many-return-statements
    # too-many-return-statements: each return is one distinct outcome the
    # operator is shown by name (not found / already decided / expired /
    # storage error / approved / rejected). Collapsing them behind a
    # result variable would hide which gate stopped a decision, and on a
    # firewall path "why didn't my approval take?" must stay readable.
    *,
    storage: StoragePort,
    pending_id: UUID,
    decision: ConfirmDecision,
    user_id: str,
    now: datetime | None = None,
) -> ConfirmOutcome:
    """Transition one ``PendingCommand`` on an operator decision.

    Args:
        storage: operator.db storage port.
        pending_id: The row to transition.
        decision: ``"approve"`` or ``"reject"``.
        user_id: Discord user id recorded as ``confirming_user_id``.
            Callers MUST have already checked this user against the
            allowlist — this function records the decider, it does not
            authorize them.
        now: Wallclock override (test seam).

    Returns:
        A :class:`ConfirmOutcome`. Storage failures return
        ``result="error"`` rather than raising: every caller is an
        event handler that must not die on one bad row.
    """
    current = Timestamp(dt=now or datetime.now(UTC))
    try:
        pending = await storage.get_pending_command(pending_id)
    except StorageError as exc:
        return ConfirmOutcome("error", f"Could not read the command: {exc}")
    if pending is None:
        return ConfirmOutcome("not_found", "That command is no longer on file.")
    if pending.status != "awaiting_confirmation":
        return ConfirmOutcome(
            "already_decided",
            f"Already {pending.status} — no change.",
        )

    if pending.ttl_expires_at.dt <= current.dt:
        expired = pending.model_copy(update={"status": "expired"})
        try:
            await storage.save_pending_command(expired)
        except StorageError as exc:
            return ConfirmOutcome("error", f"Could not expire the command: {exc}")
        return ConfirmOutcome(
            "expired",
            "The confirmation window closed before this landed. Nothing ran — re-issue it.",
        )

    status: Literal["approved", "rejected"] = "approved" if decision == "approve" else "rejected"
    updated = pending.model_copy(
        update={
            "status": status,
            "confirming_user_id": user_id,
            "confirmed_at": current,
        }
    )
    try:
        await storage.save_pending_command(updated)
    except StorageError as exc:
        return ConfirmOutcome("error", f"Could not record the decision: {exc}")

    if status == "approved":
        return ConfirmOutcome(
            "approved",
            f"Approved `{pending.command.kind}` — queued for the next daemon poll.",
        )
    return ConfirmOutcome("rejected", f"Rejected `{pending.command.kind}` — nothing ran.")


__all__ = (
    "ConfirmDecision",
    "ConfirmOutcome",
    "ConfirmResult",
    "apply_confirm_decision",
)
