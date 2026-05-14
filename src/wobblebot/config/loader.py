"""YAML loader for the WobbleBot configuration file.

Returns a :class:`WobbleBotConfig` containing the grid and safety sections.
Other top-level YAML keys (``application``, ``exchange``, ``logging``,
``database``, etc.) are tolerated and ignored — they are loaded by their
own modules or not yet implemented as of Stage 2.2.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from wobblebot.config.grid import GridConfig
from wobblebot.config.safety import SafetyConfig


class WobbleBotConfig(BaseModel):
    """Top-level config aggregate. Holds only what Stage 2.2 needs.

    Future stages extend this by adding fields (e.g. ``advisor``,
    ``harvester``) — extra keys in the YAML are ignored, so adding a
    section to the file before adding its schema does not break loading.
    """

    grid: GridConfig
    safety: SafetyConfig

    class Config:
        frozen = True


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
