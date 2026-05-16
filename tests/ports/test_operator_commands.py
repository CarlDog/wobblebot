"""Unit tests for the ``OperatorCommand`` typed sum (Stage 5.1.A).

Covers each concrete command variant (Pause, Resume, PauseAll,
ResumeAll, CancelOpenOrders, Stop), the discriminated-union resolution
that picks the right variant from a ``kind``-tagged payload, the
``BeforeValidator`` that lets the LLM emit ``"BTC/USD"`` strings as
well as ``{base, quote}`` dicts for ``Symbol`` fields, and the
frozen-Pydantic discipline applied to every variant.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator import (
    CancelOpenOrdersCommand,
    OperatorCommand,
    PauseAllCommand,
    PauseCommand,
    ResumeAllCommand,
    ResumeCommand,
    StopCommand,
)

pytestmark = pytest.mark.unit


_btc = Symbol(base="BTC", quote="USD")


def _adapter() -> TypeAdapter[OperatorCommand]:
    return TypeAdapter(OperatorCommand)


class TestPauseCommand:
    def test_construct_with_symbol_object(self) -> None:
        cmd = PauseCommand(symbol=_btc)
        assert cmd.kind == "pause"
        assert cmd.symbol == _btc

    def test_kind_is_fixed(self) -> None:
        cmd = PauseCommand(symbol=_btc)
        with pytest.raises(ValidationError):
            cmd.kind = "resume"  # type: ignore[misc]

    def test_frozen(self) -> None:
        cmd = PauseCommand(symbol=_btc)
        with pytest.raises(ValidationError):
            cmd.symbol = Symbol(base="ETH", quote="USD")  # type: ignore[misc]

    def test_symbol_required(self) -> None:
        with pytest.raises(ValidationError):
            PauseCommand()  # type: ignore[call-arg]


class TestResumeCommand:
    def test_construct(self) -> None:
        cmd = ResumeCommand(symbol=_btc)
        assert cmd.kind == "resume"
        assert cmd.symbol == _btc

    def test_frozen(self) -> None:
        cmd = ResumeCommand(symbol=_btc)
        with pytest.raises(ValidationError):
            cmd.symbol = Symbol(base="ETH", quote="USD")  # type: ignore[misc]


class TestPauseAllCommand:
    def test_no_args(self) -> None:
        cmd = PauseAllCommand()
        assert cmd.kind == "pause_all"


class TestResumeAllCommand:
    def test_no_args(self) -> None:
        cmd = ResumeAllCommand()
        assert cmd.kind == "resume_all"


class TestCancelOpenOrdersCommand:
    def test_default_symbol_is_none(self) -> None:
        cmd = CancelOpenOrdersCommand()
        assert cmd.kind == "cancel_open_orders"
        assert cmd.symbol is None

    def test_with_symbol(self) -> None:
        cmd = CancelOpenOrdersCommand(symbol=_btc)
        assert cmd.symbol == _btc


class TestStopCommand:
    def test_no_args(self) -> None:
        cmd = StopCommand()
        assert cmd.kind == "stop"


class TestOperatorCommandUnion:
    """Discriminator resolution from JSON-shaped dicts."""

    @pytest.mark.parametrize(
        ("payload", "expected_type"),
        [
            ({"kind": "pause", "symbol": "BTC/USD"}, PauseCommand),
            ({"kind": "resume", "symbol": "ETH/USD"}, ResumeCommand),
            ({"kind": "pause_all"}, PauseAllCommand),
            ({"kind": "resume_all"}, ResumeAllCommand),
            ({"kind": "cancel_open_orders"}, CancelOpenOrdersCommand),
            ({"kind": "cancel_open_orders", "symbol": "BTC/USD"}, CancelOpenOrdersCommand),
            ({"kind": "stop"}, StopCommand),
        ],
    )
    def test_validate_python(self, payload: dict[str, object], expected_type: type) -> None:
        cmd = _adapter().validate_python(payload)
        assert isinstance(cmd, expected_type)

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python({"kind": "fly_the_moon"})

    def test_missing_kind_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python({"symbol": "BTC/USD"})

    def test_pause_missing_symbol_raises(self) -> None:
        with pytest.raises(ValidationError):
            _adapter().validate_python({"kind": "pause"})

    def test_json_round_trip_pause(self) -> None:
        cmd = PauseCommand(symbol=_btc)
        adapter = _adapter()
        dumped = adapter.dump_python(cmd)
        revived = adapter.validate_python(dumped)
        assert revived == cmd

    def test_json_round_trip_cancel_no_symbol(self) -> None:
        cmd = CancelOpenOrdersCommand()
        adapter = _adapter()
        dumped = adapter.dump_python(cmd)
        revived = adapter.validate_python(dumped)
        assert revived == cmd


class TestSymbolCoercion:
    """The ``BeforeValidator`` on ``SymbolInput`` accepts string form."""

    def test_string_form_parses(self) -> None:
        cmd = _adapter().validate_python({"kind": "pause", "symbol": "DOGE/USD"})
        assert isinstance(cmd, PauseCommand)
        assert cmd.symbol.base == "DOGE"
        assert cmd.symbol.quote == "USD"

    def test_dict_form_parses(self) -> None:
        cmd = _adapter().validate_python(
            {"kind": "pause", "symbol": {"base": "ADA", "quote": "USD"}}
        )
        assert isinstance(cmd, PauseCommand)
        assert cmd.symbol.base == "ADA"

    def test_malformed_string_raises(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            _adapter().validate_python({"kind": "pause", "symbol": "not-a-pair"})

    def test_optional_symbol_none(self) -> None:
        cmd = _adapter().validate_python({"kind": "cancel_open_orders", "symbol": None})
        assert isinstance(cmd, CancelOpenOrdersCommand)
        assert cmd.symbol is None

    def test_optional_symbol_string(self) -> None:
        cmd = _adapter().validate_python({"kind": "cancel_open_orders", "symbol": "BTC/USD"})
        assert isinstance(cmd, CancelOpenOrdersCommand)
        assert cmd.symbol is not None
        assert cmd.symbol.base == "BTC"
