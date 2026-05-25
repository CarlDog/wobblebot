"""Pull then probe a sequence of Ollama models; aggregate results.

For each model in CANDIDATES:
  1. Skip if already pulled (idempotent / resumable)
  2. ``ollama pull <model>``
  3. ``python tools/probe_assistant.py --model <model> --skip-multi-turn``
  4. Capture both stdout streams + parse the routing accuracy

Tags here are EXPLICITLY VERIFIED against Ollama library
(2026-05-25) per docs/reference/ollama-tag-verification.md.
Plain `:size` tags would have pulled BASE models for many of
these (Qwen, Gemma, Llama 2, etc.) -- always use the explicit
instruct/chat suffix when the family publishes both variants.

At the end, print a summary table sorted by accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 in both the child's print() and the parent's capture --
# Windows defaults to cp1252 which can't represent em-dashes, arrows,
# etc. that the operator prompt + LLM responses routinely contain.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

# Verified explicit-tag manifest (2026-05-25).
# Quantization: q8_0 throughout for consistency with our existing
# validated models (phi4, mistral-nemo, phi4-reasoning) -- operator's
# "judge on accuracy not size" framing means we pick higher-quality
# quantization where available.
#
# Removed from upstream candidate list (NOT FOUND on Ollama 2026-05-25):
#   - qwen1.5 (whole family)
#   - granite3 (renamed to granite3-dense)
#   - mathstral, wizardmath (not pulled -- math specialists rejected
#     from operator-assistant role per docs/reference/operator-llm-models.md)
#   - falcon, vicuna, wizardlm (discontinued / orphaned)
#
# Tier A through Tier D current-gen first (~30 models). Legacy
# variants (llama2, qwen, mistral v0.1/v0.2, gemma v1) appended.

CANDIDATES: list[str] = [
    # ===== Tier A (<1GB) =====
    "tinyllama:1.1b-chat-v1-q8_0",
    "qwen2.5:0.5b-instruct-q8_0",
    "qwen2:0.5b-instruct-q8_0",
    "smollm2:360m-instruct-q8_0",
    "smollm2:1.7b-instruct-q8_0",
    # ===== Tier B (1-2GB) =====
    "llama3.2:1b-instruct-q8_0",
    "qwen2.5:1.5b-instruct-q8_0",
    "gemma2:2b-instruct-q8_0",
    "gemma:2b-instruct-q8_0",
    "granite3-dense:2b-instruct-q8_0",
    # ===== Tier C (2-4GB) =====
    "llama3.2:3b-instruct-q8_0",
    "qwen2.5:3b-instruct-q8_0",
    "phi3.5:3.8b-mini-instruct-q8_0",
    "phi3:3.8b-mini-4k-instruct-q8_0",
    "phi:2.7b-chat-v2-q8_0",  # phi-2
    "nemotron-mini:4b-instruct-q8_0",
    "stablelm-zephyr:3b",  # plain tag is the chat variant
    "stablelm2:1.6b-chat-q8_0",
    "orca-mini:3b",  # plain tag is the chat variant
    "dolphin-phi:2.7b",  # plain tag is the chat variant
    # ===== Tier D (4-8GB) =====
    "llama3.1:8b-instruct-q8_0",
    "llama3:8b-instruct-q8_0",
    "llama2:7b-chat-q8_0",
    "mistral:7b-instruct-v0.3-q8_0",  # current
    "qwen2.5:7b-instruct-q8_0",
    "qwen2:7b-instruct-q8_0",
    "qwen:7b-chat-v1.5-q8_0",  # qwen 1.0
    "gemma2:9b-instruct-q8_0",
    "gemma:7b-instruct-v1.1-q8_0",
    "granite3-dense:8b-instruct-q8_0",
    "yi:6b-chat-q8_0",
    "yi:9b-chat-v1.5-q8_0",
    "internlm2:7b-chat-v2.5-q8_0",
    "openchat:7b",
    "starling-lm:7b",
    "neural-chat:7b",
    "zephyr:7b",
    "solar:10.7b-instruct-v1-q8_0",
    "nous-hermes:7b",
    "nous-hermes2:10.7b",
    "deepseek-llm:7b-chat-q8_0",
    # ===== Legacy / earlier-gen (mostly for low-end hardware fallback) =====
    "llama2:13b-chat-q8_0",
    "mistral:7b-instruct-v0.2-q8_0",
    "mistral:7b-instruct-v0.1-q8_0",  # may not exist; script handles
]

PROBE_TIMEOUT = 600  # 10 min per probe
PULL_TIMEOUT = 900  # 15 min per pull (large models)
RESULTS_DIR = Path("data/probe_results")


def already_pulled(model: str) -> bool:
    """Check if ollama already has this model locally."""
    try:
        r = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return model in r.stdout


def pull_model(model: str) -> tuple[bool, str]:
    """Pull a model; return (ok, message)."""
    if already_pulled(model):
        return True, "(already pulled)"
    print(f"  pulling {model}...", flush=True)
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ["ollama", "pull", model],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PULL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "pull timed out"
    elapsed = time.monotonic() - t0
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()[-200:]
        return False, f"pull failed ({elapsed:.0f}s): {err}"
    return True, f"pulled in {elapsed:.0f}s"


def run_probe(model: str) -> dict:
    """Run probe_assistant.py against the model; capture output + parse stats."""
    print(f"  probing {model}...", flush=True)
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [
                sys.executable,
                "tools/probe_assistant.py",
                "--model",
                model,
                "--skip-multi-turn",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_UTF8_ENV,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "probe timed out", "elapsed": -1}
    elapsed = time.monotonic() - t0
    output = (r.stdout or "") + ("\n--- stderr ---\n" + r.stderr if r.stderr else "")
    out_path = RESULTS_DIR / f"{model.replace(':', '_').replace('/', '_')}.txt"
    out_path.write_text(output, encoding="utf-8")
    parsed = _parse_probe_output(output)
    parsed["elapsed"] = elapsed
    return parsed


EXPECTED: list[tuple[str, str]] = [
    ("status", "query:status"),
    ("how are things?", "query:status"),
    ("show me what's available", "query:help"),
    ("any news?", "query:recent_news"),
    ("what's the harvester doing", "query:harvester_status"),
    ("give me a brief", "query:status_report"),
    ("status report for the past 4 hours", "query:status_report"),
    ("pause BTC", "command:pause"),
    ("stop the bot", "command:stop"),
    ("buy more bitcoin", "unparseable"),
    ("pause XRP", "unparseable"),
    ("what's the weather", "conversational"),
    ("show recent fills", "query:recent_fills"),
    ("news from the past 12 hours", "query:recent_news"),
]


def _parse_probe_output(output: str) -> dict:
    msg_to_parse: dict[str, str] = {}
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(">>> "):
            msg = line[4:].strip()
            for j in range(i + 1, min(i + 5, len(lines))):
                nxt = lines[j].strip()
                if nxt:
                    msg_to_parse[msg] = nxt
                    break

    correct = 0
    errors = 0
    rows: list[dict] = []
    for msg, expected in EXPECTED:
        parsed = msg_to_parse.get(msg, "MISSING")
        if parsed.startswith("ERROR"):
            errors += 1
            ok = False
        else:
            ok = parsed.startswith(expected)
            if ok:
                correct += 1
        rows.append({"msg": msg, "expected": expected, "parsed": parsed, "ok": ok})

    return {
        "ok": True,
        "correct": correct,
        "total": len(EXPECTED),
        "errors": errors,
        "rows": rows,
    }


def _write_summary(summary: list[dict]) -> None:
    """Persist the running summary so an interrupted sweep isn't lost."""
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _print_summary(summary: list[dict]) -> None:
    print(f"\n\n{'='*70}\nSUMMARY\n{'='*70}", flush=True)
    print(f"{'Model':50s}  {'Result':12s}  {'Total time':>10s}")
    print("-" * 80)
    success = [s for s in summary if s["status"] == "OK"]
    success.sort(key=lambda s: (-s["correct"], s["elapsed"]))
    failed = [s for s in summary if s["status"] != "OK"]
    for s in success:
        acc = f"{s['correct']}/{s['total']}"
        time_str = f"{s['elapsed']:.0f}s"
        print(f"{s['model']:50s}  {acc:12s}  {time_str:>10s}")
    for s in failed:
        print(f"{s['model']:50s}  {s['status']}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep an Ollama-served candidate list against the operator-assistant "
            "routing battery. With no positional args the full hard-coded CANDIDATES "
            "list runs (~40 models); pass explicit model tags to run a subset (e.g. "
            "to re-probe just the top tier from a prior sweep)."
        ),
    )
    parser.add_argument(
        "models",
        nargs="*",
        help=(
            "Explicit Ollama tags to probe. Overrides the default CANDIDATES list. "
            "Use the same tag format the default list uses (e.g. "
            "'granite3-dense:8b-instruct-q8_0'). Default: run every entry in CANDIDATES."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    models = args.models or CANDIDATES
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []
    try:
        for i, model in enumerate(models, 1):
            print(f"\n[{i}/{len(models)}] {model}", flush=True)
            ok, msg = pull_model(model)
            if not ok:
                print(f"  -> SKIP ({msg})", flush=True)
                summary.append({"model": model, "status": f"PULL_FAILED: {msg}"})
                _write_summary(summary)
                continue
            print(f"  {msg}", flush=True)
            result = run_probe(model)
            if not result.get("ok"):
                print(f"  -> PROBE FAILED: {result.get('error')}", flush=True)
                summary.append({"model": model, "status": f"PROBE_FAILED: {result.get('error')}"})
                _write_summary(summary)
                continue
            line = (
                f"  -> {result['correct']}/{result['total']} correct, "
                f"{result['errors']} errors, {result['elapsed']:.0f}s total"
            )
            print(line, flush=True)
            summary.append(
                {
                    "model": model,
                    "status": "OK",
                    "correct": result["correct"],
                    "total": result["total"],
                    "errors": result["errors"],
                    "elapsed": result["elapsed"],
                }
            )
            _write_summary(summary)
    finally:
        _print_summary(summary)
        _write_summary(summary)
        print(f"\nFull per-model outputs: {RESULTS_DIR}/*.txt")
        print(f"Summary JSON: {RESULTS_DIR}/summary.json")


if __name__ == "__main__":
    main()
