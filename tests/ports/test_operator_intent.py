"""Unit tests for the ``OperatorIntent`` top-level discriminated union
(Stage 5.1.A).

``OperatorIntent`` is the outermost shape the assistant emits per
operator turn. It nests ``OperatorCommand`` and ``OperatorQuery``,
so these tests exercise the two-level discriminator resolution as
well as ``Conversational`` and ``Unparseable`` terminal variants.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    IntentQuery,
    IntentUnparseable,
    OperatorIntent,
    PauseCommand,
    StatusQuery,
)

pytestmark = pytest.mark.unit


_btc = Symbol(base="BTC", quote="USD")


def _adapter() -> TypeAdapter[OperatorIntent]:
    return TypeAdapter(OperatorIntent)


class TestIntentCommand:
    def test_wraps_command(self) -> None:
        intent = IntentCommand(command=PauseCommand(symbol=_btc))
        assert intent.kind == "command"
        assert isinstance(intent.command, PauseCommand)

    def test_frozen(self) -> None:
        intent = IntentCommand(command=PauseCommand(symbol=_btc))
        with pytest.raises(ValidationError):
            intent.kind = "query"  # type: ignore[misc]


class TestIntentQuery:
    def test_wraps_query(self) -> None:
        intent = IntentQuery(query=StatusQuery())
        assert intent.kind == "query"
        assert isinstance(intent.query, StatusQuery)


class TestIntentConversational:
    def test_construct(self) -> None:
        intent = IntentConversational(reply_text="hey there")
        assert intent.kind == "conversational"
        assert intent.reply_text == "hey there"

    def test_reply_text_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            IntentConversational(reply_text="")


class TestIntentUnparseable:
    def test_construct(self) -> None:
        intent = IntentUnparseable(reason="I'm not sure what 'wibble' means.")
        assert intent.kind == "unparseable"

    def test_reason_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            IntentUnparseable(reason="")


class TestOperatorIntentUnion:
    """Top-level discriminator resolution + nested command/query resolution."""

    @pytest.mark.parametrize(
        ("payload", "expected_type"),
        [
            (
                {"kind": "command", "command": {"kind": "pause", "symbol": "BTC/USD"}},
                IntentCommand,
            ),
            (
                {"kind": "command", "command": {"kind": "stop"}},
                IntentCommand,
            ),
            (
                {"kind": "query", "query": {"kind": "status"}},
                IntentQuery,
            ),
            (
                {"kind": "query", "query": {"kind": "recent_fills", "limit": 5}},
                IntentQuery,
            ),
            (
                {"kind": "conversational", "reply_text": "thanks"},
                IntentConversational,
            ),
            (
                {"kind": "unparseable", "reason": "no symbol named XYZ"},
                IntentUnparseable,
            ),
        ],
    )
    def test_validate_python(self, payload: dict[str, object], expected_type: type) -> None:
        intent = _adapter().validate_python(payload)
        assert isinstance(intent, expected_type)

    def test_nested_command_resolves(self) -> None:
        intent = _adapter().validate_python(
            {"kind": "command", "command": {"kind": "pause", "symbol": "ETH/USD"}}
        )
        assert isinstance(intent, IntentCommand)
        assert isinstance(intent.command, PauseCommand)
        assert intent.command.symbol.base == "ETH"

    def test_nested_query_resolves(self) -> None:
        intent = _adapter().validate_python({"kind": "query", "query": {"kind": "status"}})
        assert isinstance(intent, IntentQuery)
        assert isinstance(intent.query, StatusQuery)

    def test_unknown_outer_kind_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python({"kind": "telepathy", "thought": "..."})

    def test_unknown_inner_command_kind_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python(
                {"kind": "command", "command": {"kind": "withdraw_everything"}}
            )

    def test_missing_inner_command_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python({"kind": "command"})

    def test_round_trip_command_intent(self) -> None:
        intent = IntentCommand(command=PauseCommand(symbol=_btc))
        adapter = _adapter()
        dumped = adapter.dump_python(intent)
        revived = adapter.validate_python(dumped)
        assert revived == intent

    def test_round_trip_conversational(self) -> None:
        intent = IntentConversational(reply_text="ok")
        adapter = _adapter()
        revived = adapter.validate_python(adapter.dump_python(intent))
        assert revived == intent

    def test_json_dump_emits_kind_first(self) -> None:
        intent = IntentCommand(command=PauseCommand(symbol=_btc))
        dumped = _adapter().dump_python(intent)
        assert dumped["kind"] == "command"
        assert dumped["command"]["kind"] == "pause"
