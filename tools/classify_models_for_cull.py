"""Audit installed Ollama models and classify each for the post-sweep cull.

Rules:
- KEEP_PRE_EXISTING : Installed before the 2026-05-24 sweep. Untouched by today's work.
- KEEP_TOP         : Scored as a top performer in one of the sweeps.
- KEEP_TIER_REC    : Documented low-end-hardware tier recommendation.
- KEEP_USER_PIN    : Explicitly retained by operator (e.g. phi4-mini-reasoning).
- PRUNE_UNSUITABLE : Pulled in the last two days AND scored as broken
                     (high error rate, near-zero score, or wrong-direction
                     dominant in the role it was tested for).
- UNCERTAIN        : Borderline; default behavior is KEEP and report for
                     manual review.

The classifier ONLY recommends PRUNE for models that pass BOTH gates:
(1) the model was pulled by yesterday's or today's sweeps, AND
(2) the model's sweep verdicts justify removal.

A model installed before the sweeps is KEPT regardless of how it would
have scored.
"""

from __future__ import annotations

import subprocess
import sys

# Models pulled by pull_and_probe_assistants.py CANDIDATES (2026-05-24
# operator-assistant sweep + today's falcon3 follow-up). Tag must match
# exactly -- "tinyllama:1.1b-chat-v1-q8_0" != "tinyllama:latest".
PULLED_YESTERDAY_ASSISTANT: set[str] = {
    "tinyllama:1.1b-chat-v1-q8_0",
    "qwen2.5:0.5b-instruct-q8_0",
    "qwen2:0.5b-instruct-q8_0",
    "smollm2:360m-instruct-q8_0",
    "smollm2:1.7b-instruct-q8_0",
    "llama3.2:1b-instruct-q8_0",
    "qwen2.5:1.5b-instruct-q8_0",
    "gemma2:2b-instruct-q8_0",
    "gemma:2b-instruct-q8_0",
    "granite3-dense:2b-instruct-q8_0",
    "llama3.2:3b-instruct-q8_0",
    "qwen2.5:3b-instruct-q8_0",
    "phi3.5:3.8b-mini-instruct-q8_0",
    "phi3:3.8b-mini-4k-instruct-q8_0",
    "phi:2.7b-chat-v2-q8_0",
    "nemotron-mini:4b-instruct-q8_0",
    "stablelm-zephyr:3b",
    "stablelm2:1.6b-chat-q8_0",
    "orca-mini:3b",
    "dolphin-phi:2.7b",
    "llama3.1:8b-instruct-q8_0",
    "llama3:8b-instruct-q8_0",
    "llama2:7b-chat-q8_0",
    "mistral:7b-instruct-v0.3-q8_0",
    "qwen2.5:7b-instruct-q8_0",
    "qwen2:7b-instruct-q8_0",
    "qwen:7b-chat-v1.5-q8_0",
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
    "llama2:13b-chat-q8_0",
    "mistral:7b-instruct-v0.2-q8_0",
    # falcon3 added today (2026-05-25)
    "falcon3:1b-instruct-q8_0",
    "falcon3:3b-instruct-q8_0",
    "falcon3:7b-instruct-q8_0",
    "falcon3:10b-instruct-q8_0",
}

# Low-end-hardware q4_K_M audit (2026-05-25)
PULLED_TODAY_LOWEND: set[str] = {
    "llama3.2:1b-instruct-q4_K_M",
    "qwen2.5:1.5b-instruct-q4_K_M",
    "gemma2:2b-instruct-q4_K_M",
    "granite3-dense:2b-instruct-q4_K_M",
    "qwen2.5:3b-instruct-q4_K_M",
    "phi3.5:3.8b-mini-instruct-q4_K_M",
    "nemotron-mini:4b-instruct-q4_K_M",
    "mistral:7b-instruct-v0.3-q4_K_M",
    "qwen2.5:7b-instruct-q4_K_M",
    "llama3:8b-instruct-q4_K_M",
    "granite3-dense:8b-instruct-q4_K_M",
    "solar:10.7b-instruct-v1-q4_K_M",
}

# Advisor sweep additions today (math specialists + already-counted falcon3)
PULLED_TODAY_ADVISOR: set[str] = {
    "mathstral:7b",
    "mathstral:7b-v0.1-q8_0",
    "wizard-math:7b",
    "wizard-math:13b",
    # phi4-mini-reasoning:3.8b resolved to the PRE-EXISTING :3.8b-fp16 alias;
    # no separate blob was downloaded.
}

PULLED_RECENT: set[str] = PULLED_YESTERDAY_ASSISTANT | PULLED_TODAY_LOWEND | PULLED_TODAY_ADVISOR

# Top performers per sweep — explicit keeps.
KEEP_TOP: set[str] = {
    # Advisor sweep top tier
    "llama3.1:8b-instruct-q8_0",  # 14/18 winner
    "wizard-math:13b",  # 13/18 math specialist
    "mathstral:7b",  # 12/18, zero wrong direction; q4 baseline
    # Operator-assistant sweep top tier (q8 from 2026-05-24)
    "granite3-dense:8b-instruct-q8_0",  # 13/14 winner
    "qwen2.5:3b-instruct-q8_0",  # 12/14
    "qwen2.5:7b-instruct-q8_0",  # 12/14
    "qwen2:7b-instruct-q8_0",  # 12/14
    "solar:10.7b-instruct-v1-q8_0",  # 12/14
    "starling-lm:7b",  # 12/14
    "zephyr:7b",  # 12/14
    # falcon3:3b — new operator-assistant low-end winner (13/15, 0 errors)
    "falcon3:3b-instruct-q8_0",
}

