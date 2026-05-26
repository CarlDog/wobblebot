"""Probe the trading-advisor LLM's recommendation quality.

Calls ``OllamaAdapter.get_recommendation`` directly against the
configured Ollama model with a battery of canned
``PerformanceSummary`` scenarios. Prints each call's outcome:
schema-validity, directional correctness, and magnitude reasonability.

**Sister to ``tools/probe_assistant.py``** which exercises the
OPERATOR-ASSISTANT role. The advisor role's measurement model is
different: there's no single "right answer" per scenario — only a
direction-correct + magnitude-sensible band. The scoring rubric
below reflects that.

**Use when:**

- Editing ``config/prompts/quant.md`` and you want to know whether
  the advisor's recommendations still move in the right direction.
- Swapping models (``advisor.model``) and you want a quick quality
  check before pointing cli/advise at the new one.
- Evaluating math-specialist models (mathstral, wizardmath,
  phi4-mini-reasoning) which were rejected from the operator-
  assistant role but are explicit candidates for the advisor role
  per docs/reference/operator-llm-models.md.

**Use ``tools/probe_assistant.py`` instead when** you're testing
the OPERATOR-INTERACTION role (Discord command routing).

Run as: ``python tools/probe_advisor.py``
Override the model via ``--model <model>``.

No external state mutated. No Discord traffic. No DB writes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
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

# Scenario fixtures. Each fixture is (name, expected_direction, summary).
# ``expected_direction`` is one of:
#   "tighten"   -- spacing should decrease (low vol / quiet market)
#   "widen"     -- spacing should increase (high vol / drawdown / trend)
#   "hold"      -- minor or zero change (healthy churn / mild trend)
#
# Note: the advisor's response schema only emits param changes
# (spacing/levels/order_size). There is no "pause" recommendation in
# the advisor vocabulary -- that's an operator decision. So scenarios
# that would warrant a pause in the operator's mind are scored against
# what the advisor CAN do: recommend a defensive (wide) grid.
#
# Recommendations that match the expected direction earn full credit.
# Off-by-one cases (e.g. "hold" when "tighten" was expected) are scored
# partial. Off-direction (e.g. "widen" when "tighten" was expected) is
# scored zero on direction.
#
# Magnitude bounds are loose: spacing change within ±25% of the current
# value is "reasonable"; outside that is "overshoot".

_BASE_GRID = CurrentGridParams(
    spacing_percentage=1.0,
    levels_above=4,
    levels_below=4,
    order_size_usd=10.0,
)


def _build_summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    symbol: str = "BTC/USD",
    lookback_hours: float = 6.0,
    latest_price: float = 79000.0,
    snapshot_count: int = 720,
    volatility: float,
    max_drawdown: float,
    flatness: float,
    cycle_count: int,
    win_rate: float,
    total_pnl: float = 0.0,
    active_orders: int = 8,
) -> PerformanceSummary:
    return PerformanceSummary(
        symbol=symbol,
        lookback_hours=lookback_hours,
        latest_price=latest_price,
        snapshot_count=snapshot_count,
        volatility=volatility,
        max_drawdown=max_drawdown,
        flatness=flatness,
        cycle_count=cycle_count,
        win_rate=win_rate,
        total_pnl=total_pnl,
        active_orders=active_orders,
        current_grid=_BASE_GRID,
    )


SCENARIOS: tuple[tuple[str, str, PerformanceSummary], ...] = (
    (
        "quiet_market",
        "tighten",
        _build_summary(
            volatility=0.0008,
            max_drawdown=-0.002,
            flatness=0.95,
            cycle_count=1,
            win_rate=1.0,
            total_pnl=0.20,
            active_orders=8,
        ),
    ),
    (
        "healthy_churn",
        "hold",
        _build_summary(
            volatility=0.003,
            max_drawdown=-0.008,
            flatness=0.55,
            cycle_count=4,
            win_rate=0.75,
            total_pnl=0.80,
            active_orders=6,
        ),
    ),
    (
        "whipsaw",
        "widen",
        _build_summary(
            volatility=0.012,
            max_drawdown=-0.035,
            flatness=0.30,
            cycle_count=8,
            win_rate=0.375,
            total_pnl=-0.40,
            active_orders=3,
        ),
    ),
    (
        "trending_up",
        "hold",
        _build_summary(
            latest_price=82000.0,
            volatility=0.004,
            max_drawdown=-0.005,
            flatness=0.40,
            cycle_count=2,
            win_rate=1.0,
            total_pnl=0.30,
            active_orders=4,
        ),
    ),
    (
        "trending_down",
        "widen",
        _build_summary(
            latest_price=74000.0,
            volatility=0.006,
            max_drawdown=-0.045,
            flatness=0.45,
            cycle_count=1,
            win_rate=0.0,
            total_pnl=-0.55,
            active_orders=2,
        ),
    ),
    (
        "post_cap_trip",
        "widen",
        _build_summary(
            volatility=0.008,
            max_drawdown=-0.060,
            flatness=0.50,
            cycle_count=0,
            win_rate=0.0,
            total_pnl=-1.20,
            active_orders=0,
        ),
    ),
)


def _classify_direction(
    rec: AdvisorRecommendation,
    current: CurrentGridParams,
) -> str:
    """Map a recommendation's spacing change to a coarse direction.

    Returns one of: ``"tighten"``, ``"widen"``, ``"hold"``. The
    classifier is rough -- it captures intent, not arithmetic
    precision.
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
    # Threshold: anything within ±5% is "hold"; bigger is the
    # corresponding direction.
    if abs(delta_pct) < 0.05:
        return "hold"
    return "widen" if delta_pct > 0 else "tighten"


