"""Tests for ``domain/llm_cost.py`` (Stage 6.1.A, Phase 6 / ADR-014)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp

pytestmark = pytest.mark.unit


def _ts() -> Timestamp:
    return Timestamp(dt=datetime.now(UTC))


def _record(**overrides: object) -> LLMCallRecord:
    base: dict[str, object] = {
        "timestamp": _ts(),
        "role": "operator",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 100,
        "tokens_out": 200,
        "cost_usd": Decimal("0.003"),
        "success": True,
    }
    base.update(overrides)
    return LLMCallRecord(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# Construction                                                          #
# --------------------------------------------------------------------- #


class TestConstruction:
    def test_minimal_record(self) -> None:
        rec = _record()
        assert isinstance(rec.id, UUID)
        assert rec.role == "operator"
        assert rec.provider == "anthropic"
        assert rec.tokens_reasoning is None
        assert rec.request_id is None
        assert rec.error_kind is None

    def test_default_id_is_unique_per_construction(self) -> None:
        a = _record()
        b = _record()
        assert a.id != b.id

    def test_thinking_mode_record(self) -> None:
        rec = _record(tokens_reasoning=500)
        assert rec.tokens_reasoning == 500

    def test_failed_call_record(self) -> None:
        rec = _record(success=False, error_kind="rate_limited", tokens_out=0)
        assert rec.success is False
        assert rec.error_kind == "rate_limited"

    def test_record_is_frozen(self) -> None:
        rec = _record()
        with pytest.raises((ValidationError, TypeError)):
            rec.tokens_in = 999  # type: ignore[misc]


# --------------------------------------------------------------------- #
# Validation                                                            #
# --------------------------------------------------------------------- #


class TestValidation:
    def test_negative_tokens_in_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(tokens_in=-1)

    def test_negative_tokens_out_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(tokens_out=-1)

    def test_negative_tokens_reasoning_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(tokens_reasoning=-1)

    def test_negative_cost_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(cost_usd=Decimal("-0.01"))

    def test_empty_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(model="")

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(provider="cohere")  # type: ignore[arg-type]

    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _record(role="overlord")  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# JSON round-trip                                                       #
# --------------------------------------------------------------------- #


class TestRoundTrip:
    def test_dump_then_validate_round_trips(self) -> None:
        rec = _record(tokens_reasoning=42, request_id="req-abc", error_kind=None)
        payload = rec.model_dump_json()
        restored = LLMCallRecord.model_validate_json(payload)
        assert restored == rec

    def test_failed_call_round_trips(self) -> None:
        rec = _record(
            success=False,
            error_kind="timeout",
            tokens_out=0,
            cost_usd=Decimal("0"),
        )
        restored = LLMCallRecord.model_validate_json(rec.model_dump_json())
        assert restored == rec
        assert restored.success is False


# --------------------------------------------------------------------- #
# LLMCostCapExceeded domain exception                                   #
# --------------------------------------------------------------------- #


class TestCostCapExceededExc:
    def test_default_message_renders_cap_state(self) -> None:
        exc = LLMCostCapExceeded(
            cap_kind="daily",
            cap_value_usd=Decimal("1.00"),
            daily_spent_usd=Decimal("1.03"),
            session_spent_usd=Decimal("0.20"),
        )
        msg = str(exc)
        assert "daily" in msg
        assert "1.00" in msg
        assert "1.03" in msg
        assert "0.20" in msg

    def test_custom_message_overrides_default(self) -> None:
        exc = LLMCostCapExceeded(
            cap_kind="session",
            cap_value_usd=Decimal("0.50"),
            daily_spent_usd=Decimal("0.20"),
            session_spent_usd=Decimal("0.51"),
            message="halt: budget reached",
        )
        assert str(exc) == "halt: budget reached"

    def test_attributes_accessible(self) -> None:
        exc = LLMCostCapExceeded(
            cap_kind="daily",
            cap_value_usd=Decimal("1.00"),
            daily_spent_usd=Decimal("1.00"),
            session_spent_usd=Decimal("0.00"),
        )
        assert exc.cap_kind == "daily"
        assert exc.cap_value_usd == Decimal("1.00")
        assert exc.daily_spent_usd == Decimal("1.00")
        assert exc.session_spent_usd == Decimal("0.00")


# --------------------------------------------------------------------- #
# Caller-minted id                                                      #
# --------------------------------------------------------------------- #


def test_caller_can_mint_id() -> None:
    explicit = uuid4()
    rec = _record(id=explicit)
    assert rec.id == explicit
