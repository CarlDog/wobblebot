"""Profile resolution + CLI override layering.

The audit's layering contract (per ADR-009): a CLI invocation's
effective config is the result of three layers, applied in order:

1. **Base config** — the YAML file's top-level sections, minus the
   ``profiles:`` block.
2. **Profile overrides** — the named block from ``profiles[name]``,
   applied via deep-merge if ``--profile name`` was passed.
3. **CLI flag overrides** — explicit flags from ``argparse``, applied
   via deep-merge so they win over both YAML and profile.

The result is a flat ``dict`` ready for
``WobbleBotConfig.model_validate(...)``. Pydantic does the rest of
the schema enforcement.

Deep-merge semantics:
- **Dicts merge recursively.** Nested keys not present in the overlay
  survive; conflicting keys: overlay wins.
- **Lists override entirely.** Per ADR-009 — operator who wants to add
  experts to a profile re-lists all of them. Append-semantics would
  surprise more often than they help.
- **Scalars override.** No magic.

This module does NOT do any YAML parsing or filesystem I/O. It takes
already-parsed dicts and returns merged dicts. The CLI is responsible
for loading the YAML, picking the profile name from argparse, building
the overrides dict, and validating the result.
"""

from __future__ import annotations

from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, RootModel

from wobblebot.config.loader import WobbleBotConfig


def _strip_optional(annotation: Any) -> Any:
    """Reduce ``X | None`` (or ``Optional[X]``) to ``X``; pass through otherwise."""
    if get_origin(annotation) in (UnionType, Union):
        non_none = [a for a in get_args(annotation) if a is not NoneType]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _dict_value_model(annotation: Any) -> type[BaseModel] | None:
    """If ``annotation`` is ``dict[str, SomeModel]``, return ``SomeModel``.

    Used for free-form-keyed sections like ``grid.coins`` (keyed by coin
    symbol, not a fixed field name) — the key itself isn't checked against
    a schema, but each value still validates against ``SomeModel``.
    """
    if get_origin(annotation) is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
            return args[1]
    return None


def _list_item_model(annotation: Any) -> type[BaseModel] | None:
    """If ``annotation`` is ``list[SomeModel]``, return ``SomeModel``.

    Used for fields like ``advisor.experts: list[ExpertConfig]`` — a
    profile overlay replaces the whole list (deep_merge's "lists override
    entirely"), but each replacement item still validates against
    ``SomeModel`` so a typo inside a list item isn't invisible to the
    guard just because it arrived via a list instead of a dict.
    """
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def _root_model_dict_value_model(target: type[BaseModel]) -> type[BaseModel] | None:
    """For a ``RootModel`` subclass, resolve its wrapped ``dict[str, SubModel]``.

    ``target`` is itself free-form-keyed (e.g. ``SchedulesConfig`` over
    ``dict[str, timedelta]``) — its only declared field is named
    ``"root"``, which is never a real schema key, so callers must not
    check overlay keys against it directly. Returns ``None`` when the
    wrapped type has no nested model to validate (a scalar-valued root
    like ``timedelta`` — the whole section is then opaque).
    """
    root_field = target.model_fields.get("root")
    if root_field is None:
        return None
    return _dict_value_model(root_field.annotation)


def _paths_under_dict_value_model(
    value: dict[str, Any], value_model: type[BaseModel], path: str
) -> list[str]:
    """Recurse into each dict entry's value against ``value_model``.

    The dict's own keys are free-form (a coin symbol, a schedule name, ...)
    and aren't checked against a schema; only each entry's value is.
    """
    bad_paths: list[str] = []
    for sub_key, sub_value in value.items():
        if isinstance(sub_value, dict):
            bad_paths.extend(
                _unknown_overlay_paths(sub_value, value_model, prefix=f"{path}.{sub_key}")
            )
    return bad_paths


def _paths_for_dict_value(value: dict[str, Any], target: Any, path: str) -> list[str]:
    """Resolve an overlay dict ``value`` against its field's ``target`` type.

    ``target`` may be a fixed-field submodel, a ``RootModel``-wrapped
    free-form section, or a ``dict[str, SubModel]`` mapping — each needs
    a different walk, factored out here so the main loop in
    ``_unknown_overlay_paths`` stays flat.
    """
    if isinstance(target, type) and issubclass(target, BaseModel):
        if issubclass(target, RootModel):
            value_model = _root_model_dict_value_model(target)
            return (
                []
                if value_model is None
                else _paths_under_dict_value_model(value, value_model, path)
            )
        return _unknown_overlay_paths(value, target, prefix=path)
    value_model = _dict_value_model(target)
    return [] if value_model is None else _paths_under_dict_value_model(value, value_model, path)


