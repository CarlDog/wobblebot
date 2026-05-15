"""Advise CLI — long-running passive advisor (Stage 3.3).

Run as a module::

    python -m wobblebot.cli.advise
    python -m wobblebot.cli.advise --profile aggressive
    python -m wobblebot.cli.advise --symbol ETH/USD

**Advisory only — no money movement.** Per ADR-002 + ADR-007, this
daemon's job is to periodically:

1. Build a ``PerformanceSummary`` from the observe DB (prices) and
   news DB (recent items), using the operator-configured grid
   params for context.
2. Call the configured ``AdvisorPort`` (Stage 3.2 single-LLM
   Ollama; Stage 3.4a MoE later).
3. Persist the recommendation as an ``AdvisorSuggestion`` for
   operator review.

Cadence comes from ``schedules.advise`` in settings.yml. Per-cycle
errors (advisor call failure, storage write failure) are logged
with structured fields and the loop continues. One bad tick can't
kill the daemon.

The advise daemon does NOT touch ``cli/live`` or the engine's
trading path. It runs independently — kill it, and live trading
keeps going on whatever grid params the engine was started with.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, collect_overrides, identity, load_operator_env
from wobblebot.config.advisor import AdvisorConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import load_prompt
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorSuggestion,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError, StorageError
from wobblebot.services.summary_builder import SummaryBuilder

_LOGGER = logging.getLogger("wobblebot.cli.advise")


def _current_grid_from_config(config: WobbleBotConfig, symbol: Symbol) -> CurrentGridParams:
    """Map the engine's grid config for the symbol into the advisor's view."""
    coin_config = config.grid.for_coin(symbol.base)
    return CurrentGridParams(
        spacing_percentage=float(coin_config.spacing_percentage),
        levels_above=coin_config.levels_above,
        levels_below=coin_config.levels_below,
        order_size_usd=float(coin_config.order_size_usd),
    )


def _build_advisor(advisor: AdvisorConfig, model_name_out: list[str]) -> AdvisorPort:
    """Construct the configured AdvisorPort. Stage 3.3 supports type=single+ollama.

    Records the resolved model name into ``model_name_out[0]`` for the
    caller to include in persisted suggestions.
    """
    if advisor.type != "single":
        raise ValueError(
            f"Stage 3.3 only supports advisor.type=single (got {advisor.type!r}). "
            "MoE arrives in Stage 3.4a."
        )
    if advisor.provider != "ollama":
        raise ValueError(
            f"Stage 3.3 only ships the Ollama adapter (got provider={advisor.provider!r}). "
            "Cloud experts arrive in Stage 3.4a."
        )
    assert advisor.model is not None  # validator enforces this for type=single
    assert advisor.prompt_file is not None

    prompt = load_prompt(Path(advisor.prompt_file))
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model_name_out.append(advisor.model)
    return OllamaAdapter(
        model=advisor.model,
        prompt=prompt,
        role="single",
        base_url=base_url,
        temperature=float(advisor.inference_params.temperature),
        max_tokens=advisor.inference_params.max_tokens,
        timeout_seconds=advisor.inference_params.timeout_seconds,
    )


async def _run_cycle(  # pylint: disable=too-many-arguments
    advisor: AdvisorPort,
    summary_builder: SummaryBuilder,
    advise_storage: SQLiteStorageAdapter,
    *,
    symbol: Symbol,
    metrics_lookback: timedelta,
    news_lookback: timedelta | None,
    news_limit: int,
    news_match_coin: bool,
    current_grid: CurrentGridParams,
    model_name: str,
) -> bool:
    """One advise tick: build summary → call advisor → persist suggestion.

    Returns True on success, False on a recoverable failure (advisor or
    storage error). The caller's outer loop swallows the False so the
    daemon keeps going.
    """
    try:
        summary = await summary_builder.build(
            symbol,
            lookback=metrics_lookback,
            news_lookback=news_lookback,
            news_limit=news_limit,
            news_match_coin=news_match_coin,
            current_grid=current_grid,
        )
    except StorageError as exc:
        _LOGGER.error(
            "summary build failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return False

    try:
        recommendation = await advisor.get_recommendation(summary)
    except AdvisorError as exc:
        _LOGGER.error(
            "advisor call failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return False

    suggestion = AdvisorSuggestion(
        recommendation=recommendation,
        created_at=Timestamp(dt=datetime.now(UTC)),
        input_summary=_summary_to_dict(summary),
        model_name=model_name,
    )
    try:
        await advise_storage.save_advisor_suggestion(suggestion)
    except StorageError as exc:
        _LOGGER.error(
            "suggestion persist failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return False

    _LOGGER.info(
        "advise cycle complete",
        extra={
            "symbol": str(symbol),
            "recommendation_id": recommendation.recommendation_id,
            "model_name": model_name,
            "role": recommendation.role,
            "confidence": recommendation.confidence,
            "recommendations": recommendation.recommendations,
            "rationale": recommendation.rationale[:200],
        },
    )
    return True


def _summary_to_dict(summary: PerformanceSummary) -> dict[str, Any]:
    """Serialize the summary for the AdvisorSuggestion audit field.

    Uses ``model_dump(mode="json")`` so timestamps + nested models
    become JSON-native types. Persisted as-is for forensic review.
    """
    return summary.model_dump(mode="json")


async def _run_loop(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    advisor: AdvisorPort,
    summary_builder: SummaryBuilder,
    advise_storage: SQLiteStorageAdapter,
    symbol: Symbol,
    interval: timedelta,
    metrics_lookback: timedelta,
    news_lookback: timedelta | None,
    news_limit: int,
    news_match_coin: bool,
    current_grid: CurrentGridParams,
    model_name: str,
    stop_event: asyncio.Event,
) -> int:
    started_at = time.monotonic()
    cycles_run = 0
    cycles_succeeded = 0
    interval_seconds = interval.total_seconds()
    _LOGGER.info(
        "advise session start",
        extra={
            "symbol": str(symbol),
            "interval_seconds": interval_seconds,
            "metrics_lookback_hours": metrics_lookback.total_seconds() / 3600,
            "news_lookback_hours": (
                news_lookback.total_seconds() / 3600 if news_lookback else None
            ),
            "model_name": model_name,
        },
    )
    try:
        while not stop_event.is_set():
            cycles_run += 1
            ok = await _run_cycle(
                advisor,
                summary_builder,
                advise_storage,
                symbol=symbol,
                metrics_lookback=metrics_lookback,
                news_lookback=news_lookback,
                news_limit=news_limit,
                news_match_coin=news_match_coin,
                current_grid=current_grid,
                model_name=model_name,
            )
            if ok:
                cycles_succeeded += 1
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        _LOGGER.info(
            "advise session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "cycles_run": cycles_run,
                "cycles_succeeded": cycles_succeeded,
            },
        )
    return 0


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


async def _main_async(  # pylint: disable=too-many-locals,too-many-return-statements
    config: WobbleBotConfig,
) -> int:
    if config.advise is None:
        _LOGGER.error("settings.yml is missing the `advise:` section")
        return 2
    if config.advisor is None:
        _LOGGER.error("settings.yml is missing the `advisor:` section")
        return 2

    try:
        interval = config.schedules.get("advise")
    except KeyError as exc:
        _LOGGER.error("missing schedule", extra={"error": str(exc)})
        return 2

    model_name_holder: list[str] = []
    try:
        advisor = _build_advisor(config.advisor, model_name_holder)
    except (ValueError, FileNotFoundError) as exc:
        _LOGGER.error("advisor setup failed", extra={"error": str(exc)})
        return 2
    model_name = model_name_holder[0]

    observe_storage = SQLiteStorageAdapter(config.advise.observe_db)
    news_storage = SQLiteStorageAdapter(config.advise.news_db)
    advise_storage = SQLiteStorageAdapter(config.advise.db)
    await observe_storage.connect()
    await news_storage.connect()
    await advise_storage.connect()

    summary_builder = SummaryBuilder(observe_storage, news_storage=news_storage)
    current_grid = _current_grid_from_config(config, config.advise.symbol)

    metrics_lookback = timedelta(hours=config.advise.metrics_lookback_hours)
    news_lookback: timedelta | None = (
        timedelta(hours=config.advise.news_lookback_hours)
        if config.advise.news_lookback_hours > 0
        else None
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        return await _run_loop(
            advisor=advisor,
            summary_builder=summary_builder,
            advise_storage=advise_storage,
            symbol=config.advise.symbol,
            interval=interval,
            metrics_lookback=metrics_lookback,
            news_lookback=news_lookback,
            news_limit=config.advise.news_limit,
            news_match_coin=config.advise.news_match_coin,
            current_grid=current_grid,
            model_name=model_name,
            stop_event=stop_event,
        )
    finally:
        aclose = getattr(advisor, "aclose", None)
        if aclose is not None:
            await aclose()
        await observe_storage.close()
        await news_storage.close()
        await advise_storage.close()


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    def _parse_symbol_one(value: str) -> Symbol:
        return Symbol.from_string(value)

    return collect_overrides(
        args,
        "advise",
        {
            "symbol": ("symbol", _parse_symbol_one),
            "db": ("db", identity),
            "observe_db": ("observe_db", identity),
            "news_db": ("news_db", identity),
            "metrics_lookback_hours": ("metrics_lookback_hours", identity),
            "news_lookback_hours": ("news_lookback_hours", identity),
            "log_format": ("log_format", identity),
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument("--symbol", default=None, help="Trading pair (BASE/QUOTE).")
    parser.add_argument("--db", default=None)
    parser.add_argument("--observe-db", default=None)
    parser.add_argument("--news-db", default=None)
    parser.add_argument("--metrics-lookback-hours", type=float, default=None)
    parser.add_argument("--news-lookback-hours", type=float, default=None)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = config.advise.log_format if config.advise else "plain"
    configure_logging(log_format=log_format)

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
