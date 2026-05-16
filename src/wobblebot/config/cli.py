"""Pydantic schemas for the per-CLI sections of settings.yml.

One model per CLI entry point (cli/live, cli/shadow, cli/observe,
cli/preflight, cli/status, cli/sandbox). Each holds only the knobs
that CLI cares about; engine knobs (grid, safety) and advisor knobs
live in their own sections.

Symbol fields accept YAML string form (e.g. ``BTC/USD``) and parse
to :class:`Symbol` via the value object's ``from_string`` classmethod.
The CLI flag layer converts comma-separated strings to lists before
passing to these models.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from wobblebot.domain.value_objects import Symbol

LogFormat = Literal["plain", "json"]


def _coerce_symbol_list(value: object) -> list[Symbol]:
    """Accept a list of ``Symbol``-shaped inputs and return ``list[Symbol]``.

    Each item may be a ``Symbol`` instance, a ``"BASE/QUOTE"`` string,
    or a ``{"base": ..., "quote": ...}`` mapping. Anything else raises.
    """
    if not isinstance(value, list):
        raise ValueError(f"symbols must be a list; got {type(value).__name__}")
    if not value:
        raise ValueError("symbols list must contain at least one entry")
    out: list[Symbol] = []
    for item in value:
        if isinstance(item, Symbol):
            out.append(item)
        elif isinstance(item, str):
            out.append(Symbol.from_string(item))
        elif isinstance(item, dict):
            out.append(Symbol(**item))
        else:
            raise ValueError(f"cannot parse symbol from {type(item).__name__}: {item!r}")
    return out


def _coerce_symbol(value: object) -> Symbol:
    """Single-symbol counterpart to ``_coerce_symbol_list``."""
    if isinstance(value, Symbol):
        return value
    if isinstance(value, str):
        return Symbol.from_string(value)
    if isinstance(value, dict):
        return Symbol(**value)
    raise ValueError(f"cannot parse symbol from {type(value).__name__}: {value!r}")


# ---------------------------------------------------------------------------
# Live operational CLI
# ---------------------------------------------------------------------------


class LiveConfig(BaseModel):
    """Settings for ``cli/live`` (real-money trading)."""

    symbols: list[Symbol]
    db: str = "data/wobblebot-live.db"
    tick_seconds: float = Field(default=5.0, gt=0)
    # ``None`` means "run indefinitely" — for long-running operational
    # mode. Positive number caps the session at that many minutes.
    # Stage 3.6a introduced the Optional shape; pre-3.6a the field was
    # ``Field(gt=0)`` with no escape hatch.
    max_runtime_minutes: float | None = Field(default=60.0, gt=0)
    max_session_loss_usd: Decimal = Field(default=Decimal("5"), gt=Decimal("0"))
    log_format: LogFormat = "plain"

    class Config:
        frozen = True

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[Symbol]:
        return _coerce_symbol_list(v)


# ---------------------------------------------------------------------------
# Shadow CLI (simulated trading against live prices)
# ---------------------------------------------------------------------------


class ShadowConfig(BaseModel):
    """Settings for ``cli/shadow`` — same shape as Live plus synthetic
    balances and per-fill fee rates.

    ``initial_balances`` is required (no inference from real Kraken,
    per ADR-008's muscle-memory guard). ``USD`` must always be present.
    """

    symbols: list[Symbol]
    db: str = "data/wobblebot-shadow.db"
    tick_seconds: float = Field(default=5.0, gt=0)
    # ``None`` means "run indefinitely." Same shape as ``LiveConfig``.
    max_runtime_minutes: float | None = Field(default=60.0, gt=0)
    max_session_loss_usd: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))
    log_format: LogFormat = "plain"

    initial_balances: dict[str, Decimal]
    maker_fee_rate: Decimal = Field(default=Decimal("0.0026"), ge=Decimal("0"))
    taker_fee_rate: Decimal = Field(default=Decimal("0.0040"), ge=Decimal("0"))

    class Config:
        frozen = True

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[Symbol]:
        return _coerce_symbol_list(v)

    @field_validator("initial_balances")
    @classmethod
    def _require_usd(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        if "USD" not in v:
            raise ValueError("shadow.initial_balances must include a USD entry")
        return v


# ---------------------------------------------------------------------------
# Observe CLI (read-only data collection)
# ---------------------------------------------------------------------------


class ObserveConfig(BaseModel):
    """Settings for ``cli/observe``.

    Polling cadences live in the top-level ``schedules:`` block:
    ``schedules.observe_prices`` and ``schedules.observe_balances``.
    The balance schedule may be ``0s`` to disable balance polling.
    """

    symbols: list[Symbol]
    db: str = "data/wobblebot-observe.db"
    log_format: LogFormat = "plain"

    class Config:
        frozen = True

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[Symbol]:
        return _coerce_symbol_list(v)


# ---------------------------------------------------------------------------
# Validate CLI (live dry-run via validate=true)
# ---------------------------------------------------------------------------


class PreflightConfig(BaseModel):
    """Settings for ``cli/preflight``. Single ``symbol`` (singular)
    because validate runs ONE engine step."""

    symbol: Symbol
    log_format: LogFormat = "plain"

    class Config:
        frozen = True

    @field_validator("symbol", mode="before")
    @classmethod
    def _parse_symbol(cls, v: object) -> Symbol:
        return _coerce_symbol(v)


# ---------------------------------------------------------------------------
# Check CLI (read-only price + balance fetch)
# ---------------------------------------------------------------------------


class StatusConfig(BaseModel):
    """Settings for ``cli/status`` — live read-only sanity check."""

    symbol: Symbol
    log_format: LogFormat = "plain"

    class Config:
        frozen = True

    @field_validator("symbol", mode="before")
    @classmethod
    def _parse_symbol(cls, v: object) -> Symbol:
        return _coerce_symbol(v)


# ---------------------------------------------------------------------------
# Simulate CLI (Phase 1 mock-only sandbox)
# ---------------------------------------------------------------------------


class SandboxConfig(BaseModel):
    """Settings for ``cli/sandbox`` — mock-only paper cycle.
    No symbols list; the simulator hard-codes BTC/USD scenarios."""

    db: str = "data/wobblebot-sim.db"
    log_format: LogFormat = "plain"

    class Config:
        frozen = True


# ---------------------------------------------------------------------------
# News CLI (Stage 3.2.5 — long-running news poller)
# ---------------------------------------------------------------------------


class RssFeedSpec(BaseModel):
    """One RSS/Atom feed entry under ``news.rss_feeds``.

    ``source_id`` is operator-chosen (e.g. ``"rss:coindesk"``) and
    becomes ``NewsItem.source``. ``enabled: false`` parks the feed
    in the config without removing it.
    """

    source_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    enabled: bool = True

    class Config:
        frozen = True


class CryptoCompareSpec(BaseModel):
    """CryptoCompare News block under ``news.cryptocompare``.

    The API key itself lives in ``$CRYPTOCOMPARE_API_KEY`` (not in
    the YAML). ``enabled: false`` skips the adapter even if the key
    is present.
    """

    enabled: bool = False
    lang: str = "EN"
    categories: str | None = None

    class Config:
        frozen = True


class NewsConfig(BaseModel):
    """Settings for ``cli/news``.

    Operator lists every feed source they want polled. Disabled
    sources are parsed but not constructed at runtime — one toggle
    to silence a noisy outlet without removing it from the YAML.

    Polling cadence lives in the top-level ``schedules:`` block as
    ``schedules.news`` (applies uniformly to every enabled source).
    Per ADR-007 we don't need per-source cadence at this stage —
    news is hours-cycle anyway.
    """

    db: str = "data/wobblebot-news.db"
    rss_feeds: list[RssFeedSpec] = Field(default_factory=list)
    cryptocompare: CryptoCompareSpec = Field(default_factory=CryptoCompareSpec)
    log_format: LogFormat = "plain"

    class Config:
        frozen = True


# ---------------------------------------------------------------------------
# Advise CLI (Stage 3.3 — Passive Advisory Workflow)
# ---------------------------------------------------------------------------


class AdviseConfig(BaseModel):
    """Settings for ``cli/advise``.

    Reads prices from ``observe_db`` and news from ``news_db`` (project
    convention keeps the per-CLI DBs separated). Persists advisor
    suggestions to its own ``db``. Cadence lives in
    ``schedules.advise``.

    Stage 3.6b grew this from single-symbol (one daemon per coin) to
    multi-symbol with **per-symbol-isolated LLM calls** — the daemon
    iterates serial per symbol within each tick, building a
    single-symbol ``PerformanceSummary`` for each call so the LLM
    never sees more than one coin's context at a time. Cross-
    contamination of opinions is prevented by construction; the
    operator gets one process to monitor instead of N.
    """

    symbols: list[Symbol]
    db: str = "data/wobblebot-advise.db"
    observe_db: str = "data/wobblebot-observe.db"
    news_db: str = "data/wobblebot-news.db"
    metrics_lookback_hours: float = Field(default=6.0, gt=0)
    news_lookback_hours: float = Field(default=24.0, ge=0)  # 0 disables news context
    news_match_coin: bool = False
    news_limit: int = Field(default=20, ge=0)
    log_format: LogFormat = "plain"

    class Config:
        frozen = True

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[Symbol]:
        return _coerce_symbol_list(v)


# ---------------------------------------------------------------------------
# Harvest CLI (Phase 4 — Stage 4.2 read-only balance monitoring)
# ---------------------------------------------------------------------------


class HarvestConfig(BaseModel):
    """Settings for ``cli/harvest`` — Phase 4 treasury monitor.

    Stage 4.2 surface: poll Kraken USD balance, run the
    ``propose_transfer()`` decision against the operator's
    ``HarvesterConfig`` thresholds, and log what *would* be proposed.
    No transfers, no DB writes for proposals (that's 4.3's job once
    proposals become operator-reviewable). Uses the read-only
    ``KRAKEN_API_KEY`` — the Harvester key with Withdraw scope isn't
    needed until 4.4.

    The Stage 4.2 ``today_total_withdrawn_usd`` parameter always
    flows in as 0 because no transfers happen. Once 4.3+ persists
    real withdrawals, the daemon queries that history for the rolling
    24h total.
    """

    log_format: LogFormat = "plain"

    class Config:
        frozen = True


__all__ = [
    "AdviseConfig",
    "CryptoCompareSpec",
    "HarvestConfig",
    "LiveConfig",
    "LogFormat",
    "NewsConfig",
    "ObserveConfig",
    "PreflightConfig",
    "RssFeedSpec",
    "SandboxConfig",
    "ShadowConfig",
    "StatusConfig",
]
