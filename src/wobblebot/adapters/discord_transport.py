"""Discord transport for the operator interaction layer (Stage 5.2, ADR-013).

Wraps ``discord.py``'s Gateway client. Inbound Discord events (messages,
reaction add/remove) are normalized into typed ``InboundMessage`` /
``ReactionEvent`` value objects, allowlist-filtered against the
configured ``allowed_user_ids`` + ``allowed_channel_ids``, and
dispatched to registered handler callbacks. Outbound APIs cover plain
messages, rich embeds, and confirmation prompts (embed + ✅ / ❌
reactions that the operator clicks to approve or reject a pending
command).

Stage 5.2 ships the adapter standalone — Stage 5.3 wires it to the
Ollama assistant adapter, and Stage 5.6's ``cli/operator`` daemon
becomes its owner. Per ADR-013 decision 9, ``cli/live`` does NOT
import this module: outbound notifications flow through
``NotifierPort`` -> ``notifications`` SQLite table ->
``cli/operator``'s forwarder, which uses this adapter.

Design choices ratified in ``stage-5.1-design.md`` decision 8 + ADR-013:

- ``discord.py`` 2.x is the chosen library. Stable, MIT,
  actively maintained, supports the Gateway client needed for
  bidirectional chat.
- ``message_content`` Intent enabled. Required to read message text;
  must also be enabled in the Discord developer portal for the bot
  account (privileged intent since 2022).
- Bot's own messages are dropped at the allowlist filter so the bot
  cannot react to its own output.
- Empty allowlist == empty allowlist (deny-by-default). Operators
  must explicitly list ``allowed_user_ids`` and ``allowed_channel_ids``
  to enable the bot at all. No fail-open.
- The transport is concrete, not behind a ``TransportPort`` ABC. Only
  ``cli/operator`` consumes it; an abstraction would be speculative.
- Error wrapping: ``DiscordTransportError`` wraps
  ``discord.DiscordException`` and friends for callers who don't want
  to import ``discord.py`` to handle errors.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import discord
from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Constants — embed colors and reaction emojis                          #
# --------------------------------------------------------------------- #

# Colors as integer ARGB (Discord embed convention).
COLOR_INFO = 0x3498DB  # blue
COLOR_SUCCESS = 0x2ECC71  # green
COLOR_WARNING = 0xF39C12  # amber
COLOR_ERROR = 0xE74C3C  # red
COLOR_PENDING = 0xF1C40F  # yellow — for confirmation embeds awaiting operator click

CONFIRM_EMOJI = "✅"
REJECT_EMOJI = "❌"
ACK_EMOJI = "👀"  # bot acknowledges a parsed inbound message
WARN_EMOJI = "⚠️"  # bot flags an unparseable inbound message


# --------------------------------------------------------------------- #
# Config + value objects                                                #
# --------------------------------------------------------------------- #


class DiscordTransportConfig(BaseModel):
    """Operator-facing configuration for the Discord transport.

    Token comes from an env var (referenced by name here so the actual
    secret never lives in ``settings.yml``); user + channel allowlists
    are explicit. Empty allowlists = deny-by-default.
    """

    bot_token_env_var: str = Field(default="DISCORD_BOT_TOKEN", min_length=1)
    allowed_user_ids: frozenset[str] = Field(default_factory=frozenset)
    allowed_channel_ids: frozenset[str] = Field(default_factory=frozenset)

    class Config:
        frozen = True


class InboundMessage(BaseModel):
    """One Discord message normalized for the transport's handler protocol."""

    message_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    content: str
    timestamp: Timestamp

    class Config:
        frozen = True


class ReactionEvent(BaseModel):
    """One reaction add/remove event normalized for the handler protocol."""

    message_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    emoji: str = Field(min_length=1)
    action: Literal["add", "remove"]
    timestamp: Timestamp

    class Config:
        frozen = True


class DiscordTransportError(Exception):
    """Raised when a Discord transport operation fails.

    Wraps protocol / API / channel-resolution failures so callers don't
    need to import ``discord.py`` exception types. The ``__cause__``
    chain preserves the original error for forensic debugging.
    """


# --------------------------------------------------------------------- #
# Handler protocols                                                     #
# --------------------------------------------------------------------- #


MessageHandler = Callable[[InboundMessage], Awaitable[None]]
ReactionHandler = Callable[[ReactionEvent], Awaitable[None]]


# --------------------------------------------------------------------- #
# Adapter                                                               #
# --------------------------------------------------------------------- #


