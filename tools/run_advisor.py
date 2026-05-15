"""End-to-end Stage 3.2 demo: build a PerformanceSummary and ask the advisor.

Reads observe data and the engine config, constructs the configured
single-LLM advisor (Ollama by default), prints the recommendation,
and persists a JSONL receipt. **No live engine, no money movement.**

Usage::

    python tools/run_advisor.py
    python tools/run_advisor.py --db-path data/wobblebot-observe.db
    python tools/run_advisor.py --symbol ETH/USD --lookback-hours 6
    python tools/run_advisor.py --config /path/to/custom-settings.yml \
        --profile aggressive

Requires a local Ollama server reachable at ``OLLAMA_BASE_URL``
(default ``http://localhost:11434``). If Ollama isn't running, the
adapter wraps the transport failure as ``AdvisorError`` and exits
non-zero with a clean message.

Safe to run against the live observe DB while ``cli/observe`` is
polling — SQLite handles concurrent readers; no write surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import add_config_args, load_operator_env
from wobblebot.config.advisor import AdvisorConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import load_prompt
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.advisor import CurrentGridParams, PerformanceSummary
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.metrics import (
    compute_cycle_stats,
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)

_LOGGER = logging.getLogger("wobblebot.tools.run_advisor")
_DEFAULT_DB = Path("data") / "wobblebot-observe.db"
_DEFAULT_SYMBOL = Symbol(base="BTC", quote="USD")


def _parse_symbol(value: str) -> Symbol:
    if "/" not in value:
        raise argparse.ArgumentTypeError(f"--symbol must look like BASE/QUOTE; got {value!r}")
    base, quote = value.split("/", 1)
    return Symbol(base=base.strip(), quote=quote.strip())


async def _build_summary(
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    lookback: timedelta,
    current_grid: CurrentGridParams,
) -> PerformanceSummary:
    start_time = datetime.now(UTC) - lookback
    snapshots = await storage.get_price_snapshots(symbol=symbol, start_time=start_time)
    trades_desc = await storage.get_trades(symbol=symbol, start_time=start_time, limit=10000)
    trades_asc = list(reversed(trades_desc))
    prices = [s.price.amount for s in snapshots]
    cycle = compute_cycle_stats(trades_asc)

    return PerformanceSummary(
        symbol=str(symbol),
        lookback_hours=lookback.total_seconds() / 3600,
        latest_price=float(snapshots[-1].price.amount) if snapshots else None,
        snapshot_count=len(snapshots),
        volatility=float(compute_volatility(prices)) if prices else 0.0,
        max_drawdown=float(compute_max_drawdown(prices)) if prices else 0.0,
        flatness=float(compute_flatness(prices)) if prices else 1.0,
        cycle_count=cycle.cycle_count,
        win_rate=float(cycle.win_rate),
        total_pnl=float(cycle.total_pnl),
        current_grid=current_grid,
    )


def _current_grid_from_config(advisor_config_owner: Any, symbol: Symbol) -> CurrentGridParams:
    """Pull the per-coin grid params out of the resolved config, if present."""
    grid = getattr(advisor_config_owner, "grid", None)
    if grid is None:
        return CurrentGridParams()
    coin_config = grid.for_coin(symbol.base)
    return CurrentGridParams(
        spacing_percentage=float(coin_config.spacing_percentage),
        levels_above=coin_config.levels_above,
        levels_below=coin_config.levels_below,
        order_size_usd=float(coin_config.order_size_usd),
    )


def _build_adapter(advisor: AdvisorConfig) -> OllamaAdapter:
    """Construct an OllamaAdapter from the resolved AdvisorConfig.

    Stage 3.2 supports type=single backed by an Ollama provider.
    Anything else raises a clean error so the operator sees the
    boundary.
    """
    if advisor.type != "single":
        raise ValueError(
            f"Stage 3.2 only supports advisor.type=single (got {advisor.type!r}). "
            "Override via `--profile` or settings.yml to switch providers."
        )
    if advisor.provider != "ollama":
        raise ValueError(
            f"Stage 3.2 only ships the Ollama adapter (got provider={advisor.provider!r}). "
            "Cloud experts arrive in Stage 3.4a."
        )
    assert advisor.model is not None  # validator enforces this for type=single
    assert advisor.prompt_file is not None

    prompt = load_prompt(Path(advisor.prompt_file))
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    return OllamaAdapter(
        model=advisor.model,
        prompt=prompt,
        role="single",
        base_url=base_url,
        temperature=float(advisor.inference_params.temperature),
        max_tokens=advisor.inference_params.max_tokens,
    )


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        _LOGGER.error("db not found", extra={"db_path": str(db_path)})
        return 2

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides={},
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        _LOGGER.error("config load failed", extra={"error": str(exc)})
        return 2

    if config.advisor is None:
        _LOGGER.error("settings.yml is missing the `advisor:` section")
        return 2

    try:
        adapter = _build_adapter(config.advisor)
    except (ValueError, FileNotFoundError) as exc:
        _LOGGER.error("advisor setup failed", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    current_grid = _current_grid_from_config(config, args.symbol)
    receipt_path = Path("data") / (
        f"run_advisor_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )

    try:
        summary = await _build_summary(
            storage, args.symbol, timedelta(hours=args.lookback_hours), current_grid
        )
        _LOGGER.info(
            "summary built",
            extra={
                "symbol": summary.symbol,
                "snapshot_count": summary.snapshot_count,
                "latest_price": summary.latest_price,
                "volatility": summary.volatility,
                "max_drawdown": summary.max_drawdown,
                "flatness": summary.flatness,
            },
        )
        try:
            recommendation = await adapter.get_recommendation(summary)
        except AdvisorError as exc:
            _LOGGER.error(
                "advisor call failed",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return 1

        _LOGGER.info(
            "recommendation received",
            extra={
                "recommendation_id": recommendation.recommendation_id,
                "role": recommendation.role,
                "confidence": recommendation.confidence,
                "recommendations": recommendation.recommendations,
                "rationale": recommendation.rationale[:200],
            },
        )
        # Persist forensic JSONL: one record for the request, one for the response.
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with receipt_path.open("w", encoding="utf-8") as f:
            f.write(
                json.dumps({"type": "request_summary", "data": summary.model_dump(mode="json")})
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "recommendation",
                        "data": recommendation.model_dump(mode="json"),
                    },
                    default=str,
                )
                + "\n"
            )
        _LOGGER.info("receipt written", extra={"path": str(receipt_path)})
    finally:
        await adapter.aclose()
        await storage.close()
    return 0


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"SQLite DB to read snapshots/trades from (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--symbol",
        type=_parse_symbol,
        default=_DEFAULT_SYMBOL,
        help="Trading pair (BASE/QUOTE). Default: BTC/USD.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=24.0,
        help="Window for metrics, in hours (default: 24).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
    )
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
