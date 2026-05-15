"""
Config layer - Configuration schemas, loaders, and validation.

This layer handles loading, parsing, and validating configuration from files,
environment variables, and providing type-safe configuration objects.
"""

from wobblebot.config.advisor import (
    AdvisorConfig,
    AdvisorType,
    AggregatorStrategy,
    ArbitratorConfig,
    AutoApplyConfig,
    ExpertConfig,
    ExpertRole,
    InferenceParams,
    LLMProvider,
)
from wobblebot.config.cli import (
    LiveConfig,
    LogFormat,
    ObserveConfig,
    PreflightConfig,
    SandboxConfig,
    ShadowConfig,
    StatusConfig,
)
from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels
from wobblebot.config.loader import WobbleBotConfig, load_config
from wobblebot.config.logging import JsonFormatter, configure_logging
from wobblebot.config.resolver import deep_merge, resolve_config
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig

__all__ = [
    "AdvisorConfig",
    "AdvisorType",
    "AggregatorStrategy",
    "ArbitratorConfig",
    "AutoApplyConfig",
    "CoinGridConfig",
    "EmergencyStopConfig",
    "ExpertConfig",
    "ExpertRole",
    "GridConfig",
    "GridLevels",
    "InferenceParams",
    "JsonFormatter",
    "LLMProvider",
    "LiveConfig",
    "LogFormat",
    "ObserveConfig",
    "PreflightConfig",
    "SafetyConfig",
    "SandboxConfig",
    "ShadowConfig",
    "StatusConfig",
    "WobbleBotConfig",
    "configure_logging",
    "deep_merge",
    "load_config",
    "resolve_config",
]