class DiscordTransport:  # pylint: disable=too-many-instance-attributes
    """Bidirectional Discord transport.

    Construct with a ``DiscordTransportConfig``, register zero-or-more
    inbound handlers via ``on_message`` / ``on_reaction``, then call
    ``start()`` to connect to the Gateway and ``close()`` to disconnect.

    For tests, ``receive_message`` and ``receive_reaction`` can be
    invoked directly with synthetic events — no Gateway connection
    needed. For outbound test coverage, inject a mock ``discord.Client``
    via ``attach_client`` before exercising the send methods.
    """

    def __init__(self, config: DiscordTransportConfig) -> None:
        self._config = config
        self._client: discord.Client | None = None
        self._message_handlers: list[MessageHandler] = []
        self._reaction_handlers: list[ReactionHandler] = []
        self._bot_user_id: str | None = None

    # ---- handler registration ------------------------------------- #

    def on_message(self, handler: MessageHandler) -> None:
        """Register a handler to receive every allowlisted inbound message."""
        self._message_handlers.append(handler)

    def on_reaction(self, handler: ReactionHandler) -> None:
        """Register a handler to receive every allowlisted reaction event."""
        self._reaction_handlers.append(handler)

    # ---- allowlist + dispatch ------------------------------------- #

    def is_allowed(self, user_id: str, channel_id: str) -> bool:
        """Return True iff ``(user_id, channel_id)`` passes the allowlist.

        Empty allowlists are deny-by-default — the operator must opt in
        explicitly. The bot's own user id is always rejected so the bot
        cannot trigger itself.
        """
        if self._bot_user_id is not None and user_id == self._bot_user_id:
            return False
        if not self._config.allowed_user_ids:
            return False
        if user_id not in self._config.allowed_user_ids:
            return False
        if not self._config.allowed_channel_ids:
            return False
        if channel_id not in self._config.allowed_channel_ids:
            return False
        return True

    async def receive_message(self, message: InboundMessage) -> None:
        """Dispatch an inbound message to handlers (after allowlist filter).

        Exceptions raised by individual handlers are logged and swallowed
        so one bad handler cannot starve the others.
        """
        if not self.is_allowed(message.user_id, message.channel_id):
            LOGGER.debug(
                "dropped non-allowlisted message",
                extra={"user_id": message.user_id, "channel_id": message.channel_id},
            )
            return
        for handler in self._message_handlers:
            try:
                await handler(message)
            except Exception:  # pylint: disable=broad-exception-caught
                LOGGER.exception(
                    "message handler raised; continuing",
                    extra={"message_id": message.message_id},
                )

    async def receive_reaction(self, event: ReactionEvent) -> None:
        """Dispatch a reaction event to handlers (after allowlist filter)."""
        if not self.is_allowed(event.user_id, event.channel_id):
            LOGGER.debug(
                "dropped non-allowlisted reaction",
                extra={"user_id": event.user_id, "channel_id": event.channel_id},
            )
            return
        for handler in self._reaction_handlers:
            try:
                await handler(event)
            except Exception:  # pylint: disable=broad-exception-caught
                LOGGER.exception(
                    "reaction handler raised; continuing",
                    extra={"message_id": event.message_id, "action": event.action},
                )

    # ---- outbound ------------------------------------------------- #

    async def send_message(self, channel_id: str, content: str) -> str:
        """Send a plain-text message. Returns the new message's id."""
        channel = await self._resolve_text_channel(channel_id)
        try:
            message = await channel.send(content=content)
        except discord.DiscordException as exc:
            raise DiscordTransportError(
                f"Failed to send message to channel {channel_id}: {exc}"
            ) from exc
        return str(message.id)

    async def send_embed(  # pylint: disable=too-many-arguments
        self,
        channel_id: str,
        *,
        title: str,
        description: str,
        color: int = COLOR_INFO,
        fields: list[tuple[str, str]] | None = None,
        footer: str | None = None,
    ) -> str:
        """Send a rich embed. Returns the new message's id."""
        channel = await self._resolve_text_channel(channel_id)
        embed = discord.Embed(title=title, description=description, color=color)
        for name, value in fields or []:
            embed.add_field(name=name, value=value, inline=False)
        if footer:
            embed.set_footer(text=footer)
        try:
            message = await channel.send(embed=embed)
        except discord.DiscordException as exc:
            raise DiscordTransportError(
                f"Failed to send embed to channel {channel_id}: {exc}"
            ) from exc
        return str(message.id)

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """Add a reaction emoji to an existing message in ``channel_id``.

        Used by ``cli/operator`` to acknowledge inbound messages — a
        lightweight signal that the bot saw + parsed the message,
        without consuming an outbound message slot in the channel.

        Wraps the message fetch (``channel.fetch_message``) and the
        reaction add (``message.add_reaction``); both can raise
        ``discord.DiscordException`` which is re-wrapped as
        ``DiscordTransportError``.
        """
        channel = await self._resolve_text_channel(channel_id)
        try:
            numeric_msg_id = int(message_id)
        except ValueError as exc:
            raise DiscordTransportError(f"Message id {message_id!r} is not numeric") from exc
        try:
            message = await channel.fetch_message(numeric_msg_id)
            await message.add_reaction(emoji)
        except discord.DiscordException as exc:
            raise DiscordTransportError(
                f"Failed to add reaction to message {message_id}: {exc}"
            ) from exc

    async def send_confirmation(
        self,
        channel_id: str,
        *,
        summary: str,
        ref_id: str,
    ) -> str:
        """Post a confirmation embed with ✅ / ❌ reactions.

        The caller correlates the eventual reaction event back to its
        originating pending-command row via ``ref_id`` (rendered in the
        embed footer) AND via the returned ``message_id`` (the operator
        service stores the mapping table). Either is a valid join key.
        """
        channel = await self._resolve_text_channel(channel_id)
        embed = discord.Embed(
            title="Confirm command",
            description=summary,
            color=COLOR_PENDING,
        )
        embed.set_footer(text=f"id: {ref_id}  •  react ✅ to approve, ❌ to reject")
        try:
            message = await channel.send(embed=embed)
            await message.add_reaction(CONFIRM_EMOJI)
            await message.add_reaction(REJECT_EMOJI)
        except discord.DiscordException as exc:
            raise DiscordTransportError(
                f"Failed to send confirmation to channel {channel_id}: {exc}"
            ) from exc
        return str(message.id)

    # ---- lifecycle ------------------------------------------------ #

    async def start(self) -> None:
        """Connect to the Discord Gateway and run until ``close()``.

        Reads the bot token from the env var named by
        ``config.bot_token_env_var``; raises ``DiscordTransportError`` if
        the env var is missing or empty.
        """
        token = os.environ.get(self._config.bot_token_env_var, "").strip()
        if not token:
            raise DiscordTransportError(
                f"Bot token env var {self._config.bot_token_env_var!r} is not set"
            )

        intents = discord.Intents.default()
        intents.message_content = True  # privileged; enable in Discord developer portal

        client = discord.Client(intents=intents)
        self.attach_client(client)
        try:
            await client.start(token)
        except discord.LoginFailure as exc:
            raise DiscordTransportError(f"Discord login failed: {exc}") from exc

    async def close(self) -> None:
        """Disconnect from the Gateway. Idempotent."""
        if self._client is None:
            return
        await self._client.close()
        self._client = None
        self._bot_user_id = None

    def attach_client(self, client: discord.Client) -> None:
        """Bind a ``discord.Client`` instance and register event handlers.

        Public so tests can inject a mock client without going through
        ``start()`` (which requires a real token + Gateway connection).
        """
        self._client = client
        self._bind_events(client)

    # ---- internals ------------------------------------------------ #

    def _require_client(self) -> discord.Client:
        if self._client is None:
            raise DiscordTransportError("Transport not started; call start() first")
        return self._client

    async def _resolve_text_channel(self, channel_id: str) -> Any:
        client = self._require_client()
        try:
            numeric_id = int(channel_id)
        except ValueError as exc:
            raise DiscordTransportError(f"Channel id {channel_id!r} is not numeric") from exc
        channel = client.get_channel(numeric_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(numeric_id)
            except discord.DiscordException as exc:
                raise DiscordTransportError(
                    f"Channel {channel_id} could not be resolved: {exc}"
                ) from exc
        if not hasattr(channel, "send"):
            raise DiscordTransportError(
                f"Channel {channel_id} is not a sendable text channel "
                f"({type(channel).__name__})"
            )
        return channel

    def _bind_events(self, client: discord.Client) -> None:
        transport = self

        @client.event
        async def on_ready() -> None:  # pragma: no cover — Gateway-bound
            # pylint: disable=protected-access
            transport._bot_user_id = str(client.user.id) if client.user else None
            LOGGER.info(
                "discord transport connected",
                extra={"bot_user_id": transport._bot_user_id},
            )

        @client.event
        async def on_message(message: discord.Message) -> None:  # pragma: no cover
            await transport.receive_message(_inbound_from_message(message))

        @client.event
        async def on_raw_reaction_add(
            payload: discord.RawReactionActionEvent,
        ) -> None:  # pragma: no cover
            await transport.receive_reaction(_reaction_from_payload(payload, "add"))

        @client.event
        async def on_raw_reaction_remove(
            payload: discord.RawReactionActionEvent,
        ) -> None:  # pragma: no cover
            await transport.receive_reaction(_reaction_from_payload(payload, "remove"))


# --------------------------------------------------------------------- #
# discord.py event -> value object conversion (module-level helpers)    #
# --------------------------------------------------------------------- #


def _inbound_from_message(message: discord.Message) -> InboundMessage:
    return InboundMessage(
        message_id=str(message.id),
        channel_id=str(message.channel.id),
        user_id=str(message.author.id),
        content=message.content,
        timestamp=Timestamp(dt=message.created_at),
    )


def _reaction_from_payload(
    payload: discord.RawReactionActionEvent,
    action: Literal["add", "remove"],
) -> ReactionEvent:
    return ReactionEvent(
        message_id=str(payload.message_id),
        channel_id=str(payload.channel_id),
        user_id=str(payload.user_id),
        emoji=str(payload.emoji),
        action=action,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
