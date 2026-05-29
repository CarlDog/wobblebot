"""Stress-test Ollama models for the cli/operator assistant role on the NAS.

Iterates over a list of installed models, runs each through a
condensed battery of intent-routing probes against the operator's
NAS-hosted Ollama, and reports per-model timing + schema-pass rate.

Designed for finding the fastest + most reliable model for the
cpu-only deployment profile. Targets http://carldog-nas:11434 by
default; override via --base-url.

Bypasses the suitability blocklist (so e.g. phi4-mini-reasoning,
llava, etc. could be probed if pulled — though they're filtered
from the default candidate list by name pattern).

Run as: python tools/sweep_assistant_nas.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from wobblebot.adapters.ollama_assistant import OllamaAssistantAdapter
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.assistant import (
    ConversationContext,
    EngineStateSnapshot,
    SymbolStateSnapshot,
)
from wobblebot.ports.exceptions import AssistantError

# Condensed battery covering each routing surface. Smaller than
# probe_assistant.py's 27-message default so the multi-model sweep
# stays under an hour. Edit if probes need broader coverage.
BATTERY: tuple[tuple[str, str], ...] = (
    ("query", "status"),
    ("query", "show grid"),
    ("query", "any news?"),
    ("command", "pause BTC"),
    ("command", "stop the bot"),
    ("brief", "give me a brief"),
    ("edge", "thanks"),
    ("edge", "what's the weather"),
)


def _make_engine_state() -> EngineStateSnapshot:
    """Realistic single-symbol snapshot for the system prompt context."""
    return EngineStateSnapshot(
        snapshot_at=Timestamp(dt=datetime.now(UTC)),
        symbols=[
            SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=4),
        ],
        total_usd_balance=59.92,
        session_pnl=0.05,
        session_runtime_seconds=600.0,
        recent_fill_count=2,
        harvester_band="hold",
    )


async def _probe_one(
    adapter: OllamaAssistantAdapter,
    message: str,
    state: EngineStateSnapshot,
) -> tuple[bool, float, str]:
    """One probe call. Returns (success, wall_seconds, detail).

    Success = parse_intent returned a typed OperatorIntent without
    raising. detail is either the intent class name on success or
    a short error description on failure.
    """
    context = ConversationContext(
        current_message=message,
        channel_id="probe-sweep",
        user_id="probe-sweep",
        recent_turns=[],
        engine_state_snapshot=state,
    )
    t0 = time.monotonic()
    try:
        intent = await adapter.parse_intent(context)
        wall = time.monotonic() - t0
        return True, wall, type(intent).__name__
    except AssistantError as exc:
        wall = time.monotonic() - t0
        msg = str(exc)
        # Trim long pydantic ValidationError dumps.
        if len(msg) > 160:
            msg = msg[:160] + "..."
        return False, wall, msg


async def sweep_one_model(
    model: str,
    prompt_file: Path,
    base_url: str,
    timeout_seconds: float,
) -> dict:
    """Run the full battery against one model. Returns a result dict."""
    print(f"\n=== {model} ===", flush=True)
    prompt = load_prompt(prompt_file)
    try:
        adapter = OllamaAssistantAdapter(
            model=model,
            prompt=prompt,
            base_url=base_url,
            temperature=0.3,
            max_tokens=512,
            timeout_seconds=timeout_seconds,
            bypass_suitability_check=True,
        )
    except AssistantError as exc:
        print(f"  ADAPTER REFUSED: {exc}", flush=True)
        return {"model": model, "skipped": True, "reason": str(exc)}

    # Warmup so per-message numbers reflect hot inference.
    t_warm0 = time.monotonic()
    await adapter.warmup()
    warm_seconds = time.monotonic() - t_warm0
    print(f"  warmup: {warm_seconds:.1f}s", flush=True)

    state = _make_engine_state()
    results = []
    passes = 0
    total_time = 0.0
    for category, msg in BATTERY:
        ok, wall, detail = await _probe_one(adapter, msg, state)
        total_time += wall
        if ok:
            passes += 1
            print(f"  PASS {wall:6.2f}s  [{category:7}] {msg!r:35s} -> {detail}", flush=True)
        else:
            print(f"  FAIL {wall:6.2f}s  [{category:7}] {msg!r:35s} -> {detail}", flush=True)
        results.append(
            {"category": category, "message": msg, "ok": ok, "wall_s": wall, "detail": detail}
        )

    await adapter.aclose()
    return {
        "model": model,
        "warmup_s": warm_seconds,
        "passes": passes,
        "total": len(BATTERY),
        "pass_rate": passes / len(BATTERY),
        "mean_wall_s": total_time / len(BATTERY),
        "total_wall_s": total_time,
        "results": results,
    }


def _print_summary(reports: list[dict]) -> None:
    """Sort + print the final comparison table."""
    ranked = [r for r in reports if not r.get("skipped")]
    ranked.sort(key=lambda r: (-r["pass_rate"], r["mean_wall_s"]))
    print("\n\n=== SUMMARY (best first: highest pass-rate, lowest mean latency) ===\n")
    print(f"  {'model':50s} {'pass':>10s}  {'mean':>8s}  {'warmup':>8s}")
    print(f"  {'-'*50}  {'-'*10}  {'-'*8}  {'-'*8}")
    for r in ranked:
        pass_str = f"{r['passes']}/{r['total']}"
        print(
            f"  {r['model']:50s} {pass_str:>10s}  {r['mean_wall_s']:6.2f}s  "
            f"{r['warmup_s']:6.1f}s"
        )
    skipped = [r for r in reports if r.get("skipped")]
    if skipped:
        print(f"\n  Skipped ({len(skipped)}):")
        for r in skipped:
            print(f"    {r['model']}: {r['reason']}")


async def sweep_async(
    models: list[str], prompt_file: Path, base_url: str, timeout_seconds: float
) -> None:
    reports = []
    for model in models:
        try:
            result = await sweep_one_model(model, prompt_file, base_url, timeout_seconds)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"  UNCAUGHT: {type(exc).__name__}: {exc}", flush=True)
            result = {"model": model, "skipped": True, "reason": f"{type(exc).__name__}: {exc}"}
        reports.append(result)
    _print_summary(reports)


# 16 instruct-tuned candidates currently on the operator's NAS, ≤9.5GB.
# Edit to add new pulls.
DEFAULT_CANDIDATES = (
    "qwen2.5:1.5b-instruct-q4_K_M",
    "llama3.2:1b",
    "qwen2.5:3b-instruct-q4_K_M",
    "qwen2.5:3b-instruct-q8_0",
    "falcon3:3b-instruct-q8_0",
    "starling-lm:7b",
    "neural-chat:7b",
    "zephyr:7b",
    "qwen2:7b-instruct-q4_K_M",
    "qwen2.5:7b-instruct-q4_K_M",
    "llama3:8b-instruct-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
    "granite3-dense:8b-instruct-q4_K_M",
    "solar:10.7b-instruct-v1-q4_K_M",
    "mistral-nemo:12b-instruct-2407-q4_K_M",
    "phi4:14b-q4_K_M",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools.sweep_assistant_nas",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://carldog-nas:11434",
        help="Ollama base URL. Default: http://carldog-nas:11434",
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default="config/prompts/operator.md",
        help="System prompt file (must have role=operator).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="Per-call HTTP timeout. Bigger models on CPU need more.",
    )
    parser.add_argument(
        "--models",
        type=str,
        default=None,
        help=(
            "Comma-separated model tags to sweep. Default: the bundled "
            "16-model candidate list of installed instruct-tuned models "
            "<= 9.5 GB."
        ),
    )
    args = parser.parse_args()

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = list(DEFAULT_CANDIDATES)

    print(f"Sweep target: {args.base_url}")
    print(f"Prompt: {args.prompt_file}")
    print(f"Models: {len(models)}")
    print(f"Battery: {len(BATTERY)} messages per model")
    print(f"Timeout: {args.timeout_seconds}s per call")

    asyncio.run(
        sweep_async(models, Path(args.prompt_file), args.base_url, args.timeout_seconds)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