def _unknown_overlay_paths(
    overlay: dict[str, Any], model_cls: type[BaseModel], *, prefix: str = ""
) -> list[str]:
    """Recursively find ``overlay`` keys with no matching field on ``model_cls``.

    Fleet-review #19 finding 2: ``profiles.<name>`` is captured as a raw
    ``dict[str, Any]`` and deep-merged over the base config without ever
    being validated against the real schema. Pydantic's default
    ``extra='ignore'`` then silently drops a typo'd key (e.g.
    ``profiles.conservative.saftey``) when the merged dict is validated —
    the session runs on base-config limits while the operator believes
    the profile is active. This walks the overlay's dotted key paths
    against ``model_cls``'s actual fields (recursing into nested
    sub-models, ``dict[str, SubModel]`` sections like ``grid.coins``,
    ``list[SubModel]`` sections like ``advisor.experts``, and unwrapping
    ``RootModel``-backed free-form sections like ``schedules`` so their
    own keys aren't mistaken for fixed field names) so a typo surfaces
    as a loud config error instead of vanishing.
    """
    bad_paths: list[str] = []
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else key
        field = model_cls.model_fields.get(key)
        if field is None:
            bad_paths.append(path)
            continue
        target = _strip_optional(field.annotation)

        if isinstance(value, list):
            item_model = _list_item_model(target)
            if item_model is not None:
                for idx, item in enumerate(value):
                    if isinstance(item, dict):
                        bad_paths.extend(
                            _unknown_overlay_paths(item, item_model, prefix=f"{path}[{idx}]")
                        )
        elif isinstance(value, dict):
            bad_paths.extend(_paths_for_dict_value(value, target, path))
    return bad_paths


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``; return a new dict.

    Neither input is mutated. Lists in ``overlay`` replace lists in
    ``base`` (no append); scalars in ``overlay`` replace scalars in
    ``base``; dicts in ``overlay`` merge with same-keyed dicts in
    ``base`` recursively.
    """
    result: dict[str, Any] = dict(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(base_value, overlay_value)
        else:
            # Scalar, list, or type-mismatch: overlay wins
            result[key] = overlay_value
    return result


def resolve_config(
    raw: dict[str, Any],
    profile_name: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply profile + CLI overrides to a raw YAML config dict.

    Args:
        raw: The dict produced by ``yaml.safe_load(settings.yml)``.
            Must include a top-level ``profiles:`` key (possibly
            empty) — present even when no profiles are defined keeps
            the schema predictable.
        profile_name: If provided, the resolver looks up
            ``raw["profiles"][profile_name]`` and deep-merges it
            over the base. ``KeyError`` if the name doesn't exist.
        cli_overrides: A dict structured like the YAML config (e.g.
            ``{"live": {"tick_seconds": 10.0}}``). Deep-merged on
            top of base+profile. Empty / ``None`` skips this layer.

    Returns:
        A new dict with the ``profiles`` block stripped and overrides
        applied. Ready for ``WobbleBotConfig.model_validate(...)``.

    Raises:
        KeyError: ``profile_name`` was provided but not found.
        ValueError: The named profile's overlay contains a field path
            that doesn't exist on ``WobbleBotConfig`` — almost always a
            typo, since ``extra='ignore'`` would otherwise drop it
            silently and run the session on base-config limits.
    """
    # Pop profiles out — they're configuration metadata, not config
    # values themselves. The schema model also ignores `profiles:` if
    # it's left in (default_factory=dict catches it), but stripping
    # here makes the merged result self-evidently the operator's
    # intended config.
    base = {k: v for k, v in raw.items() if k != "profiles"}
    profiles: dict[str, Any] = raw.get("profiles", {}) or {}

    if profile_name is not None:
        if profile_name not in profiles:
            available = sorted(profiles.keys()) if profiles else []
            raise KeyError(
                f"Profile {profile_name!r} not found in config; "
                f"available profiles: {available}. If you expected this profile, "
                f"your settings.yml may predate it — copy the "
                f"'profiles.{profile_name}' block from config/settings.example.yml."
            )
        overlay = profiles[profile_name]
        bad_paths = _unknown_overlay_paths(overlay, WobbleBotConfig)
        if bad_paths:
            raise ValueError(
                f"Profile {profile_name!r} has unrecognized field path(s): "
                f"{', '.join(sorted(bad_paths))}. This is almost always a typo — "
                f"an unrecognized key is silently dropped by the config schema, "
                f"so the session would otherwise run on base config values while "
                f"you believe this profile is active. Check against "
                f"config/settings.example.yml."
            )
        base = deep_merge(base, overlay)

    if cli_overrides:
        base = deep_merge(base, cli_overrides)

    return base
