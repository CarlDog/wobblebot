"""Pin the news battery's design, especially the gen2 rebuild (2026-08-11).

`v1` ceilinged — haiku-4-5, sonnet-5 and opus-5 all scored 12/12, so it
could no longer rank the models it exists to rank. It was never UNSOUND
(constant baseline 58% vs 100% best), just exhausted. `gen2` adds ten
boundary cases.

These tests run the constant-baseline check BEFORE any model does, which
is the discipline that took three batteries months to learn. They also
pin the property the new fixtures were built around: half the hold-cases
contain a named high-impact trigger word, so a keyword-matcher scores
badly on purpose.

Note on ``_grade``: probe_news keeps its grader private where the sibling
``probe_risk`` exposes ``grade``. Testing a module's own private grader is
the normal thing to do, so the access is disabled here rather than
renaming a function mid-flight — but the naming inconsistency across the
three battery tools is worth a tidy-up pass.
"""

# pylint: disable=protected-access

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "probe_news.py"
    spec = importlib.util.spec_from_file_location("probe_news", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_news"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

# news.md's named high-impact categories, plus the venue words that make a
# keyword-matcher fire. The gen2 hold-cases are seeded with these on purpose.
_TRIGGER_WORDS = (
    "regulat",
    "suspend",
    "hack",
    "breach",
    "withdrawal",
    "exchange",
    "fed ",
    "liquidity",
)


def _rec(spacing: float | None, confidence: str = "medium") -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id="test",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="news",
        recommendations={} if spacing is None else {"spacing_percentage": spacing},
        rationale="test",
        confidence=confidence,
    )


def _score(fixtures: list, spacing: float | None, confidence: str = "medium") -> int:
    return sum(1 for fx in fixtures if mod._grade(fx, _rec(spacing, confidence)).ok)


class TestConstantBaselines:
    """No degenerate strategy may look competent on either set."""

    @pytest.mark.parametrize("name", ["v1", "gen2"])
    def test_no_constant_strategy_beats_sixty_percent(self, name: str) -> None:
        fixtures = mod.FIXTURE_SETS[name]
        # constant-HOLD omits spacing; constant-WIDEN always raises it.
        for label, spacing in [("HOLD", None), ("WIDEN", 4.5)]:
            frac = _score(fixtures, spacing, "low") / len(fixtures)
            assert frac <= 0.60, f"{name}: constant-{label} scores {frac:.0%}"

    @pytest.mark.parametrize("name", ["v1", "gen2"])
    def test_constant_tighten_is_near_zero(self, name: str) -> None:
        """news.md forbids narrowing outright, so this must score terribly."""
        fixtures = mod.FIXTURE_SETS[name]
        assert _score(fixtures, 1.0) == 0

    def test_gen2_keeps_the_hold_imbalance(self) -> None:
        """The honest prior is that most windows warrant nothing; an even
        split would hand constant-WIDEN 50%."""
        counts = Counter(fx.expect for fx in mod.FIXTURE_SETS["gen2"])
        assert counts == {"hold": 13, "widen": 9}

    def test_a_perfect_reader_scores_100_on_gen2(self) -> None:
        """Headroom check — the set must be winnable, or it measures nothing."""
        fixtures = mod.FIXTURE_SETS["gen2"]
        ok = sum(
            1
            for fx in fixtures
            if mod._grade(
                fx,
                _rec(None, "low") if fx.expect == "hold" else _rec(4.5),
            ).ok
        )
        assert ok == len(fixtures)


class TestGen2IsHarderNotDifferent:
    def test_gen2_is_a_superset_of_v1(self) -> None:
        """Historical v1 scores must stay comparable — gen2 ADDS, never
        edits. (quant/heldout was frozen rather than fixed for the same
        reason.)"""
        v1_names = [fx.name for fx in mod.FIXTURE_SETS["v1"]]
        gen2_names = [fx.name for fx in mod.FIXTURE_SETS["gen2"]]
        assert gen2_names[: len(v1_names)] == v1_names
        assert len(gen2_names) == len(v1_names) + 10

    def test_keyword_matching_is_actively_punished(self) -> None:
        """The gen2 design premise: a model that widens whenever it sees a
        named trigger word must FAIL a meaningful share of hold-cases. If
        this drops to zero the new fixtures have lost their teeth."""
        extra = mod.FIXTURE_SETS["gen2"][len(mod.FIXTURE_SETS["v1"]) :]
        holds = [fx for fx in extra if fx.expect == "hold"]
        decoys = [
            fx
            for fx in holds
            if any(w in " ".join(i.headline for i in fx.items).lower() for w in _TRIGGER_WORDS)
        ]
        assert len(decoys) >= 4, f"only {len(decoys)} of {len(holds)} hold-cases carry a decoy"

    def test_every_new_fixture_argues_its_label(self) -> None:
        """A label the prompt doesn't support measures the fixture author,
        not the model — two `hard` fixtures were withdrawn for exactly
        that."""
        extra = mod.FIXTURE_SETS["gen2"][len(mod.FIXTURE_SETS["v1"]) :]
        for fx in extra:
            assert len(fx.why) > 100, f"{fx.name}: rationale too thin to audit"

    def test_widen_and_hold_twins_exist(self) -> None:
        """Several gen2 cases are deliberately paired — same category word,
        opposite answer — so the discriminator is the reasoning, not the
        vocabulary."""
        names = {fx.name: fx.expect for fx in mod.FIXTURE_SETS["gen2"]}
        for hold_case, widen_case in [
            ("favorable_regulatory_clarity", "regulatory_deadline_imminent"),
            ("distant_macro_event", "macro_print_imminent"),
            ("resolved_major_incident", "degrading_venue_health"),
        ]:
            assert names[hold_case] == "hold"
            assert names[widen_case] == "widen"


class TestOutOfLaneStillFailsOutright:
    def test_touching_order_size_fails_even_with_the_right_spacing(self) -> None:
        """news.md: 'Don't touch order_size_usd or level counts ... omit
        them.' Leaving the role is a failure regardless of the call."""
        fx = next(f for f in mod.FIXTURE_SETS["gen2"] if f.name == "liquidity_withdrawal_lane_trap")
        rec = _rec(4.5)
        rec.recommendations["order_size_usd"] = 8.0
        result = mod._grade(fx, rec)
        assert result.verdict == "OUT_OF_LANE"
        assert not result.ok
