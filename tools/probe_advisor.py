"""Probe the trading-advisor LLM's recommendation quality.

Calls ``OllamaAdapter.get_recommendation`` directly against an
Ollama-served model with a battery of canned ``PerformanceSummary``
scenarios. Prints each call's outcome: schema-validity, directional
correctness, magnitude reasonability, and wall-clock latency.

**Sister to ``tools/probe_assistant.py``** which exercises the
OPERATOR-ASSISTANT role. The advisor role's measurement model is
different: there's no single "right answer" per scenario — only a
direction-correct + magnitude-sensible band. The scoring rubric below
reflects that.

**Fixture battery (rev 2026-05-29).** The original 6-fixture battery
was gameable: it used ONE fixed baseline spacing for every scenario,
so a model emitting a constant value (or a constant "+10% widen")
scored well by accident of fixture distribution (the documented
"11/18 lazy baseline"). This battery fixes that:

1. **Per-fixture baseline spacing, decoupled from direction.** Each of
   the three directions (widen / hold / tighten) spans the FULL range
   of current spacing values, so current spacing alone predicts
   nothing. The correct direction is ``sign(ideal(vol) - current)``,
   so a model must read volatility *relative to* the current grid.
   Overlap fixtures (widen at HIGH spacing under extreme vol; tighten
   at LOW spacing under dead-calm) actively punish any constant answer.
2. **No partial credit for not-holding.** On an action fixture,
   holding / omitting spacing scores 0 (MISS); on a hold fixture, any
   change scores 0 (OVERTRADE). Only a correct call earns points. This
   closes the "always hold / omit spacing" loophole (now 33% = chance,
   not 56%) and the earlier OVERTRADE=1 credit leak.
3. **Wide current<->ideal gaps on every action fixture.** Each action
   fixture's ideal sits clearly outside the +/-5% hold deadband around
   its current spacing, so a correct move is unambiguous and the
   direction deadband can't collide with the magnitude band (no
   "timid-but-correct scored as MISS" dead zones).

**Discriminator + its limit.** A do-nothing (always-hold) model scores
33% (chance). A *constant* answer cannot be driven below ~52%: with
three direction classes and a +/-30% magnitude band, a constant near
the population-median spacing is direction-correct on one whole
direction's fixtures plus coincidental hold/tighten matches (the worst
case is ~1.9, scoring 19/36). This ~52% ceiling is inherent — pushing
it lower means shrinking the band (penalizes real reasoners) or
reintroducing dead zones. A genuine reasoner should clear ~75%+, so
the SCORE still ranks reasoners above constants — but the headline
number alone can rank a ~52% near-constant above a weak (~50%)
reasoner. So ALWAYS inspect the top model's per-fixture VERDICT
PROFILE: a reasoner earns OK across all three directions with ~zero
WRONG, whereas a constant shows OK clustered on one direction and
multiple WRONG on the opposite. If no candidate clears ~60%, no
NAS-viable model reasons well for this task — itself a useful finding.

**The ideal-spacing-vs-volatility curve below is the load-bearing
judgment** — it generates every fixture's expected direction and the
magnitude target. Operator-reviewed + independently re-derived by a
5-agent blind adjudication (12/12 unanimous) 2026-05-29. See
``docs/reference/advisor-llm-models.md``.

**Use when:**

- Editing ``config/prompts/quant.md`` and you want to know whether the
  advisor's recommendations still move in the right direction.
- Swapping models (``advisor.model``) and you want a quality check
  before pointing cli/advise at the new one. Point ``--base-url`` at a
  remote Ollama (e.g. the NAS) to probe where the model actually runs.

Run as: ``python tools/probe_advisor.py``
Override the model via ``--model``, the host via ``--base-url``, the
per-call budget via ``--timeout-seconds``. ``--json`` appends a single
machine-readable ``JSON_RESULT:`` line consumed by
``tools/pull_and_probe_advisors.py``.

No external state mutated. No Discord traffic. No DB writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.config.prompts import load_prompt
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_SECONDS = 180.0

# Ideal grid spacing (%) as a rough function of realized per-tick
# volatility. This is the maintainer judgment that defines "correct":
# a grid wants spacing wide enough that round-trips clear ~2x the
# 0.26% maker fee and capture the typical swing, tight enough to fill
# often. Direction per fixture = sign(ideal(vol) - current_spacing);
# the magnitude target is the ideal value.
#
#   vol (per-tick sigma)   ideal spacing %
#   .0008  (dead quiet)        0.65
#   .002   (calm)              0.90
#   .003   (calm+)             1.05
#   .004   (moderate)          1.25
#   .006   (moderate+)         1.60
#   .008   (active)            1.90
#   .012   (whipsaw)           2.50
#   .014   (extreme whipsaw)   2.70

# Magnitude band: a direction-correct recommendation that lands within
# +/-30% of the ideal spacing earns full marks; outside that band it's
# right-direction-wrong-size (OVERSHOOT/undershoot). Every action
# fixture keeps its current spacing clearly outside the +/-5% hold
# deadband from its ideal, so this band never collides with the
# direction deadband (see _assert_no_dead_zone in the tests).
_MAGNITUDE_BAND = 0.30


@dataclass(frozen=True)
class Fixture:
    """One probe scenario.

    ``expected`` is the correct coarse direction ("widen" | "hold" |
    "tighten"); ``ideal_spacing`` is the magnitude target used to grade
    a direction-correct recommendation. ``summary`` carries the market
    metrics + the per-fixture current grid (whose
    ``spacing_percentage`` is what direction is judged against).
    """

    name: str
    expected: str
    ideal_spacing: float
    summary: PerformanceSummary


def _summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    current_spacing: float,
    volatility: float,
    max_drawdown: float,
    win_rate: float,
    flatness: float,
    cycle_count: int,
    active_orders: int,
    latest_price: float = 79000.0,
) -> PerformanceSummary:
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=latest_price,
        snapshot_count=720,
        volatility=volatility,
        max_drawdown=max_drawdown,
        flatness=flatness,
        cycle_count=cycle_count,
        win_rate=win_rate,
        total_pnl=0.0,
        active_orders=active_orders,
        current_grid=CurrentGridParams(
            spacing_percentage=current_spacing,
            levels_above=4,
            levels_below=4,
            order_size_usd=10.0,
        ),
    )


# 12 fixtures, balanced 4 WIDEN / 4 HOLD / 4 TIGHTEN. Within each
# direction the current spacing spans the full range, so a constant
# output cannot be directionally right across the battery. The
# "overlap" fixtures (annotated) are the load-bearing decouplers:
# WIDEN at high spacing (W3/W4) and TIGHTEN at low spacing (T1) force
# the model to read volatility relative to the current grid. Tighten
# ideals are de-clustered (0.65/1.05/1.25/1.60) so no single constant
# can collect OK on more than one tighten fixture, and every action
# fixture's current spacing sits clearly outside the +/-5% deadband
# from its ideal (no direction/magnitude dead zones).
FIXTURES: tuple[Fixture, ...] = (
    # ---- WIDEN: current spacing well below the vol-appropriate ideal ----
    Fixture(
        "widen_tight_moderate",
        "widen",
        1.25,
        _summary(
            current_spacing=0.6,
            volatility=0.004,
            max_drawdown=-0.010,
            win_rate=0.45,
            flatness=0.50,
            cycle_count=3,
            active_orders=6,
        ),
    ),
    Fixture(
        "widen_tight_active",
        "widen",
        1.90,
        _summary(
            current_spacing=1.0,
            volatility=0.008,
            max_drawdown=-0.020,
            win_rate=0.35,
            flatness=0.40,
            cycle_count=5,
            active_orders=5,
        ),
    ),
    Fixture(
        "widen_whipsaw_widegrid",  # OVERLAP: widen at relatively HIGH current spacing
        "widen",
        2.50,
        _summary(
            current_spacing=1.5,
            volatility=0.012,
            max_drawdown=-0.030,
            win_rate=0.40,
            flatness=0.30,
            cycle_count=7,
            active_orders=4,
        ),
    ),
    Fixture(
        "widen_extreme_whipsaw",  # OVERLAP: widen at relatively HIGH current spacing
        "widen",
        2.70,
        _summary(
            current_spacing=1.7,
            volatility=0.014,
            max_drawdown=-0.038,
            win_rate=0.33,
            flatness=0.25,
            cycle_count=8,
            active_orders=3,
        ),
    ),
    # ---- HOLD: current spacing matched to the vol-appropriate ideal ----
    Fixture(
        "hold_quiet_matched",
        "hold",
        0.65,
        _summary(
            current_spacing=0.65,
            volatility=0.0008,
            max_drawdown=-0.002,
            win_rate=1.0,
            flatness=0.95,
            cycle_count=5,
            active_orders=8,
        ),
    ),
    Fixture(
        "hold_moderate_matched",
        "hold",
        1.25,
        _summary(
            current_spacing=1.25,
            volatility=0.004,
            max_drawdown=-0.008,
            win_rate=0.75,
            flatness=0.55,
            cycle_count=6,
            active_orders=6,
        ),
    ),
    Fixture(
        "hold_active_matched",
        "hold",
        1.90,
        _summary(
            current_spacing=1.9,
            volatility=0.008,
            max_drawdown=-0.012,
            win_rate=0.70,
            flatness=0.45,
            cycle_count=6,
            active_orders=6,
        ),
    ),
    Fixture(
        "hold_whipsaw_matched",  # high vol but WIDE spacing already => HOLD
        "hold",
        2.50,
        _summary(
            current_spacing=2.5,
            volatility=0.012,
            max_drawdown=-0.015,
            win_rate=0.65,
            flatness=0.35,
            cycle_count=6,
            active_orders=5,
        ),
    ),
    # ---- TIGHTEN: current spacing well above the vol-appropriate ideal ----
    Fixture(
        "tighten_quiet_toowide",  # OVERLAP: tighten at relatively LOW current spacing
        "tighten",
        0.65,
        _summary(
            current_spacing=0.95,
            volatility=0.0008,
            max_drawdown=-0.002,
            win_rate=1.0,
            flatness=0.92,
            cycle_count=2,
            active_orders=8,
        ),
    ),
    Fixture(
        "tighten_calm_overwide",
        "tighten",
        1.05,
        _summary(
            current_spacing=1.5,
            volatility=0.003,
            max_drawdown=-0.004,
            win_rate=0.90,
            flatness=0.80,
            cycle_count=2,
            active_orders=7,
        ),
    ),
    Fixture(
        "tighten_moderate_overwide",
        "tighten",
        1.25,
        _summary(
            current_spacing=2.0,
            volatility=0.004,
            max_drawdown=-0.006,
            win_rate=0.85,
            flatness=0.60,
            cycle_count=2,
            active_orders=6,
        ),
    ),
    Fixture(
        "tighten_active_overwide",
        "tighten",
        1.60,
        _summary(
            current_spacing=2.4,
            volatility=0.006,
            max_drawdown=-0.008,
            win_rate=0.80,
            flatness=0.50,
            cycle_count=2,
            active_orders=6,
        ),
    ),
)


def _classify_direction(
    rec: AdvisorRecommendation,
    current: CurrentGridParams,
) -> str:
    """Map a recommendation's spacing change to a coarse direction.

    Returns one of ``"tighten"``, ``"widen"``, ``"hold"``. An omitted
    ``spacing_percentage`` is a deliberate "no change" => ``"hold"``
    (the prompt instructs the model to omit fields it doesn't want to
    change). Direction is judged against the CURRENT spacing; magnitude
    quality is judged separately against the ideal (see ``_magnitude_ok``).
    """
    new_spacing = rec.recommendations.get("spacing_percentage")
    if new_spacing is None or current.spacing_percentage is None:
        return "hold"
    try:
        ns = float(new_spacing)
    except (TypeError, ValueError):
        return "hold"
    cs = current.spacing_percentage
    delta_pct = (ns - cs) / cs if cs != 0 else 0.0
    # Within +/-5% of current is "no meaningful change" => hold.
    if abs(delta_pct) < 0.05:
        return "hold"
    return "widen" if delta_pct > 0 else "tighten"


def _magnitude_ok(
    rec: AdvisorRecommendation,
    ideal_spacing: float,
    band: float = _MAGNITUDE_BAND,
) -> bool:
    """Is the recommended spacing within +/-band of the ideal target?

    Graded against the fixture's IDEAL (not the current spacing) so a
    correct large correction — e.g. 2.0% -> ~1.25% when the market is
    moderate — can still earn full marks. A missing spacing can't be
    magnitude-graded; return True so the (hold-classified) row is
    scored purely on direction.
    """
    new_spacing = rec.recommendations.get("spacing_percentage")
    if new_spacing is None or ideal_spacing <= 0:
        return True
    try:
        ns = float(new_spacing)
    except (TypeError, ValueError):
        return True
    return ideal_spacing * (1 - band) <= ns <= ideal_spacing * (1 + band)


def _score_row(expected: str, actual: str, magnitude_ok: bool) -> tuple[int, str]:
    """Map (expected, actual, magnitude_ok) to a 0-3 score + verdict token.

    Only a correct call earns points; both ways of being wrong about
    whether to act score 0 (no partial credit), which is what closes
    the always-hold and constant-minimum loopholes:

      OK        3  right direction + magnitude within band (holds: always)
      OVERSHOOT 2  right direction, magnitude off the ideal band
      OVERTRADE 0  hold was correct but the model acted (needless churn)
      MISS      0  action was warranted but the model held (failed to act)
      WRONG     0  opposite direction (widen<->tighten)

    A model that omits spacing on every fixture (=> always "hold")
    scores 4 holds x3 + 8 MISS x0 = 12/36 (chance). A constant value
    tops out at ~52% (19/36, inherent — see the module docstring); a
    genuine reasoner should clear ~75%+. OVERTRADE is kept as a
    distinct (zero-scored) token so the summary can show *how* a model
    failed a hold fixture vs an action fixture.
    """
    if expected == actual:
        if actual == "hold":
            return 3, "OK"
        return (3, "OK") if magnitude_ok else (2, "OVERSHOOT")
    if expected in ("widen", "tighten") and actual == "hold":
        return 0, "MISS"
    if expected == "hold" and actual in ("widen", "tighten"):
        return 0, "OVERTRADE"
    return 0, "WRONG"


_MAX_PER_FIXTURE = 3
_VERDICT_KEYS = ("OK", "OVERSHOOT", "OVERTRADE", "MISS", "WRONG", "ERROR")


async def main_async(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    model_override: str | None,
    prompt_file_override: str | None,
    force_json: bool,
    base_url: str,
    timeout_seconds: float,
    json_output: bool,
) -> int:
    config = load_resolved_config(config_path=None, profile_name=None, cli_overrides={})
    advisor_cfg = config.advisor
    if advisor_cfg is None:
        print("error: settings.yml is missing the advisor block", file=sys.stderr)
        return 2
    if advisor_cfg.provider != "ollama":
        print(
            f"warning: advisor.provider is {advisor_cfg.provider!r}; "
            "this probe only knows how to construct the Ollama adapter.",
            file=sys.stderr,
        )
        return 2

    prompt_path = prompt_file_override or advisor_cfg.prompt_file or "config/prompts/quant.md"
    prompt = load_prompt(Path(prompt_path))
    model = model_override or advisor_cfg.model
    if model is None:
        print("error: advisor.model is unset", file=sys.stderr)
        return 2

    print(f"# probe model: {model}")
    print(f"# base url:    {base_url}  (timeout {timeout_seconds:.0f}s)")
    print(f"# prompt file: {prompt_path} ({len(prompt.body)} chars)")
    if force_json:
        print("# force_json: ON (overrides is_thinking_model heuristic)")
    inference = advisor_cfg.inference_params
    adapter = OllamaAdapter(
        model=model,
        prompt=prompt,
        base_url=base_url,
        # AdvisorConfig stores temperature as Decimal for YAML
        # roundtrip-precision; OllamaAdapter passes the value into the
        # httpx JSON payload which rejects Decimal. Coerce to float here
        # so the probe doesn't crash on the first call.
        temperature=float(inference.temperature),
        max_tokens=inference.max_tokens,
        timeout_seconds=timeout_seconds,
        force_json=force_json,
    )

    total_score = 0
    max_score = _MAX_PER_FIXTURE * len(FIXTURES)
    error_count = 0
    verdict_counts = dict.fromkeys(_VERDICT_KEYS, 0)
    call_times: list[float] = []
    # (name, expected, actual, verdict, spacing, ideal, elapsed)
    rows: list[tuple[str, str, str, str, str, float, float]] = []

    try:
        for fx in FIXTURES:
            t0 = time.monotonic()
            try:
                rec = await adapter.get_recommendation(fx.summary)
                elapsed = time.monotonic() - t0
                actual = _classify_direction(rec, fx.summary.current_grid)
                magnitude_ok = _magnitude_ok(rec, fx.ideal_spacing)
                score, verdict = _score_row(fx.expected, actual, magnitude_ok)
                total_score += score
                spacing_str = str(rec.recommendations.get("spacing_percentage", "—"))
                rows.append(
                    (fx.name, fx.expected, actual, verdict, spacing_str, fx.ideal_spacing, elapsed)
                )
            except AdvisorError as exc:
                elapsed = time.monotonic() - t0
                error_count += 1
                verdict = "ERROR"
                rows.append(
                    (fx.name, fx.expected, "ERROR", str(exc)[:60], "—", fx.ideal_spacing, elapsed)
                )
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            call_times.append(elapsed)
            print(f">>> {fx.name}  expect:{fx.expected}  ideal:{fx.ideal_spacing}")
            print(
                f"    -> {rows[-1][2]:10s} verdict:{rows[-1][3]:10s} "
                f"spacing:{rows[-1][4]:8s} ({elapsed:.1f}s)"
            )
    finally:
        await adapter.aclose()

    max_call = max(call_times) if call_times else 0.0
    mean_call = sum(call_times) / len(call_times) if call_times else 0.0

    print()
    header = (
        f"{'Scenario':28s} {'Exp':9s} {'Act':9s} {'Verdict':10s} {'Spacing':8s} {'Ideal':6s} Time"
    )
    print(header)
    print("-" * len(header))
    for name, expected, actual, verdict, spacing, ideal, elapsed in rows:
        print(
            f"{name:28s} {expected:9s} {actual:9s} {verdict:10s} "
            f"{spacing:8s} {ideal:<6.2f} {elapsed:.1f}s"
        )
    print()
    print(f"TOTAL: {total_score}/{max_score}  errors:{error_count}")
    print(f"TIMING: max_call={max_call:.1f}s  mean_call={mean_call:.1f}s")

    if json_output:
        result = {
            "model": model,
            "prompt_chars": len(prompt.body),
            "score": total_score,
            "max_score": max_score,
            "errors": error_count,
            "verdicts": verdict_counts,
            "max_call_seconds": round(max_call, 1),
            "mean_call_seconds": round(mean_call, 1),
            "scenarios": [
                {
                    "name": name,
                    "expected": expected,
                    "actual": actual,
                    "verdict": verdict,
                    "spacing": spacing,
                    "ideal": ideal,
                    "elapsed_s": round(elapsed, 1),
                }
                for name, expected, actual, verdict, spacing, ideal, elapsed in rows
            ],
        }
        print("JSON_RESULT: " + json.dumps(result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools.probe_advisor",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=(
            "Override the configured advisor.model (e.g. 'mathstral:7b'). "
            "Useful for A/B comparison across Ollama-served models without "
            "editing settings.yml. Default: use the configured model."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help=(
            "Override the system prompt path (default: advisor.prompt_file "
            "from settings, else config/prompts/quant.md)."
        ),
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=_DEFAULT_BASE_URL,
        help=(
            "Ollama server base URL. Point at the host where the model "
            "actually runs (e.g. http://carldog-nas:11434 for the NAS "
            f"CPU-only deployment). Default: {_DEFAULT_BASE_URL}."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Per-call timeout. The advisor is a daemon (latency-tolerant), "
            "so a large budget is fine on CPU-only hosts; raise this when "
            "sweeping big/slow models. Default: "
            f"{_DEFAULT_TIMEOUT_SECONDS:.0f}s."
        ),
    )
    parser.add_argument(
        "--force-json",
        action="store_true",
        help=(
            "Force Ollama 'format=json' even for thinking-model name "
            "patterns. The 2026-05-25 diagnostic showed newer reasoning "
            "models (phi4-reasoning) emit clean JSON under format=json; use "
            "this to tell a probe-budget artifact from a real incapability."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help=(
            "Append a single machine-readable 'JSON_RESULT: {...}' line with "
            "per-scenario verdicts + timing. Consumed by "
            "tools/pull_and_probe_advisors.py."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(
        main_async(
            args.model,
            args.prompt_file,
            args.force_json,
            args.base_url,
            args.timeout_seconds,
            args.json_output,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
