"""Equities activation boundary; no real-share broker adapter ships in this build.

A crypto or tokenized-asset adapter cannot satisfy this capability. The explicit
flag travels through the ordinary YAML/profile/config validation path so no CLI,
deployment generator or web startup can silently accept unavailable activation.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, StrictBool, model_validator


class EquitiesConfig(BaseModel):
    """Disabled by default; enabling requires a future verified securities adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: StrictBool = False

    @model_validator(mode="after")
    def require_supported_adapter(self) -> Self:
        """Fail before any provider wiring instead of pretending crypto is equities."""
        if self.enabled:
            raise ValueError(
                "Equities support unavailable: this build has no supported Kraken Securities "
                "stock/ETF adapter. Set equities.enabled=false to continue crypto operation. "
                "Enable only after a verified official securities API contract and adapter "
                "are implemented; Spot crypto and xStocks do not satisfy this requirement."
            )
        return self
