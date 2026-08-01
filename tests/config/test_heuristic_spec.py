"""Tests for the heuristic-spec schema + loader (Stage 8.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wobblebot.config.heuristic import HeuristicSpec, load_heuristic_spec

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_SPEC = _REPO_ROOT / "config" / "heuristic" / "quant.yml"

_MINIMAL = """
curve:
  - {vol: 0.001, spacing: 0.7}
  - {vol: 0.01, spacing: 2.0}
"""


class TestLoadShippedSpec:
    def test_committed_default_loads_and_validates(self) -> None:
        spec = load_heuristic_spec(_SHIPPED_SPEC)
        assert len(spec.curve) == 8
        assert spec.fee_floor == pytest.approx(0.52)
        # All four guards present and enabled by default.
        assert spec.guards.directional_runaway.enabled
        assert spec.guards.defensive_drawdown.widen_factor == pytest.approx(1.5)
        assert spec.guards.dont_fix_working.cycles_min == 8
        assert spec.guards.fee_floor_calm.near_floor_spacing == pytest.approx(0.68)


class TestLoaderErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_heuristic_spec(tmp_path / "nope.yml")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.yml"
        f.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            load_heuristic_spec(f)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "list.yml"
        f.write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_heuristic_spec(f)

    def test_minimal_curve_only_applies_defaults(self, tmp_path: Path) -> None:
        f = tmp_path / "min.yml"
        f.write_text(_MINIMAL, encoding="utf-8")
        spec = load_heuristic_spec(f)
        assert len(spec.curve) == 2
        assert spec.fee_floor == pytest.approx(0.52)  # code default
        assert spec.guards.defensive_drawdown.enabled is True  # code default


class TestSchemaValidation:
    def test_curve_requires_two_points(self) -> None:
        with pytest.raises(ValidationError):
            HeuristicSpec(curve=[{"vol": 0.001, "spacing": 0.7}])  # type: ignore[list-item]

    def test_curve_must_be_strictly_increasing_in_vol(self) -> None:
        with pytest.raises(ValidationError, match="strictly increasing"):
            HeuristicSpec(
                curve=[  # type: ignore[list-item]
                    {"vol": 0.01, "spacing": 2.0},
                    {"vol": 0.001, "spacing": 0.7},
                ]
            )

    def test_curve_rejects_duplicate_vol(self) -> None:
        with pytest.raises(ValidationError, match="strictly increasing"):
            HeuristicSpec(
                curve=[  # type: ignore[list-item]
                    {"vol": 0.001, "spacing": 0.7},
                    {"vol": 0.001, "spacing": 0.9},
                ]
            )

    def test_guard_toggle_parses(self) -> None:
        spec = HeuristicSpec(
            curve=[{"vol": 0.001, "spacing": 0.7}, {"vol": 0.01, "spacing": 2.0}],  # type: ignore[list-item]
            guards={"defensive_drawdown": {"enabled": False}},  # type: ignore[arg-type]
        )
        assert spec.guards.defensive_drawdown.enabled is False
        # Untouched guards keep their defaults.
        assert spec.guards.dont_fix_working.enabled is True

    def test_spec_is_frozen(self) -> None:
        spec = HeuristicSpec(
            curve=[{"vol": 0.001, "spacing": 0.7}, {"vol": 0.01, "spacing": 2.0}]  # type: ignore[list-item]
        )
        with pytest.raises(ValidationError):
            spec.fee_floor = 0.9  # type: ignore[misc]
