"""Approve / Reject button behavior (P3 buttons-over-reactions, ADR-013).

The load-bearing tests here are the ``interaction_check`` ones: that
method IS the firewall's edge on the Discord side. Anyone who can see the
channel can click a button; only allowlisted operators may decide.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from wobblebot.adapters.discord_confirm_view import (
    COLOR_APPROVED,
    COLOR_REJECTED,
    COLOR_STALE,
    CONTEXT_ATTR,
    CUSTOM_ID_TEMPLATE,
    ConfirmButton,
    ConfirmContext,
    build_confirm_view,
)
from wobblebot.adapters.discord_transport import DiscordTransport, DiscordTransportConfig
from wobblebot.ports.operator import ConfirmOutcome

pytestmark = pytest.mark.unit


def _transport(
    *,
    allowed_users: frozenset[str] = frozenset({"42"}),
    allowed_channels: frozenset[str] = frozenset({"100"}),
) -> DiscordTransport:
    return DiscordTransport(
        DiscordTransportConfig(
            allowed_user_ids=allowed_users,
            allowed_channel_ids=allowed_channels,
        )
    )


def _interaction(
    *,
    user_id: str = "42",
    channel_id: int | None = 100,
    context: ConfirmContext | None = None,
) -> MagicMock:
    """A stand-in for ``discord.Interaction`` with the fields we read."""
    interaction = MagicMock()
    interaction.user = SimpleNamespace(id=user_id, display_name=f"op-{user_id}")
    interaction.channel_id = channel_id
    interaction.client = SimpleNamespace()
    if context is not None:
        setattr(interaction.client, CONTEXT_ATTR, context)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def _context(transport: DiscordTransport, handler: AsyncMock) -> ConfirmContext:
    return ConfirmContext(transport=transport, handler=handler)


class TestCustomId:
    def test_view_carries_both_buttons_with_the_row_id(self) -> None:
        pending_id = str(uuid4())
        view = build_confirm_view(pending_id)
        custom_ids = [item.custom_id for item in view.children]
        assert custom_ids == [
            f"wb:confirm:approve:{pending_id}",
            f"wb:confirm:reject:{pending_id}",
        ]

    def test_view_never_times_out(self) -> None:
        """A timing-out view would re-introduce the staleness buttons fix."""
        assert build_confirm_view(str(uuid4())).timeout is None

    def test_template_matches_generated_ids(self) -> None:
        pending_id = str(uuid4())
        for item in build_confirm_view(pending_id).children:
            assert re.fullmatch(CUSTOM_ID_TEMPLATE, item.custom_id) is not None

    def test_template_rejects_a_non_uuid_id(self) -> None:
        """A malformed id must never reach storage."""
        assert re.fullmatch(CUSTOM_ID_TEMPLATE, "wb:confirm:approve:not-a-uuid") is None

    def test_template_rejects_an_unknown_decision(self) -> None:
        assert re.fullmatch(CUSTOM_ID_TEMPLATE, f"wb:confirm:destroy:{uuid4()}") is None

    @pytest.mark.asyncio
    async def test_from_custom_id_round_trips_after_restart(self) -> None:
        """The restart-survival path: id in, working button out."""
        pending_id = str(uuid4())
        match = re.fullmatch(CUSTOM_ID_TEMPLATE, f"wb:confirm:reject:{pending_id}")
        assert match is not None

        rebuilt = await ConfirmButton.from_custom_id(_interaction(), MagicMock(), match)

        assert rebuilt.decision == "reject"
        assert rebuilt.pending_id == pending_id


@pytest.mark.asyncio
class TestInteractionCheck:
    """The firewall's edge — who is allowed to decide."""

    async def test_allowlisted_operator_passes(self) -> None:
        button = ConfirmButton("approve", str(uuid4()))
        interaction = _interaction(context=_context(_transport(), AsyncMock()))

        assert await button.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_awaited()

    async def test_non_allowlisted_user_is_refused(self) -> None:
        button = ConfirmButton("approve", str(uuid4()))
        handler = AsyncMock()
        interaction = _interaction(user_id="999", context=_context(_transport(), handler))

        assert await button.interaction_check(interaction) is False
        handler.assert_not_awaited()
        # Refusal is ephemeral — it doesn't rewrite the shared message.
        _, kwargs = interaction.response.send_message.await_args
        assert kwargs["ephemeral"] is True

    async def test_wrong_channel_is_refused(self) -> None:
        button = ConfirmButton("approve", str(uuid4()))
        handler = AsyncMock()
        interaction = _interaction(channel_id=999, context=_context(_transport(), handler))

        assert await button.interaction_check(interaction) is False
        handler.assert_not_awaited()

    async def test_missing_context_fails_closed(self) -> None:
        """A daemon that never wired the buttons must accept nothing."""
        button = ConfirmButton("approve", str(uuid4()))
        interaction = _interaction(context=None)

        assert await button.interaction_check(interaction) is False
        interaction.response.send_message.assert_awaited_once()

    async def test_empty_allowlist_denies(self) -> None:
        """Deny-by-default carries through to the buttons."""
        button = ConfirmButton("approve", str(uuid4()))
        transport = _transport(allowed_users=frozenset())
        interaction = _interaction(context=_context(transport, AsyncMock()))

        assert await button.interaction_check(interaction) is False


