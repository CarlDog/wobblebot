"""Tests for cli/operator daemon (Stage 5.6.C).

The full Gateway lifecycle is integration territory (Stage 5.7).
These unit tests target the testable seams: notification forwarder,
inbound-message routing through each OperatorIntent variant, and
reaction → pending-command transition.

The Discord transport is mocked at the class level — tests call the
handler functions directly with synthetic ``InboundMessage`` /
``ReactionEvent`` objects and verify side effects (pending rows
persisted, embeds sent, conversation turns saved).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.discord_transport import (
    CONFIRM_EMOJI,
    REJECT_EMOJI,
    DiscordTransport,
    InboundMessage,
    ReactionEvent,
)
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.operator import (
    _backfill_history_for_channel,
    _compose_engine_state_snapshot,
    _forward_pending_notifications,
    _handle_inbound_message,
    _handle_reaction,
    _summarize_command,
)
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.assistant import (
    AssistantPort,
    ConversationContext,
    ConversationTurn,
)
from wobblebot.ports.notifier import Notification
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    IntentQuery,
    IntentUnparseable,
    OperatorIntent,
    PauseCommand,
    StatusQuery,
    StopCommand,
)
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class _StubAssistant(AssistantPort):
    """AssistantPort stub returning a pre-set intent."""

    def __init__(self, intent: OperatorIntent) -> None:
        self.intent = intent
        self.contexts: list[ConversationContext] = []

    async def parse_intent(self, context: ConversationContext) -> OperatorIntent:
        self.contexts.append(context)
        return self.intent

    async def summarize(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 2048
    ) -> str:
        return "stub summary"


def _mock_transport() -> Any:
    """Build a MagicMock with the DiscordTransport methods we use."""
    t = MagicMock(spec=DiscordTransport)
    t.send_message = AsyncMock(return_value="msg-id")
    t.send_embed = AsyncMock(return_value="embed-id")
    t.send_confirmation = AsyncMock(return_value="confirm-msg-id")
    t.add_reaction = AsyncMock(return_value=None)
    return t


def _operator_service(storage: SQLiteStorageAdapter) -> OperatorService:
    exchange = MockExchangeAdapter(starting_balances={}, starting_prices={})
    engine = GridEngine(exchange, storage, grid_config(), safety_config())
    return OperatorService(
        engine=engine,
        storage=storage,
        active_symbols=(Symbol(base="BTC", quote="USD"),),
        grid_config=grid_config(),
    )


def _inbound(
    *,
    content: str = "hello",
    channel_id: str = "C-1",
    user_id: str = "U-1",
    message_id: str = "m-1",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        channel_id=channel_id,
        user_id=user_id,
        content=content,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


# --------------------------------------------------------------------- #
# _summarize_command                                                    #
# --------------------------------------------------------------------- #


class TestSummarizeCommand:
    async def test_pause_includes_symbol(self) -> None:
        intent = IntentCommand(command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")))
        assert "BTC/USD" in _summarize_command(intent)
        assert "pause" in _summarize_command(intent)

    async def test_stop_no_args(self) -> None:
        intent = IntentCommand(command=StopCommand())
        summary = _summarize_command(intent)
        assert "stop" in summary


# --------------------------------------------------------------------- #
# _forward_pending_notifications                                        #
# --------------------------------------------------------------------- #


class TestForwarder:
    async def test_empty_table_returns_zero(self, storage: SQLiteStorageAdapter) -> None:
        transport = _mock_transport()
        forwarded = await _forward_pending_notifications(
            storage=storage, transport=transport, channel_id="100"
        )
        assert forwarded == 0
        transport.send_embed.assert_not_called()

    async def test_forwards_unforwarded_rows(self, storage: SQLiteStorageAdapter) -> None:
        for level in ("info", "warning", "error"):
            await storage.save_notification(
                Notification(
                    level=level,  # type: ignore[arg-type]
                    title=f"event {level}",
                    message=f"msg {level}",
                    timestamp=Timestamp(dt=datetime.now(UTC)),
                    context={"k": level},
                )
            )
        transport = _mock_transport()
        forwarded = await _forward_pending_notifications(
            storage=storage, transport=transport, channel_id="100"
        )
        assert forwarded == 3
        assert transport.send_embed.await_count == 3

        # All rows now marked forwarded; second pass is a no-op
        forwarded_again = await _forward_pending_notifications(
            storage=storage, transport=transport, channel_id="100"
        )
        assert forwarded_again == 0

    async def test_per_row_failure_does_not_abort_batch(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.adapters.discord_transport import DiscordTransportError

        for idx in range(3):
            await storage.save_notification(
                Notification(
                    level="info",
                    title=f"event-{idx}",
                    message="msg",
                    timestamp=Timestamp(dt=datetime.now(UTC)),
                )
            )
        transport = _mock_transport()
        # Make the SECOND send_embed fail
        side_effects: list[Any] = [
            "msg-a",
            DiscordTransportError("rate limited"),
            "msg-c",
        ]
        transport.send_embed = AsyncMock(side_effect=side_effects)

        forwarded = await _forward_pending_notifications(
            storage=storage, transport=transport, channel_id="100"
        )
        # Two succeeded; one failed (still marked unforwarded for retry).
        assert forwarded == 2
        unforwarded = await storage.get_notifications(forwarded=False)
        assert len(unforwarded) == 1


# --------------------------------------------------------------------- #
# _handle_inbound_message — routing                                     #
# --------------------------------------------------------------------- #


class TestHandleInboundMessage:
    async def test_command_intent_persists_pending_and_posts_confirm(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        intent: OperatorIntent = IntentCommand(
            command=PauseCommand(symbol=Symbol(base="BTC", quote="USD"))
        )
        transport = _mock_transport()
        pending_map: dict[str, UUID] = {}
        await _handle_inbound_message(
            _inbound(content="pause BTC"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map=pending_map,
            assistant_model_name="test-model",
        )

        # Pending command persisted with awaiting_confirmation
        pendings = await storage.get_pending_commands()
        assert len(pendings) == 1
        assert pendings[0].status == "awaiting_confirmation"
        # Confirmation embed posted
        transport.send_confirmation.assert_awaited_once()
        # In-memory map populated
        assert "confirm-msg-id" in pending_map
        assert pending_map["confirm-msg-id"] == pendings[0].id
        # Operator turn + assistant turn persisted
        turns = await storage.get_conversation_turns("C-1", "U-1")
        roles = [t.role for t in turns]
        assert "operator" in roles
        assert "assistant" in roles

    async def test_query_intent_calls_operator_service_and_posts_embed(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        intent: OperatorIntent = IntentQuery(query=StatusQuery())
        transport = _mock_transport()
        await _handle_inbound_message(
            _inbound(content="status?"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map={},
            assistant_model_name="test-model",
        )
        # Query result posted as embed
        transport.send_embed.assert_awaited_once()
        # No pending_command persisted (query is read-only)
        pendings = await storage.get_pending_commands()
        assert pendings == []

    async def test_conversational_intent_sends_reply(self, storage: SQLiteStorageAdapter) -> None:
        intent: OperatorIntent = IntentConversational(reply_text="you're welcome")
        transport = _mock_transport()
        await _handle_inbound_message(
            _inbound(content="thanks"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map={},
            assistant_model_name="test-model",
        )
        transport.send_message.assert_awaited_once_with("100", "you're welcome")

    async def test_unparseable_intent_surfaces_reason(self, storage: SQLiteStorageAdapter) -> None:
        intent: OperatorIntent = IntentUnparseable(reason="no symbol XYZ")
        transport = _mock_transport()
        await _handle_inbound_message(
            _inbound(content="wibble"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map={},
            assistant_model_name="test-model",
        )
        transport.send_message.assert_awaited_once()
        args, _ = transport.send_message.await_args
        assert "no symbol XYZ" in args[1]

    async def test_parsed_intent_reacts_with_ack_emoji(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.adapters.discord_transport import ACK_EMOJI

        intent: OperatorIntent = IntentConversational(reply_text="hi")
        transport = _mock_transport()
        await _handle_inbound_message(
            _inbound(content="hello", message_id="msg-abc", channel_id="C-9"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map={},
            assistant_model_name="test-model",
        )
        transport.add_reaction.assert_awaited_once_with("C-9", "msg-abc", ACK_EMOJI)

    async def test_unparseable_intent_reacts_with_warn_emoji(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.adapters.discord_transport import WARN_EMOJI

        intent: OperatorIntent = IntentUnparseable(reason="huh")
        transport = _mock_transport()
        await _handle_inbound_message(
            _inbound(content="wibble", message_id="msg-xyz", channel_id="C-7"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map={},
            assistant_model_name="test-model",
        )
        transport.add_reaction.assert_awaited_once_with("C-7", "msg-xyz", WARN_EMOJI)

    async def test_query_embed_footer_contains_model_name(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        intent: OperatorIntent = IntentQuery(query=StatusQuery())
        transport = _mock_transport()
        await _handle_inbound_message(
            _inbound(content="status"),
            operator_storage=storage,
            live_storage=None,
            observe_storage=None,
            active_symbols=(),
            assistant=_StubAssistant(intent),
            operator_service=_operator_service(storage),
            transport=transport,
            outbound_channel_id="100",
            context_window_turns=10,
            confirm_ttl_seconds=300,
            pending_message_map={},
            assistant_model_name="claude-sonnet-4-6",
        )
        transport.send_embed.assert_awaited_once()
        _, kwargs = transport.send_embed.await_args
        assert "claude-sonnet-4-6" in kwargs["footer"]


# --------------------------------------------------------------------- #
# _handle_reaction — confirm / reject transitions                       #
# --------------------------------------------------------------------- #


class TestHandleReaction:
    async def test_confirm_transitions_to_approved(self, storage: SQLiteStorageAdapter) -> None:
        # Seed an awaiting_confirmation row + the in-memory map
        from wobblebot.ports.operator import PendingCommand

        pending_id = uuid4()
        pending = PendingCommand(
            id=pending_id,
            command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
            status="awaiting_confirmation",
            channel_id="C-1",
            requesting_user_id="U-1",
            ttl_expires_at=Timestamp(dt=datetime.now(UTC)),
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        await storage.save_pending_command(pending)
        pending_map: dict[str, UUID] = {"msg-1": pending_id}

        event = ReactionEvent(
            message_id="msg-1",
            channel_id="C-1",
            user_id="U-2",  # different operator confirms
            emoji=CONFIRM_EMOJI,
            action="add",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
        await _handle_reaction(event, operator_storage=storage, pending_message_map=pending_map)
        fetched = await storage.get_pending_command(pending_id)
        assert fetched is not None
        assert fetched.status == "approved"
        assert fetched.confirming_user_id == "U-2"

    async def test_reject_transitions_to_rejected(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.ports.operator import PendingCommand

        pending_id = uuid4()
        pending = PendingCommand(
            id=pending_id,
            command=StopCommand(),
            status="awaiting_confirmation",
            channel_id="C-1",
            requesting_user_id="U-1",
            ttl_expires_at=Timestamp(dt=datetime.now(UTC)),
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        await storage.save_pending_command(pending)
        pending_map: dict[str, UUID] = {"msg-1": pending_id}

        event = ReactionEvent(
            message_id="msg-1",
            channel_id="C-1",
            user_id="U-2",
            emoji=REJECT_EMOJI,
            action="add",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
        await _handle_reaction(event, operator_storage=storage, pending_message_map=pending_map)
        fetched = await storage.get_pending_command(pending_id)
        assert fetched is not None
        assert fetched.status == "rejected"

    async def test_unknown_message_id_ignored(self, storage: SQLiteStorageAdapter) -> None:
        # Empty map; reaction does not crash
        event = ReactionEvent(
            message_id="unknown",
            channel_id="C-1",
            user_id="U-2",
            emoji=CONFIRM_EMOJI,
            action="add",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
        await _handle_reaction(event, operator_storage=storage, pending_message_map={})
        # No exception is the assertion

    async def test_double_reaction_does_not_overwrite(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.ports.operator import PendingCommand

        pending_id = uuid4()
        pending = PendingCommand(
            id=pending_id,
            command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")),
            status="approved",  # already approved
            channel_id="C-1",
            requesting_user_id="U-1",
            confirming_user_id="U-2",
            confirmed_at=Timestamp(dt=datetime.now(UTC)),
            ttl_expires_at=Timestamp(dt=datetime.now(UTC)),
            created_at=Timestamp(dt=datetime.now(UTC)),
        )
        await storage.save_pending_command(pending)
        pending_map: dict[str, UUID] = {"msg-1": pending_id}

        # A REJECT after an APPROVE must not flip the status.
        event = ReactionEvent(
            message_id="msg-1",
            channel_id="C-1",
            user_id="U-3",
            emoji=REJECT_EMOJI,
            action="add",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
        await _handle_reaction(event, operator_storage=storage, pending_message_map=pending_map)
        fetched = await storage.get_pending_command(pending_id)
        assert fetched is not None
        assert fetched.status == "approved"  # unchanged

    async def test_remove_action_ignored(self, storage: SQLiteStorageAdapter) -> None:
        # We only care about reaction add — remove events are no-ops
        event = ReactionEvent(
            message_id="msg-1",
            channel_id="C-1",
            user_id="U-2",
            emoji=CONFIRM_EMOJI,
            action="remove",
            timestamp=Timestamp(dt=datetime.now(UTC)),
        )
        await _handle_reaction(
            event, operator_storage=storage, pending_message_map={"msg-1": uuid4()}
        )
        # No exception


# --------------------------------------------------------------------- #
# _backfill_history_for_channel — startup conversation_turns seeding    #
# --------------------------------------------------------------------- #


def _history_message(
    *,
    user_id: str = "U-1",
    channel_id: str = "C-1",
    content: str,
    ts: datetime,
    message_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id or f"m-{ts.timestamp()}",
        channel_id=channel_id,
        user_id=user_id,
        content=content,
        timestamp=Timestamp(dt=ts),
    )


def _backfill_transport(history: list[InboundMessage]) -> Any:
    t = _mock_transport()
    t.fetch_channel_history = AsyncMock(return_value=history)
    return t


class TestBackfillHistory:
    async def test_limit_zero_is_no_op(self, storage: SQLiteStorageAdapter) -> None:
        transport = _backfill_transport([])
        inserted = await _backfill_history_for_channel(
            channel_id="C-1",
            transport=transport,
            storage=storage,
            allowed_user_ids=frozenset({"U-1"}),
            limit=0,
        )
        assert inserted == 0
        transport.fetch_channel_history.assert_not_called()

    async def test_empty_allowlist_is_no_op(self, storage: SQLiteStorageAdapter) -> None:
        transport = _backfill_transport([])
        inserted = await _backfill_history_for_channel(
            channel_id="C-1",
            transport=transport,
            storage=storage,
            allowed_user_ids=frozenset(),
            limit=20,
        )
        assert inserted == 0

    async def test_inserts_all_messages_for_empty_storage(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        now = datetime.now(UTC)
        history = [
            _history_message(content="msg1", ts=now - timedelta(minutes=5)),
            _history_message(content="msg2", ts=now - timedelta(minutes=3)),
            _history_message(content="msg3", ts=now - timedelta(minutes=1)),
        ]
        transport = _backfill_transport(history)
        inserted = await _backfill_history_for_channel(
            channel_id="C-1",
            transport=transport,
            storage=storage,
            allowed_user_ids=frozenset({"U-1"}),
            limit=20,
        )
        assert inserted == 3
        stored = await storage.get_conversation_turns("C-1", "U-1")
        assert {t.content for t in stored} == {"msg1", "msg2", "msg3"}
        assert all(t.role == "operator" for t in stored)
        assert all(t.intent is None for t in stored)

    async def test_skips_messages_at_or_before_stored_max_timestamp(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        now = datetime.now(UTC)
        # Pre-seed a stored turn at T-2min
        existing_ts = now - timedelta(minutes=2)
        await storage.save_conversation_turn(
            ConversationTurn(
                id=uuid4(),
                channel_id="C-1",
                user_id="U-1",
                role="operator",
                content="previous",
                intent=None,
                timestamp=Timestamp(dt=existing_ts),
            )
        )
        history = [
            # Older than stored max → skip
            _history_message(content="old", ts=now - timedelta(minutes=5)),
            # Exactly equal to stored max → skip
            _history_message(content="dup", ts=existing_ts),
            # Newer → insert
            _history_message(content="new1", ts=now - timedelta(minutes=1)),
            _history_message(content="new2", ts=now),
        ]
        transport = _backfill_transport(history)
        inserted = await _backfill_history_for_channel(
            channel_id="C-1",
            transport=transport,
            storage=storage,
            allowed_user_ids=frozenset({"U-1"}),
            limit=20,
        )
        assert inserted == 2
        stored = await storage.get_conversation_turns("C-1", "U-1")
        contents = {t.content for t in stored}
        assert "previous" in contents
        assert "new1" in contents
        assert "new2" in contents
        assert "old" not in contents
        assert "dup" not in contents

    async def test_filters_messages_to_matching_user(self, storage: SQLiteStorageAdapter) -> None:
        now = datetime.now(UTC)
        history = [
            _history_message(user_id="U-1", content="mine", ts=now - timedelta(minutes=2)),
            _history_message(user_id="U-2", content="other", ts=now - timedelta(minutes=1)),
        ]
        transport = _backfill_transport(history)
        inserted = await _backfill_history_for_channel(
            channel_id="C-1",
            transport=transport,
            storage=storage,
            allowed_user_ids=frozenset({"U-1"}),
            limit=20,
        )
        assert inserted == 1
        stored = await storage.get_conversation_turns("C-1", "U-1")
        assert {t.content for t in stored} == {"mine"}

    async def test_fetch_failure_returns_zero(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.adapters.discord_transport import DiscordTransportError

        transport = _mock_transport()
        transport.fetch_channel_history = AsyncMock(
            side_effect=DiscordTransportError("rate limited")
        )
        inserted = await _backfill_history_for_channel(
            channel_id="C-1",
            transport=transport,
            storage=storage,
            allowed_user_ids=frozenset({"U-1"}),
            limit=20,
        )
        assert inserted == 0


# --------------------------------------------------------------------- #
# _compose_engine_state_snapshot — LLM context for the assistant        #
# --------------------------------------------------------------------- #


class TestComposeEngineStateSnapshot:
    async def test_no_storage_returns_zeros(self) -> None:
        snap = await _compose_engine_state_snapshot(live_storage=None)
        assert snap.symbols == []
        assert snap.total_usd_balance == 0.0

    async def test_active_symbols_populated_from_tuple_of_symbols(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        symbols = (
            Symbol(base="BTC", quote="USD"),
            Symbol(base="ETH", quote="USD"),
        )
        snap = await _compose_engine_state_snapshot(live_storage=storage, active_symbols=symbols)
        assert [s.symbol for s in snap.symbols] == ["BTC/USD", "ETH/USD"]
        assert all(s.state == "active" for s in snap.symbols)
        assert all(s.open_order_count == 0 for s in snap.symbols)

    async def test_balance_prefers_observe_storage_when_provided(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        from wobblebot.domain.models import Balance

        # Seed observe.db with a USD balance snapshot; live.db stays empty.
        observe = SQLiteStorageAdapter(":memory:")
        await observe.connect()
        try:
            balances = [Balance(asset="USD", total=89.92, locked=0.0, available=89.92)]
            await observe.save_balance_snapshot(balances)

            snap = await _compose_engine_state_snapshot(
                live_storage=storage,
                observe_storage=observe,
                active_symbols=(Symbol(base="BTC", quote="USD"),),
            )
            assert snap.total_usd_balance == 89.92
        finally:
            await observe.close()

    async def test_balance_falls_back_to_live_storage(self, storage: SQLiteStorageAdapter) -> None:
        from wobblebot.domain.models import Balance

        await storage.save_balance_snapshot(
            [Balance(asset="USD", total=12.34, locked=0.0, available=12.34)]
        )
        snap = await _compose_engine_state_snapshot(
            live_storage=storage, observe_storage=None, active_symbols=()
        )
        assert snap.total_usd_balance == 12.34