# Per-tier low-end-hardware q4 picks (recommended in operator-llm-models.md).
KEEP_TIER_REC: set[str] = {
    "qwen2.5:1.5b-instruct-q4_K_M",  # bottom tier winner
    "qwen2.5:3b-instruct-q4_K_M",  # mid tier standout (zero errors)
    "solar:10.7b-instruct-v1-q4_K_M",  # upper-mid winner
    "granite3-dense:8b-instruct-q4_K_M",  # upper-mid 13/15
    "qwen2.5:7b-instruct-q4_K_M",  # upper-mid 13/15
}

# Operator-explicit keeps (user requested retention).
# phi4-mini-reasoning:3.8b-fp16 was the original pre-existing pull;
# Ollama resolves "phi4-mini-reasoning:3.8b" to the same blob.
KEEP_USER_PIN: set[str] = {
    "phi4-mini-reasoning:3.8b-fp16",
}

# Sweep-verdict gate: a model is PRUNE-eligible only if it appears here
# (regardless of provenance, it had to score badly in the role it was
# tested for). Source: 2026-05-24/25 sweep summaries.
UNSUITABLE: set[str] = {
    # Operator-assistant sweep failures (≤ 5/14)
    "tinyllama:1.1b-chat-v1-q8_0",  # 1/14
    "smollm2:360m-instruct-q8_0",  # 1/14
    "gemma:2b-instruct-q8_0",  # 2/14
    "nous-hermes:7b",  # 2/14
    "qwen:7b-chat-v1.5-q8_0",  # 2/14
    "orca-mini:3b",  # 1/14
    "llama3.2:1b-instruct-q8_0",  # 4/14
    "qwen2.5:0.5b-instruct-q8_0",  # 5/14
    "qwen2:0.5b-instruct-q8_0",  # 4/14
    "phi:2.7b-chat-v2-q8_0",  # 5/14
    "stablelm2:1.6b-chat-q8_0",  # 4/14
    "dolphin-phi:2.7b",  # 4/14
    "phi3:3.8b-mini-4k-instruct-q8_0",  # 0/14 — broken on the routing prompt
    # Yesterday's low-end audit floor
    "llama3.2:1b-instruct-q4_K_M",  # 1/15 — same broken pattern at q4
    "granite3-dense:2b-instruct-q4_K_M",  # 9/15 — underperforms qwen2.5:1.5b
    "gemma2:2b-instruct-q4_K_M",  # 10/15 — marginal
    "llama3:8b-instruct-q4_K_M",  # 10/15 — q4 hurt this one
    # Advisor sweep failures (5/18 with 3 WRONG)
    "nous-hermes2:10.7b",  # was operator-assistant top tier but advisor 5/18
    "openchat:7b",  # was operator-assistant top tier but advisor 5/18
    # falcon3 family inversions
    "falcon3:7b-instruct-q8_0",  # 11/15 with 1 err on assistant; 11/18 advisor; no scaling benefit
    "falcon3:10b-instruct-q8_0",  # same pattern
    "falcon3:1b-instruct-q8_0",  # 2/15 assistant + 1/18 advisor with 5 errors
}


def classify(tag: str) -> tuple[str, str]:
    """Return (verdict, reason)."""
    if tag in KEEP_USER_PIN:
        return ("KEEP", "operator-pinned (more tests planned)")
    if tag not in PULLED_RECENT:
        return ("KEEP", "pre-existing (not pulled in 2026-05-24/25 sweeps)")
    if tag in KEEP_TOP:
        return ("KEEP", "top sweep performer")
    if tag in KEEP_TIER_REC:
        return ("KEEP", "low-end-hardware tier recommendation")
    if tag in UNSUITABLE:
        return ("PRUNE", "scored unsuitable in 2026-05-24/25 sweeps")
    return ("KEEP", "pulled recently but not flagged unsuitable (default-keep)")


def main() -> int:
    try:
        out = subprocess.check_output(["ollama", "list"], text=True, encoding="utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    lines = out.strip().splitlines()
    # Skip header "NAME ID SIZE MODIFIED"
    installed = []
    for raw in lines[1:]:
        parts = raw.split()
        if not parts:
            continue
        installed.append(parts[0])

    keep, prune = [], []
    for tag in installed:
        verdict, reason = classify(tag)
        if verdict == "PRUNE":
            prune.append((tag, reason))
        else:
            keep.append((tag, reason))

    print(f"\n=== KEEP ({len(keep)} models) ===")
    for tag, reason in keep:
        print(f"  {tag:55s} {reason}")
    print(f"\n=== PRUNE ({len(prune)} models) ===")
    for tag, reason in prune:
        print(f"  {tag:55s} {reason}")

    if prune:
        print(f"\nConcrete cull commands ({len(prune)} ollama rm calls):")
        for tag, _ in prune:
            print(f"  ollama rm {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
