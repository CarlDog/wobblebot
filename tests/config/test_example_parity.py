"""Structural parity between ``config/settings.example.yml`` and the Pydantic
config schema (``WobbleBotConfig``).

Reads only committed code + the example — so it runs in CI **without** the
gitignored operator ``settings.yml`` (which the existing ``test_schema_drift``
needs). Two guards:

1. ``test_example_loads_as_valid_config`` — the example validates through the
   real loader. Catches missing REQUIRED fields, type errors, and the
   fee-floor ``GridConfig`` validator.
2. ``test_example_has_no_dead_keys`` — every example key maps to a real model
   field. Catches a key removed from the schema but left stale in the example.
3. ``test_example_covers_required_fields`` — every required schema field is
   present in the example. Catches a new required field added to the models
   but never documented in the example.

Arbitrary-key dict subtrees (``grid.coins``, ``profiles``, ``schedules``,
``harvester.withdrawal_destinations``, ``shadow.initial_balances``) are exempt:
operators/templates populate their keys freely, so they cannot be checked
against fixed model fields. These are detected automatically (dict / RootModel
typed fields) plus a small belt-and-suspenders set.
"""

from __future__ import annotations

import inspect
import typing
from pathlib import Path
from types import UnionType

import pytest
import yaml
from pydantic import BaseModel, RootModel

from wobblebot.config.loader import WobbleBotConfig, load_config

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.example.yml"

# Auto-detected as dict/RootModel fields below; listed too for clarity.
_EXTRA_EXEMPT = {"grid.coins", "profiles", "schedules"}


def _unwrap_optional(ann: object) -> object:
    """Strip ``X | None`` down to ``X`` (leave other annotations untouched)."""
    origin = typing.get_origin(ann)
    if origin is typing.Union or origin is UnionType:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ann


def _is_model(t: object) -> bool:
    return inspect.isclass(t) and issubclass(t, BaseModel) and not issubclass(t, RootModel)


def _is_rootmodel(t: object) -> bool:
    return inspect.isclass(t) and issubclass(t, RootModel)


def _walk(model_cls: type[BaseModel], prefix: str = "") -> tuple[set[str], set[str], set[str]]:
    """Return ``(all_paths, dict_leaf_skip_paths, required_paths)``.

    Required-ness propagates only through required parents: an optional section
    (e.g. ``live: LiveConfig | None``) never makes its children required.
    """
    paths: set[str] = set()
    skip: set[str] = set()
    required: set[str] = set()
    for name, field in model_cls.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        paths.add(path)
        is_req = field.is_required()
        if is_req:
            required.add(path)
        ann = _unwrap_optional(field.annotation)
        if _is_rootmodel(ann) or typing.get_origin(ann) is dict:
            # Arbitrary-key map — leaf for parity purposes; skip its children.
            skip.add(path)
        elif _is_model(ann):
            sub_paths, sub_skip, sub_required = _walk(ann, path)
            paths |= sub_paths
            skip |= sub_skip
            if is_req:
                required |= sub_required
    return paths, skip, required


def _extract_example_paths(skip_under: set[str]) -> set[str]:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))

    def _walk_yaml(data: object, prefix: str = "") -> set[str]:
        out: set[str] = set()
        if not isinstance(data, dict):
            return out
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            out.add(path)
            if path in skip_under:
                continue
            if isinstance(value, dict):
                out |= _walk_yaml(value, path)
        return out

    return _walk_yaml(raw)


def test_example_loads_as_valid_config() -> None:
    # Validates through the real loader: missing-required, type errors, and the
    # GridConfig fee-floor validator all surface here.
    load_config(_EXAMPLE)


def test_example_has_no_dead_keys() -> None:
    model_paths, model_skip, _ = _walk(WobbleBotConfig)
    skip = model_skip | _EXTRA_EXEMPT
    example_paths = _extract_example_paths(skip)
    dead = example_paths - model_paths
    assert not dead, (
        "settings.example.yml has keys not present in the WobbleBotConfig schema "
        f"(removed from code but left stale in the example?): {sorted(dead)}"
    )


def test_example_covers_required_fields() -> None:
    _, model_skip, required = _walk(WobbleBotConfig)
    skip = model_skip | _EXTRA_EXEMPT
    example_paths = _extract_example_paths(skip)
    missing = required - example_paths
    assert not missing, (
        "settings.example.yml is missing required schema fields "
        f"(new required field added to the models but not documented?): {sorted(missing)}"
    )
