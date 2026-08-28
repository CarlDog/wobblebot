"""YAML loader for the WobbleBot configuration file.

Returns a :class:`WobbleBotConfig` containing the per-section schemas
that have shipped so far. Extra top-level YAML keys (e.g.
``application``, ``exchange``, ``logging``, ``database``,
``harvester``) are tolerated and ignored — they are loaded by their
own modules or not yet implemented.

The ``profiles:`` block is captured as a raw dict; the audit-slice-3
resolver merges a named profile into the base config before
validation. ``WobbleBotConfig.model_validate`` itself does NOT apply
profiles — that's a separate layer between YAML load and CLI use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from wobblebot.config.advisor import AdvisorConfig
from wobblebot.config.cli import (
    AdviseConfig,
    ApplicationConfig,
    HarvestConfig,
    LiveConfig,
    MaintenanceConfig,
    NewsConfig,
    ObserveConfig,
    OperatorConfig,
    PreflightConfig,
    SandboxConfig,
    ScreenerConfig,
    ShadowConfig,
    StatusConfig,
    WebConfig,
)
from wobblebot.config.grid import GridConfig
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.config.llm import LLMConfig
from wobblebot.config.safety import SafetyConfig
from wobblebot.config.schedules import SchedulesConfig

# Config keys that a previous release honored and this one does not.
#
# WHY THIS EXISTS. Pydantic's default is ``extra="ignore"``, so a
# retired key loads silently — the operator's file still says
# ``emergency_stop``, the operator still believes it's a hard balance
# floor, and nothing tells them otherwise. That is *precisely* the
# failure ADR-032 retired the block to end ("a silent dead safety knob
# is worse than none"), so letting the retirement itself land silently
# would preserve the exact problem for exactly the operators the fix
# was for.
#
# Deliberately NOT ``extra="forbid"`` on every config model: that would
# reject any unknown key anywhere, including harmless operator
# annotations, and turn a targeted upgrade check into a broad
# compatibility break. This is a named list of keys we know we killed.
#
# Add a row whenever an ADR retires a key. Dotted path → a message that
# says what replaced it, so the error is a fix instruction rather than
# a complaint.
#
# ⚠️ BEFORE ADDING A ROW, CHECK THE DEPLOYED CONFIG. This is a hard
# failure at load, and six of the eight daemons run under
# `restart: unless-stopped` — shipping a row that matches a key still
# present in the operator's live `settings.yml` turns one stale line
# into a crash loop (the fleet's docker-deployments rule #6). Hard
# failure is still right for this class of key, because a WARN would
# recreate exactly the silent-untrue-config problem the retirement was
# meant to end; the safeguard is verifying first, not softening the
# check. The `emergency_stop` row was verified clean against the live
# NAS config, the local checkout, and settings.example.yml on
# 2026-08-28 before it shipped.
_RETIRED_KEYS: dict[str, str] = {
    "safety.emergency_stop": (
        "retired by ADR-032. It was read by nobody — an operator could reasonably "
        "believe it was a hard balance floor while it did nothing. The session halt "
        "is enforced by `live.max_session_loss_usd` (with the ADR-024 cool-down), and "
        "the cost-basis `safety.sell_guard` supersedes the rest. Delete the "
        "`emergency_stop` block; no replacement key is needed."
    ),
}


def _find_retired_keys(raw: Any) -> list[tuple[str, str]]:
    """Locate any ``_RETIRED_KEYS`` path present in a raw config mapping.

    Scans the effective (already profile-merged) config and, separately,
    every ``profiles.<name>`` sub-tree — a retired key parked in a
    profile the operator isn't currently running is still a key they
    believe is doing something.

    Returns ``(where_it_was_found, registry_key)`` pairs in registry
    order, so the error message is stable across runs.
    """
    if not isinstance(raw, dict):
        return []

    def _present(tree: Any, dotted: str) -> bool:
        node: Any = tree
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    found: list[tuple[str, str]] = []
    profiles = raw.get("profiles")
    for dotted in _RETIRED_KEYS:
        if _present(raw, dotted):
            found.append((dotted, dotted))
        if isinstance(profiles, dict):
            for profile_name, profile_body in profiles.items():
                if _present(profile_body, dotted):
                    found.append((f"profiles.{profile_name}.{dotted}", dotted))
    return found


class WobbleBotConfig(BaseModel):
    """Top-level config aggregate.

    Engine knobs (``grid``, ``safety``) are required for any CLI that
    runs the engine. Per-CLI sections are optional — operator can
    leave out sections for CLIs they don't use; CLI defaults fill in.
    The ``profiles`` map is loaded as raw dicts and consumed by the
    profile resolver before this model validates.
    """

    grid: GridConfig
    safety: SafetyConfig
    schedules: SchedulesConfig = Field(default_factory=lambda: SchedulesConfig(root={}))
    # Application metadata + the single deployment-wide trading mode
    # (live / shadow / sandbox). Optional — defaults to live when the
    # block is omitted. cli/web reads ``application.mode`` for the
    # dashboard mode-badge.
    application: ApplicationConfig | None = None
    live: LiveConfig | None = None
    shadow: ShadowConfig | None = None
    observe: ObserveConfig | None = None
    preflight: PreflightConfig | None = None
    status: StatusConfig | None = None
    sandbox: SandboxConfig | None = None
    screener: ScreenerConfig | None = None
    news: NewsConfig | None = None
    advise: AdviseConfig | None = None
    advisor: AdvisorConfig | None = None
    harvest: HarvestConfig | None = None
    harvester: HarvesterConfig | None = None
    operator: OperatorConfig | None = None
    llm: LLMConfig | None = None
    web: WebConfig | None = None
    maintenance: MaintenanceConfig | None = None
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    class Config:
        frozen = True
        # Pydantic v2: this is the equivalent of populate_by_name plus
        # accepting alias on input.

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_keys(cls, raw: Any) -> Any:
        """Refuse a config carrying a key a previous release honored.

        Runs ``mode="before"`` because validation is what *drops* the
        key — by the time the model exists there is nothing left to see.
        Placed on the model rather than in a loader function so both
        entry points (``load_config`` and ``runtime.load_resolved_config``,
        which each call ``model_validate`` separately) are covered by one
        implementation that a third caller could not bypass.
        """
        found = _find_retired_keys(raw)
        if not found:
            return raw
        details = "\n".join(f"  - {where}: {_RETIRED_KEYS[key]}" for where, key in found)
        raise ValueError(
            "this config carries settings a previous release honored and this one "
            f"does not:\n{details}\n"
            "Remove the listed key(s). They are silently ignored otherwise, which "
            "leaves the file claiming a behavior the running system does not have."
        )


def load_config(path: Path) -> WobbleBotConfig:
    """Load and validate a WobbleBot YAML config from ``path``.

    Args:
        path: Filesystem path to a YAML config file.

    Returns:
        Parsed :class:`WobbleBotConfig`.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: The file is not a YAML mapping at the root.
        pydantic.ValidationError: Required sections are missing or
            individual fields fail validation.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping at the root")

    return WobbleBotConfig.model_validate(raw)
