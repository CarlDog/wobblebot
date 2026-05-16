"""Unit tests for the ``AssistantPort`` domain types (Stage 5.1.B).

Covers ``SymbolStateSnapshot``, ``EngineStateSnapshot``,
``ConversationTurn``, and ``ConversationContext`` — construction,
validation, frozenness, JSON round-trip, and the multi-turn /
snapshot composition discipline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.assistant import (
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
    SymbolStateSnapshot,
)
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    PauseCommand,
)

pytestmark = pytest.mark.unit


def _ts(offset_seconds: int = 0) -> Timestamp:
    return Timestamp(dt=datetime.now(UTC) + timedelta(seconds=offset_seconds))


def _snapshot(**overrides: object) -> EngineStateSnapshot:
    base: dict[str, object] = {
        "snapshot_at": _ts(),
        "symbols": [],
        "total_usd_balance": 100.0,
        "session_pnl": 0.0,
        "session_runtime_seconds": 0.0,
    }
    base.update(overrides)
    return EngineStateSnapshot(**base)  # type: ignore[arg-type]


class TestSymbolStateSnapshot:
    def test_minimal(self) -> None:
        s = SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=4)
        assert s.symbol == "BTC/USD"
        assert s.latest_price is None

    def test_with_price(self) -> None:
        s = SymbolStateSnapshot(
            symbol="ETH/USD", state="paused", open_order_count=0, latest_price=3200.0
        )
        assert s.latest_price == 3200.0

    def test_frozen(self) -> None:
        s = SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=4)
        with pytest.raises(ValidationError):
            s.state = "paused"  # type: ignore[misc]

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(ValidationError):
            SymbolStateSnapshot(
                symbol="BTC/USD",
                state="zombie",  # type: ignore[arg-type]
                open_order_count=0,
            )

    def test_open_order_count_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=-1)


class TestEngineStateSnapshot:
    def test_empty_symbols(self) -> None:
        snap = _snapshot()
        assert snap.symbols == []
        assert snap.harvester_band is None
        assert snap.recent_fill_count == 0

    def test_with_symbols(self) -> None:
        snap = _snapshot(
            symbols=[
                SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=6),
                SymbolStateSnapshot(symbol="ETH/USD", state="paused", open_order_count=0),
            ]
        )
        assert len(snap.symbols) == 2
        assert snap.symbols[1].state == "paused"

    def test_frozen(self) -> None:
        snap = _snapshot()
        with pytest.raises(ValidationError):
            snap.total_usd_balance = 200.0  # type: ignore[misc]

    def test_runtime_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            _snapshot(session_runtime_seconds=-1)

    def test_invalid_band_raises(self) -> None:
        with pytest.raises(ValidationError):
            _snapshot(harvester_band="extra_surplus")

    def test_band_valid_values(self) -> None:
        for band in ("deficit", "topup", "hold", "surplus"):
            snap = _snapshot(harvester_band=band)
            assert snap.harvester_band == band


class TestConversationTurn:
    def test_operator_turn_with_intent(self) -> None:
        turn = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="operator",
            content="pause BTC",
            intent=IntentCommand(command=PauseCommand(symbol=Symbol(base="BTC", quote="USD"))),
            timestamp=_ts(),
        )
        assert turn.role == "operator"
        assert isinstance(turn.intent, IntentCommand)

    def test_assistant_turn_no_intent(self) -> None:
        turn = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-bot",
            role="assistant",
            content="BTC paused.",
            timestamp=_ts(),
        )
        assert turn.role == "assistant"
        assert turn.intent is None

    def test_frozen(self) -> None:
        turn = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="operator",
            content="hi",
            timestamp=_ts(),
        )
        with pytest.raises(ValidationError):
            turn.content = "different"  # type: ignore[misc]

    def test_content_required(self) -> None:
        with pytest.raises(ValidationError):
            ConversationTurn(
                id=uuid4(),
                channel_id="C-1",
                user_id="U-1",
                role="operator",
                content="",
                timestamp=_ts(),
            )

    def test_channel_id_required(self) -> None:
        with pytest.raises(ValidationError):
            ConversationTurn(
                id=uuid4(),
                channel_id="",
                user_id="U-1",
                role="operator",
                content="hi",
                timestamp=_ts(),
            )

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValidationError):
            ConversationTurn(
                id=uuid4(),
                channel_id="C-1",
                user_id="U-1",
                role="moderator",  # type: ignore[arg-type]
                content="hi",
                timestamp=_ts(),
            )

    def test_round_trip_operator_turn(self) -> None:
        turn = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="operator",
            content="status?",
            intent=IntentConversational(reply_text="hi"),
            timestamp=_ts(),
        )
        adapter = TypeAdapter(ConversationTurn)
        revived = adapter.validate_python(adapter.dump_python(turn))
        assert revived == turn


class TestConversationContext:
    def test_empty_history(self) -> None:
        ctx = ConversationContext(
            current_message="pause BTC",
            channel_id="C-1",
            user_id="U-1",
            engine_state_snapshot=_snapshot(),
        )
        assert ctx.recent_turns == ()
        assert isinstance(ctx.recent_turns, tuple)

    def test_with_history(self) -> None:
        prior = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="operator",
            content="hi",
            timestamp=_ts(-60),
        )
        ctx = ConversationContext(
            current_message="pause BTC",
            channel_id="C-1",
            user_id="U-1",
            recent_turns=(prior,),
            engine_state_snapshot=_snapshot(),
        )
        assert len(ctx.recent_turns) == 1
        assert ctx.recent_turns[0].content == "hi"

    def test_recent_turns_coerces_list_to_tuple(self) -> None:
        prior = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="operator",
            content="hi",
            timestamp=_ts(-60),
        )
        # Pydantic should coerce list input into a tuple (declared type).
        ctx = ConversationContext(
            current_message="m",
            channel_id="C-1",
            user_id="U-1",
            recent_turns=[prior],  # type: ignore[arg-type]
            engine_state_snapshot=_snapshot(),
        )
        assert isinstance(ctx.recent_turns, tuple)

    def test_frozen(self) -> None:
        ctx = ConversationContext(
            current_message="hi",
            channel_id="C-1",
            user_id="U-1",
            engine_state_snapshot=_snapshot(),
        )
        with pytest.raises(ValidationError):
            ctx.current_message = "different"  # type: ignore[misc]

    def test_current_message_required(self) -> None:
        with pytest.raises(ValidationError):
            ConversationContext(
                current_message="",
                channel_id="C-1",
                user_id="U-1",
                engine_state_snapshot=_snapshot(),
            )

    def test_engine_state_snapshot_required(self) -> None:
        with pytest.raises(ValidationError):
            ConversationContext(  # type: ignore[call-arg]
                current_message="hi",
                channel_id="C-1",
                user_id="U-1",
            )

    def test_round_trip(self) -> None:
        prior = ConversationTurn(
            id=uuid4(),
            channel_id="C-1",
            user_id="U-1",
            role="assistant",
            content="paused BTC",
            timestamp=_ts(-30),
        )
        ctx = ConversationContext(
            current_message="thanks",
            channel_id="C-1",
            user_id="U-1",
            recent_turns=(prior,),
            engine_state_snapshot=_snapshot(
                symbols=[SymbolStateSnapshot(symbol="BTC/USD", state="paused", open_order_count=0)]
            ),
        )
        adapter = TypeAdapter(ConversationContext)
        revived = adapter.validate_python(adapter.dump_python(ctx))
        assert revived == ctx
