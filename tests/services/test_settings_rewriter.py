"""Unit tests for the Stage 3.4b settings.yml rewriter."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from wobblebot.services.settings_rewriter import (
    SettingsRewriteError,
    apply_grid_overrides,
)

pytestmark = pytest.mark.unit


_BASE_SETTINGS = """\
# Top-of-file comment that must survive a round trip.
grid:
  # Default grid params shared across coins without overrides.
  default:
    spacing_percentage: 1.0   # narrow grid for mean-revert
    levels_above: 3
    levels_below: 3
    order_size_usd: 10        # USD per level
  coins:
    BTC:
      enabled: true
      spacing_percentage: 1.0   # tightened for BTC
      levels_above: 3
      levels_below: 3
      order_size_usd: 10
    ETH:
      enabled: true
      spacing_percentage: 2.0
      levels_above: 3
      levels_below: 3
      order_size_usd: 8

# Trailing comment, also kept.
safety:
  emergency_stop:
    enabled: true
"""


def _write_settings(tmp_path: Path) -> Path:
    p = tmp_path / "settings.yml"
    p.write_text(_BASE_SETTINGS, encoding="utf-8")
    return p


class TestHappyPath:
    def test_per_coin_override_updated(self, tmp_path: Path) -> None:
        path = _write_settings(tmp_path)
        diff = apply_grid_overrides(
            path,
            symbol="BTC",
            overrides={"spacing_percentage": Decimal("1.1")},
        )
        contents = path.read_text(encoding="utf-8")
        assert "spacing_percentage: 1.1" in contents
        # The diff should reference the changed line.
        assert "spacing_percentage" in diff
        assert "1.1" in diff

    def test_decimal_integer_value_stays_int(self, tmp_path: Path) -> None:
        """An order_size of Decimal('8') must render as ``8``, not ``8.0`` —
        the operator's file uses ``10`` (int) and we shouldn't switch the
        number style for no reason."""
        path = _write_settings(tmp_path)
        apply_grid_overrides(
            path,
            symbol="BTC",
            overrides={"order_size_usd": Decimal("8")},
        )
        contents = path.read_text(encoding="utf-8")
        # The BTC section's order_size_usd should be 8 (no decimal point).
        # Find the BTC block and assert the line.
        assert "order_size_usd: 8\n" in contents

    def test_comments_preserved(self, tmp_path: Path) -> None:
        """ruamel.yaml round-trip must keep every comment from the source."""
        path = _write_settings(tmp_path)
        apply_grid_overrides(
            path,
            symbol="BTC",
            overrides={"spacing_percentage": Decimal("1.1")},
        )
        contents = path.read_text(encoding="utf-8")
        for comment in (
            "Top-of-file comment that must survive a round trip.",
            "Default grid params shared",
            "narrow grid for mean-revert",
            "USD per level",
            "Trailing comment, also kept.",
        ):
            assert comment in contents, f"missing comment: {comment!r}"

    def test_fallback_to_default_when_no_per_coin_entry(self, tmp_path: Path) -> None:
        """A symbol without a coins.<SYMBOL> override updates the
        ``grid.default`` block, matching how the engine resolved the
        effective grid for that coin."""
        path = _write_settings(tmp_path)
        diff = apply_grid_overrides(
            path,
            symbol="DOGE",  # no coins.DOGE entry
            overrides={"spacing_percentage": Decimal("1.1")},
        )
        contents = path.read_text(encoding="utf-8")
        assert diff
        # The default block should be updated; the BTC override unchanged.
        # Approximation: find the default block and check spacing.
        default_idx = contents.index("default:")
        coins_idx = contents.index("coins:")
        default_block = contents[default_idx:coins_idx]
        assert "spacing_percentage: 1.1" in default_block
        # And the BTC block stays at 1.0
        btc_idx = contents.index("BTC:")
        btc_block = contents[btc_idx : btc_idx + 200]
        assert "spacing_percentage: 1.0" in btc_block

    def test_case_insensitive_symbol_match(self, tmp_path: Path) -> None:
        """``"btc"`` should find ``coins.BTC``, matching the engine's
        GridConfig.for_coin() case-insensitivity."""
        path = _write_settings(tmp_path)
        apply_grid_overrides(
            path,
            symbol="btc",
            overrides={"spacing_percentage": Decimal("1.05")},
        )
        contents = path.read_text(encoding="utf-8")
        btc_idx = contents.index("BTC:")
        btc_block = contents[btc_idx : btc_idx + 300]
        assert "spacing_percentage: 1.05" in btc_block

    def test_no_overrides_returns_empty_diff(self, tmp_path: Path) -> None:
        path = _write_settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        diff = apply_grid_overrides(path, symbol="BTC", overrides={})
        assert diff == ""
        # File untouched.
        assert path.read_text(encoding="utf-8") == before

    def test_noop_override_returns_empty_diff(self, tmp_path: Path) -> None:
        """Overrides equal to existing values aren't a write event."""
        path = _write_settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        # BTC.spacing_percentage is already 1.0 in the fixture.
        diff = apply_grid_overrides(
            path,
            symbol="BTC",
            overrides={"spacing_percentage": Decimal("1.0")},
        )
        assert diff == ""
        assert path.read_text(encoding="utf-8") == before


