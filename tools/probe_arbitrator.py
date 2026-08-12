"""Probe the MoE ARBITRATOR seat — and score it against the free baselines.

Sister to ``tools/probe_advisor.py`` (which grades the *quant* role) and
built for a different question. The arbitrator does not read market
metrics and produce an opinion; it reads **other experts' opinions** and
reconciles them. So its rubric comes from ``config/prompts/arbitrator.md``,
whose five numbered rules are mechanically checkable:

1. **Capital protection wins** — a high-confidence risk conservative call
   beats quant/news arguments to the contrary.
2. **News informs the rationale, never drives a number** — the reconciled
   dict must be justifiable from quant + risk alone (ADR-007).
3. **Quant + risk concord** → reconcile, high confidence.
4. **Disagreement → the more conservative PROPOSED value**, never an
   invented midpoint. Collective HOLD → omit the param.
5. **Insufficient signal** (collective low confidence) → no recommendations.

Plus two hard constraints: never reconcile below the ~0.66% fee floor, and
never emit a tighten (auto-apply discards it; prefer HOLD).

**Why this tool exists.** On 2026-08-10 a live MoE run produced two good
expert opinions and a garbage aggregate, because the arbitrator seat held a
model that could not follow the schema. Deterministic ``voting`` on the
same opinions produced a clean, correct result. That raised a question
nobody had tested: *does an LLM arbitrator earn its per-tick cost over
mechanical aggregation at all?* This battery answers it directly — every
fixture is scored for the candidate model AND for ``aggregate_voting`` and
``aggregate_weighted_confidence``, which are pure functions and therefore
free.

Expect the deterministic baselines to fail specific rules by construction:
they have no concept of expert ROLE (so rule 1 and rule 2 are unreachable)
and ``weighted_confidence`` averages (so rule 4's "never invent a midpoint"
is unreachable). Quantifying *which* rules they can't reach is the point —
it converts "should we pay for an arbitrator?" from opinion into a table.

Usage::

    python tools/probe_arbitrator.py                      # baselines only, free
    python tools/probe_arbitrator.py --model gpt-5-mini --provider openai
    python tools/probe_arbitrator.py --model phi4:14b-q8_0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wobblebot.cli._common import load_operator_env
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.aggregators import (
    aggregate_arbitrator,
    aggregate_voting,
    aggregate_weighted_confidence,
)

_LOGGER = logging.getLogger("wobblebot.tools.probe_arbitrator")

# The operator's live grid at time of writing. Fixtures express "wider" /
# "tighter" relative to this so the never-tighten constraint is concrete.
_CURRENT_SPACING = 3.0
_CURRENT_ORDER_SIZE = 5.0
# arbitrator.md: "Never reconcile to a spacing below the fee floor
# (~0.66%, the maker+taker round-trip break-even)."
_FEE_FLOOR = 0.66
# Float slop for "did the model restate the live spacing" — matches
# probe_news's constant of the same name so the two batteries agree on
# what counts as an echo.
_ECHO_TOLERANCE = 0.001


def _op(
    role: str,
    *,
    confidence: str = "high",
    rationale: str = "test opinion",
    **recs: Any,
) -> AdvisorRecommendation:
    """One expert opinion. ``recs`` become the recommendations dict."""
    return AdvisorRecommendation(
        recommendation_id=f"fix-{role}",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role=role,
        recommendations=dict(recs),
        rationale=rationale,
        confidence=confidence,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class Fixture:  # pylint: disable=too-many-instance-attributes
    """One arbitration scenario with an objectively-correct outcome.

    Nine fields trips pylint's 7-attribute default; this is a frozen
    fixture record, not a class with behaviour. The last two arrived
    2026-08-12 with order-size coverage.

    ``expect_spacing`` is the required reconciled value; ``None`` means the
    key MUST be omitted (a HOLD). ``forbid_spacing_change`` additionally
    asserts no spacing key at all, used where any number is a rule breach.
    """

    name: str
    rule: str
    opinions: list[AdvisorRecommendation]
    expect_spacing: float | None
    why: str
    forbid_any_recommendation: bool = False
    tolerance: float = 0.01
    #: Set True to also assert ``order_size_usd``. arbitrator.md names a
    #: smaller order size as a rule-1 conservative lever and rule 4 says
    #: "the smaller order size" outright, but v1 never exercised either —
    #: the whole set moved spacing only (gap closed 2026-08-12).
    #: ``expect_order_size=None`` with this True means the key must be
    #: ABSENT, matching ``expect_spacing`` semantics.
    assert_order_size: bool = False
    expect_order_size: float | None = None


def _fixtures() -> list[Fixture]:
    """Eight fixtures, one or more per arbitration rule.

    Every expected value is a value some expert actually PROPOSED (rule 4
    forbids invented midpoints), except where the rule requires omission.
    """
    return [
        Fixture(
            name="concord_widen",
            rule="3 (quant+risk concord)",
            opinions=[
                _op("quant", spacing_percentage=3.5, rationale="Vol expanding."),
                _op("risk", spacing_percentage=3.8, rationale="Drawdown deepening."),
                _op("news", confidence="low", rationale="Nothing material."),
            ],
            expect_spacing=3.8,
            why="Both widen; rule 4 takes the MORE CONSERVATIVE proposed value (3.8), not 3.65.",
        ),
        Fixture(
            name="risk_overrides_quant",
            rule="1 (capital protection wins)",
            opinions=[
                _op("quant", spacing_percentage=1.0, rationale="Calm; tighten to cycle."),
                _op("risk", spacing_percentage=4.0, rationale="Drawdown near cap."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=4.0,
            why="High-confidence risk conservative call beats the quant tighten.",
        ),
        Fixture(
            name="news_cannot_drive_a_number",
            rule="2 (news informs, never drives)",
            opinions=[
                _op("quant", confidence="medium", rationale="No change warranted."),
                _op("risk", confidence="medium", rationale="Ample headroom; hold."),
                _op("news", spacing_percentage=5.0, rationale="Exchange outage chatter."),
            ],
            expect_spacing=None,
            why="Only news proposed a number; ADR-007 forbids it driving the value. HOLD.",
        ),
        Fixture(
            name="garbage_expert_ignored",
            rule="4 (reconcile the coherent experts)",
            opinions=[
                _op("quant", rationale="ema_stochart_ata", **{"data_ema_data": 0}),
                _op("risk", spacing_percentage=3.5, rationale="Modest de-risk."),
                _op("news", spacing_percentage=3.5, rationale="Nothing material."),
            ],
            expect_spacing=3.5,
            why=(
                "The 2026-08-10 live failure: one expert emits salad; "
                "follow the two coherent ones."
            ),
        ),
        Fixture(
            name="all_low_confidence",
            rule="5 (insufficient signal)",
            opinions=[
                _op("quant", confidence="low", spacing_percentage=3.4, rationale="Unsure."),
                _op("risk", confidence="low", spacing_percentage=3.6, rationale="Thin window."),
                _op("news", confidence="low", rationale="No signal."),
            ],
            expect_spacing=None,
            why="Collective low confidence → return no recommendations.",
            forbid_any_recommendation=True,
        ),
        Fixture(
            name="never_emit_a_tighten",
            rule="constraint (tighten is discarded; prefer HOLD)",
            opinions=[
                _op("quant", spacing_percentage=0.8, rationale="Very calm."),
                _op("risk", spacing_percentage=0.9, rationale="Low exposure."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=None,
            why=f"Both propose below the live {_CURRENT_SPACING}%; a tighten cannot land, so HOLD.",
        ),
        Fixture(
            name="never_below_fee_floor",
            rule="constraint (fee floor ~0.66%)",
            opinions=[
                _op("quant", spacing_percentage=0.5, rationale="Scalp it."),
                _op("risk", spacing_percentage=0.55, rationale="Small size instead."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=None,
            why=f"Both proposals sit under the {_FEE_FLOOR}% break-even; never reconcile there.",
        ),
        Fixture(
            name="no_invented_midpoint",
            rule="4 (more conservative PROPOSED value)",
            opinions=[
                _op("quant", spacing_percentage=3.2, rationale="Slight widen."),
                _op("risk", spacing_percentage=4.5, rationale="Bigger widen; drawdown."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=4.5,
            why="4.5 is the more conservative PROPOSED value; 3.85 would be invented.",
        ),
    ]


def _fixtures_gen2_extra() -> list[Fixture]:
    """Nine boundary cases (2026-08-12) — v1 could not rank its candidates.

    v1 put gpt-5-mini at 23/24 and claude-haiku-4-5 at 24/24 over three
    rounds: a one-point spread inside the measured run-to-run noise. Same
    diagnosis as the news battery's v1 — EXHAUSTED, not unsound — and the
    same fix.

    Two gaps these close:

    - **v1 moved spacing only.** arbitrator.md names a smaller
      ``order_size_usd`` as a rule-1 conservative lever and rule 4 says
      "the smaller order size" outright. Neither was ever tested.
    - **v1's cases are each one rule in isolation.** The interesting
      failures are where two rules point different ways, and the prompt
      resolves it explicitly — most sharply when risk asks for something
      the constraints forbid.

    Every label is quoted from arbitrator.md in its ``why``. Cases whose
    correct answer the prompt does NOT settle were deliberately left out
    rather than resolved by author's fiat — that mistake has already cost
    three fixtures across two other batteries.
    """
    return [
        Fixture(
            name="risk_proposes_a_tighten",
            rule="1 x constraint (risk wins, but tightening is not safety)",
            opinions=[
                _op("quant", rationale="Regime unchanged; no case for a move."),
                _op(
                    "risk",
                    spacing_percentage=2.0,
                    rationale="Cut exposure — tighten so cycles complete faster.",
                ),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=None,
            why=(
                "The sharpest collision in the prompt. Rule 1 says a "
                "high-confidence risk call wins — and the SAME rule says "
                "'*Tightening is NOT a safety move on this bot; widening is*', "
                "with the constraint adding 'never output a tighten the gate "
                "will discard; prefer HOLD.' A model applying 'risk wins' "
                "mechanically emits 2.0. Correct answer is HOLD."
            ),
        ),
        Fixture(
            name="news_echoes_quant_number",
            rule="2 (news informs, never drives) — the true-negative twin",
            opinions=[
                _op(
                    "quant",
                    spacing_percentage=3.8,
                    confidence="medium",
                    rationale="ATR expanded; 3.8 fits the realised range.",
                ),
                _op("risk", confidence="medium", rationale="Headroom fine; no objection."),
                _op(
                    "news",
                    spacing_percentage=3.8,
                    rationale="Exchange-outage chatter; widen.",
                ),
            ],
            expect_spacing=3.8,
            why=(
                "Twin of news_cannot_drive_a_number. Rule 2 bars news from "
                "being 'the SOLE driver of any numeric value' — here 3.8 is "
                "independently justifiable from quant alone, so it stands. A "
                "model over-applying rule 2 discards a good number because "
                "news happened to agree with it."
            ),
        ),
        Fixture(
            name="concord_hold",
            rule="3 x 4 (concord can mean HOLD)",
            opinions=[
                _op("quant", rationale="Grid is well matched to the regime."),
                _op("risk", rationale="Exposure comfortable on every axis."),
                _op("news", confidence="low", rationale="Nothing material."),
            ],
            expect_spacing=None,
            why=(
                "Rule 3 concord with high confidence, but nobody proposed a "
                "number. Rule 4: 'If the experts collectively favor HOLD, the "
                "reconciled output is HOLD — omit the param rather than "
                "averaging toward a change.' Tests that concord does not mean "
                "'must emit something'."
            ),
        ),
        Fixture(
            name="conservative_is_the_smaller_size",
            rule="4 (the smaller order size)",
            opinions=[
                _op("quant", order_size_usd=8.0, rationale="Scale up; grid is working."),
                _op("risk", order_size_usd=4.0, rationale="Trim per-cycle exposure."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=None,
            assert_order_size=True,
            expect_order_size=4.0,
            why=(
                "Rule 4 names this lever explicitly — 'the wider spacing, the "
                "smaller order size'. v1 never tested it. 6.0 would be the "
                "invented midpoint; 8.0 is the less conservative proposal."
            ),
        ),
        Fixture(
            name="risk_cuts_size_quant_widens_spacing",
            rule="1 (different levers are not a disagreement)",
            opinions=[
                _op("quant", spacing_percentage=3.6, rationale="Range widened; space out."),
                _op("risk", order_size_usd=3.0, rationale="Trim size; drawdown building."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=3.6,
            assert_order_size=True,
            expect_order_size=3.0,
            why=(
                "Both experts are conservative on DIFFERENT levers, so rule 4's "
                "'pick the more conservative' never fires — there is nothing to "
                "choose between. Both proposals stand. A model that treats any "
                "two different numbers as conflict drops one of them."
            ),
        ),
        Fixture(
            name="only_risk_is_confident",
            rule="5 (insufficient signal needs COLLECTIVE low confidence)",
            opinions=[
                _op("quant", confidence="low", rationale="Window too thin to call."),
                _op("risk", spacing_percentage=4.2, rationale="Drawdown approaching the cap."),
                _op("news", confidence="low", rationale="No signal."),
            ],
            expect_spacing=4.2,
            why=(
                "Twin of all_low_confidence. Rule 5 fires when 'the experts "
                "COLLECTIVELY express low confidence' — two of three is not "
                "collective, and rule 1 puts a high-confidence risk concern in "
                "charge. Counting low-confidence heads instead of reading rule "
                "1 yields a wrong HOLD."
            ),
        ),
        Fixture(
            name="silent_expert_is_not_dissent",
            rule="4 (silence is not a competing proposal)",
            opinions=[
                _op("quant", spacing_percentage=3.6, rationale="Realised range widened."),
                _op(
                    "risk",
                    confidence="medium",
                    rationale="Ample headroom; no objection to a widen.",
                ),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=3.6,
            why=(
                "Risk proposes no number and explicitly does not object. Rule 4 "
                "resolves DISAGREEMENT between proposed values; there is only "
                "one proposal, so it stands. Reading silence as dissent "
                "produces an unwarranted HOLD."
            ),
        ),
        Fixture(
            name="news_tips_but_does_not_drive",
            rule="2 (news may tip a close call, not invent a value)",
            opinions=[
                _op("quant", spacing_percentage=3.4, rationale="Mild widen fits the range."),
                _op("risk", spacing_percentage=3.6, rationale="Slightly wider; drawdown edging."),
                _op("news", rationale="Regulatory action lands tonight; expect volatility."),
            ],
            expect_spacing=3.6,
            why=(
                "Rule 2's second half: 'News may shape your narrative and tip a "
                "close call toward caution.' Caution here means the more "
                "conservative PROPOSED value (3.6) — not a bigger number "
                "nobody proposed. A model that escalates to 4.5 because the "
                "news is alarming has let news drive."
            ),
        ),
        Fixture(
            name="one_expert_below_floor_other_widens",
            rule="constraint (a sub-floor proposal is discarded, not contagious)",
            opinions=[
                _op("quant", spacing_percentage=0.6, rationale="Scalp the chop."),
                _op("risk", spacing_percentage=3.9, rationale="Drawdown; widen instead."),
                _op("news", confidence="low", rationale="Quiet."),
            ],
            expect_spacing=3.9,
            why=(
                "Twin of never_below_fee_floor, where BOTH proposals sat under "
                "the floor and HOLD was right. Here only one does; it is "
                "discarded, and rule 4's more-conservative survivor (3.9) is a "
                "legitimate widen. Treating one bad proposal as poisoning the "
                "whole reconciliation produces a wrong HOLD."
            ),
        ),
    ]


FIXTURE_SETS: dict[str, list[Fixture]] = {
    "v1": _fixtures(),
    "gen2": _fixtures() + _fixtures_gen2_extra(),
}


def _summary() -> PerformanceSummary:
    """A neutral market window. The arbitrator reconciles opinions, so the
    summary is context, not the thing under test."""
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=64000.0,
        snapshot_count=720,
        volatility=0.004,
        max_drawdown=-0.01,
        flatness=0.9,
        cycle_count=3,
        win_rate=0.66,
        total_pnl=0.15,
        active_orders=6,
        current_grid=CurrentGridParams(
            spacing_percentage=_CURRENT_SPACING,
            levels_above=3,
            levels_below=3,
            order_size_usd=_CURRENT_ORDER_SIZE,
        ),
    )


@dataclass
class Verdict:
    fixture: str
    rule: str
    ok: bool
    detail: str
    got: Any = None


@dataclass
class Scorecard:
    label: str
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(1 for v in self.verdicts if v.ok)

    @property
    def total(self) -> int:
        return len(self.verdicts)


def _grade_order_size(fx: Fixture, recs: dict[str, Any]) -> Verdict | None:
    """Check ``order_size_usd``; ``None`` means it passed, keep grading.

    Returned early from :func:`_grade` so a correct spacing call cannot
    mask a wrong size — rule 1 treats a smaller size as a first-class
    conservative lever, not a footnote to spacing.
    """
    size = recs.get("order_size_usd")
    if fx.expect_order_size is None:
        if size is None:
            return None
        return Verdict(
            fx.name, fx.rule, False, f"expected order_size_usd omitted, got {size}", got=size
        )
    if size is None:
        return Verdict(
            fx.name,
            fx.rule,
            False,
            f"expected order_size_usd {fx.expect_order_size}, got omitted",
            got=None,
        )
    try:
        if abs(float(size) - fx.expect_order_size) <= fx.tolerance:
            return None
    except (TypeError, ValueError):
        return Verdict(fx.name, fx.rule, False, f"non-numeric order_size {size!r}", got=size)
    return Verdict(
        fx.name,
        fx.rule,
        False,
        f"expected order_size_usd {fx.expect_order_size}, got {size}",
        got=size,
    )


def _grade(  # pylint: disable=too-many-return-statements
    fx: Fixture, result: AdvisorRecommendation
) -> Verdict:
    """Check one reconciled recommendation against the fixture's rule.

    Seven returns rather than six: each is a distinct, separately-worded
    verdict, and collapsing them would trade a reader's ability to see
    which rule was breached for a metric.
    """
    recs = result.recommendations or {}
    spacing = recs.get("spacing_percentage")

    if fx.forbid_any_recommendation:
        ok = not recs
        return Verdict(fx.name, fx.rule, ok, "expected NO recommendations", got=recs)

    if fx.assert_order_size:
        size_verdict = _grade_order_size(fx, recs)
        if size_verdict is not None:
            return size_verdict

    if fx.expect_spacing is None:
        # HOLD: the key must be absent. A tighten is the specific breach.
        if spacing is None:
            return Verdict(fx.name, fx.rule, True, "correctly omitted spacing", got=None)
        # An ECHO of the live spacing is semantically a HOLD, not a change,
        # and the auto-apply gate treats it as a no-op. Grading it as a
        # failure calls the CORRECT decision a breach — and on the fee-floor
        # and never-tighten fixtures it would report the safest possible
        # answer as the dangerous one.
        #
        # probe_news learned this on 2026-08-10 (a first-draft rubric scored
        # qwen3.6 at 5/12 for emitting exactly 3.0 on five quiet windows) and
        # grew ``_is_echo``. This battery did not, so the two siblings
        # disagreed on the same question until 2026-08-12, when the local
        # sweep surfaced it: llama3.1:8b-q4 answers HOLD by restating 3.0 and
        # was losing four fixtures for being right. arbitrator.md does prefer
        # omission ("Omit any field you don't want to change"), so this is a
        # style deviation — not a rule breach, and not scored as one.
        if isinstance(spacing, int | float) and abs(spacing - _CURRENT_SPACING) <= _ECHO_TOLERANCE:
            return Verdict(fx.name, fx.rule, True, f"held (echoed live {spacing})", got=spacing)
        detail = f"expected HOLD (omit spacing), got {spacing}"
        if isinstance(spacing, int | float) and spacing < _FEE_FLOOR:
            detail += f" — BELOW FEE FLOOR {_FEE_FLOOR}"
        elif isinstance(spacing, int | float) and spacing < _CURRENT_SPACING:
            detail += f" — TIGHTEN below live {_CURRENT_SPACING}, auto-apply would discard"
        return Verdict(fx.name, fx.rule, False, detail, got=spacing)

    if spacing is None:
        return Verdict(
            fx.name, fx.rule, False, f"expected {fx.expect_spacing}, got HOLD/omitted", got=None
        )
    try:
        ok = abs(float(spacing) - fx.expect_spacing) <= fx.tolerance
    except (TypeError, ValueError):
        return Verdict(fx.name, fx.rule, False, f"non-numeric spacing {spacing!r}", got=spacing)
    return Verdict(
        fx.name, fx.rule, ok, f"expected {fx.expect_spacing}, got {spacing}", got=spacing
    )


def _score_deterministic(label: str, fn: Any, fixtures: list[Fixture]) -> Scorecard:
    card = Scorecard(label=label)
    for fx in fixtures:
        try:
            result = fn(fx.opinions)
        except Exception as exc:  # noqa: BLE001 - a baseline that raises is a failure
            card.verdicts.append(Verdict(fx.name, fx.rule, False, f"{type(exc).__name__}: {exc}"))
            continue
        card.verdicts.append(_grade(fx, result))
    return card


async def _score_llm(label: str, arbitrator: Any, fixtures: list[Fixture]) -> Scorecard:
    card = Scorecard(label=label)
    summary = _summary()
    for fx in fixtures:
        try:
            result = await aggregate_arbitrator(fx.opinions, arbitrator, summary)
        except AdvisorError as exc:
            card.verdicts.append(Verdict(fx.name, fx.rule, False, f"ERROR: {exc}"))
            continue
        card.verdicts.append(_grade(fx, result))
    return card


_CLOUD_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "atlas": "ATLASCLOUD_API_KEY",
}
# OpenAI-compatible gateway; see the same note in probe_advisor.py.
_ATLAS_BASE_URL = "https://api.atlascloud.ai"


async def _build_llm(args: argparse.Namespace) -> tuple[Any, Any]:
    """Return (arbitrator, storage_or_None).

    All four adapters accept the ``extra_context`` kwarg, so all satisfy
    ``ArbitratorAdvisor`` — the protocol docstring's "future cloud
    adapters will add the same kwarg" is stale as of 2026-08-10.
    """
    prompt = load_prompt(Path(args.prompt_file))
    if args.provider == "ollama":
        from wobblebot.adapters.ollama import OllamaAdapter

        return (
            OllamaAdapter(
                model=args.model,
                prompt=prompt,
                role="arbitrator",
                base_url=args.base_url,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
            ),
            None,
        )

    # Cloud: mirrors probe_advisor's construction — key from env, a
    # directly-built cost gate, and an ISOLATED ledger so probe spend
    # never pollutes the operator's real llm_calls table.
    import os
    from decimal import Decimal

    from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
    from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
    from wobblebot.services.llm_retry import LLMRetryConfig

    key_var = _CLOUD_KEY_ENV[args.provider]
    api_key = os.environ.get(key_var)
    if not api_key:
        raise SystemExit(f"{key_var} missing from environment (.env / shell)")
    storage = SQLiteStorageAdapter("data/probe_llm_cost.db")
    await storage.connect()
    common: dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        "role": "arbitrator",
        "api_key": api_key,
        "storage": storage,
        "session_tracker": SessionCostTracker(),
        "cost_config": LLMCostConfig(
            max_spend_per_day_usd=Decimal(str(args.daily_cap)),
            max_spend_per_session_usd=Decimal(str(args.session_cap)),
            enforce=True,
        ),
        "retry_config": LLMRetryConfig(max_retries=2, initial_backoff_seconds=1.0),
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout_seconds": args.timeout_seconds,
    }
    if args.provider in ("openai", "atlas"):
        from wobblebot.adapters.openai import OpenAIAdvisorAdapter

        if args.provider == "atlas":
            return OpenAIAdvisorAdapter(base_url=_ATLAS_BASE_URL, **common), storage
        return (
            OpenAIAdvisorAdapter(
                organization=os.environ.get("OPENAI_ORGANIZATION") or None, **common
            ),
            storage,
        )
    if args.provider == "anthropic":
        from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter

        return AnthropicAdvisorAdapter(**common), storage
    from wobblebot.adapters.google import GoogleAdvisorAdapter

    return GoogleAdvisorAdapter(**common), storage


def _report(cards: list[Scorecard], fixtures: list[Fixture]) -> None:
    by_name = {f.name: f for f in fixtures}
    for card in cards:
        _LOGGER.info("=== %s: %d/%d ===", card.label, card.score, card.total)
        for v in card.verdicts:
            _LOGGER.info(
                "  %-28s %-42s %s",
                v.fixture,
                v.detail,
                "OK" if v.ok else f"FAIL   [rule {by_name[v.fixture].rule}]",
            )


async def _run(args: argparse.Namespace) -> int:
    fixtures = FIXTURE_SETS[args.fixture_set]
    _LOGGER.info(
        "arbitrator battery [%s]: %d fixtures across %d rules",
        args.fixture_set,
        len(fixtures),
        len({fx.rule.split()[0] for fx in fixtures}),
    )
    cards = [
        _score_deterministic("voting (free)", aggregate_voting, fixtures),
        _score_deterministic("weighted_confidence (free)", aggregate_weighted_confidence, fixtures),
    ]
    if args.model:
        llm, storage = await _build_llm(args)
        try:
            cards.append(await _score_llm(f"arbitrator {args.model}", llm, fixtures))
        finally:
            aclose = getattr(llm, "aclose", None)
            if aclose is not None:
                await aclose()
            if storage is not None:
                await storage.close()

    _report(cards, fixtures)
    if args.json:
        payload = {
            "fixtures": len(fixtures),
            "cards": [
                {
                    "label": c.label,
                    "score": c.score,
                    "max_score": c.total,
                    "verdicts": [
                        {"fixture": v.fixture, "rule": v.rule, "ok": v.ok, "detail": v.detail}
                        for v in c.verdicts
                    ],
                }
                for c in cards
            ],
        }
        print(f"JSON_RESULT: {json.dumps(payload)}")
    return 0


def main() -> int:
    # Match probe_freejudge / probe_news / probe_risk: this module's
    # docstring is non-ASCII and argparse prints it as the description, so
    # even `--help` raised UnicodeEncodeError on a cp1252 console
    # (pre-existing; caught 2026-08-11). probe_advisor / probe_assistant /
    # probe_discord_bot lack this too but their help text is currently
    # ASCII-clean, so they fail only latently.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="Arbitrator model; omit for baselines only.")
    parser.add_argument(
        "--provider",
        default="ollama",
        choices=("ollama", "openai", "anthropic", "google", "atlas"),
    )
    parser.add_argument("--session-cap", type=float, default=2.0)
    parser.add_argument("--daily-cap", type=float, default=5.0)
    parser.add_argument("--prompt-file", default="config/prompts/arbitrator.md")
    parser.add_argument(
        "--base-url",
        default="http://localhost:11434",
        help="Ollama endpoint. The NAS runs the deployed models on a "
        "CPU-only box, so point here to measure REAL deployment latency "
        "(e.g. http://carldog-nas:11434); ignored for cloud providers.",
    )
    parser.add_argument(
        "--fixture-set",
        choices=tuple(FIXTURE_SETS),
        default="gen2",
        help="v1 = the original eight (CEILINGED - 23/24 vs 24/24, inside the "
        "noise); gen2 = v1 plus nine boundary cases. Default gen2.",
    )
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    # Cloud providers read their key from the operator's .env; use the
    # project's own cwd-based discovery rather than a bespoke call.
    load_operator_env()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
