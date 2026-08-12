"""Pin the arbitrator battery's design, especially the gen2 rebuild.

v1 put gpt-5-mini at 23/24 and claude-haiku-4-5 at 24/24 over three
rounds — a one-point spread inside the measured run-to-run noise, so it
could not rank its own candidates. Not unsound; exhausted. `gen2` adds
nine boundary cases (2026-08-12).

These tests run the baseline check BEFORE any model does, and pin the two
properties the rebuild depends on: that the deterministic aggregators
still fail by construction (they are the "does an LLM arbitrator earn its
cost" control), and that every gen2 case is a rule COLLISION rather than
a rule restated.
"""

# pylint: disable=protected-access

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "probe_arbitrator.py"
    spec = importlib.util.spec_from_file_location("probe_arbitrator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_arbitrator"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


class TestDeterministicBaselines:
    """The free aggregators must stay clearly beatable — they are the
    control that answers "is an LLM in this seat worth paying for?"."""

    def _frac(self, name: str, fn) -> float:  # type: ignore[no-untyped-def]
        fixtures = mod.FIXTURE_SETS[name]
        return mod._score_deterministic("x", fn, fixtures).score / len(fixtures)

    def test_gen2_lowered_the_free_floor(self) -> None:
        """The point of the rebuild, stated as a test.

        On v1 free ``voting`` scores 5/8 = 62% — high enough that the gap
        to a competent LLM (23-24/24) was carried by only ~3 fixtures.
        gen2's boundary cases are ones a role-blind aggregator cannot
        reach, so the floor drops. If a future edit raises it back, the
        battery has quietly become easier to fake.
        """
        assert self._frac("gen2", mod.aggregate_voting) < self._frac("v1", mod.aggregate_voting)

    def test_the_free_floor_on_gen2_stays_beatable(self) -> None:
        for label, fn in [
            ("voting", mod.aggregate_voting),
            ("weighted_confidence", mod.aggregate_weighted_confidence),
        ]:
            frac = self._frac("gen2", fn)
            assert frac <= 0.55, f"{label} scores {frac:.0%} on gen2 — no longer a floor"

    def test_the_free_aggregators_cannot_reach_the_role_rules(self) -> None:
        """They have no concept of expert ROLE, so rules 1 and 2 are
        structurally unreachable. If one ever passes both, the fixtures
        have stopped testing what their names claim."""
        fixtures = mod.FIXTURE_SETS["gen2"]
        card = mod._score_deterministic("voting", mod.aggregate_voting, fixtures)
        role_rules = [v for v in card.verdicts if v.rule.startswith(("1", "2"))]
        assert role_rules, "no rule-1/rule-2 fixtures found"
        assert not all(v.ok for v in role_rules)


class TestGen2Shape:
    def test_gen2_is_a_superset_of_v1(self) -> None:
        """v1 stays byte-identical so its historical scores remain
        comparable — quant/heldout was frozen for the same reason."""
        v1 = [f.name for f in mod.FIXTURE_SETS["v1"]]
        gen2 = [f.name for f in mod.FIXTURE_SETS["gen2"]]
        assert gen2[: len(v1)] == v1
        assert len(gen2) == len(v1) + 9

    def test_hold_and_act_are_both_well_represented(self) -> None:
        """A set skewed to HOLD would hand "always omit" a good score."""
        gen2 = mod.FIXTURE_SETS["gen2"]
        holds = sum(1 for f in gen2 if f.expect_spacing is None and not f.assert_order_size)
        assert 4 <= holds <= len(gen2) - 4

    def test_order_size_is_actually_exercised(self) -> None:
        """arbitrator.md names a smaller order_size as a rule-1 lever and
        rule 4 says "the smaller order size" — v1 tested neither."""
        assert not any(f.assert_order_size for f in mod.FIXTURE_SETS["v1"])
        assert sum(1 for f in mod.FIXTURE_SETS["gen2"] if f.assert_order_size) >= 2

    def test_every_new_fixture_argues_its_label(self) -> None:
        """A label the prompt doesn't settle measures the fixture author.
        Three fixtures across two other batteries died of this."""
        extra = mod.FIXTURE_SETS["gen2"][len(mod.FIXTURE_SETS["v1"]) :]
        for fx in extra:
            assert len(fx.why) > 120, f"{fx.name}: rationale too thin to audit"

    def test_expected_values_were_proposed_by_some_expert(self) -> None:
        """Rule 4 forbids invented midpoints — the ANSWER KEY must obey
        the same rule it grades, or the battery teaches the violation."""
        for fx in mod.FIXTURE_SETS["gen2"]:
            if fx.expect_spacing is None:
                continue
            proposed = {
                op.recommendations.get("spacing_percentage")
                for op in fx.opinions
                if op.recommendations
            }
            assert fx.expect_spacing in proposed, f"{fx.name}: {fx.expect_spacing} was invented"


class TestOrderSizeGrading:
    def _fx(self, name: str):  # type: ignore[no-untyped-def]
        return next(f for f in mod.FIXTURE_SETS["gen2"] if f.name == name)

    def test_a_wrong_size_fails_even_with_the_right_spacing(self) -> None:
        """Graded before spacing so a correct spacing call cannot mask a
        wrong size — rule 1 treats size as a first-class lever."""
        fx = self._fx("risk_cuts_size_quant_widens_spacing")
        rec = mod._op("arbitrator", spacing_percentage=3.6, order_size_usd=8.0)
        result = mod._grade(fx, rec)
        assert not result.ok
        assert "order_size_usd" in result.detail

    def test_the_invented_midpoint_on_size_fails(self) -> None:
        fx = self._fx("conservative_is_the_smaller_size")
        assert not mod._grade(fx, mod._op("arbitrator", order_size_usd=6.0)).ok
        assert mod._grade(fx, mod._op("arbitrator", order_size_usd=4.0)).ok

    def test_a_stray_size_fails_a_spacing_only_fixture(self) -> None:
        """Emitting a lever nobody proposed is generating a novel
        proposal, which the prompt's opening paragraph forbids."""
        fx = self._fx("news_tips_but_does_not_drive")
        assert fx.assert_order_size is False
        fx_strict = mod.Fixture(
            name=fx.name,
            rule=fx.rule,
            opinions=fx.opinions,
            expect_spacing=fx.expect_spacing,
            why=fx.why,
            assert_order_size=True,
            expect_order_size=None,
        )
        rec = mod._op("arbitrator", spacing_percentage=3.6, order_size_usd=4.0)
        assert not mod._grade(fx_strict, rec).ok


class TestRuleCoverage:
    def test_gen2_covers_every_numbered_rule_plus_the_constraints(self) -> None:
        rules = Counter(fx.rule.split()[0] for fx in mod.FIXTURE_SETS["gen2"])
        for rule in ("1", "2", "3", "4", "5", "constraint"):
            assert rules[rule] >= 1, f"rule {rule} unrepresented"

    def test_the_tighten_collision_is_present(self) -> None:
        """The single most valuable case: rule 1 says risk wins, the same
        rule says tightening is not safety, and the constraint says never
        emit a tighten. A model applying "risk wins" mechanically fails."""
        fx = next(f for f in mod.FIXTURE_SETS["gen2"] if f.name == "risk_proposes_a_tighten")
        assert fx.expect_spacing is None
        risk = next(o for o in fx.opinions if o.role == "risk")
        assert risk.recommendations["spacing_percentage"] < mod._CURRENT_SPACING
        assert risk.confidence == "high"