def _magnitude_reasonable(
    rec: AdvisorRecommendation,
    current: CurrentGridParams,
    max_change_fraction: float = 0.25,
) -> bool:
    """Is the proposed spacing change within ±max_change_fraction of current?"""
    new_spacing = rec.recommendations.get("spacing_percentage")
    if new_spacing is None or current.spacing_percentage is None:
        return True
    try:
        ns = float(new_spacing)
    except (TypeError, ValueError):
        return True
    cs = current.spacing_percentage
    if cs == 0:
        return True
    return abs((ns - cs) / cs) <= max_change_fraction


def _score_row(
    expected: str,
    actual: str,
    magnitude_ok: bool,
) -> tuple[int, str]:
    """Map (expected_direction, actual_direction, magnitude_ok) to a
    coarse score 0-3 + a one-letter verdict for the summary table.

      3: right direction + magnitude in bounds         (verdict: ✓)
      2: right direction, magnitude out of bounds      (verdict: ~)
      1: adjacent direction (hold↔tighten/widen)       (verdict: -)
      0: wrong direction                               (verdict: ✗)
    """
    if expected == actual:
        return (3, "OK") if magnitude_ok else (2, "OVERSHOOT")
    adjacent = {
        ("hold", "tighten"),
        ("hold", "widen"),
        ("tighten", "hold"),
        ("widen", "hold"),
    }
    if (expected, actual) in adjacent:
        return (1, "ADJACENT")
    return (0, "WRONG")


async def main_async(  # pylint: disable=too-many-locals
    model_override: str | None,
    prompt_file_override: str | None,
    force_json: bool,
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
    print(f"# prompt file: {prompt_path} ({len(prompt.body)} chars)")
    if force_json:
        print("# force_json: ON (overrides is_thinking_model heuristic)")
    inference = advisor_cfg.inference_params
    adapter = OllamaAdapter(
        model=model,
        prompt=prompt,
        base_url="http://localhost:11434",
        # AdvisorConfig stores temperature as Decimal for YAML
        # roundtrip-precision; OllamaAdapter passes the value into the
        # httpx JSON payload which rejects Decimal. Coerce to float here
        # so the probe doesn't crash on the first call.
        temperature=float(inference.temperature),
        max_tokens=inference.max_tokens,
        timeout_seconds=180.0,
        force_json=force_json,
    )

    total_score = 0
    max_score = 3 * len(SCENARIOS)
    error_count = 0
    rows: list[tuple[str, str, str, str, str]] = []

    try:
        for name, expected, summary in SCENARIOS:
            try:
                rec = await adapter.get_recommendation(summary)
                actual = _classify_direction(rec, summary.current_grid)
                magnitude_ok = _magnitude_reasonable(rec, summary.current_grid)
                score, verdict = _score_row(expected, actual, magnitude_ok)
                total_score += score
                spacing_str = str(rec.recommendations.get("spacing_percentage", "—"))
                rows.append((name, expected, actual, verdict, spacing_str))
            except AdvisorError as exc:
                error_count += 1
                rows.append((name, expected, "ERROR", str(exc)[:60], "—"))
            print(f">>> {name}  expect:{expected}")
            print(f"    -> {rows[-1][2]:10s} verdict:{rows[-1][3]} spacing:{rows[-1][4]}")
    finally:
        await adapter.aclose()

    print()
    print(f"{'Scenario':18s} {'Expected':10s} {'Actual':10s} {'Verdict':10s} Spacing")
    print("-" * 70)
    for name, expected, actual, verdict, spacing in rows:
        print(f"{name:18s} {expected:10s} {actual:10s} {verdict:10s} {spacing}")
    print()
    print(f"TOTAL: {total_score}/{max_score}  errors:{error_count}")
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
            "Useful for A/B comparison across Ollama-served models "
            "without editing settings.yml. Default: use the configured model."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help=(
            "Override the system prompt path (default: advisor.prompt_file "
            "from settings, else config/prompts/quant.md). Used to evaluate "
            "compact prompt variants against small reasoning models."
        ),
    )
    parser.add_argument(
        "--force-json",
        action="store_true",
        help=(
            "Force Ollama 'format=json' even for thinking-model name "
            "patterns. The 2026-05-25 diagnostic showed newer reasoning "
            "models (phi4-reasoning) emit clean JSON under format=json "
            "rather than the assumed empty-{}. Use this flag to evaluate "
            "whether a candidate's TIMEOUT/slow-result is a probe-budget "
            "artifact rather than a model incapability."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.model, args.prompt_file, args.force_json))


if __name__ == "__main__":
    sys.exit(main())
