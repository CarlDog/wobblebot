"""Lock the advisor-probe scoring rubric against regression (rev 2026-05-29).

``tools/probe_advisor.py`` grades LLM advisor candidates for the NAS
model sweep. The whole ranking is only meaningful if a degenerate
strategy — a do-nothing (always-hold) model or a constant answer —
scores poorly while a genuine reasoner scores high. The original
6-fixture battery failed this (a constant "+10% widen" hit the
documented 11/18 "lazy baseline").

These tests pin the invariants validated when the 12-fixture,
asymmetric-adjacency battery landed:

- 12 fixtures, balanced 4 widen / 4 hold / 4 tighten
- an oracle that emits the ideal spacing scores a perfect 36/36
- always-hold (omit spacing) scores exactly chance (12/36) — the
  no-partial-credit rubric (MISS=0, OVERTRADE=0) closes the always-hold
  loophole that scored 56% under the old rubric
- no constant-value or constant-direction strategy beats the inherent
  ~52% ceiling (worst case ~19/36 at spacing ~1.9), which stays well
  below the ~75% a genuine reasoner clears
- no action fixture has a direction-deadband / magnitude-band dead zone

If a future fixture/rubric edit breaks any of these, the sweep can no
longer distinguish reasoning from guessing — fail loudly here first.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _load_probe_advisor() -> ModuleType:
    """Load ``tools/probe_advisor.py`` by path.

    ``tools/`` is intentionally not a package; importlib-by-path is the
    repo convention for testing one-shot operator scripts (see
    ``test_profile_storage.py``). Registering the module in
    ``sys.modules`` under its spec name is required so the
    ``@dataclass`` + ``from __future__ import annotations`` combo can
    resolve its own module namespace at class-creation time.
    """
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "tools" / "probe_advisor.py"
    spec = importlib.util.spec_from_file_location("probe_advisor", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_advisor"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe_advisor()

_MAX = probe._MAX_PER_FIXTURE * len(probe.FIXTURES)


def _current_spacing(fixture: object) -> float:
    return fixture.summary.current_grid.spacing_percentage  # type: ignore[attr-defined]


def _score_strategy(rec_fn) -> tuple[int, dict[str, int]]:
    """Score a recommendation strategy across the whole battery.

    ``rec_fn(fixture)`` returns the ``recommendations`` dict a model
    would emit for that fixture. Returns (total_score, verdict_counts).
    """
    total = 0
    verdicts: dict[str, int] = {}
    for fixture in probe.FIXTURES:
        rec = SimpleNamespace(recommendations=rec_fn(fixture))
        actual = probe._classify_direction(rec, fixture.summary.current_grid)
        magnitude_ok = probe._magnitude_ok(rec, fixture.ideal_spacing)
        score, verdict = probe._score_row(fixture.expected, actual, magnitude_ok)
        total += score
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    return total, verdicts


def test_battery_is_balanced_twelve() -> None:
    assert len(probe.FIXTURES) == 12
    counts = {"widen": 0, "hold": 0, "tighten": 0}
    for fixture in probe.FIXTURES:
        counts[fixture.expected] += 1
    assert counts == {"widen": 4, "hold": 4, "tighten": 4}


def test_oracle_scores_perfect() -> None:
    total, verdicts = _score_strategy(lambda fx: {"spacing_percentage": fx.ideal_spacing})
    assert total == _MAX
    assert verdicts.get("OK") == len(probe.FIXTURES)


# 22/36 = 61%; the measured worst constant is 19/36 (~1.9). The ceiling
# stays well below the ~27/36 (75%) a deployable reasoner should clear.
_CONSTANT_CEILING = 22


def test_always_hold_scores_at_chance() -> None:
    # Omitting spacing => "hold" on every fixture. 4 correct holds (12)
    # + 8 action-warranted fixtures scored MISS (0) = 12/36 = chance.
    total, verdicts = _score_strategy(lambda fx: {})
    assert total == 12
    assert verdicts.get("MISS") == 8
    assert verdicts.get("OVERTRADE", 0) == 0


def test_no_constant_value_beats_the_ceiling() -> None:
    # Sweep every plausible fixed spacing; none may exceed the ceiling,
    # and the gap to a perfect score must stay wide. This is what the
    # 2026-05-29 verification caught: the prior battery let a constant
    # 0.65 reach 52.8% above a claimed 44% ceiling.
    worst = 0
    c = 0.40
    while c <= 3.05:
        total, _ = _score_strategy(lambda fx, cc=round(c, 3): {"spacing_percentage": cc})
        worst = max(worst, total)
        c += 0.05
    assert worst <= _CONSTANT_CEILING, f"a constant scored {worst}/{_MAX} (> ceiling)"
    assert _MAX - worst >= 12  # wide gap to a perfect reasoner


@pytest.mark.parametrize("factor", [1.1, 0.9, 1.3, 0.7])
def test_constant_direction_cannot_beat_the_ceiling(factor: float) -> None:
    total, _ = _score_strategy(
        lambda fx, f=factor: {"spacing_percentage": round(_current_spacing(fx) * f, 4)}
    )
    assert total <= _CONSTANT_CEILING


def test_no_dead_zone_on_action_fixtures() -> None:
    # Every action fixture's +/-30% magnitude band (around ideal) must not
    # overlap the +/-5% hold deadband (around current). Overlap => a
    # timid-but-correct move is mis-scored MISS (verification defect #2).
    band = probe._MAGNITUDE_BAND
    for fx in probe.FIXTURES:
        if fx.expected == "hold":
            continue
        cur = _current_spacing(fx)
        ideal = fx.ideal_spacing
        deadband = (cur * 0.95, cur * 1.05)
        magband = (ideal * (1 - band), ideal * (1 + band))
        overlap = not (magband[1] < deadband[0] or magband[0] > deadband[1])
        assert not overlap, f"{fx.name}: deadband {deadband} overlaps magnitude band {magband}"


def test_overlap_fixtures_decouple_direction_from_spacing() -> None:
    # The load-bearing decouplers: a WIDEN at high current spacing and a
    # TIGHTEN at low current spacing, so current spacing alone predicts
    # nothing about the correct direction.
    widen_at_high = any(
        fx.expected == "widen" and _current_spacing(fx) >= 1.5 for fx in probe.FIXTURES
    )
    tighten_at_low = any(
        fx.expected == "tighten" and _current_spacing(fx) <= 1.0 for fx in probe.FIXTURES
    )
    assert widen_at_high
    assert tighten_at_low