class TestAtomicWrite:
    def test_no_lingering_tmp_after_success(self, tmp_path: Path) -> None:
        """The .tmp file used during the atomic rename must be gone
        after a successful write."""
        path = _write_settings(tmp_path)
        apply_grid_overrides(
            path,
            symbol="BTC",
            overrides={"spacing_percentage": Decimal("1.1")},
        )
        tmp_file = path.with_suffix(path.suffix + ".tmp")
        assert not tmp_file.exists()


class TestErrorPaths:
    def test_missing_grid_section_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yml"
        path.write_text("safety:\n  emergency_stop:\n    enabled: true\n", encoding="utf-8")
        with pytest.raises(SettingsRewriteError, match="no `grid:` block"):
            apply_grid_overrides(
                path,
                symbol="BTC",
                overrides={"spacing_percentage": Decimal("1.1")},
            )

    def test_missing_default_with_no_coin_entry_raises(self, tmp_path: Path) -> None:
        """A grid block with ``coins:`` but no ``default:`` and the requested
        symbol absent from ``coins:`` is structurally unrecoverable."""
        path = tmp_path / "bad.yml"
        path.write_text(
            "grid:\n  coins:\n    BTC:\n      spacing_percentage: 1.0\n",
            encoding="utf-8",
        )
        with pytest.raises(SettingsRewriteError, match="no `grid.default:` block"):
            apply_grid_overrides(
                path,
                symbol="DOGE",  # not in coins
                overrides={"spacing_percentage": Decimal("1.1")},
            )

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "does-not-exist.yml"
        with pytest.raises(FileNotFoundError):
            apply_grid_overrides(
                path,
                symbol="BTC",
                overrides={"spacing_percentage": Decimal("1.1")},
            )

    def test_bool_override_rejected(self, tmp_path: Path) -> None:
        """Defensive: bools are int subtypes; refuse to coerce them."""
        path = _write_settings(tmp_path)
        with pytest.raises(SettingsRewriteError, match="bool"):
            apply_grid_overrides(
                path,
                symbol="BTC",
                overrides={"spacing_percentage": True},  # type: ignore[dict-item]
            )


class TestMultipleOverrides:
    def test_multiple_keys_in_one_write(self, tmp_path: Path) -> None:
        path = _write_settings(tmp_path)
        diff = apply_grid_overrides(
            path,
            symbol="BTC",
            overrides={
                "spacing_percentage": Decimal("1.1"),
                "order_size_usd": Decimal("9"),
            },
        )
        contents = path.read_text(encoding="utf-8")
        assert "spacing_percentage: 1.1" in contents
        # The BTC block's order_size_usd should be 9, not 9.0.
        btc_idx = contents.index("BTC:")
        btc_block = contents[btc_idx : btc_idx + 300]
        assert "order_size_usd: 9\n" in btc_block
        # Diff should mention both keys.
        assert "spacing_percentage" in diff
        assert "order_size_usd" in diff
