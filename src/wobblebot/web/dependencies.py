"""FastAPI dependency factories for the Phase 7 web UI.

Threads ports + config into route handlers via FastAPI's DI system.
Per ADR-016 decision 2, routes consume ports — they never reach
into ``adapters/`` or compute business logic themselves.

Stage 7.1.B ships the factory functions; Stage 7.1.C adds the
``current_user`` dependency that gates auth-protected routes.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from wobblebot.config.cli import WebConfig
from wobblebot.ports.storage import StoragePort


def get_config(request: Request) -> WebConfig:
    """Pull the ``WebConfig`` instance off ``app.state``."""
    return request.app.state.config  # type: ignore[no-any-return]


def get_operator_storage(request: Request) -> StoragePort:
    """Pull the operator.db ``StoragePort`` — required everywhere
    (users, pending_commands, notifications, llm_calls all live
    here)."""
    return request.app.state.operator_storage  # type: ignore[no-any-return]


def get_advise_storage(request: Request) -> StoragePort | None:
    """Pull the advise.db storage if wired; ``None`` otherwise.
    Routes that need it implement the graceful-degrade card pattern."""
    return request.app.state.advise_storage  # type: ignore[no-any-return]


def get_harvest_storage(request: Request) -> StoragePort | None:
    """Pull the harvest.db storage if wired; ``None`` otherwise."""
    return request.app.state.harvest_storage  # type: ignore[no-any-return]


def get_observe_storage(request: Request) -> StoragePort | None:
    """Pull the observe.db storage if wired; ``None`` otherwise."""
    return request.app.state.observe_storage  # type: ignore[no-any-return]


def get_news_storage(request: Request) -> StoragePort | None:
    """Pull the news.db storage if wired; ``None`` otherwise."""
    return request.app.state.news_storage  # type: ignore[no-any-return]


def get_live_storage(request: Request) -> StoragePort | None:
    """Pull the live.db storage if wired; ``None`` otherwise."""
    return request.app.state.live_storage  # type: ignore[no-any-return]


def get_cool_down_minutes(request: Request) -> float | None:
    """Pull ``LiveConfig.cool_down_minutes`` (ADR-024) off ``app.state``.

    Mirrors ``trading_mode``: a fact that lives on ``LiveConfig``, not
    ``WebConfig``, threaded through at app-build time so the session
    card can tell whether the cool-down gate is currently active.
    ``None`` when the operator disabled the gate or didn't give
    ``cli/web`` a ``live:`` config section.
    """
    return request.app.state.cool_down_minutes  # type: ignore[no-any-return]


def get_live_tick_seconds(request: Request) -> float | None:
    """Pull ``LiveConfig.tick_seconds`` off ``app.state`` (ADR-030).

    Feeds the engine_state freshness guard (~3 ticks): the dashboard
    must know the writer's cadence to judge whether a row is current.
    ``None`` when ``cli/web`` wasn't given a ``live:`` section — the
    guard falls back to the schema-default tick.
    """
    return request.app.state.live_tick_seconds  # type: ignore[no-any-return]


def get_templates(request: Request) -> Jinja2Templates:
    """Pull the shared ``Jinja2Templates`` instance off ``app.state``.
    Routes use this to render HTML responses."""
    return request.app.state.templates  # type: ignore[no-any-return]
