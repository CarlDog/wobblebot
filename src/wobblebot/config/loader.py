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
from pydantic import BaseModel, Field

from wobblebot.config.advisor import AdvisorConfig
from wobblebot.config.cli import (
    AdviseConfig,
    HarvestConfig,
    LiveConfig,
    NewsConfig,
    ObserveConfig,
    OperatorConfig,
    PreflightConfig,
    SandboxConfig,
    ShadowConfig,
    StatusConfig,
)
from wobblebot.config.grid import GridConfig
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.config.llm import LLMConfig
from wobblebot.config.safety import SafetyConfig
from wobblebot.config.schedules import SchedulesConfig


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
    live: LiveConfig | None = None
    shadow: ShadowConfig | None = None
    observe: ObserveConfig | None = None
    preflight: PreflightConfig | None = None
    status: StatusConfig | None = None
    sandbox: SandboxConfig | None = None
    news: NewsConfig | None = None
    advise: AdviseConfig | None = None
    advisor: AdvisorConfig | None = None
    harvest: HarvestConfig | None = None
    harvester: HarvesterConfig | None = None
    operator: OperatorConfig | None = None
    llm: LLMConfig | None = None
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    class Config:
        frozen = True
        # Pydantic v2: this is the equivalent of populate_by_name plus
        # accepting alias on input.


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