@pytest.mark.asyncio
class TestCallback:
    async def test_approve_calls_handler_with_parsed_id(self) -> None:
        pending_id = uuid4()
        handler = AsyncMock(
            return_value=ConfirmOutcome(result="approved", message="Approved `pause`.")
        )
        button = ConfirmButton("approve", str(pending_id))
        interaction = _interaction(context=_context(_transport(), handler))

        await button.callback(interaction)

        _, kwargs = handler.await_args
        assert kwargs["pending_id"] == pending_id
        assert kwargs["decision"] == "approve"
        assert kwargs["user_id"] == "42"

    async def test_reject_passes_its_own_decision(self) -> None:
        handler = AsyncMock(return_value=ConfirmOutcome(result="rejected", message="Rejected."))
        button = ConfirmButton("reject", str(uuid4()))

        await button.callback(_interaction(context=_context(_transport(), handler)))

        _, kwargs = handler.await_args
        assert kwargs["decision"] == "reject"

    async def test_result_replaces_the_message_and_drops_the_buttons(self) -> None:
        """A decided row must not offer a second click."""
        handler = AsyncMock(
            return_value=ConfirmOutcome(result="approved", message="Approved `pause` — queued.")
        )
        button = ConfirmButton("approve", str(uuid4()))
        interaction = _interaction(context=_context(_transport(), handler))

        await button.callback(interaction)

        _, kwargs = interaction.response.edit_message.await_args
        assert kwargs["view"] is None
        embed = kwargs["embed"]
        assert embed.title == "Approved"
        assert embed.description == "Approved `pause` — queued."

    async def test_template_shaped_non_uuid_never_reaches_the_handler(self) -> None:
        """The template is a SHAPE check, not a UUID check.

        ``[0-9a-fA-F-]{36}`` happily matches 36 dashes, which parses as
        no UUID at all — so a crafted or corrupted custom_id can satisfy
        discord.py's own validation and still be garbage. The callback's
        UUID parse is what stops it before storage.
        """
        handler = AsyncMock()
        shaped_but_invalid = "-" * 36
        assert re.fullmatch(CUSTOM_ID_TEMPLATE, f"wb:confirm:approve:{shaped_but_invalid}")
        with pytest.raises(ValueError):
            UUID(shaped_but_invalid)

        button = ConfirmButton("approve", shaped_but_invalid)
        interaction = _interaction(context=_context(_transport(), handler))

        await button.callback(interaction)

        handler.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        interaction.response.edit_message.assert_not_awaited()


