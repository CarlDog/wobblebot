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


class NewsDedupConfig(BaseModel):
    """Stage 8.4 follow-up: fuzzy headline dedup for cli/news.

    Two-layer dedup: storage's ``UNIQUE(source, external_id)`` catches
    same-source reposts; this config drives the fuzzy layer that catches
    cross-source syndication ("CoinDesk and Decrypt both republished
    Reuters' wire story about Bitcoin breaking $80k").

    See ``services/news_dedup.py`` for the algorithm. Operator knob:
    set ``fuzzy_threshold=0`` to disable fuzzy dedup entirely (keep
    storage-level exact dedup only).
    """

    # Hours of recent items to compare each new candidate against.
    # Default 6h aligns with typical news-cycle decay — stories
    # older than ~6h are unlikely to be republished verbatim.
    window_hours: float = Field(default=6.0, gt=0.0, le=72.0)

    # Minimum token-set ratio (0-100) to classify a candidate as
    # duplicate of an existing item. Default 60 measured against
    # real-world syndicated wire stories (Reuters → CoinDesk +
    # Decrypt rewording typically scores 60-66 on token_set_ratio).
    # The mentioned-coins overlap guard in services/news_dedup
    # prevents most false positives below 60 anyway. Raise toward
    # 70 for stricter dedup (more syndicated copies pass through);
    # lower toward 55 for aggressive dedup. Set to 0 to disable.
    fuzzy_threshold: float = Field(default=60.0, ge=0.0, le=100.0)

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
    dedup: NewsDedupConfig = Field(default_factory=NewsDedupConfig)
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

    provider: Literal["ollama", "anthropic", "openai", "google"] = "ollama"
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

    # TTL expirer poll cadence. Scans pending_commands WHERE
    # status='awaiting_confirmation' AND ttl_expires_at < now and
    # transitions matches to 'expired'. Doesn't need to be fast —
    # the operator's expectation is "wait a minute, maybe two".
    ttl_expirer_poll_seconds: float = Field(default=30.0, gt=0)

    log_format: LogFormat = "plain"

    class Config:
        frozen = True


class WebConfig(BaseModel):
    """Phase 7 web UI configuration (ADR-016 + ADR-017).

    Composed onto ``WobbleBotConfig.web: WebConfig | None`` (None =
    no web layer). Knobs split into four groups: serving (bind /
    port), auth (session secret + lifetime + rate-limit),
    presentation (htmx poll cadence), and the cross-DB paths the
    dashboard needs.

    Per ADR-016 decision 7, ``bind_host`` defaults to ``127.0.0.1`` —
    the operator's reverse proxy is responsible for LAN exposure.
    Per ADR-017 decision 3, ``session_secret_env_var`` points at an
    env var holding 32+ random bytes (use
    ``python -c "import secrets; print(secrets.token_urlsafe(32))"``
    to mint one); ``cli/web`` refuses to start if the env var is
    unset.
    """

    # ---- serving ---------------------------------------------------- #

    bind_host: str = Field(default="127.0.0.1", min_length=1)
    bind_port: int = Field(default=8000, ge=1, le=65535)

    # ---- session / auth (ADR-017) ----------------------------------- #

    # Env var holding the cookie-signing key. Not the key itself.
    session_secret_env_var: str = Field(default="WOBBLEBOT_WEB_SESSION_SECRET", min_length=1)
    # Sliding session lifetime. Cookie expires after this many days of
    # inactivity.
    session_max_age_days: int = Field(default=7, ge=1, le=90)
    # Per-IP login attempts allowed in `rate_limit_window_seconds`
    # before further attempts return 429.
    rate_limit_attempts: int = Field(default=5, ge=1, le=100)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    # Bcrypt cost factor for new password hashes. 12 is the ADR-017
    # default. Bump to 13 / 14 if the operator's hardware warrants.
    bcrypt_cost: int = Field(default=12, ge=10, le=15)

    # ---- presentation ---------------------------------------------- #

    # How often HTMX-polled cards refresh (e.g. cost ledger, open
    # orders). Static-ish pages (news headlines, audit logs) ignore
    # this — they're full-reload-on-navigation.
    htmx_poll_seconds: float = Field(default=15.0, gt=0.0, le=300.0)

    # Optional external Kraken account URL surfaced as a header link
    # for one-click access to the operator's Kraken Pro account.
    # Default https://pro.kraken.com/app/trade lands on Kraken Pro's
    # trade page; operators in non-US regions may override.
    # Set to null to suppress the link entirely.
    kraken_account_url: str | None = Field(default="https://pro.kraken.com/app/trade", min_length=1)

    # ---- cross-DB paths -------------------------------------------- #

    # The dashboard reads from up to five DBs. operator.db is required
    # (users table + pending_commands + notifications + llm_calls);
    # the other four are optional per the OperatorService graceful-
    # degrade pattern (Stage 5.6.C). When unset, cards that would
    # query the missing DB simply don't render.
    operator_db: str = Field(default="data/wobblebot-operator.db", min_length=1)
    live_db: str | None = None
    advise_db: str | None = None
    harvest_db: str | None = None
    observe_db: str | None = None
    news_db: str | None = None

    log_format: LogFormat = "plain"

    class Config:
        frozen = True


