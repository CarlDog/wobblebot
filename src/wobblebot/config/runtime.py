"""End-to-end config loading for CLIs.

One entry point — :func:`load_resolved_config` — that every CLI calls
at startup to turn the YAML file + ``--profile`` + ``--config`` +
explicit CLI flag overrides into a fully validated
:class:`WobbleBotConfig`.

Discovery order for the config file:
1. ``--config path`` if explicitly passed (error if missing).
2. ``config/settings.yml`` if it exists in the working directory.
3. ``config/settings.example.yml`` as a last-resort fallback (warn
   on stderr — operator hasn't created their own copy).

The profile resolver (see ``resolver.py``) handles the
``--profile name`` deep-merge over the base config. ``cli_overrides``
is a dict structured like the YAML config (e.g.
``{"live": {"tick_seconds": 10.0}}``); only explicitly-passed flag
values should be present so the YAML/profile defaults win for
omitted flags.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.resolver import resolve_config

_LOGGER = logging.getLogger("wobblebot.config.runtime")

_DEFAULT_CONFIG = Path("config/settings.yml")
_EXAMPLE_CONFIG = Path("config/settings.example.yml")


def load_resolved_config(
    config_path: Path | None = None,
    profile_name: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> WobbleBotConfig:
    """Load the YAML, apply profile + CLI overrides, validate, return.

    Args:
        config_path: Explicit path to the config file. ``None`` triggers
            discovery (settings.yml → settings.example.yml fallback).
        profile_name: Name of a profile in the YAML's ``profiles:`` block
            to deep-merge over the base. ``None`` skips this layer.
        cli_overrides: Per-section overrides from explicitly-passed CLI
            flags. ``None`` or empty dict skips this layer.

    Returns:
        Validated :class:`WobbleBotConfig` with profile + overrides applied.

    Raises:
        FileNotFoundError: ``config_path`` explicitly passed but missing,
            or discovery exhausted (neither settings.yml nor
            settings.example.yml exists).
        ValueError: YAML file's root is not a mapping.
        KeyError: ``profile_name`` was provided but not found.
        pydantic.ValidationError: Final merged config fails schema
            validation (missing required sections, bad types, etc.).
    """
    resolved_path = _discover_config_path(config_path)
    raw = _load_yaml(resolved_path)
    merged = resolve_config(raw, profile_name=profile_name, cli_overrides=cli_overrides)
    return WobbleBotConfig.model_validate(merged)


def _discover_config_path(config_path: Path | None) -> Path:
    """Pick the config file to load. Explicit path wins; otherwise
    operator's settings.yml; otherwise the example as a fallback."""
    if config_path is not None:
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        return config_path
    if _DEFAULT_CONFIG.exists():
        return _DEFAULT_CONFIG
    if _EXAMPLE_CONFIG.exists():
        _LOGGER.warning(
            "no operator config found; falling back to example file",
            extra={"path": str(_EXAMPLE_CONFIG)},
        )
        return _EXAMPLE_CONFIG
    raise FileNotFoundError(
        f"No config file found. Expected one of: {_DEFAULT_CONFIG}, {_EXAMPLE_CONFIG}"
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML from ``path`` as a dict. Raises ValueError if root
    isn't a mapping."""
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping at the root")
    return raw
