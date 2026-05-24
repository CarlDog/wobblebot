"""Unit tests for the Discord transport (Stage 5.2, ADR-013).

The adapter wraps ``discord.py``'s Gateway client. Tests cover the
testable seams without an actual Gateway connection:

- Config + value-object construction / frozenness / validation.
- ``is_allowed`` allowlist semantics (deny-by-default, bot self-rejection).
- ``receive_message`` / ``receive_reaction`` handler dispatch + filtering +
  exception swallowing.
- ``send_message`` / ``send_embed`` / ``send_confirmation`` against a
  mock ``discord.Client`` injected via ``attach_client``.
- ``start`` token-env-var validation.
- ``close`` idempotency.
- ``_resolve_text_channel`` fallback path (``get_channel`` returns
  ``None`` -> ``fetch_channel``).

The discord.py event handlers themselves (``on_message``, etc.) are
covered by ``# pragma: no cover`` in the adapter — they're a thin
shim over already-tested logic and can only meaningfully run against
a real Gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import ValidationError

from wobblebot.adapters.discord_transport import (
    COLOR_PENDING,
    CONFIRM_EMOJI,
    REJECT_EMOJI,
    DiscordTransport,
    DiscordTransportConfig,
    DiscordTransportError,
    InboundMessage,
    ReactionEvent,
)
from wobblebot.domain.value_objects import Timestamp

pytestmark = pytest.mark.unit


def _ts() -> Timestamp:
    return Timestamp(dt=datetime.now(UTC))


def _inbound(
    *,
    user_id: str = "42",
    channel_id: str = "100",
    content: str = "hi",
    message_id: str = "m-1",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        channel_id=channel_id,
        user_id=user_id,
        content=content,
        timestamp=_ts(),
    )


def _reaction(
    *,
    user_id: str = "42",
    channel_id: str = "100",
    emoji: str = CONFIRM_EMOJI,
    action: str = "add",
    message_id: str = "m-1",
) -> ReactionEvent:
    return ReactionEvent(
        message_id=message_id,
        channel_id=channel_id,
        user_id=user_id,
        emoji=emoji,
        action=action,  # type: ignore[arg-type]
        timestamp=_ts(),
    )


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


# --------------------------------------------------------------------- #
# Config + value objects                                                #
# --------------------------------------------------------------------- #


class TestDiscordTransportConfig:
    def test_defaults(self) -> None:
        cfg = DiscordTransportConfig()
        assert cfg.bot_token_env_var == "DISCORD_BOT_TOKEN"
        assert cfg.allowed_user_ids == frozenset()
        assert cfg.allowed_channel_ids == frozenset()

    def test_construct_with_allowlists(self) -> None:
        cfg = DiscordTransportConfig(
            allowed_user_ids=frozenset({"a", "b"}),
            allowed_channel_ids=frozenset({"c"}),
        )
        assert cfg.allowed_user_ids == frozenset({"a", "b"})
        assert cfg.allowed_channel_ids == frozenset({"c"})

    def test_frozen(self) -> None:
        cfg = DiscordTransportConfig()
        with pytest.raises(ValidationError):
            cfg.bot_token_env_var = "OTHER"  # type: ignore[misc]

    def test_token_env_var_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            DiscordTransportConfig(bot_token_env_var="")


class TestInboundMessage:
    def test_construct(self) -> None:
        msg = _inbound()
        assert msg.message_id == "m-1"
        assert msg.content == "hi"

    def test_frozen(self) -> None:
        msg = _inbound()
        with pytest.raises(ValidationError):
            msg.content = "other"  # type: ignore[misc]

    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            InboundMessage(
                message_id="",
                channel_id="100",
                user_id="42",
                content="x",
                timestamp=_ts(),
            )


class TestReactionEvent:
    def test_add(self) -> None:
        evt = _reaction()
        assert evt.action == "add"

    def test_remove(self) -> None:
        evt = _reaction(action="remove")
        assert evt.action == "remove"

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValidationError):
            _reaction(action="bounce")

    def test_frozen(self) -> None:
        evt = _reaction()
        with pytest.raises(ValidationError):
            evt.emoji = "different"  # type: ignore[misc]


# --------------------------------------------------------------------- #
# is_allowed                                                            #
# --------------------------------------------------------------------- #


class TestIsAllowed:
    def test_user_and_channel_match(self) -> None:
        t = _transport()
        assert t.is_allowed("42", "100") is True

    def test_user_not_in_allowlist(self) -> None:
        t = _transport()
        assert t.is_allowed("999", "100") is False

    def test_channel_not_in_allowlist(self) -> None:
        t = _transport()
        assert t.is_allowed("42", "999") is False

    def test_empty_user_allowlist_denies(self) -> None:
        t = _transport(allowed_users=frozenset())
        assert t.is_allowed("42", "100") is False

    def test_empty_channel_allowlist_denies(self) -> None:
        t = _transport(allowed_channels=frozenset())
        assert t.is_allowed("42", "100") is False

    def test_bot_self_rejected(self) -> None:
        t = _transport(allowed_users=frozenset({"BOT-1", "42"}))
        t._bot_user_id = "BOT-1"  # pylint: disable=protected-access
        assert t.is_allowed("BOT-1", "100") is False
        assert t.is_allowed("42", "100") is True


# --------------------------------------------------------------------- #
# receive_message / receive_reaction                                    #
# --------------------------------------------------------------------- #


class TestReceiveMessage:
    @pytest.mark.asyncio
    async def test_dispatches_to_registered_handler(self) -> None:
        t = _transport()
        received: list[InboundMessage] = []

        async def handler(m: InboundMessage) -> None:
            received.append(m)

        t.on_message(handler)
        msg = _inbound()
        await t.receive_message(msg)
        assert received == [msg]

    @pytest.mark.asyncio
    async def test_drops_non_allowlisted(self) -> None:
        t = _transport()
        called: list[InboundMessage] = []

        async def handler(m: InboundMessage) -> None:
            called.append(m)

        t.on_message(handler)
        await t.receive_message(_inbound(channel_id="999"))
        assert called == []

    @pytest.mark.asyncio
    async def test_multiple_handlers_all_called(self) -> None:
        t = _transport()
        order: list[str] = []

        async def h1(_: InboundMessage) -> None:
            order.append("h1")

        async def h2(_: InboundMessage) -> None:
            order.append("h2")

        t.on_message(h1)
        t.on_message(h2)
        await t.receive_message(_inbound())
        assert order == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_handler_exception_swallowed(self) -> None:
        t = _transport()
        survivor: list[InboundMessage] = []

        async def angry(_: InboundMessage) -> None:
            raise RuntimeError("boom")

        async def quiet(m: InboundMessage) -> None:
            survivor.append(m)

        t.on_message(angry)
        t.on_message(quiet)
        msg = _inbound()
        await t.receive_message(msg)  # should NOT raise
        assert survivor == [msg]


class TestReceiveReaction:
    @pytest.mark.asyncio
    async def test_dispatches_to_registered_handler(self) -> None:
        t = _transport()
        received: list[ReactionEvent] = []

        async def handler(e: ReactionEvent) -> None:
            received.append(e)

        t.on_reaction(handler)
        evt = _reaction()
        await t.receive_reaction(evt)
        assert received == [evt]

    @pytest.mark.asyncio
    async def test_drops_non_allowlisted(self) -> None:
        t = _transport()
        called: list[ReactionEvent] = []

        async def handler(e: ReactionEvent) -> None:
            called.append(e)

        t.on_reaction(handler)
        await t.receive_reaction(_reaction(user_id="999"))
        assert called == []

    @pytest.mark.asyncio
    async def test_handler_exception_swallowed(self) -> None:
        t = _transport()
        survivor: list[ReactionEvent] = []

        async def angry(_: ReactionEvent) -> None:
            raise RuntimeError("boom")

        async def quiet(e: ReactionEvent) -> None:
            survivor.append(e)

        t.on_reaction(angry)
        t.on_reaction(quiet)
        evt = _reaction()
        await t.receive_reaction(evt)
        assert survivor == [evt]


# --------------------------------------------------------------------- #
# Outbound (mocked client + channel)                                    #
# --------------------------------------------------------------------- #


def _mock_client(channel: Any) -> Any:
    """Build a mock ``discord.Client`` whose ``get_channel`` returns ``channel``."""
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    client.close = AsyncMock(return_value=None)
    return client


def _mock_channel(*, message_id: int = 12345) -> Any:
    """Build a mock text channel.

    ``send()`` returns a mock message with an integer ``id`` attribute
    and an ``add_reaction`` coroutine.
    """
    sent_message = MagicMock()
    sent_message.id = message_id
    sent_message.add_reaction = AsyncMock(return_value=None)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_message)
    return channel


class TestOutbound:
    @pytest.mark.asyncio
    async def test_send_message_returns_message_id(self) -> None:
        t = _transport()
        channel = _mock_channel(message_id=789)
        t.attach_client(_mock_client(channel))
        msg_id = await t.send_message("100", "hello")
        assert msg_id == "789"
        channel.send.assert_awaited_once_with(content="hello")

    @pytest.mark.asyncio
    async def test_send_embed_builds_correct_payload(self) -> None:
        t = _transport()
        channel = _mock_channel(message_id=42)
        t.attach_client(_mock_client(channel))
        msg_id = await t.send_embed(
            "100",
            title="Status",
            description="all green",
            fields=[("BTC/USD", "active (6 orders)")],
            footer="footer here",
        )
        assert msg_id == "42"
        # The embed should have been built and passed to channel.send
        _, kwargs = channel.send.call_args
        embed = kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert embed.title == "Status"
        assert embed.description == "all green"
        assert embed.fields[0].name == "BTC/USD"
        assert embed.fields[0].value == "active (6 orders)"
        assert embed.footer.text == "footer here"

    @pytest.mark.asyncio
    async def test_send_confirmation_adds_both_reactions(self) -> None:
        t = _transport()
        channel = _mock_channel(message_id=999)
        t.attach_client(_mock_client(channel))
        msg_id = await t.send_confirmation("100", summary="Pause BTC/USD", ref_id="pc-abc")
        assert msg_id == "999"
        # Embed sent
        _, kwargs = channel.send.call_args
        embed = kwargs["embed"]
        assert embed.title == "Confirm command"
        assert embed.description == "Pause BTC/USD"
        assert embed.color.value == COLOR_PENDING
        assert "pc-abc" in (embed.footer.text or "")
        # Both reactions added in order
        sent_message = channel.send.return_value
        sent_message.add_reaction.assert_any_await(CONFIRM_EMOJI)
        sent_message.add_reaction.assert_any_await(REJECT_EMOJI)
        assert sent_message.add_reaction.await_count == 2

    @pytest.mark.asyncio
    async def test_add_reaction_adds_to_existing_message(self) -> None:
        t = _transport()
        channel = _mock_channel()
        existing_message = MagicMock()
        existing_message.add_reaction = AsyncMock(return_value=None)
        channel.fetch_message = AsyncMock(return_value=existing_message)
        t.attach_client(_mock_client(channel))
        await t.add_reaction("100", "555", "✅")
        channel.fetch_message.assert_awaited_once_with(555)
        existing_message.add_reaction.assert_awaited_once_with("✅")

    @pytest.mark.asyncio
    async def test_add_reaction_non_numeric_message_id_raises(self) -> None:
        t = _transport()
        t.attach_client(_mock_client(_mock_channel()))
        with pytest.raises(DiscordTransportError, match="not numeric"):
            await t.add_reaction("100", "not-a-number", "✅")

    @pytest.mark.asyncio
    async def test_add_reaction_wraps_discord_exception(self) -> None:
        t = _transport()
        channel = _mock_channel()
        channel.fetch_message = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        t.attach_client(_mock_client(channel))
        with pytest.raises(DiscordTransportError, match="Failed to add reaction"):
            await t.add_reaction("100", "555", "✅")

    @pytest.mark.asyncio
    async def test_send_without_client_raises(self) -> None:
        t = _transport()
        with pytest.raises(DiscordTransportError, match="not started"):
            await t.send_message("100", "x")

    @pytest.mark.asyncio
    async def test_send_to_non_numeric_channel_raises(self) -> None:
        t = _transport()
        t.attach_client(_mock_client(_mock_channel()))
        with pytest.raises(DiscordTransportError, match="not numeric"):
            await t.send_message("not-a-number", "x")

    @pytest.mark.asyncio
    async def test_send_wraps_discord_exception(self) -> None:
        t = _transport()
        channel = _mock_channel()
        channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        t.attach_client(_mock_client(channel))
        with pytest.raises(DiscordTransportError, match="Failed to send message"):
            await t.send_message("100", "x")

    @pytest.mark.asyncio
    async def test_resolve_channel_falls_back_to_fetch(self) -> None:
        t = _transport()
        channel = _mock_channel()
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(return_value=channel)
        client.close = AsyncMock(return_value=None)
        t.attach_client(client)
        msg_id = await t.send_message("100", "hi")
        assert msg_id  # message id string
        client.fetch_channel.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_resolve_channel_non_text_channel_raises(self) -> None:
        t = _transport()
        # A "channel" with no ``send`` attribute — simulates voice or category.
        bad_channel = object()
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=bad_channel)
        client.fetch_channel = AsyncMock(return_value=bad_channel)
        client.close = AsyncMock(return_value=None)
        t.attach_client(client)
        with pytest.raises(DiscordTransportError, match="not a sendable"):
            await t.send_message("100", "x")


# --------------------------------------------------------------------- #
# Lifecycle                                                             #
# --------------------------------------------------------------------- #


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_without_token_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        t = _transport()
        with pytest.raises(DiscordTransportError, match="not set"):
            await t.start()

    @pytest.mark.asyncio
    async def test_start_empty_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "   ")
        t = _transport()
        with pytest.raises(DiscordTransportError, match="not set"):
            await t.start()

    @pytest.mark.asyncio
    async def test_close_idempotent_without_client(self) -> None:
        t = _transport()
        await t.close()  # no-op
        await t.close()  # still no-op

    @pytest.mark.asyncio
    async def test_close_closes_client(self) -> None:
        t = _transport()
        client = _mock_client(_mock_channel())
        t.attach_client(client)
        await t.close()
        client.close.assert_awaited_once()
        # second close is a no-op
        await t.close()
        client.close.assert_awaited_once()
