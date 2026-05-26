"""Pull then probe a sequence of Ollama models against the advisor role.

For each model in CANDIDATES:
  1. Skip if already pulled (idempotent / resumable)
  2. ``ollama pull <model>``
  3. ``python tools/probe_advisor.py --model <model>``
  4. Capture stdout + parse the per-scenario verdict + total score

Sister of tools/pull_and_probe_assistants.py which exercises the
operator-assistant role. The candidate list here adds math
specialists (mathstral, wizardmath, phi4-mini-reasoning) that were
explicitly rejected for operator-assistant per
docs/reference/operator-llm-models.md but are explicit candidates
for the advisor role per that same doc.

Tags verified against Ollama library 2026-05-25 per
docs/reference/ollama-tag-verification.md.

At the end, print a summary table sorted by total score (descending)
then by error count (ascending).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 in both the child's print() and the parent's capture --
# Windows defaults to cp1252 which can't represent em-dashes, arrows,
# etc. that the advisor prompt + LLM responses routinely contain.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

# Candidate manifest for the advisor sweep (2026-05-25). q8_0 throughout
# for the candidates that ALSO appear in the operator-assistant sweep
# (apples-to-apples) plus the math specialists at their published default
# quant since math specialists are evaluated FOR THIS ROLE for the first
# time -- baseline before optimization.
CANDIDATES: list[str] = [
    # ===== Math specialists (advisor-role candidates) ===== #
    # Rejected from operator-assistant per
    # docs/reference/operator-llm-models.md. Advisor-role candidates
    # because the task is numerical-reasoning over PerformanceSummary
    # metrics. WizardMath uses the hyphenated tag (wizard-math) on
    # Ollama's library, not wizardmath -- 2026-05-25 sweep tried the
    # latter and got "manifest not found" until the operator caught
    # the correct URL.
    "mathstral:7b",
    "wizard-math:7b",
    "wizard-math:13b",
    "phi4-mini-reasoning:3.8b",
    # ===== Newer-gen general-purpose (added 2026-05-25 follow-up) ===== #
    # TII Falcon3 series -- newer than the rejected "falcon" family
    # in docs/reference/operator-llm-models.md. Treat as a fresh
    # candidate rather than inheriting the older Falcon's rejection.
    "falcon3:1b-instruct-q8_0",
    "falcon3:3b-instruct-q8_0",
    "falcon3:7b-instruct-q8_0",
    "falcon3:10b-instruct-q8_0",
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
    "phi:2.7b-chat-v2-q8_0",
    "nemotron-mini:4b-instruct-q8_0",
    "stablelm-zephyr:3b",
    "stablelm2:1.6b-chat-q8_0",
    "orca-mini:3b",
    "dolphin-phi:2.7b",
    # ===== Tier D (4-8GB) =====
    "llama3.1:8b-instruct-q8_0",
    "llama3:8b-instruct-q8_0",
    "llama2:7b-chat-q8_0",
    "mistral:7b-instruct-v0.3-q8_0",
    "qwen2.5:7b-instruct-q8_0",
    "qwen2:7b-instruct-q8_0",
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
    # ===== Legacy / earlier-gen =====
    "llama2:13b-chat-q8_0",
    "mistral:7b-instruct-v0.2-q8_0",
]

# The advisor probe is slower than the assistant probe -- each call
# emits a larger JSON object with reasoning, and 6 scenarios per model
# vs the assistant's 14 single-shot messages. Per-probe budget bumped
# to 15 min to cover thinking-style models (phi4-mini-reasoning could
# take minutes per scenario).
PROBE_TIMEOUT = 900  # 15 min per probe
PULL_TIMEOUT = 900  # 15 min per pull (large models)
RESULTS_DIR = Path("data/advisor_probe_results")


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
    """Run probe_advisor.py against the model; capture output + parse stats."""
    print(f"  probing {model}...", flush=True)
    t0 = time.monotonic()
    cmd = [sys.executable, "tools/probe_advisor.py", "--model", model]
    try:
        r = subprocess.run(
            cmd,
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


# Verdict tokens that probe_advisor.py emits in its summary table.
# Used to count how many scenarios landed in each bucket. The total
# score is parsed from the explicit ``TOTAL: N/18`` line.
_VERDICT_RE = re.compile(
    r"^\S+\s+\S+\s+\S+\s+(OK|OVERSHOOT|ADJACENT|WRONG|ERROR)\b",
    re.MULTILINE,
)
_TOTAL_RE = re.compile(r"^TOTAL:\s+(\d+)/(\d+)\s+errors:(\d+)", re.MULTILINE)


def _parse_probe_output(output: str) -> dict:
    """Extract the score + per-verdict counts from probe_advisor.py output."""
    total_match = _TOTAL_RE.search(output)
    if total_match is None:
        return {"ok": False, "error": "no TOTAL line in probe output"}
    score = int(total_match.group(1))
    max_score = int(total_match.group(2))
    errors = int(total_match.group(3))

    verdicts: dict[str, int] = {
        "OK": 0,
        "OVERSHOOT": 0,
        "ADJACENT": 0,
        "WRONG": 0,
        "ERROR": 0,
    }
    for match in _VERDICT_RE.finditer(output):
        verdict = match.group(1)
        verdicts[verdict] = verdicts.get(verdict, 0) + 1

    return {
        "ok": True,
        "score": score,
        "max_score": max_score,
        "errors": errors,
        "verdicts": verdicts,
    }


def _write_summary(summary: list[dict]) -> None:
    """Persist the running summary so an interrupted sweep isn't lost."""
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _print_summary(summary: list[dict]) -> None:
    print(f"\n\n{'='*80}\nSUMMARY\n{'='*80}", flush=True)
    header = (
        f"{'Model':50s}  {'Score':>7s}  "
        f"{'OK':>3s} {'OVER':>4s} {'ADJ':>4s} {'WR':>3s} {'ERR':>4s} {'Time':>6s}"
    )
    print(header)
    print("-" * 90)
    success = [s for s in summary if s["status"] == "OK"]
    success.sort(key=lambda s: (-s["score"], s["errors"], s["elapsed"]))
    failed = [s for s in summary if s["status"] != "OK"]
    for s in success:
        v = s["verdicts"]
        score_str = f"{s['score']}/{s['max_score']}"
        time_str = f"{s['elapsed']:.0f}s"
        print(
            f"{s['model']:50s}  {score_str:>7s}  "
            f"{v['OK']:>3d} {v['OVERSHOOT']:>4d} {v['ADJACENT']:>4d} "
            f"{v['WRONG']:>3d} {v['ERROR']:>4d} {time_str:>6s}"
        )
    for s in failed:
        print(f"{s['model']:50s}  {s['status']}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep an Ollama-served candidate list against the trading-advisor "
            "scenario battery. With no positional args the full hard-coded "
            "CANDIDATES list runs (~45 models including math specialists); pass "
            "explicit model tags to run a subset."
        ),
    )
    parser.add_argument(
        "models",
        nargs="*",
        help=(
            "Explicit Ollama tags to probe. Overrides the default CANDIDATES "
            "list. Default: run every entry in CANDIDATES."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    # Reconfigure stdout/stderr to UTF-8 because PowerShell's default
    # cp1252 cannot encode Braille-spinner characters that show up in
    # ``ollama pull``'s captured stderr when a model fails to pull. The
    # 2026-05-25 first advisor sweep crashed printing such output from
    # a wizardmath:7b pull failure mid-run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

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
                f"  -> {result['score']}/{result['max_score']} score, "
                f"{result['errors']} errors, {result['elapsed']:.0f}s total"
            )
            print(line, flush=True)
            summary.append(
                {
                    "model": model,
                    "status": "OK",
                    "score": result["score"],
                    "max_score": result["max_score"],
                    "errors": result["errors"],
                    "verdicts": result["verdicts"],
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
