"""Approve / Reject buttons for a Discord confirmation (P3, ADR-013).

Replaces the ✅ / ❌ *reaction* flow. Two things this buys beyond looks:

1. **Restart survival.** The old flow resolved a reaction back to its row
   through an in-memory ``pending_message_map``, so every daemon restart
   orphaned every outstanding confirmation — and the operator's container
   restarts on every deploy. These are ``discord.ui.DynamicItem`` buttons:
   the pending id rides in the component's ``custom_id``, and the client
   re-hydrates the handler from that id after a restart. No map, no leak
   (deep-scan finding F7c disappears with it).
2. **An answer.** A reaction that couldn't be resolved did nothing,
   silently. A button always replies — approved, rejected, expired, or
   "not yours" — so the operator is never left guessing whether the click
   registered.

**Firewall.** ``interaction_check`` defers to the transport's own
``is_allowed`` rather than re-implementing the allowlist: a second copy
of that rule is exactly how a firewall regression gets introduced. A
non-allowlisted click is refused before any storage call. Approving still
only moves the row to ``approved`` — the daemons' ``status='approved'``
polls remain the sole path to the engine (ADR-002/ADR-013).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import discord

from wobblebot.ports.operator import ConfirmDecision, ConfirmOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from wobblebot.adapters.discord_transport import DiscordTransport

LOGGER = logging.getLogger(__name__)

# Decided-card colors. Defined here rather than imported from
# discord_transport to avoid a circular import (that module imports this
# one). Green/red mirror the button styles; amber marks a decision that
# did NOT take effect.
COLOR_APPROVED = 0x2ECC71
COLOR_REJECTED = 0x95A5A6
COLOR_STALE = 0xF39C12

# The client attribute the buttons read their wiring from. Namespaced
# because it hangs off a third-party ``discord.Client`` instance.
CONTEXT_ATTR = "wobblebot_confirm_context"

# custom_id grammar: wb:confirm:<decision>:<uuid>. The uuid is the
# pending_commands row id — that is the whole restart-survival trick,
# so the pattern is strict (a malformed id must not reach storage).
CUSTOM_ID_TEMPLATE = r"wb:confirm:(?P<decision>approve|reject):(?P<pending_id>[0-9a-fA-F-]{36})"


class ConfirmDecisionHandler(Protocol):
    """What a button calls once a click passes the allowlist.

    Implemented by ``cli/operator`` (which owns pending_commands
    transitions); the adapter never touches storage itself.
    """

    async def __call__(
        self,
        *,
        pending_id: UUID,
        decision: ConfirmDecision,
        user_id: str,
    ) -> ConfirmOutcome:
        """Apply the decision; return what actually happened to the row."""


@dataclass(frozen=True)
class ConfirmContext:
    """Wiring the buttons need, attached to the live ``discord.Client``.

    A ``DynamicItem`` is re-created by the library from a ``custom_id``
    and only ever sees the ``Interaction``, so it cannot close over the
    daemon's objects — it reaches them through ``interaction.client``.
    """

    transport: DiscordTransport
    handler: ConfirmDecisionHandler


def _original_summary(interaction: discord.Interaction) -> str | None:
    """Pull the confirm embed's description off the message being edited.

    Best-effort: the message is right there in the interaction, so the
    decided card can restate the request without the daemon having to
    re-read the row. Returns ``None`` when there is nothing to carry.
    """
    message = getattr(interaction, "message", None)
    embeds = getattr(message, "embeds", None) or []
    if not embeds:
        return None
    description = getattr(embeds[0], "description", None)
    return description if isinstance(description, str) and description else None


def _context_of(interaction: discord.Interaction) -> ConfirmContext | None:
    context = getattr(interaction.client, CONTEXT_ATTR, None)
    if isinstance(context, ConfirmContext):
        return context
    return None


class ConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=CUSTOM_ID_TEMPLATE,
):
    """One Approve or Reject button bound to a pending-command id."""

    def __init__(self, decision: ConfirmDecision, pending_id: str) -> None:
        self.decision: ConfirmDecision = decision
        self.pending_id = pending_id
        approve = decision == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve" if approve else "Reject",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"wb:confirm:{decision}:{pending_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> ConfirmButton:
        """Rebuild the button after a restart, straight from its custom_id."""
        del interaction, item
        decision: ConfirmDecision = "approve" if match["decision"] == "approve" else "reject"
        return cls(decision, match["pending_id"])

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Allowlist gate — the firewall's edge (ADR-013).

        Anyone who can see the channel can click; only allowlisted
        operators may decide. Defers to the transport's own
        ``is_allowed`` so there is one definition of "allowed".
        Fails CLOSED when the context is missing (a daemon that never
        wired the buttons must not accept decisions).
        """
        context = _context_of(interaction)
        if context is None:
            LOGGER.error("confirm button clicked but no confirm context is wired; refusing")
            await self._refuse(interaction, "This bot is not accepting confirmations right now.")
            return False
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel_id) if interaction.channel_id else ""
        if not context.transport.is_allowed(user_id, channel_id):
            LOGGER.warning(
                "refused confirm click from non-allowlisted user %s in channel %s",
                user_id,
                channel_id,
                extra={"user_id": user_id, "channel_id": channel_id},
            )
            await self._refuse(interaction, "You are not on this bot's operator allowlist.")
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        """Apply the decision and tell the operator what happened."""
        context = _context_of(interaction)
        if context is None:  # pragma: no cover - interaction_check refuses first
            return
        try:
            pending_uuid = UUID(self.pending_id)
        except ValueError:
            LOGGER.error("confirm button carried a non-uuid id: %s", self.pending_id)
            await self._refuse(interaction, "That confirmation's id is unreadable.")
            return
        outcome = await context.handler(
            pending_id=pending_uuid,
            decision=self.decision,
            user_id=str(interaction.user.id),
        )
        # Rewrite the CARD, not just the text above it. Leaving the
        # original "Confirm command" embed in place made a decided
        # request keep looking pending (operator-caught on the first live
        # click, 2026-08-10). Dropping the buttons at the same time: the
        # decision is made, and a second click could only ever say
        # "already decided".
        await interaction.response.edit_message(
            content=None,
            embed=self._outcome_embed(interaction, outcome),
            view=None,
        )

    def _outcome_embed(
        self, interaction: discord.Interaction, outcome: ConfirmOutcome
    ) -> discord.Embed:
        """Render the decided state, keeping the original request visible.

        Colour tracks whether the decision was APPLIED, not which button
        was pressed: a reject that took effect is a success (grey), while
        an approve that arrived after the TTL is not (amber) — the
        operator needs to see that difference at a glance.
        """
        if not outcome.decided:
            title, color = "Not applied", COLOR_STALE
        elif outcome.result == "approved":
            title, color = "Approved", COLOR_APPROVED
        else:
            title, color = "Rejected", COLOR_REJECTED
        embed = discord.Embed(title=title, description=outcome.message, color=color)
        # Carry the original request forward so the message still records
        # WHAT was decided, not just the verdict.
        summary = _original_summary(interaction)
        if summary:
            embed.add_field(name="Request", value=summary, inline=False)
        # display_name isn't guaranteed on every user object discord.py
        # hands back (cached vs fetched); the id always is. A missing
        # attribute must not raise here — that would leave the operator
        # staring at "This interaction failed" after a decision that
        # already took effect.
        decider = getattr(interaction.user, "display_name", None) or interaction.user.id
        embed.set_footer(text=f"id: {self.pending_id}  •  by {decider}")
        return embed

    @staticmethod
    async def _refuse(interaction: discord.Interaction, message: str) -> None:
        """Reply privately; never edits the shared confirmation message."""
        try:
            await interaction.response.send_message(message, ephemeral=True)
        except discord.DiscordException:
            LOGGER.exception("failed to send refusal response")


def build_confirm_view(pending_id: str) -> discord.ui.View:
    """A timeout-free view carrying both buttons for ``pending_id``.

    ``timeout=None`` is required for persistence: a view that times out
    stops listening, which would re-introduce the very staleness these
    buttons exist to remove.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(ConfirmButton("approve", pending_id))
    view.add_item(ConfirmButton("reject", pending_id))
    return view


__all__ = (
    "CONTEXT_ATTR",
    "CUSTOM_ID_TEMPLATE",
    "ConfirmButton",
    "ConfirmContext",
    "ConfirmDecisionHandler",
    "build_confirm_view",
)
