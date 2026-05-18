"""Settings.yml rewriter for the Stage 3.4b auto-apply flow.

The operator's ``config/settings.yml`` is the source of truth for
runtime grid params; ``cli/apply --commit`` mutates it in place when a
suggestion clears the gate. PyYAML can't round-trip comments or
preserve key order, both of which matter for an operator-edited file
heavily annotated with WHY each value is what it is. ``ruamel.yaml``'s
round-trip mode handles both, plus block-style preservation and
quoting.

Public surface:

- ``apply_grid_overrides(path, *, symbol, overrides) -> str`` reads
  the file, applies the per-key overrides under the right grid
  section (per-coin override if it exists, else the default block),
  writes atomically (temp file + rename), and returns a unified diff
  of the before/after contents.

The function refuses to write if the file has structural surprises
(missing ``grid:`` block, missing ``default:``, etc.) — better to bail
loudly than silently rewrite into a broken state.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Mapping
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


class SettingsRewriteError(RuntimeError):
    """Raised when the rewriter can't safely apply the requested change.

    Distinct from ValueError so callers can pattern-match without
    swallowing unrelated parse failures.
    """


def apply_grid_overrides(
    path: Path,
    *,
    symbol: str,
    overrides: dict[str, Decimal | int | float],
) -> str:
    """Apply per-key grid overrides to ``settings.yml`` and return a diff.

    Behavior:
    - Reads ``path`` with ``ruamel.yaml`` round-trip preserving every
      comment, key order, and quoting style.
    - Locates the section to update: ``grid.coins.<symbol>`` if the
      coin has a per-coin entry, otherwise ``grid.default``.
    - Updates each key in ``overrides`` in place. Decimal values are
      written as plain numerics (no Python-style precision noise).
    - Writes to ``path.with_suffix('.yml.tmp')`` first then renames —
      a partial write can't leave the file half-rewritten.
    - Returns a unified diff (string) of the change for the caller to
      log / show the operator.

    Args:
        path: Absolute or relative path to the settings YAML.
        symbol: Coin base (e.g. ``"BTC"``). Looked up case-insensitive
            inside ``grid.coins``.
        overrides: Per-key proposals. Only the keys present are
            written; existing keys not in ``overrides`` are left
            untouched.

    Returns:
        Unified diff string. Empty string if no keys changed
        (overrides were no-ops because the values already matched).

    Raises:
        SettingsRewriteError: When the file structure is missing the
            expected ``grid.default`` block, or another integrity
            condition fails.
        FileNotFoundError: If ``path`` doesn't exist.
    """
    if not overrides:
        return ""

    before_text = path.read_text(encoding="utf-8")
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    document = yaml.load(before_text)

    grid_section = document.get("grid") if document is not None else None
    if grid_section is None:
        raise SettingsRewriteError(
            f"settings file {path} has no `grid:` block; cannot apply overrides"
        )

    target = _resolve_grid_target(grid_section, symbol=symbol)

    for key, raw_value in overrides.items():
        existing = target.get(key) if hasattr(target, "get") else None
        target[key] = _yaml_scalar(raw_value, existing=existing)

    after_buffer = _dump_to_string(yaml, document)
    if after_buffer == before_text:
        return ""

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(after_buffer, encoding="utf-8")
    os.replace(tmp_path, path)

    return _unified_diff(before_text, after_buffer, path=path)


def apply_dotted_overrides(
    path: Path,
    *,
    overrides: Mapping[str, Decimal | int | float],
) -> str:
    """Apply dotted-path overrides spanning multiple top-level sections.

    Companion to :func:`apply_grid_overrides`. Where that function is
    grid-aware (per-coin lookup + default fallback), this one is
    section-agnostic: each key is a fully-qualified dotted path into
    the YAML document (e.g. ``"safety.max_total_exposure_usd"`` or
    ``"harvester.topup_threshold_usd"`` or
    ``"grid.coins.DOGE.order_size_usd"``).

    Used by ``cli/recalibrate --commit`` (Stage 7.6.B) to write the
    calibrator's :class:`RecalibrationProposal` changes back to
    settings.yml in one atomic round-trip.

    Args:
        path: Absolute or relative path to the settings YAML.
        overrides: Map from dotted path to scalar value. Each path
            must already exist in the document — this function will
            NOT create new keys (a typo'd path raises rather than
            silently appending a new field).

    Returns:
        Unified diff (string) of the change. Empty when no keys
        changed (e.g. proposed values matched current).

    Raises:
        SettingsRewriteError: When a dotted path doesn't resolve in
            the document, or the file structure is otherwise
            surprising.
        FileNotFoundError: If ``path`` doesn't exist.
    """
    if not overrides:
        return ""

    before_text = path.read_text(encoding="utf-8")
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    document = yaml.load(before_text)

    if document is None:
        raise SettingsRewriteError(f"settings file {path} parsed as empty / null")

    for dotted_path, raw_value in overrides.items():
        _write_dotted_path(document, dotted_path, raw_value)

    after_buffer = _dump_to_string(yaml, document)
    if after_buffer == before_text:
        return ""

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(after_buffer, encoding="utf-8")
    os.replace(tmp_path, path)

    return _unified_diff(before_text, after_buffer, path=path)


def _write_dotted_path(document: Any, dotted_path: str, raw_value: Decimal | int | float) -> None:
    """Walk ``document`` to the parent of the final key and write."""
    parts = dotted_path.split(".")
    if not parts:
        raise SettingsRewriteError(f"empty dotted path: {dotted_path!r}")
    cursor: Any = document
    for part in parts[:-1]:
        if not hasattr(cursor, "get") or cursor.get(part) is None:
            raise SettingsRewriteError(
                f"dotted path {dotted_path!r} does not resolve: " f"missing {part!r} in document"
            )
        cursor = cursor[part]
    leaf = parts[-1]
    if not hasattr(cursor, "get"):
        raise SettingsRewriteError(
            f"dotted path {dotted_path!r} parent is not a mapping; " f"cannot write {leaf!r}"
        )
    if leaf not in cursor:
        raise SettingsRewriteError(
            f"dotted path {dotted_path!r} does not resolve: "
            f"missing leaf {leaf!r}. The rewriter will not create new "
            "keys; verify the path against settings.yml."
        )
    existing = cursor.get(leaf)
    cursor[leaf] = _yaml_scalar(raw_value, existing=existing)


def _resolve_grid_target(grid_section: Any, *, symbol: str) -> Any:
    """Return the mapping that holds the per-coin grid for ``symbol``.

    Operator config has ``grid.default`` plus optional per-coin
    overrides under ``grid.coins.<SYMBOL>``. Symbol matching is
    case-insensitive — both ``"BTC"`` and ``"btc"`` find the same
    entry. Falls back to ``grid.default`` when the operator hasn't
    pinned a per-coin override yet (the gate ran against an effective
    grid derived from ``default``, so we mutate the same place).
    """
    coins_section = grid_section.get("coins") if hasattr(grid_section, "get") else None
    if coins_section is not None:
        match_key = _find_case_insensitive_key(coins_section, symbol)
        if match_key is not None:
            return coins_section[match_key]

    default_section = grid_section.get("default") if hasattr(grid_section, "get") else None
    if default_section is None:
        raise SettingsRewriteError(
            "settings file has no `grid.default:` block; cannot apply overrides "
            f"for symbol {symbol!r}"
        )
    return default_section


def _find_case_insensitive_key(mapping: Any, target: str) -> str | None:
    target_upper = target.upper()
    for key in mapping.keys():
        if str(key).upper() == target_upper:
            return str(key)
    return None


def _yaml_scalar(value: Decimal | int | float, *, existing: Any = None) -> Any:
    """Coerce a numeric override into a ruamel.yaml-friendly scalar.

    Style preservation: when the override is an integer-valued
    ``Decimal`` AND the existing field was a float (e.g. operator
    wrote ``1.0``), we emit a float (``1.0``) rather than ``1``. That
    keeps the file's existing style — ``1.0`` doesn't suddenly
    become ``1`` and trigger a churny diff. When the existing field
    is missing or already an int, integer-valued Decimals render as
    int.
    """
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass; guard it first so we don't
        # silently coerce True/False into 1/0.
        raise SettingsRewriteError(
            f"cannot rewrite bool value into a numeric grid field: {value!r}"
        )
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            # Match existing style: if the operator's field was a float,
            # keep it a float. Otherwise emit int.
            if isinstance(existing, float):
                return float(value)
            return int(value)
        return float(value)
    if isinstance(value, (int, float)):
        return value
    raise SettingsRewriteError(f"cannot rewrite non-numeric value into grid field: {value!r}")


def _dump_to_string(yaml: YAML, document: Any) -> str:
    """ruamel's ``dump`` writes to a stream; round-trip to a string."""
    buffer = StringIO()
    yaml.dump(document, buffer)
    return buffer.getvalue()


def _unified_diff(before: str, after: str, *, path: Path) -> str:
    """Return a unified-diff string for operator review.

    Three context lines is enough to anchor each change inside the
    surrounding grid block; the file's long comment paragraphs don't
    need to land in the diff.
    """
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=3,
    )
    return "".join(diff_lines)
