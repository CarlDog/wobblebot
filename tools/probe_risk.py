"""Probe the MoE RISK seat — capital preservation on a severity-graded battery.

Fourth role battery, after quant (`probe_advisor` / `probe_freejudge`),
arbitrator and news. It did not exist until 2026-08-11: PR #83 plumbed the
seven exposure fields into ``PerformanceSummary`` (total/coin exposure,
daily spend, their three caps, ``max_orders_per_coin``) via
``services/exposure.py`` — the SAME code ``GridEngine._check_safety``
enforces with — and then the battery was never built. This closes that gap.

**The answer space is narrower than quant's, and that shapes everything.**
``risk.md`` gives exactly two conservative levers — WIDER ``spacing_percentage``
and SMALLER ``order_size_usd`` — states that tighter spacing is never a
de-risk lever, and says a comfortable book means "no change is the honest
answer; do not manufacture a loosening". So the defensible answers are
DE-RISK and HOLD. Two classes, not three.

**Consequence, disclosed rather than hidden: a constant strategy floors at
~50% on direction here, not the ~33% the quant sets achieve.** That is
inherent to a binary answer space and cannot be fixed by rebalancing.
Splitting DE-RISK into "smaller size" vs "wider spacing" would give a third
class, but ``risk.md`` says "smaller ``order_size_usd``, **and/or** WIDER
spacing" — both are acceptable for the same situation, so inventing that
distinction would encode a rule the prompt does not state. That is exactly
how two `hard` fixtures went wrong on 2026-08-11.

**So the discriminating axis is SEVERITY, not direction** (operator-approved
framing). Each fixture carries a severity, and the same `hold` answer is
graded differently by it:

- ``comfortable`` — healthy headroom, shallow drawdown. HOLD is correct;
  de-risking is SUBOPTIMAL (manufacturing action).
- ``moderate`` — one pressure signal. DE-RISK is correct; HOLD is
  SUBOPTIMAL.
- ``severe`` — approaching a cap AND a fresh drawdown. DE-RISK is required;
  **HOLD is UNSAFE**, because failing to protect capital under compounding
  pressure is the exact failure this seat exists to prevent.

That makes constant-HOLD score ~50% on direction *while accumulating UNSAFE
calls*, and constant-DE-RISK score ~50% while never earning a clean sheet.
Neither degenerate strategy produces a safe high-scoring run.

**Known weak spot, stated plainly:** constant-DE-RISK scores ~50% with ZERO
unsafe calls. Always de-risking is economically destructive (it shrinks the
book toward nothing) but ``risk.md`` does not call it dangerous, so grading
it UNSAFE would be inventing a rule. It is graded SUBOPTIMAL and the
discriminator against it is OK%, not safety.

Tighter spacing is UNSAFE on every fixture regardless of severity —
``risk.md`` states that without qualification.

Run: ``python -m tools.probe_risk --provider openai --model gpt-5-mini``.
Spends real money under the ADR-014 cost gate (isolated
``data/probe_llm_cost.db``). ``--json`` appends a machine-readable
``JSON_RESULT:`` line.
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

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import load_operator_env
from wobblebot.config.logging import configure_logging
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

# A proposed value within +/-5% of current is "no meaningful change".
_DEADBAND = 0.05

Severity = Literal["comfortable", "moderate", "severe"]
Direction = Literal["de_risk", "hold", "loosen", "tighten"]
Verdict = Literal["OK", "SUBOPTIMAL", "UNSAFE", "ERROR"]


@dataclass(frozen=True)
class RiskFixture:
    """One exposure scenario plus its severity-graded expectation."""

    name: str
    summary: PerformanceSummary
    expect: Literal["de_risk", "hold"]
    severity: Severity
    note: str


@dataclass(frozen=True)
class Graded:
    name: str
    verdict: Verdict
    direction: Direction
    why: str


def _summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    spacing: float,
    order_size: float,
    coin_exposure: float,
    total_exposure: float,
    daily_spend: float,
    max_drawdown: float,
    win_rate: float = 0.6,
    cycle_count: int = 5,
    active_orders: int = 4,
    total_pnl: float = 0.0,
) -> PerformanceSummary:
    """Build an exposure window. Caps are fixed across the battery so the
    binding RATIO is the only thing that varies — the model is meant to
    react to headroom, not to memorise cap values."""
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=79000.0,
        snapshot_count=720,
        volatility=0.004,
        max_drawdown=max_drawdown,
        flatness=0.7,
        cycle_count=cycle_count,
        win_rate=win_rate,
        total_pnl=total_pnl,
        active_orders=active_orders,
        current_grid=CurrentGridParams(
            spacing_percentage=spacing,
            levels_above=4,
            levels_below=4,
            order_size_usd=order_size,
        ),
        total_exposure_usd=total_exposure,
        max_total_exposure_usd=150.0,
        coin_exposure_usd=coin_exposure,
        max_per_coin_exposure_usd=40.0,
        daily_spend_usd=daily_spend,
        max_daily_spend_usd=120.0,
        max_orders_per_coin=20,
    )


def classify(rec: AdvisorRecommendation, current: CurrentGridParams) -> Direction:
    """Map a recommendation onto risk.md's lever space.

    Order matters: a tighten is reported even when paired with a size cut,
    because risk.md forbids it unconditionally and a fixture must not be
    able to hide it behind a simultaneous de-risk.
    """
    spacing = _as_float(rec.recommendations.get("spacing_percentage"))
    size = _as_float(rec.recommendations.get("order_size_usd"))
    cur_spacing = current.spacing_percentage
    cur_size = current.order_size_usd

    if spacing is not None and cur_spacing:
        delta = (spacing - cur_spacing) / cur_spacing
        if delta < -_DEADBAND:
            return "tighten"

    wider = (
        spacing is not None and cur_spacing and (spacing - cur_spacing) / cur_spacing > _DEADBAND
    )
    smaller = size is not None and cur_size and (size - cur_size) / cur_size < -_DEADBAND
    larger = size is not None and cur_size and (size - cur_size) / cur_size > _DEADBAND

    if wider or smaller:
        return "de_risk"
    if larger:
        return "loosen"
    return "hold"


def grade(  # pylint: disable=too-many-return-statements
    fx: RiskFixture, rec: AdvisorRecommendation
) -> Graded:
    """Severity-graded verdict. One early return per (expectation, answer,
    severity) branch -- flattening them into a lookup would hide which
    risk.md rule each verdict comes from, which is the whole point.

    Rules: See the module docstring for the rules."""
    direction = classify(rec, fx.summary.current_grid)

    if direction == "tighten":
        return Graded(fx.name, "UNSAFE", direction, "tighter spacing is never a de-risk lever")

    if fx.expect == "de_risk":
        if direction == "de_risk":
            return Graded(fx.name, "OK", direction, "de-risked under pressure")
        if direction == "loosen":
            return Graded(fx.name, "UNSAFE", direction, "loosened INTO cap/drawdown pressure")
        # held
        if fx.severity == "severe":
            return Graded(
                fx.name, "UNSAFE", direction, "held through compounding pressure (severe)"
            )
        return Graded(fx.name, "SUBOPTIMAL", direction, "held where de-risking was warranted")

    # expect hold
    if direction == "hold":
        return Graded(fx.name, "OK", direction, "correctly left a comfortable book alone")
    if direction == "loosen":
        return Graded(fx.name, "SUBOPTIMAL", direction, "manufactured a loosening")
    return Graded(fx.name, "SUBOPTIMAL", direction, "de-risked a comfortable book")


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 18 fixtures: 9 comfortable / 4 moderate / 5 severe.
#
# That split is deliberate. It puts BOTH degenerate strategies at ~50%:
# constant-HOLD takes the 9 comfortable and eats 5 UNSAFE on the severe
# ones; constant-DE-RISK takes the 9 pressured ones plus 9 SUBOPTIMAL. An
# even 6/6/6 would have handed constant-DE-RISK 67%.
#
# Caps are identical on every fixture (total $150, per-coin $40, daily
# $120, 20 orders) so only the RATIO varies -- the model must react to
# headroom, not memorise cap values.
#
# Labels argued from risk.md BEFORE any model ran.
# ---------------------------------------------------------------------------
FIXTURES: tuple[RiskFixture, ...] = (
    RiskFixture(
        "calm_wide_headroom",
        _summary(
            spacing=1.2,
            order_size=10.0,
            coin_exposure=8.0,
            total_exposure=22.0,
            daily_spend=14.0,
            max_drawdown=-0.006,
            total_pnl=1.1,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Every ratio is loose: coin 20% of cap, total 15%, daily 12%, "
            "drawdown -0.6%. risk.md: 'no change is the honest answer; do not "
            "manufacture a loosening'."
        ),
    ),
    RiskFixture(
        "profitable_mid_headroom",
        _summary(
            spacing=1.4,
            order_size=10.0,
            coin_exposure=16.0,
            total_exposure=48.0,
            daily_spend=30.0,
            max_drawdown=-0.011,
            win_rate=0.7,
            cycle_count=7,
            total_pnl=2.4,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Coin 40% of cap, total 32%, daily 25% -- mid-range on every axis "
            "with a shallow drawdown and a profitable book. Nothing is "
            "approaching anything."
        ),
    ),
    RiskFixture(
        "half_used_but_flat_pnl",
        _summary(
            spacing=1.3,
            order_size=10.0,
            coin_exposure=20.0,
            total_exposure=60.0,
            daily_spend=44.0,
            max_drawdown=-0.014,
            total_pnl=-0.05,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Half the coin cap and a break-even book. Flat pnl is not a risk "
            "signal -- risk.md's triggers are cap proximity, fresh drawdown and "
            "thin daily headroom, none of which is present."
        ),
    ),
    RiskFixture(
        "daily_spend_used_early",
        _summary(
            spacing=1.5,
            order_size=8.0,
            coin_exposure=12.0,
            total_exposure=35.0,
            daily_spend=52.0,
            max_drawdown=-0.009,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Daily spend 43% with exposure ratios low. One moderate ratio in "
            "isolation is not 'approaching'; the binding constraint is still "
            "loose."
        ),
    ),
    RiskFixture(
        "recovered_drawdown_low_exposure",
        _summary(
            spacing=1.6,
            order_size=10.0,
            coin_exposure=10.0,
            total_exposure=26.0,
            daily_spend=18.0,
            max_drawdown=-0.021,
            win_rate=0.66,
            cycle_count=6,
            total_pnl=0.9,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "A -2.1% drawdown the book has traded through -- still cycling, "
            "still net positive, exposure light. Reacting now is the "
            "over-trading failure."
        ),
    ),
    RiskFixture(
        "small_book_many_orders",
        _summary(
            spacing=1.1,
            order_size=5.0,
            coin_exposure=14.0,
            total_exposure=30.0,
            daily_spend=26.0,
            max_drawdown=-0.01,
            active_orders=12,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "12 of 20 orders but each is small, so dollar exposure stays at 35% "
            "of the coin cap. Order COUNT is not the binding cap -- the dollar "
            "ratios are."
        ),
    ),
    RiskFixture(
        "wide_grid_light_exposure",
        _summary(
            spacing=2.4,
            order_size=10.0,
            coin_exposure=9.0,
            total_exposure=24.0,
            daily_spend=11.0,
            max_drawdown=-0.007,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Already conservatively spaced with light exposure. Widening "
            "further would be manufacturing action on a book with no pressure."
        ),
    ),
    RiskFixture(
        "fresh_session_minimal_exposure",
        _summary(
            spacing=1.3,
            order_size=10.0,
            coin_exposure=4.0,
            total_exposure=9.0,
            daily_spend=6.0,
            max_drawdown=-0.003,
            cycle_count=1,
            active_orders=2,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Barely started: 10% of the coin cap, 5% of daily. Thin activity is "
            "not a risk signal, and there is nothing to preserve against."
        ),
    ),
    RiskFixture(
        "steady_state_two_thirds_daily",
        _summary(
            spacing=1.35,
            order_size=9.0,
            coin_exposure=18.0,
            total_exposure=55.0,
            daily_spend=68.0,
            max_drawdown=-0.013,
            win_rate=0.64,
            cycle_count=8,
            total_pnl=1.7,
        ),
        expect="hold",
        severity="comfortable",
        note=(
            "Daily spend 57% -- the highest single ratio here -- but exposure "
            "is 45%/37% and the book is profitable with a shallow drawdown. One "
            "ratio past halfway is not 'approaching the cap'."
        ),
    ),
    RiskFixture(
        "coin_cap_nearly_full",
        _summary(
            spacing=1.3,
            order_size=10.0,
            coin_exposure=35.0,
            total_exposure=70.0,
            daily_spend=50.0,
            max_drawdown=-0.015,
        ),
        expect="de_risk",
        severity="moderate",
        note=(
            "Coin exposure 88% of its cap. risk.md: 'Approaching the caps ... "
            "reduce per-cycle exposure'. Drawdown is shallow, so this is "
            "pressure on ONE axis rather than compounding -- hold is "
            "defensible-but-not-ideal, not unsafe."
        ),
    ),
    RiskFixture(
        "daily_spend_nearly_exhausted",
        _summary(
            spacing=1.4,
            order_size=10.0,
            coin_exposure=17.0,
            total_exposure=52.0,
            daily_spend=106.0,
            max_drawdown=-0.012,
        ),
        expect="de_risk",
        severity="moderate",
        note=(
            "Daily spend 88% of cap with the day still running. Thin "
            "daily-spend headroom is a named trigger; smaller order_size_usd is "
            "the direct lever."
        ),
    ),
    RiskFixture(
        "total_cap_pressure_across_book",
        _summary(
            spacing=1.25,
            order_size=10.0,
            coin_exposure=22.0,
            total_exposure=131.0,
            daily_spend=60.0,
            max_drawdown=-0.016,
        ),
        expect="de_risk",
        severity="moderate",
        note=(
            "Total exposure 87% of the account-wide cap even though this coin "
            "is only at 55%. The binding ratio is the account, not the symbol "
            "-- risk.md asks for the tightest constraint, not an average."
        ),
    ),
    RiskFixture(
        "fresh_drawdown_light_exposure",
        _summary(
            spacing=1.3,
            order_size=10.0,
            coin_exposure=14.0,
            total_exposure=38.0,
            daily_spend=28.0,
            max_drawdown=-0.042,
            win_rate=0.4,
            cycle_count=3,
            total_pnl=-1.6,
        ),
        expect="de_risk",
        severity="moderate",
        note=(
            "A fresh -4.2% drawdown with a losing book. Exposure is light, so "
            "this is the drawdown trigger alone: reduce per-cycle exposure and "
            "ride it out. Widening is the documented defensive lever; "
            "tightening would bleed."
        ),
    ),
    RiskFixture(
        "cap_pressure_plus_fresh_drawdown",
        _summary(
            spacing=1.2,
            order_size=10.0,
            coin_exposure=37.0,
            total_exposure=118.0,
            daily_spend=98.0,
            max_drawdown=-0.045,
            win_rate=0.33,
            cycle_count=4,
            total_pnl=-3.1,
        ),
        expect="de_risk",
        severity="severe",
        note=(
            "Everything at once: coin 93%, total 79%, daily 82%, a fresh -4.5% "
            "drawdown and a losing book. Holding here is the "
            "capital-preservation failure this seat exists to prevent."
        ),
    ),
    RiskFixture(
        "all_three_caps_near_limit",
        _summary(
            spacing=1.15,
            order_size=10.0,
            coin_exposure=38.0,
            total_exposure=140.0,
            daily_spend=112.0,
            max_drawdown=-0.03,
            win_rate=0.45,
            cycle_count=6,
            total_pnl=-1.2,
        ),
        expect="de_risk",
        severity="severe",
        note=(
            "Coin 95%, total 93%, daily 93% -- no headroom left on any axis "
            "with a -3% drawdown. There is no version of this where no change "
            "is honest."
        ),
    ),
    RiskFixture(
        "deep_drawdown_high_exposure",
        _summary(
            spacing=1.3,
            order_size=12.0,
            coin_exposure=34.0,
            total_exposure=126.0,
            daily_spend=88.0,
            max_drawdown=-0.048,
            win_rate=0.28,
            cycle_count=5,
            total_pnl=-4.4,
        ),
        expect="de_risk",
        severity="severe",
        note=(
            "A -4.8% drawdown (just inside the engine's -5% guard, so it "
            "reaches the LLM) with 85% coin and 84% total exposure and the "
            "largest order size in the battery. Compounding pressure on every "
            "axis."
        ),
    ),
    RiskFixture(
        "cap_full_orders_maxed",
        _summary(
            spacing=1.1,
            order_size=10.0,
            coin_exposure=39.0,
            total_exposure=133.0,
            daily_spend=104.0,
            max_drawdown=-0.036,
            active_orders=19,
            win_rate=0.38,
            cycle_count=7,
            total_pnl=-2.0,
        ),
        expect="de_risk",
        severity="severe",
        note=(
            "Coin exposure 98% of cap, 19 of 20 orders placed, daily 87%, "
            "drawdown -3.6%. Every constraint is one step from binding at once."
        ),
    ),
    RiskFixture(
        "losing_streak_near_caps",
        _summary(
            spacing=1.25,
            order_size=11.0,
            coin_exposure=36.0,
            total_exposure=122.0,
            daily_spend=95.0,
            max_drawdown=-0.041,
            win_rate=0.22,
            cycle_count=9,
            total_pnl=-5.2,
        ),
        expect="de_risk",
        severity="severe",
        note=(
            "Nine cycles at 22% win for -$5.20 while sitting at 90% coin and "
            "81% total exposure with a -4.1% drawdown. The book is actively "
            "losing at near-maximum size."
        ),
    ),
)


def _load_cloud_builder() -> object:
    """Reuse ``probe_advisor._build_cloud_advisor`` by path-load (the repo's
    tool/test interop pattern) — no duplication of the cost-gated wiring."""
    path = _REPO_ROOT / "tools" / "probe_advisor.py"
    spec = importlib.util.spec_from_file_location("probe_advisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_advisor"] = module
    spec.loader.exec_module(module)
    # Deliberate reuse of the sibling tool's cost-gated builder.
    # pylint: disable-next=protected-access
    return module._build_cloud_advisor  # type: ignore[attr-defined]


def _severity_breakdown(rows: list[dict[str, object]]) -> str:
    """UNSAFE count per severity — the axis this battery is actually for.

    A model can score 50% on direction by answering the same thing every
    time (see the module docstring); what it cannot fake is a clean sheet
    on the severe fixtures, where holding is the capital-preservation
    failure.
    """
    parts = []
    for sev in ("comfortable", "moderate", "severe"):
        sub = [r for r in rows if r.get("severity") == sev]
        if not sub:
            continue
        unsafe = sum(1 for r in sub if r.get("verdict") == "UNSAFE")
        ok = sum(1 for r in sub if r.get("verdict") == "OK")
        parts.append(f"{sev}: {ok}/{len(sub)} OK, {unsafe} unsafe")
    return "  |  ".join(parts)


async def main_async(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
) -> int:
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
    # NOTE: _build_cloud_advisor has no `role` parameter, so cost rows in the
    # isolated probe db are tagged with its default. Harmless here (the probe
    # ledger is never the operator ledger) and not worth changing a builder
    # four other tools depend on.

    print(f"# risk battery: {len(FIXTURES)} fixtures (9 comfortable / 4 moderate / 5 severe)")
    print(f"# model: {args.provider}/{args.model}  prompt: {args.prompt_file}")
    counts = {"OK": 0, "SUBOPTIMAL": 0, "UNSAFE": 0, "ERROR": 0}
    rows: list[dict[str, object]] = []
    try:
        for fx in FIXTURES:
            t0 = time.monotonic()
            try:
                rec = await adapter.get_recommendation(fx.summary)
                result = grade(fx, rec)
                verdict, direction, why = result.verdict, result.direction, result.why
                confidence: str | None = rec.confidence
            except (AdvisorError, LLMCostCapExceeded) as exc:
                verdict, direction, why = "ERROR", "hold", str(exc)[:60]
                confidence = None
            elapsed = time.monotonic() - t0
            counts[verdict] = counts.get(verdict, 0) + 1
            rows.append(
                {
                    "name": fx.name,
                    "severity": fx.severity,
                    "expect": fx.expect,
                    "verdict": verdict,
                    "direction": direction,
                    "why": why,
                    "confidence": confidence,
                    "elapsed_s": round(elapsed, 1),
                }
            )
            print(
                f"  {fx.name:36} {verdict:11} dir={direction:8} "
                f"[{fx.severity}] ({elapsed:.1f}s)  {why}"
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
    print(f"BY SEVERITY  {_severity_breakdown(rows)}")
    if args.json:
        print(
            "JSON_RESULT: "
            + json.dumps(
                {
                    "model": args.model,
                    "provider": args.provider,
                    "counts": counts,
                    "rows": rows,
                }
            )
        )
    return 0


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider",
        default="openai",
        choices=("ollama", "openai", "anthropic", "google", "atlas"),
    )
    parser.add_argument("--prompt-file", default="config/prompts/risk.md")
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--session-cap", type=float, default=2.0)
    parser.add_argument("--daily-cap", type=float, default=5.0)
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
