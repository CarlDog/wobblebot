"""SQLiteStorageAdapter tests for conversation_turns persistence (Stage 5.6.A)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.assistant import ConversationTurn
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    PauseCommand,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _turn(
    *,
    channel_id: str = "C-1",
    user_id: str = "U-1",
    role: str = "operator",
    content: str = "hello",
    intent=None,  # type: ignore[no-untyped-def]
    offset_seconds: int = 0,
    turn_id: UUID | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        id=turn_id or uuid4(),
        channel_id=channel_id,
        user_id=user_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        intent=intent,
        timestamp=Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds)),
    )


# --------------------------------------------------------------------- #
# Round-trip                                                            #
# --------------------------------------------------------------------- #


async def test_save_then_read_operator_turn_with_intent(
    storage: SQLiteStorageAdapter,
) -> None:
    intent = IntentConversational(reply_text="hello")
    turn = _turn(role="operator", content="hi", intent=intent)
    await storage.save_conversation_turn(turn)
    rows = await storage.get_conversation_turns(turn.channel_id, turn.user_id)
    assert len(rows) == 1
    assert rows[0] == turn
    assert isinstance(rows[0].intent, IntentConversational)


async def test_save_then_read_assistant_turn_without_intent(
    storage: SQLiteStorageAdapter,
) -> None:
    turn = _turn(role="assistant", content="hey there", intent=None)
    await storage.save_conversation_turn(turn)
    rows = await storage.get_conversation_turns(turn.channel_id, turn.user_id)
    assert len(rows) == 1
    assert rows[0].intent is None


async def test_nested_command_intent_round_trips(storage: SQLiteStorageAdapter) -> None:
    intent = IntentCommand(command=PauseCommand(symbol=Symbol(base="BTC", quote="USD")))
    turn = _turn(role="operator", content="pause BTC", intent=intent)
    await storage.save_conversation_turn(turn)
    rows = await storage.get_conversation_turns(turn.channel_id, turn.user_id)
    assert isinstance(rows[0].intent, IntentCommand)
    assert isinstance(rows[0].intent.command, PauseCommand)
    assert str(rows[0].intent.command.symbol) == "BTC/USD"


# --------------------------------------------------------------------- #
# Scoping                                                               #
# --------------------------------------------------------------------- #


async def test_returns_empty_for_unknown_scope(storage: SQLiteStorageAdapter) -> None:
    rows = await storage.get_conversation_turns("nope", "nada")
    assert rows == []


async def test_scoping_isolates_other_channel(storage: SQLiteStorageAdapter) -> None:
    await storage.save_conversation_turn(_turn(channel_id="C-1"))
    await storage.save_conversation_turn(_turn(channel_id="C-2"))
    rows = await storage.get_conversation_turns("C-1", "U-1")
    assert len(rows) == 1
    assert rows[0].channel_id == "C-1"


async def test_scoping_isolates_other_user(storage: SQLiteStorageAdapter) -> None:
    await storage.save_conversation_turn(_turn(user_id="U-1"))
    await storage.save_conversation_turn(_turn(user_id="U-2"))
    rows = await storage.get_conversation_turns("C-1", "U-1")
    assert len(rows) == 1
    assert rows[0].user_id == "U-1"


# --------------------------------------------------------------------- #
# Ordering + limit                                                      #
# --------------------------------------------------------------------- #


async def test_orders_chronologically_oldest_first(storage: SQLiteStorageAdapter) -> None:
    oldest = _turn(content="oldest", offset_seconds=-100)
    middle = _turn(content="middle", offset_seconds=-50)
    newest = _turn(content="newest", offset_seconds=0)
    # save out of order
    await storage.save_conversation_turn(middle)
    await storage.save_conversation_turn(newest)
    await storage.save_conversation_turn(oldest)
    rows = await storage.get_conversation_turns("C-1", "U-1")
    assert [r.content for r in rows] == ["oldest", "middle", "newest"]


async def test_limit_returns_most_recent_in_chronological_order(
    storage: SQLiteStorageAdapter,
) -> None:
    # 5 turns; limit=3 must return turns #3, #4, #5 in chronological order.
    for idx in range(5):
        await storage.save_conversation_turn(_turn(content=f"turn-{idx}", offset_seconds=idx))
    rows = await storage.get_conversation_turns("C-1", "U-1", limit=3)
    assert [r.content for r in rows] == ["turn-2", "turn-3", "turn-4"]


# --------------------------------------------------------------------- #
# Schema-level                                                          #
# --------------------------------------------------------------------- #


async def test_role_check_rejects_unknown_value(storage: SQLiteStorageAdapter) -> None:
    conn = storage._require_conn()  # pylint: disable=protected-access
    with pytest.raises(Exception):  # IntegrityError
        await conn.execute(
            """
            INSERT INTO conversation_turns
                (id, channel_id, user_id, role, content, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                "C-1",
                "U-1",
                "moderator",  # CHECK rejects
                "x",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.commit()


async def test_upsert_same_id_replaces_content_and_intent(
    storage: SQLiteStorageAdapter,
) -> None:
    tid = uuid4()
    initial = _turn(turn_id=tid, content="original", intent=None)
    await storage.save_conversation_turn(initial)
    # Re-save with parsed intent attached (the typical operator-turn flow).
    parsed = initial.model_copy(update={"intent": IntentConversational(reply_text="parsed")})
    await storage.save_conversation_turn(parsed)
    rows = await storage.get_conversation_turns("C-1", "U-1")
    assert len(rows) == 1
    assert isinstance(rows[0].intent, IntentConversational)
