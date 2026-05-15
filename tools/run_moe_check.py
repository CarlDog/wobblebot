"""Stage 3.4a MoE end-to-end live verification.

Builds a MoEAdvisorAdapter with three real Ollama experts + an
arbitrator, runs ONE cycle against the operator's observe DB, prints
the aggregated recommendation plus each expert's raw opinion, and
optionally persists the suggestion. **No engine, no money movement —
this exercises the advisor path only.**

Usage::

    python tools/run_moe_check.py
    python tools/run_moe_check.py --aggregator voting
    python tools/run_moe_check.py --aggregator weighted_confidence
    python tools/run_moe_check.py --symbol ETH/USD --lookback-hours 6

Model tags below match the operator's local Ollama lineup at Stage
3.4a close (2026-05-15). Edit the constants if your ``ollama list``
shows different tags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wobblebot.adapters.moe_advisor import MoEAdvisorAdapter, MoEExpertEntry
from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.advisor import CurrentGridParams
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.summary_builder import SummaryBuilder

_LOGGER = logging.getLogger("wobblebot.tools.run_moe_check")

_QUANT_MODEL = "phi4:14b-q8_0"
_RISK_MODEL = "granite4.1:30b-q5_K_M"
_NEWS_MODEL = "deepseek-r1:14b-qwen-distill-q8_0"
_ARBITRATOR_MODEL = "phi4:14b-q8_0"


def _build_expert(
    *, model: str, role: str, prompt_file: str, max_tokens: int = 512
) -> OllamaAdapter:
    prompt = load_prompt(Path(prompt_file))
    return OllamaAdapter(
        model=model,
        prompt=prompt,
        role=role,
        temperature=0.5,
        max_tokens=max_tokens,
        timeout_seconds=240.0,
    )


def _build_moe(aggregator: str) -> MoEAdvisorAdapter:
    experts = [
        MoEExpertEntry(
            name="quant",
            role="quant",
            advisor=_build_expert(
                model=_QUANT_MODEL,
                role="quant",
                prompt_file="config/prompts/quant.md",
            ),
        ),
        MoEExpertEntry(
            name="risk",
            role="risk",
            advisor=_build_expert(
                model=_RISK_MODEL,
                role="risk",
                prompt_file="config/prompts/risk.md",
            ),
        ),
        MoEExpertEntry(
            name="news",
            role="news",
            advisor=_build_expert(
                model=_NEWS_MODEL,
                role="news",
                prompt_file="config/prompts/news.md",
                max_tokens=2048,  # R1 needs room for <think> + JSON
            ),
        ),
    ]
    arbitrator_entry: MoEExpertEntry | None = None
    if aggregator == "arbitrator":
        arbitrator_entry = MoEExpertEntry(
            name="arbitrator",
            role="arbitrator",
            advisor=_build_expert(
                model=_ARBITRATOR_MODEL,
                role="arbitrator",
                prompt_file="config/prompts/arbitrator.md",
                max_tokens=1024,
            ),
        )
    return MoEAdvisorAdapter(
        experts=experts,
        aggregator=aggregator,  # type: ignore[arg-type]
        arbitrator=arbitrator_entry,
    )


async def _run(args: argparse.Namespace) -> int:
    symbol = Symbol.from_string(args.symbol)
    observe_storage = SQLiteStorageAdapter(args.observe_db)
    news_storage = SQLiteStorageAdapter(args.news_db)
    await observe_storage.connect()
    await news_storage.connect()
    try:
        builder = SummaryBuilder(observe_storage, news_storage=news_storage)
        summary = await builder.build(
            symbol,
            lookback=timedelta(hours=args.lookback_hours),
            news_lookback=timedelta(hours=args.news_lookback_hours),
            news_limit=args.news_limit,
            news_match_coin=True,
            current_grid=CurrentGridParams(
                spacing_percentage=1.0,
                levels_above=3,
                levels_below=3,
                order_size_usd=10.0,
            ),
        )
    finally:
        await observe_storage.close()
        await news_storage.close()

    _LOGGER.info(
        "summary built",
        extra={
            "symbol": str(symbol),
            "snapshot_count": summary.snapshot_count,
            "latest_price": summary.latest_price,
            "volatility": summary.volatility,
            "flatness": summary.flatness,
            "recent_news_count": len(summary.recent_news),
        },
    )

    moe = _build_moe(args.aggregator)
    started = datetime.now(UTC)
    _LOGGER.info(
        "MoE dispatch starting",
        extra={"aggregator": args.aggregator},
    )
    try:
        result = await moe.get_recommendation(summary)
    except AdvisorError as exc:
        _LOGGER.error("MoE dispatch failed", extra={"error": str(exc)})
        return 1
    finally:
        await moe.aclose()

    elapsed = (datetime.now(UTC) - started).total_seconds()
    _LOGGER.info(
        "MoE dispatch complete",
        extra={
            "elapsed_seconds": round(elapsed, 1),
            "aggregator": args.aggregator,
            "role": result.role,
            "confidence": result.confidence,
            "recommendations": result.recommendations,
            "experts_contributed": len(result.expert_opinions),
        },
    )

    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "aggregator": args.aggregator,
        "elapsed_seconds": round(elapsed, 1),
        "aggregated": {
            "role": result.role,
            "confidence": result.confidence,
            "recommendations": result.recommendations,
            "rationale": result.rationale,
        },
        "expert_opinions": [
            {
                "role": op.role,
                "confidence": op.confidence,
                "recommendations": op.recommendations,
                "rationale": op.rationale,
            }
            for op in result.expert_opinions
        ],
    }
    receipt_path = Path("data") / f"run_moe_check_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _LOGGER.info("receipt written", extra={"path": str(receipt_path)})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observe-db", default="data/wobblebot-observe.db")
    parser.add_argument("--news-db", default="data/wobblebot-news.db")
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--lookback-hours", type=float, default=6.0)
    parser.add_argument("--news-lookback-hours", type=float, default=24.0)
    parser.add_argument("--news-limit", type=int, default=10)
    parser.add_argument(
        "--aggregator",
        choices=("voting", "weighted_confidence", "arbitrator"),
        default="weighted_confidence",
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