# ---------------------------------------------------------------------------
# Maintenance worker (Phase 8 — Stage 8.2)
# ---------------------------------------------------------------------------


class MaintenanceConfig(BaseModel):
    """Phase 8.2 — operator-tunable knobs for ``cli/maintenance``.

    Three concurrent scheduled tasks (vacuum / prune+archive /
    backup) each pull their cadence from ``schedules:`` (keys
    ``maintenance_vacuum`` / ``maintenance_prune`` /
    ``maintenance_backup``); this block holds the per-task
    parameters those cadences operate against.

    Per ``stage-8.2-design.md`` decision 7 the maintenance daemon
    is operator-started — not auto-spawned by any other daemon.

    Per decisions 4 + 5 only local backups ship in v1.0 with a
    ``keep_n_daily`` retention.

    Per decision 3 only ``price_snapshots`` gets pruned in v1.0;
    every audit table (``orders``, ``trades``, ``llm_calls``, etc.)
    stays forever.
    """

    # ---- DBs to maintain ---- #

    # List of (db_path, "stem") pairs. The CLI iterates these for
    # each scheduled task. Default empty list = no DBs configured;
    # daemon refuses to start.
    target_dbs: list[str] = Field(default_factory=list)

    # ---- Prune ---- #

    # Snapshots older than this many days get archived + deleted
    # from price_snapshots. Default 30 days matches the typical
    # advisor-metrics rolling window.
    prune_price_snapshots_older_than_days: int = Field(default=30, gt=0, le=3650)

    # Source DB for price snapshots. Typically the same as
    # observe.db. The maintenance daemon won't infer this from
    # observe; operator passes explicitly so multi-instance
    # deployments stay clear.
    prune_source_db: str | None = None

    # Destination directory for archive CSVs.
    archive_dir: str = "data/archive"

    # ---- Backup ---- #

    # Destination directory for SQLite .backup output.
    backup_dir: str = "data/backups"

    # How many newest daily backups to keep per source DB. Older
    # backups are deleted after each backup write.
    keep_n_daily_backups: int = Field(default=7, ge=0, le=365)

    # ---- Logging ---- #

    log_format: LogFormat = "plain"

    # Optional rotating-file log destination. When set, configure_logging
    # adds a TimedRotatingFileHandler alongside stderr. Default None
    # keeps stdout-only behavior.
    log_file_path: str | None = None

    # ---- Operator interaction ---- #

    # Stage 8.4.E follow-up — when set, cli/maintenance opens
    # operator.db and writes a daemon_heartbeat row at the top of
    # each task iteration so the web UI's /health page can detect
    # liveness. Default None → no heartbeat emission; the health
    # page shows cli/maintenance as UNKNOWN.
    operator_db: str | None = None

    class Config:
        frozen = True


__all__ = [
    "AdviseConfig",
    "AssistantLLMConfig",
    "CryptoCompareSpec",
    "HarvestConfig",
    "LiveConfig",
    "LogFormat",
    "MaintenanceConfig",
    "NewsConfig",
    "ObserveConfig",
    "OperatorAuthConfig",
    "OperatorConfig",
    "PreflightConfig",
    "RssFeedSpec",
    "SandboxConfig",
    "ShadowConfig",
    "StatusConfig",
    "WebConfig",
]
