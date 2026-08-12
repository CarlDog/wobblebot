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
class Fixture:
    """One arbitration scenario with an objectively-correct outcome.

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
            why="The 2026-08-10 live failure: one expert emits salad; follow the two coherent ones.",
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


def _grade(fx: Fixture, result: AdvisorRecommendation) -> Verdict:
    """Check one reconciled recommendation against the fixture's rule."""
    recs = result.recommendations or {}
    spacing = recs.get("spacing_percentage")

    if fx.forbid_any_recommendation:
        ok = not recs
        return Verdict(fx.name, fx.rule, ok, "expected NO recommendations", got=recs)

    if fx.expect_spacing is None:
        # HOLD: the key must be absent. A tighten is the specific breach.
        if spacing is None:
            return Verdict(fx.name, fx.rule, True, "correctly omitted spacing", got=None)
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
    fixtures = _fixtures()
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
