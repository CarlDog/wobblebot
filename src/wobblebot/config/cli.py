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
    # Stage 5.4: path to the operator interaction DB. When set, cli/live
    # opens it as a second StoragePort + wires the OperatorService poll
    # loop. When None, cli/live runs Discord-ignorant (no operator
    # interaction, no pending-command processing). Per ADR-013 decision
    # 9 the engine code path stays the same either way — only the poll
    # is gated on this field.
    operator_db: str | None = None

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

    Stage 4.2 introduced the daemon (read balance + log). Stage 4.3
    adds persistence: every non-None proposal lands in
    ``transfer_proposals`` regardless of ``HarvesterConfig.enabled``
    (that flag gates execution in 4.4+, not the forensic record).

    Uses the read-only ``KRAKEN_API_KEY`` through 4.3; the Harvester
    key with Withdraw scope isn't needed until 4.4.

    The ``today_total_withdrawn_usd`` parameter that feeds the
    day-cap check flows in as 0 through 4.3 — no withdrawals happen
    yet. Stage 4.4 wires a real history query against the
    ``transfer_results`` table.
    """

    db: str = "data/wobblebot-harvest.db"
    log_format: LogFormat = "plain"
    # Stage 5.5: path to the operator interaction DB. When set,
    # cli/harvest opens it as a second StoragePort and writes outbound
    # event Notifications (proposal generated, withdrawal executed,
    # withdrawal failed) to the `notifications` table. cli/operator
    # (Stage 5.6) forwards them to Discord. When None, cli/harvest
    # runs Discord-ignorant — no notification persistence.
    operator_db: str | None = None

    class Config:
        frozen = True


# --------------------------------------------------------------------- #
# Stage 5.6 — cli/operator daemon                                       #
# --------------------------------------------------------------------- #


class AssistantLLMConfig(BaseModel):
    """Operator-assistant LLM configuration.

    Mirrors the trading-advisor's single-LLM config but is its own
    block: the assistant role is distinct (intent parsing vs trading
    recommendation), uses a different prompt, and often a different
    model best-suited to chat. Phase 5 ships Ollama-only; Phase 6
    adds cloud variants.
    """

    provider: Literal["ollama"] = "ollama"
    model: str = Field(min_length=1)
    prompt_file: str = Field(default="config/prompts/operator.md")
    base_url: str = Field(default="http://localhost:11434")
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)

    class Config:
        frozen = True


class OperatorAuthConfig(BaseModel):
    """Discord allowlists + bot identity for cli/operator.

    Per ADR-013 decision 6, both axes are deny-by-default — empty
    allowlists mean nothing reaches the operator daemon. User IDs are
    typically secrets (set via env var indirection or via this block;
    operators pick). Channel IDs are not.
    """

    bot_token_env_var: str = Field(default="DISCORD_BOT_TOKEN", min_length=1)
    allowed_user_ids: frozenset[str] = Field(default_factory=frozenset)
    allowed_channel_ids: frozenset[str] = Field(default_factory=frozenset)
    # The channel cli/operator posts outbound notifications + confirm
    # embeds to. Must be in allowed_channel_ids; the daemon validates
    # at startup.
    outbound_channel_id: str = Field(min_length=1)

    class Config:
        frozen = True


class OperatorConfig(BaseModel):
    """Settings for ``cli/operator`` — Stage 5.6 daemon (ADR-013).

    Composes the Discord transport's allowlists + bot identity, the
    assistant LLM block, paths to the four operator-visible DBs, and
    the multi-turn / confirmation knobs from ADR-013 decisions 5-6.

    cli/operator runs Discord-ignorant from the engine's perspective —
    it polls ``notifications`` rows (written by cli/live + cli/harvest
    via SqliteNotifierAdapter, Stage 5.5) and forwards them to Discord,
    plus handles inbound messages → AssistantPort.parse_intent →
    pending_commands write (the confirm-before-execute flow that ADR-002
    and ADR-013 require).
    """

    auth: OperatorAuthConfig
    assistant: AssistantLLMConfig

    # The operator daemon's own DB. Stage 5.4 + 5.5 + 5.6 tables
    # (pending_commands, notifications, conversation_turns) all live here.
    operator_db: str = Field(default="data/wobblebot-operator.db")

    # Optional cross-database paths for the read-only queries answered
    # directly from cli/operator (no engine round-trip). When unset,
    # the corresponding queries return empty results — graceful degrade.
    live_db: str | None = None
    advise_db: str | None = None
    news_db: str | None = None
    harvest_db: str | None = None

    # ADR-013 decision 5: 10-turn default context window for the
    # assistant's multi-turn prompt. Tunable per-deployment.
    context_window_turns: int = Field(default=10, ge=1, le=50)

    # ADR-013 decision 3: pending_commands TTL. After this many seconds
    # without an operator reaction, the row transitions to 'expired'.
    confirm_ttl_seconds: int = Field(default=300, gt=0)

    # Notification forwarder poll cadence. Lower = faster Discord
    # surfacing; higher = less CPU + DB load. 2s is a reasonable
    # default for a hobby bot.
    forwarder_poll_seconds: float = Field(default=2.0, gt=0)

    log_format: LogFormat = "plain"

    class Config:
        frozen = True


__all__ = [
    "AdviseConfig",
    "AssistantLLMConfig",
    "CryptoCompareSpec",
    "HarvestConfig",
    "LiveConfig",
    "LogFormat",
    "NewsConfig",
    "ObserveConfig",
    "OperatorAuthConfig",
    "OperatorConfig",
    "PreflightConfig",
    "RssFeedSpec",
    "SandboxConfig",
    "ShadowConfig",
    "StatusConfig",
]
