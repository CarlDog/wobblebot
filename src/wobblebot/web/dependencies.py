"""FastAPI dependency factories for the Phase 7 web UI.

Threads ports + config into route handlers via FastAPI's DI system.
Per ADR-016 decision 2, routes consume ports — they never reach
into ``adapters/`` or compute business logic themselves.

Stage 7.1.B ships the factory functions; Stage 7.1.C adds the
``current_user`` dependency that gates auth-protected routes.

``app.state`` is Starlette's untyped attribute bag, so every read here
crosses an ``Any`` boundary. Each accessor bridges it with a typed
assignment — the annotation *is* the contract with the app-build wiring
in ``web/app.py`` — rather than a per-line ``type: ignore``, which this
file once held eleven of (nearly half the repo's total).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from wobblebot.config.cli import ReanchorSeverity, WebConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.storage import StoragePort


def get_config(request: Request) -> WebConfig:
    """Pull the ``WebConfig`` instance off ``app.state``."""
    config: WebConfig = request.app.state.config
    return config


def get_operator_storage(request: Request) -> StoragePort:
    """Pull the operator.db ``StoragePort`` — required everywhere
    (users, pending_commands, notifications, llm_calls all live
    here)."""
    storage: StoragePort = request.app.state.operator_storage
    return storage


def get_advise_storage(request: Request) -> StoragePort | None:
    """Pull the advise.db storage if wired; ``None`` otherwise.
    Routes that need it implement the graceful-degrade card pattern."""
    storage: StoragePort | None = request.app.state.advise_storage
    return storage


def get_harvest_storage(request: Request) -> StoragePort | None:
    """Pull the harvest.db storage if wired; ``None`` otherwise."""
    storage: StoragePort | None = request.app.state.harvest_storage
    return storage


def get_observe_storage(request: Request) -> StoragePort | None:
    """Pull the observe.db storage if wired; ``None`` otherwise."""
    storage: StoragePort | None = request.app.state.observe_storage
    return storage


def get_news_storage(request: Request) -> StoragePort | None:
    """Pull the news.db storage if wired; ``None`` otherwise."""
    storage: StoragePort | None = request.app.state.news_storage
    return storage


def get_live_storage(request: Request) -> StoragePort | None:
    """Pull the live.db storage if wired; ``None`` otherwise."""
    storage: StoragePort | None = request.app.state.live_storage
    return storage


def get_cool_down_minutes(request: Request) -> float | None:
    """Pull ``LiveConfig.cool_down_minutes`` (ADR-024) off ``app.state``.

    Mirrors ``trading_mode``: a fact that lives on ``LiveConfig``, not
    ``WebConfig``, threaded through at app-build time so the session
    card can tell whether the cool-down gate is currently active.
    ``None`` when the operator disabled the gate or didn't give
    ``cli/web`` a ``live:`` config section.
    """
    minutes: float | None = request.app.state.cool_down_minutes
    return minutes


def get_live_tick_seconds(request: Request) -> float | None:
    """Pull ``LiveConfig.tick_seconds`` off ``app.state`` (ADR-030).

    Feeds the engine_state freshness guard (~3 ticks): the dashboard
    must know the writer's cadence to judge whether a row is current.
    ``None`` when ``cli/web`` wasn't given a ``live:`` section — the
    guard falls back to the schema-default tick.
    """
    seconds: float | None = request.app.state.live_tick_seconds
    return seconds


def get_reanchor_min_severity(request: Request) -> ReanchorSeverity:
    """Pull ``WebConfig.reanchor_min_severity`` off ``app.state``.

    The operator's attention floor for re-anchor banners. Unlike
    ``cool_down_minutes`` this one genuinely lives on ``WebConfig`` — it
    is a presentation choice about the dashboard, not a fact about the
    engine — so there is no ``None`` case: the schema default (``"mild"``,
    i.e. show everything) always applies.
    """
    severity: ReanchorSeverity = request.app.state.reanchor_min_severity
    return severity


def get_withdrawal_destinations(request: Request) -> dict[str, str]:
    """Pull the asset→destination-label map (ADR-034) off ``app.state``.

    Empty dict when no ``harvester:`` section is wired — the Harvester
    page then renders proposals without an Execute button, since there
    is no destination to approve.
    """
    destinations: dict[str, str] = request.app.state.withdrawal_destinations
    return destinations


def get_live_symbols(request: Request) -> frozenset[Symbol]:
    """The engine's configured trading set (``config.live.symbols``).

    Empty means UNKNOWN (``cli/web`` started without a ``live:`` section),
    not "nothing is traded" — callers fail OPEN on empty so an unwired
    deployment keeps working exactly as before this guard existed. See
    ``commands.reanchor_submit`` for the one consumer that enforces it.
    """
    symbols: frozenset[Symbol] = request.app.state.live_symbols
    return symbols


def get_templates(request: Request) -> Jinja2Templates:
    """Pull the shared ``Jinja2Templates`` instance off ``app.state``.
    Routes use this to render HTML responses."""
    templates: Jinja2Templates = request.app.state.templates
    return templates