class TestTransportWiring:
    def test_context_is_installed_on_the_client(self) -> None:
        transport = _transport()
        client = MagicMock()
        handler = AsyncMock()

        transport.attach_client(client)
        transport.set_confirm_handler(handler)

        context = getattr(client, CONTEXT_ATTR)
        assert isinstance(context, ConfirmContext)
        assert context.transport is transport
        assert context.handler is handler
        # Registering the dynamic item is what makes post-restart clicks work.
        client.add_dynamic_items.assert_called_with(ConfirmButton)

    def test_wiring_order_does_not_matter(self) -> None:
        """set_confirm_handler may land before or after attach_client."""
        transport = _transport()
        client = MagicMock()
        handler = AsyncMock()

        transport.set_confirm_handler(handler)
        transport.attach_client(client)

        assert isinstance(getattr(client, CONTEXT_ATTR), ConfirmContext)


class TestPendingIdIsAUuid:
    """Guards the join between the button and the row."""

    def test_uuid_survives_the_string_round_trip(self) -> None:
        pending_id = uuid4()
        button = ConfirmButton("approve", str(pending_id))
        assert UUID(button.pending_id) == pending_id


@pytest.mark.asyncio
class TestDecidedCard:
    """The confirm card must not keep looking pending after a decision.

    Operator-caught on the first live click (2026-08-10): the message
    text updated but the original "Confirm command" embed stayed put, so
    a rejected request still read as awaiting an answer.
    """

    async def _decide(self, outcome: ConfirmOutcome, *, decision: str = "approve") -> MagicMock:
        button = ConfirmButton(decision, str(uuid4()))  # type: ignore[arg-type]
        handler = AsyncMock(return_value=outcome)
        interaction = _interaction(context=_context(_transport(), handler))
        # Give the interaction an original embed to carry forward.
        interaction.message = SimpleNamespace(
            embeds=[SimpleNamespace(description="`pause` on ADA/USD")]
        )
        await button.callback(interaction)
        return interaction

    async def test_approved_card_is_green_and_titled(self) -> None:
        interaction = await self._decide(
            ConfirmOutcome(result="approved", message="Approved `pause` — queued.")
        )
        _, kwargs = interaction.response.edit_message.await_args
        embed = kwargs["embed"]
        assert embed.title == "Approved"
        assert embed.color.value == COLOR_APPROVED
        # The stale "Confirm command" card is gone, not stacked beneath.
        assert kwargs["content"] is None
        assert kwargs["view"] is None

    async def test_rejected_card_is_neutral(self) -> None:
        interaction = await self._decide(
            ConfirmOutcome(result="rejected", message="Rejected `pause` — nothing ran."),
            decision="reject",
        )
        embed = interaction.response.edit_message.await_args[1]["embed"]
        assert embed.title == "Rejected"
        assert embed.color.value == COLOR_REJECTED

    async def test_undecided_outcome_reads_as_not_applied(self) -> None:
        """Colour tracks APPLIED, not which button was pressed.

        An approve that arrived after the TTL is not a success, and must
        not wear the success colour.
        """
        interaction = await self._decide(
            ConfirmOutcome(result="expired", message="The confirmation window closed.")
        )
        embed = interaction.response.edit_message.await_args[1]["embed"]
        assert embed.title == "Not applied"
        assert embed.color.value == COLOR_STALE

    async def test_card_carries_the_original_request_forward(self) -> None:
        interaction = await self._decide(
            ConfirmOutcome(result="approved", message="Approved `pause` — queued.")
        )
        embed = interaction.response.edit_message.await_args[1]["embed"]
        assert [f.value for f in embed.fields] == ["`pause` on ADA/USD"]

    async def test_missing_original_embed_is_tolerated(self) -> None:
        """Best-effort: no original description means no Request field."""
        button = ConfirmButton("approve", str(uuid4()))
        handler = AsyncMock(return_value=ConfirmOutcome(result="approved", message="Approved."))
        interaction = _interaction(context=_context(_transport(), handler))
        interaction.message = SimpleNamespace(embeds=[])

        await button.callback(interaction)

        embed = interaction.response.edit_message.await_args[1]["embed"]
        assert embed.fields == []
        assert embed.title == "Approved"
