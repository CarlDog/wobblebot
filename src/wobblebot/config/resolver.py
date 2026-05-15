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

from typing import Any


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
                f"Profile {profile_name!r} not found in config; " f"available profiles: {available}"
            )
        base = deep_merge(base, profiles[profile_name])

    if cli_overrides:
        base = deep_merge(base, cli_overrides)

    return base
