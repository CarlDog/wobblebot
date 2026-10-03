"""Sanitized, persisted provider observations from an authorized daemon."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ProviderObservation(BaseModel):
    """Only bounded status information crosses into the web reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    ok: bool
    detail: str = Field(max_length=160)
    checked_at: AwareDatetime


class ProviderHealthSnapshot(BaseModel):
    """A complete producer snapshot; replacement removes retired endpoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    producer: Literal["operator", "advise"]
    checked_at: AwareDatetime
    observations: tuple[ProviderObservation, ...] = Field(max_length=10)
