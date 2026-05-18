"""One-shot live cloud-LLM smoke test (Stage 6.5.A).

Stage 6.5 verifies the Phase 6 cloud adapters end-to-end against real
APIs under live cost-cap enforcement. This script is the operator's
entry point: pick a provider + model + role, run one call, see the
receipt (tokens, real cost, request_id, persisted ``LLMCallRecord``).

**This script SPENDS REAL MONEY.** Per call cost is ~$0.001-$0.05
depending on provider + model + reasoning vs not. The default
``--max-tokens 100`` floor keeps individual calls cheap. The session
cap in ``LLMCostConfig`` (default $0.50) plus the daily cap (default
$1.00) bound the blast radius — see ``settings.example.yml``'s
``llm:`` block.

Usage::

    python tools/run_cloud_check.py --provider anthropic --role quant
    python tools/run_cloud_check.py --provider openai --role operator --model gpt-4o-mini
    python tools/run_cloud_check.py --provider google --role operator --max-tokens 50
    python tools/run_cloud_check.py --provider anthropic --role quant --log-format json

The role drives both the prompt selection (operator → operator.md;
others → quant.md as a generic-trading-advisor stand-in) and the
``LLMCallRecord.role`` column.

Reads the appropriate API key from the env (per ADR-015 decision 6):
- ``ANTHROPIC_API_KEY``
- ``OPENAI_API_KEY`` (plus optional ``OPENAI_ORGANIZATION``)
- ``GOOGLE_API_KEY``

After the call lands, query ``tools/show_llm_costs.py`` to see the
full ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter
from wobblebot.adapters.anthropic_assistant import AnthropicAssistantAdapter
from wobblebot.adapters.google import GoogleAdvisorAdapter, GoogleAssistantAdapter
from wobblebot.adapters.openai import OpenAIAdvisorAdapter, OpenAIAssistantAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import load_operator_env
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import Prompt, load_prompt
from wobblebot.domain.exceptions import LLMCostCapExceeded
from wobblebot.domain.llm_cost import LLMProvider, LLMRole
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import CurrentGridParams, PerformanceSummary
from wobblebot.ports.assistant import ConversationContext, EngineStateSnapshot
from wobblebot.ports.exceptions import AdvisorError, AssistantError
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_retry import LLMRetryConfig

_LOGGER = logging.getLogger("wobblebot.tools.run_cloud_check")
_DEFAULT_DB = Path("data") / "wobblebot-operator.db"

_DEFAULT_MODELS: dict[LLMProvider, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",  # cheapest default for the smoke test
    "google": "gemini-2.5-flash",  # cheapest default for the smoke test
}

_VALID_ROLES: tuple[LLMRole, ...] = (
    "operator",
    "quant",
    "risk",
    "news",
    "arbitrator",
    "single",
)


def _api_key_env_var(provider: LLMProvider) -> str:
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
    }[provider]


def _prompt_file_for_role(role: LLMRole) -> Path:
    """Map role → committed prompt-file path. Operator gets its dedicated
    prompt; advisor roles share the quant prompt as a generic stand-in
    (the smoke test cares about the round-trip, not the recommendation
    quality)."""
    if role == "operator":
        return Path("config/prompts/operator.md")
    return Path("config/prompts/quant.md")


def _summary_for_advisor() -> PerformanceSummary:
    """Minimal fixture summary for the advisor smoke test — small enough
    to keep token costs low."""
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=1.0,
        latest_price=80000.0,
        snapshot_count=10,
        volatility=0.001,
        max_drawdown=-0.005,
        flatness=0.99,
        cycle_count=0,
        win_rate=0.0,
        total_pnl=0.0,
        current_grid=CurrentGridParams(
            spacing_percentage=1.0,
            levels_above=3,
            levels_below=3,
            order_size_usd=10.0,
        ),
    )


def _context_for_assistant() -> ConversationContext:
    """Minimal fixture conversation context."""
    return ConversationContext(
        current_message="What's my session status?",
        channel_id="smoke-test",
        user_id="smoke-test",
        recent_turns=(),
        engine_state_snapshot=EngineStateSnapshot(
            snapshot_at=Timestamp(dt=datetime.now(UTC)),
            symbols=[],
            total_usd_balance=100.0,
            session_pnl=0.0,
            session_runtime_seconds=0.0,
        ),
    )


def _build_advisor(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    args: argparse.Namespace,
    storage: SQLiteStorageAdapter,
    prompt: Prompt,
    cost_config: LLMCostConfig,
    retry_config: LLMRetryConfig,
    tracker: SessionCostTracker,
) -> object:
    """Construct the right advisor adapter from CLI args."""
    api_key = os.environ[_api_key_env_var(args.provider)]
    common: dict[str, object] = {
        "model": args.model,
        "prompt": prompt,
        "role": args.role,
        "api_key": api_key,
        "storage": storage,
        "session_tracker": tracker,
        "cost_config": cost_config,
        "retry_config": retry_config,
        "max_tokens": args.max_tokens,
    }
    if args.provider == "anthropic":
        return AnthropicAdvisorAdapter(**common)  # type: ignore[arg-type]
    if args.provider == "openai":
        return OpenAIAdvisorAdapter(
            organization=os.environ.get("OPENAI_ORGANIZATION") or None,
            **common,  # type: ignore[arg-type]
        )
    return GoogleAdvisorAdapter(**common)  # type: ignore[arg-type]


def _build_assistant(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    args: argparse.Namespace,
    storage: SQLiteStorageAdapter,
    prompt: Prompt,
    cost_config: LLMCostConfig,
    retry_config: LLMRetryConfig,
    tracker: SessionCostTracker,
) -> object:
    """Construct the right assistant adapter from CLI args."""
    api_key = os.environ[_api_key_env_var(args.provider)]
    common: dict[str, object] = {
        "model": args.model,
        "prompt": prompt,
        "api_key": api_key,
        "storage": storage,
        "session_tracker": tracker,
        "cost_config": cost_config,
        "retry_config": retry_config,
        "max_tokens": args.max_tokens,
    }
    if args.provider == "anthropic":
        return AnthropicAssistantAdapter(**common)  # type: ignore[arg-type]
    if args.provider == "openai":
        return OpenAIAssistantAdapter(
            organization=os.environ.get("OPENAI_ORGANIZATION") or None,
            **common,  # type: ignore[arg-type]
        )
    return GoogleAssistantAdapter(**common)  # type: ignore[arg-type]


async def _run(  # pylint: disable=too-many-locals,too-many-return-statements
    args: argparse.Namespace,
) -> int:
    # Validate env first — fail fast before opening DBs / building configs.
    key_var = _api_key_env_var(args.provider)
    if not os.environ.get(key_var):
        _LOGGER.error(
            "API key missing from environment",
            extra={"env_var": key_var, "provider": args.provider},
        )
        return 2

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    cost_config = LLMCostConfig(
        max_spend_per_day_usd=Decimal(str(args.daily_cap)),
        max_spend_per_session_usd=Decimal(str(args.session_cap)),
        enforce=not args.dry_run,
    )
    retry_config = LLMRetryConfig(max_retries=2, initial_backoff_seconds=1.0)
    tracker = SessionCostTracker()

    try:
        prompt = load_prompt(_prompt_file_for_role(args.role))
    except (FileNotFoundError, ValueError) as exc:
        _LOGGER.error("failed to load prompt", extra={"error": str(exc)})
        return 2

    storage = SQLiteStorageAdapter(str(db_path))
    await storage.connect()
    try:
        if args.role == "operator":
            assistant = _build_assistant(args, storage, prompt, cost_config, retry_config, tracker)
            _LOGGER.info(
                "calling assistant",
                extra={
                    "provider": args.provider,
                    "model": args.model,
                    "max_tokens": args.max_tokens,
                    "enforce": cost_config.enforce,
                },
            )
            ctx = _context_for_assistant()
            try:
                intent = await assistant.parse_intent(ctx)  # type: ignore[attr-defined]
            except LLMCostCapExceeded as exc:
                _LOGGER.error("cost cap tripped before call", extra={"reason": str(exc)})
                return 2
            except AssistantError as exc:
                _LOGGER.error("assistant call failed", extra={"error": str(exc)})
                await _print_latest_record_via_get(storage)
                return 1
            _LOGGER.info("assistant returned", extra={"intent_kind": intent.kind})
        else:
            advisor = _build_advisor(args, storage, prompt, cost_config, retry_config, tracker)
            _LOGGER.info(
                "calling advisor",
                extra={
                    "provider": args.provider,
                    "model": args.model,
                    "role": args.role,
                    "max_tokens": args.max_tokens,
                    "enforce": cost_config.enforce,
                },
            )
            summary = _summary_for_advisor()
            get_rec = advisor.get_recommendation  # type: ignore[attr-defined]
            try:
                recommendation = await get_rec(summary)
            except LLMCostCapExceeded as exc:
                _LOGGER.error("cost cap tripped before call", extra={"reason": str(exc)})
                return 2
            except AdvisorError as exc:
                _LOGGER.error("advisor call failed", extra={"error": str(exc)})
                await _print_latest_record_via_get(storage)
                return 1
            _LOGGER.info(
                "advisor returned",
                extra={
                    "role": recommendation.role,
                    "confidence": recommendation.confidence,
                    "recommendations": recommendation.recommendations,
                },
            )

        # Print the persisted cost record so the operator sees the receipt.
        await _print_latest_record_via_get(storage)
        _LOGGER.info(
            "session total",
            extra={"total_usd": str(tracker.total)},
        )
    finally:
        # Close adapter clients if we constructed them.
        aclose = getattr(assistant if args.role == "operator" else advisor, "aclose", None)
        if aclose is not None:
            await aclose()
        await storage.close()
    return 0


async def _print_latest_record_via_get(storage: SQLiteStorageAdapter) -> None:
    """Pull the newest llm_calls row + print it for the operator."""
    rows = await storage.get_llm_calls(limit=1)
    if not rows:
        _LOGGER.info("no llm_call record found (call may have failed before persistence)")
        return
    rec = rows[0]
    _LOGGER.info(
        "receipt",
        extra={
            "id": str(rec.id),
            "provider": rec.provider,
            "model": rec.model,
            "role": rec.role,
            "tokens_in": rec.tokens_in,
            "tokens_out": rec.tokens_out,
            "tokens_reasoning": rec.tokens_reasoning,
            "cost_usd": str(rec.cost_usd),
            "request_id": rec.request_id,
            "success": rec.success,
            "error_kind": rec.error_kind,
        },
    )


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(
        prog="tools/run_cloud_check.py",
        description=(
            "One-shot live cloud-LLM smoke test. SPENDS REAL MONEY (~$0.001-$0.05 "
            "per call depending on provider + model). Default caps cap the "
            "blast radius."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("anthropic", "openai", "google"),
        required=True,
        help="Which provider to exercise.",
    )
    parser.add_argument(
        "--role",
        choices=_VALID_ROLES,
        default="operator",
        help=(
            "Role recorded in LLMCallRecord. 'operator' uses the AssistantPort "
            "path with operator.md prompt; other roles use the AdvisorPort "
            "path with quant.md as a generic-advisor stand-in. Default: operator."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model id. Defaults: anthropic→claude-sonnet-4-6, openai→gpt-4o-mini, "
            "google→gemini-2.5-flash (cheapest in each family)."
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=100,
        help="Hard cap on completion tokens. Low default keeps the call cheap.",
    )
    parser.add_argument(
        "--daily-cap",
        type=str,
        default="1.00",
        help="USD daily cap (ADR-014). Default $1.00.",
    )
    parser.add_argument(
        "--session-cap",
        type=str,
        default="0.50",
        help="USD per-session cap (ADR-014). Default $0.50.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Disable cost-gate enforcement (enforce=False). The call still "
            "happens and bills; only the gate refuses to deny. Useful for "
            "the first observation week per ADR-014 decision 8."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=str(_DEFAULT_DB),
        help=f"Operator DB path (default: {_DEFAULT_DB}).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Output format.",
    )
    args = parser.parse_args()
    if args.model is None:
        args.model = _DEFAULT_MODELS[args.provider]
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
