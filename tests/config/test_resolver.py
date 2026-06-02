"""Tests for the profile + CLI override resolver (audit slice 3)."""

from __future__ import annotations

import pytest

from wobblebot.config.resolver import deep_merge, resolve_config

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_disjoint_dicts(self) -> None:
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_overlay_wins_on_scalar_conflict(self) -> None:
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_recursive_dict_merge(self) -> None:
        base = {"grid": {"default": {"spacing_percentage": 1.0, "levels_above": 5}}}
        overlay = {"grid": {"default": {"spacing_percentage": 2.0}}}
        merged = deep_merge(base, overlay)
        # Overlay key wins; overlay-absent key survives
        assert merged == {"grid": {"default": {"spacing_percentage": 2.0, "levels_above": 5}}}

    def test_lists_override_not_append(self) -> None:
        """Per ADR-009: lists override entirely."""
        base = {"advisor": {"experts": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}}
        overlay = {"advisor": {"experts": [{"name": "x"}]}}
        merged = deep_merge(base, overlay)
        assert merged == {"advisor": {"experts": [{"name": "x"}]}}

    def test_overlay_dict_wins_over_base_scalar(self) -> None:
        """Type mismatch: overlay wins."""
        assert deep_merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}

    def test_overlay_scalar_wins_over_base_dict(self) -> None:
        assert deep_merge({"a": {"b": 2}}, {"a": 1}) == {"a": 1}

    def test_does_not_mutate_inputs(self) -> None:
        base = {"a": {"b": 1}}
        overlay = {"a": {"c": 2}}
        merged = deep_merge(base, overlay)
        assert base == {"a": {"b": 1}}
        assert overlay == {"a": {"c": 2}}
        assert merged == {"a": {"b": 1, "c": 2}}

    def test_empty_overlay_returns_copy_of_base(self) -> None:
        base = {"a": {"b": 1}}
        merged = deep_merge(base, {})
        assert merged == base
        # Top-level should be a different dict object
        assert merged is not base


# ---------------------------------------------------------------------------
# resolve_config
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def _raw(self) -> dict[str, object]:
        return {
            "grid": {"default": {"spacing_percentage": 1.0, "levels_above": 3}},
            "safety": {"max_total_exposure_usd": 100},
            "live": {"symbols": ["BTC/USD"], "tick_seconds": 5.0},
            "profiles": {
                "conservative": {
                    "grid": {"default": {"spacing_percentage": 2.0}},
                    "live": {"tick_seconds": 10.0},
                },
                "aggressive": {
                    "grid": {"default": {"spacing_percentage": 0.5, "levels_above": 5}},
                },
            },
        }

    def test_no_profile_no_overrides_strips_profiles_block(self) -> None:
        merged = resolve_config(self._raw())
        assert "profiles" not in merged
        # Base values intact
        assert merged["grid"]["default"]["spacing_percentage"] == 1.0
        assert merged["live"]["tick_seconds"] == 5.0

    def test_profile_applies_via_deep_merge(self) -> None:
        merged = resolve_config(self._raw(), profile_name="conservative")
        # Profile overrides
        assert merged["grid"]["default"]["spacing_percentage"] == 2.0
        assert merged["live"]["tick_seconds"] == 10.0
        # Base values not in profile survive
        assert merged["grid"]["default"]["levels_above"] == 3
        assert merged["safety"]["max_total_exposure_usd"] == 100

    def test_profile_with_partial_override(self) -> None:
        """Aggressive profile only changes grid; live/safety inherit base."""
        merged = resolve_config(self._raw(), profile_name="aggressive")
        assert merged["grid"]["default"]["spacing_percentage"] == 0.5
        assert merged["grid"]["default"]["levels_above"] == 5  # from profile
        assert merged["live"]["tick_seconds"] == 5.0  # from base

    def test_cli_overrides_win_over_profile(self) -> None:
        """Layering: base → profile → CLI. CLI takes precedence."""
        merged = resolve_config(
            self._raw(),
            profile_name="conservative",
            cli_overrides={"live": {"tick_seconds": 20.0}},
        )
        # CLI override wins over conservative's 10.0
        assert merged["live"]["tick_seconds"] == 20.0
        # Conservative's grid override still applies (no CLI override there)
        assert merged["grid"]["default"]["spacing_percentage"] == 2.0

    def test_cli_overrides_without_profile(self) -> None:
        merged = resolve_config(
            self._raw(),
            cli_overrides={"live": {"max_session_loss_usd": 2}},
        )
        assert merged["live"]["max_session_loss_usd"] == 2
        # Base's tick_seconds untouched
        assert merged["live"]["tick_seconds"] == 5.0

    def test_unknown_profile_raises_with_available_list(self) -> None:
        with pytest.raises(KeyError, match="not found"):
            resolve_config(self._raw(), profile_name="moonshot")

    def test_unknown_profile_error_hints_at_example_file(self) -> None:
        """The error points the operator at settings.example.yml + the
        specific profile block to copy (P0.2 — a stale settings.yml that
        predates a profile is the common cause)."""
        with pytest.raises(KeyError) as exc_info:
            resolve_config(self._raw(), profile_name="moonshot")
        message = str(exc_info.value)
        assert "settings.example.yml" in message
        assert "profiles.moonshot" in message

    def test_unknown_profile_when_none_defined(self) -> None:
        raw = {"grid": {}, "safety": {}}  # no profiles: block at all
        with pytest.raises(KeyError, match="not found"):
            resolve_config(raw, profile_name="conservative")

    def test_empty_cli_overrides_skipped(self) -> None:
        merged_a = resolve_config(self._raw())
        merged_b = resolve_config(self._raw(), cli_overrides={})
        merged_c = resolve_config(self._raw(), cli_overrides=None)
        assert merged_a == merged_b == merged_c

    def test_lists_in_profile_override_base_lists(self) -> None:
        """ADR-009: profile lists override entirely, not append."""
        raw = {
            "advisor": {"experts": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
            "profiles": {
                "single-expert": {
                    "advisor": {"experts": [{"name": "lonely"}]},
                },
            },
        }
        merged = resolve_config(raw, profile_name="single-expert")
        assert len(merged["advisor"]["experts"]) == 1
        assert merged["advisor"]["experts"][0]["name"] == "lonely"
