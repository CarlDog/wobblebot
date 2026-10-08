"""Offline negative controls for the retained G4 campaign defects."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation
from wobblebot.ports.exceptions import AdvisorError, AssistantError
from wobblebot.services.llm_cloud_call import ensure_complete_response

pytestmark = pytest.mark.unit


def load(name):
    path = Path(__file__).resolve().parents[2] / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


news = load("probe_news")
risk = load("probe_risk")


def recommendation(role="risk", confidence="low", **values):
    return AdvisorRecommendation(
        recommendation_id=str(uuid4()),
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=role,
        rationale="fixture",
        recommendations=values,
        confidence=confidence,
    )


@pytest.mark.parametrize("value", ["garbage", True, float("nan"), float("inf")])
def test_invalid_numerics_are_unavailable_not_unsafe(value):
    result = risk.grade(risk.FIXTURES[0], recommendation(order_size_usd=value))
    assert result.verdict == "ERROR" and result.direction is None
    result = news._grade(
        news.FIXTURE_SETS["v1"][0], recommendation("news", spacing_percentage=value)
    )
    assert result.verdict == "BAD_VALUE"


def test_small_correct_risk_move_is_not_unsafe():
    fixture = next(f for f in risk.FIXTURES if f.severity == "severe")
    for value in (0.001, 0.042, 0.05):
        result = risk.grade(
            fixture,
            recommendation(
                spacing_percentage=fixture.summary.current_grid.spacing_percentage * (1 + value)
            ),
        )
        assert result.verdict == "OK" and result.direction == "de_risk"
        assert "small" in result.why


def test_echo_and_omitted_hold_both_require_low_confidence():
    fixture = next(f for f in news.FIXTURE_SETS["v1"] if f.expect_low_confidence)
    for values in ({}, {"spacing_percentage": news._CURRENT_SPACING}):
        result = news._grade(fixture, recommendation("news", "high", **values))
        assert result.verdict == "HELD_OVERCONFIDENT"


class Answers:
    effective_temperature = None

    def __init__(self, answers):
        self.answers = iter(answers)
        self.closed = False

    async def get_recommendation(self, _summary):
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def aclose(self):
        self.closed = True


def args(**overrides):
    values = {
        "provider": "ollama",
        "model": "fixture",
        "fixture_set": "v1",
        "json": True,
        "prompt_file": "config/prompts/risk.md",
        "base_url": "http://localhost:11434",
        "temperature": 0.4,
        "max_tokens": 4000,
        "timeout_seconds": 1,
        "session_cap": 2,
        "daily_cap": 5,
    }
    return SimpleNamespace(**(values | overrides))


def output(capsys):
    line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("JSON_RESULT: ")
    )
    return json.loads(line.removeprefix("JSON_RESULT: "))


@pytest.mark.asyncio
async def test_news_cost_denial_keeps_scorecard_and_availability(monkeypatch, capsys):
    fixture = next(f for f in news.FIXTURE_SETS["v1"] if f.expect == "hold")
    denial = LLMCostCapExceeded(
        cap_kind="daily",
        cap_value_usd=Decimal("1"),
        daily_spent_usd=Decimal("1"),
        session_spent_usd=Decimal("0"),
        message="daily backstop denied fixture",
    )
    adapter = Answers([recommendation("news"), denial])
    monkeypatch.setattr(news, "FIXTURE_SETS", {"v1": [fixture, fixture]})
    monkeypatch.setattr(news, "_build", lambda _: (adapter, None))
    assert await news._run(args()) == 0
    report = output(capsys)
    assert (report["correct"], report["answered"], report["errored"]) == (1, 1, 1)
    assert report["judgment_accuracy"] == 1 and report["availability"] == 0.5
    assert report["max_score"] == 1 and report["effective_temperature"] is None
    assert report["verdicts"][0]["emitted"] == {}
    assert "daily backstop" in report["verdicts"][1]["detail"]
    assert adapter.closed


@pytest.mark.asyncio
async def test_risk_errors_have_no_fabricated_direction(monkeypatch, capsys):
    fixture = next(f for f in risk.FIXTURES if f.expect == "hold")
    detail = "sanitized error cause " * 10
    adapter = Answers([recommendation(), AdvisorError(detail)])
    monkeypatch.setattr(risk, "FIXTURE_SETS", {"v1": [fixture, fixture]})
    monkeypatch.setattr("wobblebot.adapters.ollama.OllamaAdapter", lambda **_: adapter)
    assert await risk.main_async(args()) == 0
    report = output(capsys)
    assert (report["correct"], report["answered"], report["errored"]) == (1, 1, 1)
    assert report["rows"][1]["direction"] is None
    assert report["rows"][1]["why"] == detail
    assert report["judgment_accuracy"] == 1 and report["availability"] == 0.5
    assert adapter.closed


@pytest.mark.parametrize(
    "envelope",
    [
        {"choices": [{"finish_reason": "length"}]},
        {"stop_reason": "max_tokens"},
        {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    ],
)
@pytest.mark.parametrize("error_type", [AdvisorError, AssistantError])
def test_provider_truncation_has_distinct_safe_error(envelope, error_type):
    with pytest.raises(error_type, match="truncated_response"):
        ensure_complete_response(envelope, error_type)


def test_complete_or_legacy_envelope_still_supported():
    for envelope in ({}, {"choices": [{"finish_reason": "stop"}]}, {"stop_reason": "end_turn"}):
        ensure_complete_response(envelope, AdvisorError)


def test_versioned_fixture_repairs_preserve_labels_and_constant_floors():
    old_news = {f.name: f for f in news.FIXTURE_SETS["gen2"]}
    new_news = news.FIXTURE_SETS["gen3"]
    assert len(new_news) == len(old_news)
    for fixture in new_news:
        old = old_news[fixture.name.removesuffix("_explicit")]
        assert fixture.expect == old.expect
        if fixture.name.endswith("_explicit"):
            assert fixture.items != old.items
    for values in ({}, {"spacing_percentage": 3.6}):
        score = sum(news._grade(f, recommendation("news", **values)).ok for f in new_news)
        assert score / len(new_news) < 0.60
    new_risk = risk.FIXTURE_SETS["gen2"]
    assert [f.expect for f in new_risk] == [f.expect for f in risk.FIXTURE_SETS["v1"]]
    for strategy in ("hold", "reduce"):
        correct = 0
        for fixture in new_risk:
            values = (
                {}
                if strategy == "hold"
                else {"order_size_usd": fixture.summary.current_grid.order_size_usd * 0.7}
            )
            correct += risk.grade(fixture, recommendation(**values)).verdict == "OK"
        assert correct / len(new_risk) <= 0.55


@pytest.mark.asyncio
async def test_risk_cloud_builder_receives_correct_ledger_role(monkeypatch):
    from unittest.mock import AsyncMock

    fixture = next(f for f in risk.FIXTURES if f.expect == "hold")
    monkeypatch.setattr(risk, "FIXTURE_SETS", {"v1": [fixture]})
    adapter = Answers([recommendation()])
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return adapter

    storage = AsyncMock()
    monkeypatch.setattr(risk, "_load_cloud_builder", lambda: build)
    monkeypatch.setattr(risk, "SQLiteStorageAdapter", lambda _path: storage)
    await risk.main_async(args(provider="openai"))
    assert built[0]["role"] == "risk"
    storage.close.assert_awaited_once()
