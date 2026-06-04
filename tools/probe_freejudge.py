"""Grade an LLM free judge on the NO-GUARD battery (ADR-022 follow-up).

Post-ADR-022 the advisor is *guards + LLM free judge*: the deterministic
heuristic makes only the four clear guard calls and escalates every other
tick to the LLM. The model bake-off (`docs/reference/advisor-llm-models.md`,
2026-06-04) picked `gpt-5-mini` — but it scored the candidates on the
`heldout` battery, of which only 3 fixtures actually escalate. This battery
is the gold-standard follow-up that ADR-022 promised: a purpose-built set of
**no-guard** scenarios — the ambiguous middle the free judge actually owns
in production.

**The oracle problem.** A no-guard tick has no mechanical "right answer"
(that's *why* it reaches the LLM and gets tracked against real outcomes over
the soak). So this battery does NOT key fixtures to a curve. Instead each
fixture is scored against the bot's **risk model**:

- ``acceptable`` — the set of directions a sound free judge could defend
  given the regime (e.g. ``{hold, widen}``).
- ``forbidden`` — the actively *unsafe* call for that regime, if any. For a
  grid whose dominant failure is over-tightening, this is almost always
  ``tighten`` (into a developing trend, toward/below the fee floor, or over a
  grid that's working).

Verdicts: **OK** (acceptable), **SUBOPTIMAL** (defensible but not ideal —
e.g. an over-trade on a hold-only fixture), **UNSAFE** (the forbidden call,
or a spacing below the ~0.52% fee floor — always unsafe regardless of label).

**Self-check (load-bearing).** Every fixture, run through the *real*
``HeuristicAdvisorAdapter`` loaded from the shipped ``quant.yml``, MUST
report ``clear_match=False`` (no guard fires). If a guard fires, the fixture
isn't a no-guard case and doesn't belong here — `main()` refuses to run and
``tests/tools/test_freejudge_battery.py`` fails loudly. This keeps the
battery honest if a guard threshold is ever retuned.

Run: ``python tools/probe_freejudge.py --provider openai --model gpt-5-mini
--max-tokens 4000``. Spends real money via the provider API under the
ADR-014 cost gate (isolated ``data/probe_llm_cost.db``). ``--json`` appends a
machine-readable ``JSON_RESULT:`` line.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import load_operator_env
from wobblebot.config.heuristic import load_heuristic_spec
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIPPED_SPEC = _REPO_ROOT / "config" / "heuristic" / "quant.yml"
_FEE_FLOOR = 0.52  # 2x the 0.26% maker fee — a spacing below this can't clear fees
_HOLD_DEADBAND = 0.05  # |Δspacing|/current below this reads as "no meaningful change"

Direction = Literal["widen", "hold", "tighten"]
Verdict = Literal["OK", "SUBOPTIMAL", "UNSAFE", "ERROR"]


@dataclass(frozen=True)
class NoGuardFixture:
    """One no-guard scenario + its risk-model labels.

    ``acceptable`` is the set of directions a sound free judge could defend;
    ``forbidden`` is the actively-unsafe call (or None). ``note`` records the
    regime read so the label is auditable.
    """

    name: str
    summary: PerformanceSummary
    acceptable: frozenset[Direction]
    forbidden: Direction | None
    note: str


def _summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    current_spacing: float,
    volatility: float,
    max_drawdown: float,
    win_rate: float,
    cycle_count: int,
    flatness: float,
    total_pnl: float = 0.0,
    latest_price: float = 79000.0,
    snapshot_count: int = 720,
    active_orders: int = 6,
) -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=latest_price,
        snapshot_count=snapshot_count,
        volatility=volatility,
        max_drawdown=max_drawdown,
        flatness=flatness,
        cycle_count=cycle_count,
        win_rate=win_rate,
        total_pnl=total_pnl,
        active_orders=active_orders,
        current_grid=CurrentGridParams(
            spacing_percentage=current_spacing,
            levels_above=4,
            levels_below=4,
            order_size_usd=10.0,
        ),
    )


# 14 no-guard fixtures spanning the ambiguous middle. Each is verified
# guard-free by `verify_no_guard` (and the test). Labels follow the bot's
# risk model: over-tightening (into a trend, toward the fee floor, or over a
# working grid) is the cardinal sin, so `forbidden` is usually `tighten`.
FIXTURES: tuple[NoGuardFixture, ...] = (
    NoGuardFixture(
        "well_matched_ranging",
        _summary(
            current_spacing=1.2,
            volatility=0.004,
            max_drawdown=-0.008,
            win_rate=0.62,
            cycle_count=6,
            flatness=0.78,
        ),
        acceptable=frozenset({"hold"}),
        forbidden=None,
        note="Spacing proportionate to vol, healthy fills, shallow dd, ranging — leave it.",
    ),
    NoGuardFixture(
        "too_tight_churning_active",
        _summary(
            current_spacing=0.6,
            volatility=0.009,
            max_drawdown=-0.015,
            win_rate=0.30,
            cycle_count=9,
            flatness=0.40,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note="Active market, lots of fills but low win — fees eat a too-tight grid; widen.",
    ),
    NoGuardFixture(
        "too_wide_calm_starved",
        _summary(
            current_spacing=2.2,
            volatility=0.0025,
            max_drawdown=-0.005,
            win_rate=0.70,
            cycle_count=1,
            flatness=0.85,
        ),
        acceptable=frozenset({"tighten", "hold"}),
        forbidden="widen",
        note="Wide grid in a calm range barely filling — tighten toward fills; widening is wrong.",
    ),
    NoGuardFixture(
        "developing_downtrend_mild",
        _summary(
            current_spacing=1.3,
            volatility=0.005,
            max_drawdown=-0.035,
            win_rate=0.35,
            cycle_count=2,
            flatness=0.35,
            total_pnl=-22.0,
            latest_price=74000.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="Price drifting down (dd -3.5%, trending), few cycles — never tighten into a trend.",
    ),
    NoGuardFixture(
        "developing_uptrend_mild",
        _summary(
            current_spacing=1.3,
            volatility=0.005,
            max_drawdown=-0.010,
            win_rate=0.55,
            cycle_count=4,
            flatness=0.40,
            total_pnl=18.0,
            latest_price=83000.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden=None,
        note=(
            "Mild uptrend, grid catching some — hold/widen; tightening to chase "
            "over-trades (SUBOPTIMAL, not dangerous — unlike tightening into a downtrend)."
        ),
    ),
    NoGuardFixture(
        "whipsaw_midspacing",
        _summary(
            current_spacing=1.8,
            volatility=0.012,
            max_drawdown=-0.020,
            win_rate=0.60,
            cycle_count=6,
            flatness=0.30,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="High-vol chop at mid spacing, decent fills — tightening into whipsaw raises churn.",
    ),
    NoGuardFixture(
        "near_floor_active_not_calm",
        _summary(
            current_spacing=0.65,
            volatility=0.006,
            max_drawdown=-0.010,
            win_rate=0.45,
            cycle_count=7,
            flatness=0.50,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="Near the fee floor but NOT calm (so no guard) — can't tighten below the floor.",
    ),
    NoGuardFixture(
        "slightly_tight_but_healthy",
        _summary(
            current_spacing=0.9,
            volatility=0.006,
            max_drawdown=-0.012,
            win_rate=0.78,
            cycle_count=7,
            flatness=0.50,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="A touch tight for vol but fills are healthy (just below dont_fix) — don't disrupt.",
    ),
    NoGuardFixture(
        "slightly_wide_moderate",
        _summary(
            current_spacing=1.8,
            volatility=0.006,
            max_drawdown=-0.008,
            win_rate=0.68,
            cycle_count=3,
            flatness=0.60,
        ),
        acceptable=frozenset({"tighten", "hold"}),
        forbidden=None,
        note="A bit wide for moderate vol, modest fills — tighten toward ideal or hold; both fine.",
    ),
    NoGuardFixture(
        "ambiguous_thin_mixed",
        _summary(
            current_spacing=1.2,
            volatility=0.005,
            max_drawdown=-0.020,
            win_rate=0.50,
            cycle_count=3,
            flatness=0.55,
            snapshot_count=40,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden=None,
        note=(
            "Mixed signal on a thin window — an honest low-confidence HOLD or a "
            "defensive widen on the -2% drawdown are both defensible."
        ),
    ),
    NoGuardFixture(
        "moderate_drawdown_below_guard",
        _summary(
            current_spacing=1.1,
            volatility=0.004,
            max_drawdown=-0.045,
            win_rate=0.40,
            cycle_count=3,
            flatness=0.45,
            total_pnl=-12.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="dd -4.5%, just shy of the -5% defensive guard — still lean defensive, never tighten.",
    ),
    NoGuardFixture(
        "high_vol_tight_low_win",
        _summary(
            current_spacing=0.8,
            volatility=0.011,
            max_drawdown=-0.025,
            win_rate=0.32,
            cycle_count=6,
            flatness=0.30,
        ),
        acceptable=frozenset({"widen"}),
        forbidden="tighten",
        note="Tight grid in high-vol whipsaw, low win — clearly too tight for the vol; widen.",
    ),
    NoGuardFixture(
        "calm_well_matched_lowcycle",
        _summary(
            current_spacing=0.9,
            volatility=0.002,
            max_drawdown=-0.004,
            win_rate=0.70,
            cycle_count=2,
            flatness=0.90,
        ),
        acceptable=frozenset({"hold"}),
        forbidden="widen",
        note=(
            "Calm, ranging, spacing at the vol-ideal — hold; a tighten just chases the "
            "fee floor (SUBOPTIMAL), widening is wrong."
        ),
    ),
    NoGuardFixture(
        "recovering_after_dip",
        _summary(
            current_spacing=1.4,
            volatility=0.007,
            max_drawdown=-0.030,
            win_rate=0.60,
            cycle_count=8,
            flatness=0.45,
            total_pnl=6.0,
        ),
        acceptable=frozenset({"hold", "widen"}),
        forbidden="tighten",
        note="Volatile then recovering, decent cycles — work through it; don't tighten into risk.",
    ),
)


def classify_direction(proposed: object, current: float | None) -> Direction:
    """Map a proposed spacing to widen/hold/tighten vs the current grid.

    An omitted / non-numeric / current-less proposal is a deliberate HOLD
    (the prompt tells the model to omit fields it won't change). Within
    ±``_HOLD_DEADBAND`` of current reads as no meaningful change → hold.
    """
    pf = _as_float(proposed)
    if pf is None or current is None or current == 0:
        return "hold"
    delta = (pf - current) / current
    if abs(delta) < _HOLD_DEADBAND:
        return "hold"
    return "widen" if delta > 0 else "tighten"


def score_fixture(rec: AdvisorRecommendation, fx: NoGuardFixture) -> tuple[Verdict, Direction, str]:
    """Grade one recommendation against a fixture's risk-model labels."""
    proposed = rec.recommendations.get("spacing_percentage")
    direction = classify_direction(proposed, fx.summary.current_grid.spacing_percentage)
    pf = _as_float(proposed)
    if pf is not None and pf < _FEE_FLOOR:
        return "UNSAFE", direction, f"proposed {pf}% is below the {_FEE_FLOOR}% fee floor"
    if fx.forbidden is not None and direction == fx.forbidden:
        return "UNSAFE", direction, f"made the forbidden call ({fx.forbidden}) for this regime"
    if direction in fx.acceptable:
        return "OK", direction, "acceptable"
    return "SUBOPTIMAL", direction, f"defensible-but-not-ideal (acceptable={sorted(fx.acceptable)})"


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def verify_no_guard(
    fixtures: tuple[NoGuardFixture, ...] = FIXTURES,
    spec_path: Path = _SHIPPED_SPEC,
) -> list[tuple[str, str]]:
    """Return ``(name, guard_reason)`` for any fixture that WRONGLY fires a
    guard under the shipped heuristic. Empty list = the battery is valid."""
    adapter = HeuristicAdvisorAdapter(spec=load_heuristic_spec(spec_path))
    offenders: list[tuple[str, str]] = []
    for fx in fixtures:
        verdict = adapter.evaluate(fx.summary)
        if verdict.clear_match:
            offenders.append((fx.name, verdict.reason))
    return offenders


def _load_cloud_builder() -> object:
    """Reuse ``probe_advisor._build_cloud_advisor`` by path-load (the repo's
    tool/test interop pattern) — no duplication of the cost-gated wiring."""
    path = _REPO_ROOT / "tools" / "probe_advisor.py"
    spec = importlib.util.spec_from_file_location("probe_advisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_advisor"] = module
    spec.loader.exec_module(module)
    # Deliberate reuse of the sibling tool's cost-gated builder (no duplication).
    # pylint: disable-next=protected-access
    return module._build_cloud_advisor  # type: ignore[attr-defined]


async def main_async(args: argparse.Namespace) -> int:  # pylint: disable=too-many-locals
    offenders = verify_no_guard()
    if offenders:
        print("error: battery integrity check FAILED — these fixtures fire a guard:")
        for name, reason in offenders:
            print(f"  {name}: {reason}")
        return 3

    prompt = load_prompt(Path(args.prompt_file))
    build = _load_cloud_builder()
    storage = SQLiteStorageAdapter("data/probe_llm_cost.db")
    Path("data").mkdir(exist_ok=True)
    await storage.connect()
    adapter: AdvisorPort = build(  # type: ignore[operator]
        provider=args.provider,
        model=args.model,
        prompt=prompt,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout_seconds=args.timeout_seconds,
        storage=storage,
        session_cap=args.session_cap,
        daily_cap=args.daily_cap,
    )

    print(f"# free-judge battery: {len(FIXTURES)} no-guard fixtures (all guard-free ✓)")
    print(f"# model: {args.provider}/{args.model}  prompt: {args.prompt_file}")
    counts = {"OK": 0, "SUBOPTIMAL": 0, "UNSAFE": 0, "ERROR": 0}
    rows: list[dict[str, object]] = []
    try:
        for fx in FIXTURES:
            t0 = time.monotonic()
            try:
                rec = await adapter.get_recommendation(fx.summary)
                verdict, direction, why = score_fixture(rec, fx)
                spacing = rec.recommendations.get("spacing_percentage", "—")
            except (AdvisorError, LLMCostCapExceeded) as exc:
                verdict, direction, why, spacing = "ERROR", "hold", str(exc)[:60], "—"
            elapsed = time.monotonic() - t0
            counts[verdict] = counts.get(verdict, 0) + 1
            rows.append(
                {
                    "name": fx.name,
                    "verdict": verdict,
                    "direction": direction,
                    "spacing": str(spacing),
                    "forbidden": fx.forbidden,
                    "acceptable": sorted(fx.acceptable),
                    "why": why,
                    "elapsed_s": round(elapsed, 1),
                }
            )
            print(
                f"  {fx.name:32s} {verdict:10s} dir={direction:7s} "
                f"spacing={str(spacing):7s} ({elapsed:.1f}s)  {why}"
            )
    finally:
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            await aclose()
        await storage.close()

    safe = counts["OK"] + counts["SUBOPTIMAL"]
    print(
        f"\nSUMMARY  OK={counts['OK']}  SUBOPTIMAL={counts['SUBOPTIMAL']}  "
        f"UNSAFE={counts['UNSAFE']}  ERROR={counts['ERROR']}  "
        f"(non-unsafe {safe}/{len(FIXTURES)})"
    )
    if args.json:
        print(
            "JSON_RESULT: "
            + json.dumps(
                {"model": args.model, "provider": args.provider, "counts": counts, "rows": rows}
            )
        )
    return 0


def main() -> int:
    load_operator_env()
    p = argparse.ArgumentParser(
        prog="tools.probe_freejudge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--provider", choices=("openai", "anthropic", "google"), default="openai")
    p.add_argument("--model", default="gpt-5-mini")
    p.add_argument("--prompt-file", default="config/prompts/quant.md")
    p.add_argument("--max-tokens", type=int, default=4000)
    p.add_argument("--temperature", type=float, default=0.5)
    p.add_argument("--timeout-seconds", type=float, default=120.0)
    p.add_argument("--session-cap", type=float, default=2.0)
    p.add_argument("--daily-cap", type=float, default=5.0)
    p.add_argument("--json", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
