"""Schema-drift tests for the example/operator config-file pairs.

Two file pairs are kept in sync by these tests:

1. ``config/settings.example.yml`` ↔ ``config/settings.yml``
2. ``.env.example`` ↔ ``.env``

For each pair, two directions of drift are checked:

- **Operator stale keys** — keys present in the operator file that no
  longer exist in the example. These are *always* a hard failure.
  They mean the operator is carrying configuration the bot will never
  read again, which silently masks bugs and makes upgrades harder.
- **Operator missing keys** — keys in the example that are absent from
  the operator file. By default these are reported as warnings (printed
  to the test report) so a fresh checkout doesn't get blocked.
  Setting the env var ``WOBBLEBOT_STRICT_CONFIG_DRIFT=1`` promotes the
  warning to a hard failure — useful in CI to enforce strict parity.

Operator files are gitignored and may not exist on a fresh clone; in
that case the operator-side checks skip cleanly. The example files
themselves are required to exist — their absence is a different kind
of bug (and would surface elsewhere).

Two specific subtrees of the YAML are deliberately exempt from leaf
checks because they are operator-extensible by design:

- ``grid.coins.*`` — operators add coin-specific overrides freely.
- ``profiles.*`` — operators define their own named profiles.

For these subtrees we only verify that the parent key exists in both
files; we don't drill into the children.

Profile *names* are a special case (``test_operator_has_all_canonical_profiles``):
although ``profiles.*`` children are exempt, the canonical profile names
shipped in the example (``conservative``, ``aggressive``, ``cpu-only``,
...) must still exist in the operator file — otherwise an operator who
copied an older example silently loses access to profiles added later,
surfacing only as a "profile not found" error at daemon startup. Custom
operator-defined profiles remain exempt (example→operator direction only).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_EXAMPLE = _REPO_ROOT / "config" / "settings.example.yml"
_SETTINGS_OPERATOR = _REPO_ROOT / "config" / "settings.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_ENV_OPERATOR = _REPO_ROOT / ".env"

_OPEN_SUBTREES: set[str] = {"grid.coins", "profiles"}
_STRICT_ENV_VAR = "WOBBLEBOT_STRICT_CONFIG_DRIFT"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_yaml_paths(
    data: object,
    prefix: str = "",
    skip_under: set[str] = _OPEN_SUBTREES,
) -> set[str]:
    """Recursively extract dotted-path keys from a YAML-loaded structure.

    Stops descending into any subtree whose path matches an entry in
    ``skip_under`` (only the parent path is recorded, not the children).
    Returns paths only for dict keys; list values aren't paths. Scalars
    contribute their containing dict's key but no further recursion.
    """
    paths: set[str] = set()
    if not isinstance(data, dict):
        return paths
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        paths.add(path)
        if path in skip_under:
            continue
        if isinstance(value, dict):
            paths |= _extract_yaml_paths(value, prefix=path, skip_under=skip_under)
    return paths


def _extract_env_keys(path: Path) -> set[str]:
    """Extract variable names from a ``.env``-style file.

    Includes commented-out ``KEY=value`` lines so that "documented but
    optional" keys (``# ANTHROPIC_API_KEY=...``) count as part of the
    schema. Single ``#`` per line is the only comment style we accept;
    inline comments after a value are treated as part of the value
    (consistent with how dotenv parses).
    """
    keys: set[str] = set()
    pattern = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").lstrip()
        match = pattern.match(stripped)
        if match:
            keys.add(match.group(1))
    return keys


def _strict_mode_enabled() -> bool:
    raw = os.environ.get(_STRICT_ENV_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _report_missing(label: str, missing: set[str]) -> None:
    """Either print a warning or fail, based on strict-mode toggle."""
    if not missing:
        return
    sorted_keys = sorted(missing)
    message = (
        f"{label} is missing keys from the example file: {sorted_keys}\n"
        f"  (set {_STRICT_ENV_VAR}=1 to make this a hard failure)"
    )
    if _strict_mode_enabled():
        pytest.fail(message)
    else:
        # Surfaces in pytest -v output without flipping the test red.
        print(f"\n[schema-drift WARNING] {message}")


# ---------------------------------------------------------------------------
# YAML drift — settings.yml ↔ settings.example.yml
# ---------------------------------------------------------------------------


def _load_yaml_paths(path: Path) -> set[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _extract_yaml_paths(raw)


def _load_profile_names(path: Path) -> set[str]:
    """Return the profile names defined directly under ``profiles:``.

    The profile NAMES are the canonical-profile surface. ``profiles.*``
    children are exempt from the leaf-path drift check (operators override
    freely inside a profile), but the canonical names shipped in the
    example must exist in the operator file or ``--profile X`` fails at
    daemon startup. Returns an empty set when there is no ``profiles:``
    block (or it isn't a mapping).
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return set()
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        return set()
    return set(profiles.keys())


class TestSettingsDrift:
    """Operator's settings.yml vs settings.example.yml."""

    def test_example_file_exists(self) -> None:
        assert _SETTINGS_EXAMPLE.exists(), (
            f"committed example missing: {_SETTINGS_EXAMPLE} "
            "(this is a repo-level bug, not operator drift)"
        )

    def test_operator_has_no_stale_keys(self) -> None:
        if not _SETTINGS_OPERATOR.exists():
            pytest.skip("no operator settings.yml; nothing to compare")
        example_paths = _load_yaml_paths(_SETTINGS_EXAMPLE)
        operator_paths = _load_yaml_paths(_SETTINGS_OPERATOR)
        stale = operator_paths - example_paths
        assert not stale, (
            f"settings.yml has keys that no longer exist in settings.example.yml: "
            f"{sorted(stale)}"
        )

    def test_operator_has_all_example_keys(self) -> None:
        if not _SETTINGS_OPERATOR.exists():
            pytest.skip("no operator settings.yml; nothing to compare")
        example_paths = _load_yaml_paths(_SETTINGS_EXAMPLE)
        operator_paths = _load_yaml_paths(_SETTINGS_OPERATOR)
        missing = example_paths - operator_paths
        _report_missing("settings.yml", missing)

    def test_operator_has_all_canonical_profiles(self) -> None:
        """Canonical profiles shipped in the example must exist in the
        operator's settings.yml.

        ``profiles.*`` children are an open subtree, so the leaf-path
        checks never see profile names; without this, an operator who
        copied an older example silently loses access to profiles added
        later (e.g. ``cpu-only``, ``cloud-only-moe``) — surfacing only as
        a "profile not found" error at daemon startup. Custom
        operator-defined profiles stay exempt: we check the
        example→operator direction only, never flagging operator-only
        names as stale.
        """
        if not _SETTINGS_OPERATOR.exists():
            pytest.skip("no operator settings.yml; nothing to compare")
        canonical = _load_profile_names(_SETTINGS_EXAMPLE)
        operator_profiles = _load_profile_names(_SETTINGS_OPERATOR)
        missing = {f"profiles.{name}" for name in (canonical - operator_profiles)}
        _report_missing("settings.yml", missing)


# ---------------------------------------------------------------------------
# .env drift — .env ↔ .env.example
# ---------------------------------------------------------------------------


class TestEnvDrift:
    """Operator's .env vs .env.example."""

    def test_example_file_exists(self) -> None:
        assert _ENV_EXAMPLE.exists(), (
            f"committed example missing: {_ENV_EXAMPLE} "
            "(this is a repo-level bug, not operator drift)"
        )

    def test_operator_has_no_stale_keys(self) -> None:
        if not _ENV_OPERATOR.exists():
            pytest.skip("no operator .env; nothing to compare")
        example_keys = _extract_env_keys(_ENV_EXAMPLE)
        operator_keys = _extract_env_keys(_ENV_OPERATOR)
        stale = operator_keys - example_keys
        assert not stale, f".env has keys that no longer exist in .env.example: {sorted(stale)}"

    def test_operator_has_all_example_keys(self) -> None:
        if not _ENV_OPERATOR.exists():
            pytest.skip("no operator .env; nothing to compare")
        example_keys = _extract_env_keys(_ENV_EXAMPLE)
        operator_keys = _extract_env_keys(_ENV_OPERATOR)
        missing = example_keys - operator_keys
        _report_missing(".env", missing)


# ---------------------------------------------------------------------------
# Self-consistency tests for the example files (always run)
# ---------------------------------------------------------------------------


class TestExampleSelfConsistency:
    """The committed example files must always parse cleanly."""

    def test_settings_example_is_valid_yaml(self) -> None:
        # If this fails the bug is in the example commit, not operator drift.
        # Force utf-8: Python's default ``read_text()`` uses the locale
        # codepage (cp1252 on Windows), which chokes on UTF-8 chars in
        # comments. The file is UTF-8 on disk; asking for that explicitly
        # makes the test portable.
        assert isinstance(yaml.safe_load(_SETTINGS_EXAMPLE.read_text(encoding="utf-8")), dict)

    def test_env_example_has_at_least_one_documented_key(self) -> None:
        keys = _extract_env_keys(_ENV_EXAMPLE)
        # If this fails the example file has been emptied; suspect a
        # bad merge or accidental overwrite.
        assert keys, ".env.example has no documented keys"


# ---------------------------------------------------------------------------
# Internal helper tests — the parsers themselves
# ---------------------------------------------------------------------------


class TestExtractYamlPaths:
    def test_flat_keys(self) -> None:
        assert _extract_yaml_paths({"a": 1, "b": 2}) == {"a", "b"}

    def test_nested_dict(self) -> None:
        result = _extract_yaml_paths({"a": {"b": {"c": 1}}})
        assert result == {"a", "a.b", "a.b.c"}

    def test_skips_under_open_subtrees(self) -> None:
        data = {
            "grid": {
                "default": {"spacing": 1.0},
                "coins": {"DOGE": {"spacing": 2.0}, "ADA": {"spacing": 1.5}},
            },
            "profiles": {"conservative": {"safety": {"max": 50}}},
        }
        result = _extract_yaml_paths(data)
        # Children of grid.coins.* and profiles.* must NOT appear
        assert "grid" in result
        assert "grid.coins" in result
        assert "grid.default" in result
        assert "grid.default.spacing" in result
        assert "profiles" in result
        # Open subtrees only get the parent path, not the children
        for forbidden in (
            "grid.coins.DOGE",
            "grid.coins.ADA",
            "grid.coins.DOGE.spacing",
            "profiles.conservative",
            "profiles.conservative.safety",
            "profiles.conservative.safety.max",
        ):
            assert forbidden not in result


class TestLoadProfileNames:
    def test_extracts_profile_names(self, tmp_path: Path) -> None:
        f = tmp_path / "s.yml"
        f.write_text(
            "profiles:\n  conservative:\n    x: 1\n  aggressive:\n    y: 2\n",
            encoding="utf-8",
        )
        assert _load_profile_names(f) == {"conservative", "aggressive"}

    def test_no_profiles_block(self, tmp_path: Path) -> None:
        f = tmp_path / "s.yml"
        f.write_text("live:\n  tick_seconds: 5.0\n", encoding="utf-8")
        assert _load_profile_names(f) == set()

    def test_empty_profiles_block(self, tmp_path: Path) -> None:
        f = tmp_path / "s.yml"
        f.write_text("profiles:\n", encoding="utf-8")
        assert _load_profile_names(f) == set()


class TestExtractEnvKeys:
    def test_active_keys(self, tmp_path: Path) -> None:
        f = tmp_path / "ex.env"
        f.write_text("FOO=1\nBAR=2\n", encoding="utf-8")
        assert _extract_env_keys(f) == {"FOO", "BAR"}

    def test_commented_keys_count(self, tmp_path: Path) -> None:
        f = tmp_path / "ex.env"
        f.write_text("# COMMENTED_KEY=value\nACTIVE_KEY=1\n", encoding="utf-8")
        assert _extract_env_keys(f) == {"COMMENTED_KEY", "ACTIVE_KEY"}

    def test_pure_comment_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "ex.env"
        f.write_text("# This is a section header\n# More words here\n", encoding="utf-8")
        assert _extract_env_keys(f) == set()

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "ex.env"
        f.write_text("\n\nKEY=value\n\n", encoding="utf-8")
        assert _extract_env_keys(f) == {"KEY"}
