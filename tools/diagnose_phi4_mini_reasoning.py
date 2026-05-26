"""Focused diagnostic for phi4-mini-reasoning:3.8b-fp16.

The model scored 0/14 on the operator-assistant battery and 0/18 on
the advisor battery. The compatibility docs claim "math-mode prose,
no valid JSON" without ever showing the actual output. This script
fixes that gap.

Sends three styles of prompt directly to Ollama, prints the **raw
unparsed text response** for each, then runs three workarounds that
might coax structured output:

1. Default operator prompt (operator.md) + a simple status message
2. Default advisor prompt (quant.md) + a minimal PerformanceSummary
3. Stripped prompt -- the absolute minimum schema instruction
4. WORKAROUND A: same as 1+2+3 with Ollama's ``format=json`` constraint
5. WORKAROUND B: same as 1+2+3 with a reasoning-first preamble
   ("Think first, then output ONLY the final JSON in a code block")

Run with: ``python tools/diagnose_phi4_mini_reasoning.py``

Prints to stdout in plain text. No DB writes, no scoring, no
parsing -- just shows what the model actually emits.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

from wobblebot.config.prompts import load_prompt

MODEL = "phi4-mini-reasoning:3.8b-fp16"
BASE_URL = "http://localhost:11434"
TIMEOUT_SECONDS = 300.0
TEMPERATURE = 0.5
NUM_PREDICT = 1024

OPERATOR_USER_MESSAGE = "status"

ADVISOR_USER_MESSAGE = """\
You are reviewing the last hour of grid-trading performance.
Current spacing_percentage: 1.0 (baseline).

PerformanceSummary:
- symbol: BTC/USD
- realized_volatility: 0.0008
- drawdown_percentage: -0.002
- cycles_completed: 1
- fills: 2

Output a single JSON object matching advisor_recommendation_v1
with fields: recommendations.spacing_percentage (float),
confidence (low/medium/high), rationale (string)."""

STRIPPED_PROMPT = """\
You are an intent router. Output exactly one JSON object with:
  {"kind": "query", "query": {"kind": "status"}}
No other text. No reasoning. No markdown. Just the JSON object."""

REASONING_FIRST_PREAMBLE = """\
You may think step-by-step internally. After your reasoning, output
ONLY the final JSON answer inside a ```json``` markdown code block.
Do not include math notation, equations, or LaTeX. Just JSON."""


def divider(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def subdivider(title: str) -> None:
    print()
    print("-" * 80)
    print(title)
    print("-" * 80)


async def call_chat(
    *,
    client: httpx.AsyncClient,
    system_prompt: str,
    user_message: str,
    format_json: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {"temperature": TEMPERATURE, "num_predict": NUM_PREDICT},
    }
    if format_json:
        payload["format"] = "json"
    response = await client.post(f"{BASE_URL}/api/chat", json=payload)
    response.raise_for_status()
    envelope = response.json()
    msg = envelope.get("message", {})
    return msg.get("content", "")


async def call_generate(
    *,
    client: httpx.AsyncClient,
    system_prompt: str,
    user_message: str,
    format_json: bool = False,
) -> str:
    full_prompt = f"{system_prompt}\n\n{user_message}"
    payload: dict[str, Any] = {
        "model": MODEL,
        "prompt": full_prompt,
        "stream": False,
        "options": {"temperature": TEMPERATURE, "num_predict": NUM_PREDICT},
    }
    if format_json:
        payload["format"] = "json"
    response = await client.post(f"{BASE_URL}/api/generate", json=payload)
    response.raise_for_status()
    envelope = response.json()
    return envelope.get("response", "")


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    print(f"Diagnosing model: {MODEL}")
    print(f"Endpoint: {BASE_URL}")
    print(f"Temperature: {TEMPERATURE}, num_predict: {NUM_PREDICT}")

    repo_root = Path(__file__).resolve().parents[1]
    operator_prompt_path = repo_root / "config" / "prompts" / "operator.md"
    quant_prompt_path = repo_root / "config" / "prompts" / "quant.md"
    operator_prompt = load_prompt(operator_prompt_path).body
    quant_prompt = load_prompt(quant_prompt_path).body
    print(f"operator.md: {len(operator_prompt)} chars")
    print(f"quant.md:    {len(quant_prompt)} chars")
    print(f"stripped:    {len(STRIPPED_PROMPT)} chars")

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        divider("TEST 1: operator.md (full) + 'status' via /api/chat")
        print(f"USER: {OPERATOR_USER_MESSAGE}")
        print()
        try:
            out = await call_chat(
                client=client,
                system_prompt=operator_prompt,
                user_message=OPERATOR_USER_MESSAGE,
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

        divider("TEST 2: quant.md (full) + minimal PerformanceSummary via /api/generate")
        print("USER: (advisor prompt body abbreviated above)")
        print()
        try:
            out = await call_generate(
                client=client,
                system_prompt=quant_prompt,
                user_message=ADVISOR_USER_MESSAGE,
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

        divider("TEST 3: stripped prompt + 'status' via /api/chat")
        print(f"USER: {OPERATOR_USER_MESSAGE}")
        print()
        try:
            out = await call_chat(
                client=client,
                system_prompt=STRIPPED_PROMPT,
                user_message=OPERATOR_USER_MESSAGE,
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

        divider("WORKAROUND A: operator.md + 'status' + format=json")
        print(f"USER: {OPERATOR_USER_MESSAGE}")
        print()
        try:
            out = await call_chat(
                client=client,
                system_prompt=operator_prompt,
                user_message=OPERATOR_USER_MESSAGE,
                format_json=True,
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

        divider("WORKAROUND B: operator.md + reasoning-first preamble + 'status'")
        preamble_prompt = REASONING_FIRST_PREAMBLE + "\n\n" + operator_prompt
        print(f"USER: {OPERATOR_USER_MESSAGE}")
        print()
        try:
            out = await call_chat(
                client=client,
                system_prompt=preamble_prompt,
                user_message=OPERATOR_USER_MESSAGE,
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

        divider("WORKAROUND C: stripped + format=json (most-constrained)")
        print(f"USER: {OPERATOR_USER_MESSAGE}")
        print()
        try:
            out = await call_chat(
                client=client,
                system_prompt=STRIPPED_PROMPT,
                user_message=OPERATOR_USER_MESSAGE,
                format_json=True,
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

        divider("WORKAROUND D: trivial 'hello' via /api/chat — control")
        print("USER: hello")
        print()
        try:
            out = await call_chat(
                client=client,
                system_prompt="You are a helpful assistant.",
                user_message="hello",
            )
            print(f"RAW RESPONSE ({len(out)} chars):")
            print(out)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

    print()
    print("=" * 80)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
