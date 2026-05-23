"""Advise CLI — long-running passive advisor (Stage 3.3; multi-symbol since 3.6b).

Run as a module::

    python -m wobblebot.cli.advise
    python -m wobblebot.cli.advise --profile aggressive
    python -m wobblebot.cli.advise --symbols BTC/USD,ETH/USD,DOGE/USD

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter
from wobblebot.adapters.google import GoogleAdvisorAdapter
from wobblebot.adapters.moe_advisor import MoEAdvisorAdapter, MoEExpertEntry
from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.openai import OpenAIAdvisorAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    identity,
    load_operator_env,
    parse_symbol_csv,
    run_poll_loop,
    safe_shutdown,
)
from wobblebot.config.advisor import (
    AdvisorConfig,
    ArbitratorConfig,
    ExpertConfig,
    InferenceParams,
)
from wobblebot.config.llm import LLMConfig
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
from wobblebot.ports.storage import StoragePort
from wobblebot.services.llm_cost_gate import SessionCostTracker
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


_UNIMPLEMENTED_PROVIDERS: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CloudWiring:
    """Cost-tracking deps every cloud-provider adapter needs.

    Bundled into a small frozen dataclass so the per-expert and
    arbitrator builders can pass it through without growing four
    new positional args. Built once in ``_run`` and shared across
    every advisor adapter the CLI constructs.
    """

    storage: StoragePort
    session_tracker: SessionCostTracker
    llm_config: LLMConfig


def _build_advisor_adapter(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    *,
    provider: str,
    model: str,
    prompt_file: str,
    inference_params: InferenceParams,
    role: str,
    cloud_wiring: _CloudWiring | None,
) -> AdvisorPort:
    """Construct one ``AdvisorPort`` from the (provider, model, prompt_file)
    triple shared by single + per-expert + arbitrator config blocks.

    Dispatches by provider:
    - ``ollama`` → ``OllamaAdapter`` (local, free; bypasses the cost gate).
    - ``anthropic`` → ``AnthropicAdvisorAdapter`` (Phase 6 Stage 6.2;
      requires ``cloud_wiring`` non-None).
    - ``openai`` / ``google`` → not yet implemented; raise.
    """
    prompt = load_prompt(Path(prompt_file))
    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaAdapter(
            model=model,
            prompt=prompt,
            role=role,
            base_url=base_url,
            temperature=float(inference_params.temperature),
            max_tokens=inference_params.max_tokens,
            timeout_seconds=inference_params.timeout_seconds,
        )
    if provider == "anthropic":
        if cloud_wiring is None:
            raise ValueError(
                "Anthropic provider configured but settings.yml has no `llm:` "
                "block. Phase 6 / ADR-014 requires cost-cap config for cloud "
                "providers; add an `llm:` block and ensure operator.operator_db "
                "is set so the cost ledger can be written."
            )
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY missing from environment; required when "
                "advisor.provider=='anthropic'."
            )
        return AnthropicAdvisorAdapter(
            model=model,
            prompt=prompt,
            role=role,  # type: ignore[arg-type]
            api_key=api_key,
            storage=cloud_wiring.storage,
            session_tracker=cloud_wiring.session_tracker,
            cost_config=cloud_wiring.llm_config.cost,
            retry_config=cloud_wiring.llm_config.retry,
            temperature=float(inference_params.temperature),
            max_tokens=inference_params.max_tokens,
            timeout_seconds=inference_params.timeout_seconds,
        )
    if provider == "openai":
        if cloud_wiring is None:
            raise ValueError(
                "OpenAI provider configured but settings.yml has no `llm:` "
                "block. Phase 6 / ADR-014 requires cost-cap config for cloud "
                "providers; add an `llm:` block and ensure operator.operator_db "
                "is set so the cost ledger can be written."
            )
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY missing from environment; required when "
                "advisor.provider=='openai'."
            )
        organization = os.environ.get("OPENAI_ORGANIZATION") or None
        return OpenAIAdvisorAdapter(
            model=model,
            prompt=prompt,
            role=role,  # type: ignore[arg-type]
            api_key=api_key,
            organization=organization,
            storage=cloud_wiring.storage,
            session_tracker=cloud_wiring.session_tracker,
            cost_config=cloud_wiring.llm_config.cost,
            retry_config=cloud_wiring.llm_config.retry,
            temperature=float(inference_params.temperature),
            max_tokens=inference_params.max_tokens,
            timeout_seconds=inference_params.timeout_seconds,
        )
    if provider == "google":
        if cloud_wiring is None:
            raise ValueError(
                "Google provider configured but settings.yml has no `llm:` "
                "block. Phase 6 / ADR-014 requires cost-cap config for cloud "
                "providers; add an `llm:` block and ensure operator.operator_db "
                "is set so the cost ledger can be written."
            )
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY missing from environment; required when "
                "advisor.provider=='google'."
            )
        return GoogleAdvisorAdapter(
            model=model,
            prompt=prompt,
            role=role,  # type: ignore[arg-type]
            api_key=api_key,
            storage=cloud_wiring.storage,
            session_tracker=cloud_wiring.session_tracker,
            cost_config=cloud_wiring.llm_config.cost,
            retry_config=cloud_wiring.llm_config.retry,
            temperature=float(inference_params.temperature),
            max_tokens=inference_params.max_tokens,
            timeout_seconds=inference_params.timeout_seconds,
        )
    if provider in _UNIMPLEMENTED_PROVIDERS:
        raise ValueError(f"provider={provider!r} not implemented yet")
    raise ValueError(f"unknown provider {provider!r}")


def _build_expert_entry(expert: ExpertConfig, cloud_wiring: _CloudWiring | None) -> MoEExpertEntry:
    adapter = _build_advisor_adapter(
        provider=expert.provider,
        model=expert.model,
        prompt_file=expert.prompt_file,
        inference_params=expert.inference_params,
        role=expert.role,
        cloud_wiring=cloud_wiring,
    )
    return MoEExpertEntry(name=expert.name, role=expert.role, advisor=adapter)


def _build_arbitrator_entry(
    arbitrator: ArbitratorConfig, cloud_wiring: _CloudWiring | None
) -> MoEExpertEntry:
    adapter = _build_advisor_adapter(
        provider=arbitrator.provider,
        model=arbitrator.model,
        prompt_file=arbitrator.prompt_file,
        inference_params=arbitrator.inference_params,
        role="arbitrator",
        cloud_wiring=cloud_wiring,
    )
    # Arbitrator's name slot in the audit log. Operator config doesn't
    # supply one (ArbitratorConfig has no `name` field) — the role is
    # already unique so use it directly. If we ever add multiple
    # arbitrators (we won't), revisit.
    return MoEExpertEntry(name="arbitrator", role="arbitrator", advisor=adapter)


def _build_advisor(
    advisor: AdvisorConfig,
    model_name_out: list[str],
    *,
    cloud_wiring: _CloudWiring | None = None,
) -> AdvisorPort:
    """Construct the configured AdvisorPort.

    Dispatches on ``advisor.type``:
    - ``single``: one provider adapter (Stage 3.2 single-LLM path,
      Stage 6.2+ for cloud providers).
    - ``moe``: ``MoEAdvisorAdapter`` wrapping one adapter per
      configured expert; arbitrator wired iff ``aggregator="arbitrator"``.

    ``cloud_wiring`` is required when any expert/arbitrator/single
    provider is non-Ollama (``anthropic`` etc.); ``_build_advisor_adapter``
    raises ``ValueError`` if a cloud provider is configured without it.

    The resolved model name is pushed into ``model_name_out[0]`` so the
    caller can persist it in ``AdvisorSuggestion.model_name``. For MoE
    the name is a compact summary of the lineup (the per-expert audit
    trail lives in ``AdvisorRecommendation.expert_opinions``).
    """
    if advisor.type == "single":
        assert advisor.provider is not None  # validator enforces for type=single
        assert advisor.model is not None
        assert advisor.prompt_file is not None
        adapter = _build_advisor_adapter(
            provider=advisor.provider,
            model=advisor.model,
            prompt_file=advisor.prompt_file,
            inference_params=advisor.inference_params,
            role="single",
            cloud_wiring=cloud_wiring,
        )
        model_name_out.append(advisor.model)
        return adapter

    # type=moe
    expert_entries = [_build_expert_entry(e, cloud_wiring) for e in advisor.experts]
    arbitrator_entry: MoEExpertEntry | None = None
    if advisor.aggregator == "arbitrator":
        assert advisor.arbitrator is not None  # validator enforces for arbitrator
        arbitrator_entry = _build_arbitrator_entry(advisor.arbitrator, cloud_wiring)
    moe = MoEAdvisorAdapter(
        experts=expert_entries,
        aggregator=advisor.aggregator,
        arbitrator=arbitrator_entry,
    )
    model_name_out.append(_moe_model_label(advisor))
    return moe


def _moe_model_label(advisor: AdvisorConfig) -> str:
    """Compact, operator-facing summary of a MoE lineup.

    Persisted as ``AdvisorSuggestion.model_name`` so a SQL-ish glance
    at the suggestions table tells the operator which model committee
    produced each aggregated recommendation. Per-expert opinions in
    ``expert_opinions`` carry the role but not the model name — that's
    a known limitation; the operator's config is the source of truth
    for role→model mapping at any given point in time."""
    parts = "/".join(f"{e.role}:{e.model}" for e in advisor.experts)
    suffix = ""
    if advisor.aggregator == "arbitrator" and advisor.arbitrator is not None:
        suffix = f"|arb={advisor.arbitrator.model}"
    return f"moe[{advisor.aggregator}:{parts}{suffix}]"


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
    symbols: list[Symbol],
    interval: timedelta,
    metrics_lookback: timedelta,
    news_lookback: timedelta | None,
    news_limit: int,
    news_match_coin: bool,
    current_grids: dict[str, CurrentGridParams],
    model_name: str,
    stop_event: asyncio.Event,
) -> int:
    started_at = time.monotonic()
    cycles_run = 0  # one cycle = one symbol's pass
    cycles_succeeded = 0
    sweeps_run = 0  # one sweep = one tick across every symbol
    interval_seconds = interval.total_seconds()
    _LOGGER.info(
        "advise session start",
        extra={
            "symbols": [str(s) for s in symbols],
            "interval_seconds": interval_seconds,
            "metrics_lookback_hours": metrics_lookback.total_seconds() / 3600,
            "news_lookback_hours": (
                news_lookback.total_seconds() / 3600 if news_lookback else None
            ),
            "model_name": model_name,
        },
    )

    async def _one_cycle() -> None:
        nonlocal sweeps_run, cycles_run, cycles_succeeded
        sweeps_run += 1
        for symbol in symbols:
            if stop_event.is_set():
                break
            cycles_run += 1
            # Per-symbol cycle errors are surfaced inside _run_cycle
            # (logged with structured fields, returns False). One
            # bad coin can't kill the sweep — Stage 2.4 discipline.
            ok = await _run_cycle(
                advisor,
                summary_builder,
                advise_storage,
                symbol=symbol,
                metrics_lookback=metrics_lookback,
                news_lookback=news_lookback,
                news_limit=news_limit,
                news_match_coin=news_match_coin,
                current_grid=current_grids[symbol.base],
                model_name=model_name,
            )
            if ok:
                cycles_succeeded += 1

    try:
        await run_poll_loop(
            _one_cycle,
            interval_seconds=interval_seconds,
            stop_event=stop_event,
        )
    finally:
        _LOGGER.info(
            "advise session end",
            extra={
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "sweeps_run": sweeps_run,
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

    # Open the cost-ledger storage (operator.db) first if config.llm is
    # set — cloud-provider adapters need it threaded through their
    # constructors. Pure-Ollama deployments skip this entirely.
    operator_storage: SQLiteStorageAdapter | None = None
    cloud_wiring: _CloudWiring | None = None
    if config.llm is not None:
        if config.operator is None:
            _LOGGER.error(
                "settings.yml has an `llm:` block but no `operator:` block; "
                "the operator.operator_db path is required to host the "
                "cost ledger (ADR-014 decision 5)."
            )
            return 2
        operator_storage = SQLiteStorageAdapter(config.operator.operator_db)
        await operator_storage.connect()
        cloud_wiring = _CloudWiring(
            storage=operator_storage,
            session_tracker=SessionCostTracker(),
            llm_config=config.llm,
        )

    model_name_holder: list[str] = []
    try:
        advisor = _build_advisor(config.advisor, model_name_holder, cloud_wiring=cloud_wiring)
    except (ValueError, FileNotFoundError) as exc:
        _LOGGER.error("advisor setup failed", extra={"error": str(exc)})
        if operator_storage is not None:
            await operator_storage.close()
        return 2
    model_name = model_name_holder[0]

    observe_storage = SQLiteStorageAdapter(config.advise.observe_db)
    news_storage = SQLiteStorageAdapter(config.advise.news_db)
    advise_storage = SQLiteStorageAdapter(config.advise.db)
    await observe_storage.connect()
    await news_storage.connect()
    await advise_storage.connect()

    summary_builder = SummaryBuilder(observe_storage, news_storage=news_storage)
    # Build a current-grid snapshot per symbol once at startup. The
    # engine's running grid lives in settings.yml; cli/apply rewrites
    # that file out-of-band, but cli/advise doesn't pick up mid-run
    # config changes (per ADR-012 the operator restarts the affected
    # daemon).
    current_grids = {
        symbol.base: _current_grid_from_config(config, symbol) for symbol in config.advise.symbols
    }

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
            symbols=config.advise.symbols,
            interval=interval,
            metrics_lookback=metrics_lookback,
            news_lookback=news_lookback,
            news_limit=config.advise.news_limit,
            news_match_coin=config.advise.news_match_coin,
            current_grids=current_grids,
            model_name=model_name,
            stop_event=stop_event,
        )
    finally:

        async def _close_advisor() -> None:
            aclose = getattr(advisor, "aclose", None)
            if aclose is not None:
                await aclose()

        phases: list[tuple[str, Any]] = [
            ("close_advisor", _close_advisor),
            ("close_observe_storage", observe_storage.close),
            ("close_news_storage", news_storage.close),
            ("close_advise_storage", advise_storage.close),
        ]
        if operator_storage is not None:
            phases.append(("close_operator_storage", operator_storage.close))
        await safe_shutdown(phases, logger=_LOGGER)


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "advise",
        {
            "symbols": ("symbols", parse_symbol_csv),
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
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated trading pairs (e.g. BTC/USD,ETH/USD). Per-symbol "
        "cycles run serial within each sweep; each LLM call sees one coin's context.",
    )
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
