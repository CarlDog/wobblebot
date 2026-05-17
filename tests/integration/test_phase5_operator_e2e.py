"""End-to-end Phase 5 integration check (Stage 5.7.B).

Exercises the full operator-interaction round-trip *without* a real
Discord Gateway, Ollama LLM, or Kraken exchange — the test stubs the
LLM and the Discord transport but uses real SQLite storage + real
GridEngine + real OperatorService + real cli/operator handler
functions + real cli/live poll helper. This is the unit-test
equivalent of "operator types pause BTC → cli/live actually pauses
BTC → notification fires back".

The flow exercised:

  1. Operator sends "pause BTC" in Discord.
  2. cli/operator's _handle_inbound_message receives, calls the
     stub assistant which returns IntentCommand(PauseCommand(BTC/USD)).
  3. The handler persists a PendingCommand in awaiting_confirmation
     status and posts a confirm embed (stubbed transport records
     the call + returns a message id).
  4. Operator reacts ✅. cli/operator's _handle_reaction transitions
     the row to approved with the confirming user id.
  5. cli/live's _process_pending_commands sees the approved row,
     dispatches it via OperatorService → engine.pause_symbol → row
     marked dispatched with a success CommandResult.
  6. Engine is_paused(BTC/USD) is True; subsequent step() returns
     'skipped_paused'.
  7. (Out-of-band) cli/live emits a session-event notification.
     cli/operator's forwarder posts it via the stubbed transport.

Marked ``integration`` so the default ``pytest`` run skips it; run
explicitly with ``pytest -m integration``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import pytest_asyncio

from wobblebot.adapters.discord_transport import (
    CONFIRM_EMOJI,
    DiscordTransport,
    InboundMessage,
    ReactionEvent,
)
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.sqlite_notifier import SqliteNotifierAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.live import _process_pending_commands
from wobblebot.cli.operator import (
    _forward_pending_notifications,
    _handle_inbound_message,
    _handle_reaction,
)
from wobblebot.config.grid import GridConfig, GridLevels
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.assistant import (
    AssistantPort,
    ConversationContext,
)
from wobblebot.ports.notifier import Notification
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    IntentQuery,
    OperatorIntent,
    PauseCommand,
    StatusQuery,
)
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.operator_service import OperatorService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


BTC_USD = Symbol(base="BTC", quote="USD")


class _ScriptedAssistant(AssistantPort):
    """AssistantPort stub returning intents from a list, in order."""

    def __init__(self, intents: list[OperatorIntent]) -> None:
        self._intents = list(intents)
        self.contexts: list[ConversationContext] = []

    async def parse_intent(self, context: ConversationContext) -> OperatorIntent:
        self.contexts.append(context)
        if not self._intents:
            raise AssertionError("scripted assistant exhausted")
        return self._intents.pop(0)


def _mock_transport() -> Any:
    t = MagicMock(spec=DiscordTransport)
    t.send_message = AsyncMock(return_value="msg-id")
    t.send_embed = AsyncMock(return_value="embed-id")
    t.send_confirmation = AsyncMock(return_value="confirm-msg-1")
    return t


def _grid_config() -> GridConfig:
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal("1"),
            levels_above=3,
            levels_below=3,
            order_size_usd=Decimal("10"),
        )
    )


def _safety_config() -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal("100000"),
        max_daily_spend_usd=Decimal("100000"),
        max_per_coin_exposure_usd=Decimal("100000"),
        max_orders_per_coin=100,
        emergency_stop=EmergencyStopConfig(
            enabled=True,
            max_loss_percentage=Decimal("20"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    """One shared operator.db serves both cli/operator and cli/live's poll
    in the integration test — they're the same SQLite file in production."""
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _inbound(content: str, *, user_id: str = "U-1") -> InboundMessage:
    return InboundMessage(
        message_id=f"m-{content[:10]}",
        channel_id="C-1",
        user_id=user_id,
        content=content,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


# --------------------------------------------------------------------- #
# Full pause→confirm→approve→dispatch→notify round-trip                  #
# --------------------------------------------------------------------- #


async def test_full_pause_round_trip(storage: SQLiteStorageAdapter) -> None:
    """End-to-end: operator pauses BTC and the engine actually pauses."""
    # ---- setup: shared engine + operator service ----
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("1000"), "BTC": Decimal("1")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
    operator_service = OperatorService(
        engine=engine,
        storage=storage,
        active_symbols=(BTC_USD,),
        grid_config=_grid_config(),
    )
    transport = _mock_transport()
    pending_map: dict[str, UUID] = {}
    assistant = _ScriptedAssistant([IntentCommand(command=PauseCommand(symbol=BTC_USD))])

    # ---- Step 1-3: operator sends "pause BTC" → confirm embed posted ----
    await _handle_inbound_message(
        _inbound("pause BTC"),
        operator_storage=storage,
        live_storage=storage,  # one DB for the integration test
        assistant=assistant,
        operator_service=operator_service,
        transport=transport,
        outbound_channel_id="100",
        context_window_turns=10,
        confirm_ttl_seconds=300,
        pending_message_map=pending_map,
    )
    transport.send_confirmation.assert_awaited_once()
    awaiting = await storage.get_pending_commands(status="awaiting_confirmation")
    assert len(awaiting) == 1
    pending_id = awaiting[0].id
    assert pending_map["confirm-msg-1"] == pending_id

    # ---- Step 4: operator reacts ✅ → row transitions to approved ----
    confirm_event = ReactionEvent(
        message_id="confirm-msg-1",
        channel_id="C-1",
        user_id="U-1",
        emoji=CONFIRM_EMOJI,
        action="add",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    await _handle_reaction(confirm_event, operator_storage=storage, pending_message_map=pending_map)
    approved = await storage.get_pending_commands(status="approved")
    assert len(approved) == 1
    assert approved[0].confirming_user_id == "U-1"
    assert approved[0].id == pending_id

    # ---- Step 5: cli/live's poll dispatches the approved command ----
    processed = await _process_pending_commands(operator_service, storage)
    assert processed == 1

    # ---- Step 6: engine actually paused ----
    assert engine.is_paused(BTC_USD) is True

    # ---- Step 7: pending_commands row marked dispatched with success result ----
    dispatched = await storage.get_pending_command(pending_id)
    assert dispatched is not None
    assert dispatched.status == "dispatched"
    assert dispatched.result is not None
    assert dispatched.result.success is True
    assert dispatched.result.command_kind == "pause"


# --------------------------------------------------------------------- #
# Reject flow                                                           #
# --------------------------------------------------------------------- #


async def test_reject_flow_does_not_dispatch(storage: SQLiteStorageAdapter) -> None:
    """❌ reaction marks rejected; cli/live's poll skips it."""
    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("1000"), "BTC": Decimal("1")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
    operator_service = OperatorService(
        engine=engine,
        storage=storage,
        active_symbols=(BTC_USD,),
    )
    transport = _mock_transport()
    pending_map: dict[str, UUID] = {}
    assistant = _ScriptedAssistant([IntentCommand(command=PauseCommand(symbol=BTC_USD))])

    await _handle_inbound_message(
        _inbound("pause BTC"),
        operator_storage=storage,
        live_storage=storage,
        assistant=assistant,
        operator_service=operator_service,
        transport=transport,
        outbound_channel_id="100",
        context_window_turns=10,
        confirm_ttl_seconds=300,
        pending_message_map=pending_map,
    )

    from wobblebot.adapters.discord_transport import REJECT_EMOJI

    reject_event = ReactionEvent(
        message_id="confirm-msg-1",
        channel_id="C-1",
        user_id="U-1",
        emoji=REJECT_EMOJI,
        action="add",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    await _handle_reaction(reject_event, operator_storage=storage, pending_message_map=pending_map)

    # cli/live's poll sees no approved rows; engine never pauses
    processed = await _process_pending_commands(operator_service, storage)
    assert processed == 0
    assert engine.is_paused(BTC_USD) is False
    rejected = await storage.get_pending_commands(status="rejected")
    assert len(rejected) == 1


# --------------------------------------------------------------------- #
# Multi-turn conversation                                               #
# --------------------------------------------------------------------- #


async def test_multi_turn_conversation_records_history(
    storage: SQLiteStorageAdapter,
) -> None:
    """Two operator messages produce a full turn history in conversation_turns."""
    exchange = MockExchangeAdapter(starting_balances={}, starting_prices={})
    engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
    operator_service = OperatorService(engine=engine, storage=storage)
    transport = _mock_transport()
    assistant = _ScriptedAssistant(
        [
            IntentQuery(query=StatusQuery()),
            IntentConversational(reply_text="here you go"),
        ]
    )

    await _handle_inbound_message(
        _inbound("status?"),
        operator_storage=storage,
        live_storage=None,
        assistant=assistant,
        operator_service=operator_service,
        transport=transport,
        outbound_channel_id="100",
        context_window_turns=10,
        confirm_ttl_seconds=300,
        pending_message_map={},
    )
    await _handle_inbound_message(
        _inbound("thanks"),
        operator_storage=storage,
        live_storage=None,
        assistant=assistant,
        operator_service=operator_service,
        transport=transport,
        outbound_channel_id="100",
        context_window_turns=10,
        confirm_ttl_seconds=300,
        pending_message_map={},
    )

    turns = await storage.get_conversation_turns("C-1", "U-1")
    # 2 operator turns + 2 assistant replies = 4
    assert len(turns) == 4
    operator_turns = [t for t in turns if t.role == "operator"]
    assistant_turns = [t for t in turns if t.role == "assistant"]
    assert len(operator_turns) == 2
    assert len(assistant_turns) == 2

    # Second invocation's context should include the first turn pair as recent_turns
    second_context = assistant.contexts[1]
    assert len(second_context.recent_turns) >= 2


# --------------------------------------------------------------------- #
# Notification forwarder round-trip                                     #
# --------------------------------------------------------------------- #


async def test_notification_persisted_and_forwarded(
    storage: SQLiteStorageAdapter,
) -> None:
    """cli/live writes via SqliteNotifierAdapter; forwarder posts to Discord."""
    notifier = SqliteNotifierAdapter(storage)
    transport = _mock_transport()

    # cli/live (or cli/harvest) emits a session-event notification
    await notifier.send_notification(
        Notification(
            level="info",
            title="Live session started",
            message="trading BTC/USD",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            context={"symbols": ["BTC/USD"], "tick_seconds": 5},
        )
    )

    # Operator daemon's forwarder picks it up and posts an embed
    forwarded = await _forward_pending_notifications(
        storage=storage, transport=transport, channel_id="100"
    )
    assert forwarded == 1
    transport.send_embed.assert_awaited_once()

    # Row marked forwarded; second pass is a no-op
    second_pass = await _forward_pending_notifications(
        storage=storage, transport=transport, channel_id="100"
    )
    assert second_pass == 0


# --------------------------------------------------------------------- #
# TTL expiry                                                            #
# --------------------------------------------------------------------- #


async def test_ttl_expiry_skipped_by_dispatch(storage: SQLiteStorageAdapter) -> None:
    """Expired commands don't dispatch even if cli/live polls."""
    from wobblebot.cli.operator import _expire_stale_pending_commands

    exchange = MockExchangeAdapter(
        starting_balances={"USD": Decimal("1000"), "BTC": Decimal("1")},
        starting_prices={BTC_USD: Decimal("50000")},
    )
    engine = GridEngine(exchange, storage, _grid_config(), _safety_config())
    operator_service = OperatorService(engine=engine, storage=storage, active_symbols=(BTC_USD,))
    transport = _mock_transport()
    pending_map: dict[str, UUID] = {}
    assistant = _ScriptedAssistant([IntentCommand(command=PauseCommand(symbol=BTC_USD))])

    # Operator issues a command but never reacts
    await _handle_inbound_message(
        _inbound("pause BTC"),
        operator_storage=storage,
        live_storage=storage,
        assistant=assistant,
        operator_service=operator_service,
        transport=transport,
        outbound_channel_id="100",
        context_window_turns=10,
        confirm_ttl_seconds=300,
        pending_message_map=pending_map,
    )

    # Force the row's TTL to be in the past
    awaiting = await storage.get_pending_commands(status="awaiting_confirmation")
    stale = awaiting[0].model_copy(
        update={"ttl_expires_at": Timestamp(dt=datetime.now(UTC) - timedelta(seconds=60))}
    )
    await storage.save_pending_command(stale)

    # Expirer transitions to expired
    expired_count = await _expire_stale_pending_commands(storage)
    assert expired_count == 1

    # cli/live's poll skips expired rows (only sees 'approved')
    processed = await _process_pending_commands(operator_service, storage)
    assert processed == 0
    assert engine.is_paused(BTC_USD) is False
