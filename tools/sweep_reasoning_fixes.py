"""Sweep driver for the 2026-05-25 reasoning-model probe-fix evaluation.

Runs six informative probe configurations across three reasoning-tuned
models, captures stdout per run, and prints a final summary table.
The configurations are chosen to isolate which fix (compact prompt vs
``format=json``) is load-bearing per model size:

1. phi4-mini-reasoning:3.8b-fp16, operator, compact + force_json
2. phi4-mini-reasoning:3.8b-fp16, advisor, force_json only (quant.md
   is already 1288 chars -- does prompt length matter for advisor?)
3. phi4-mini-reasoning:3.8b-fp16, advisor, compact + force_json
4. phi4-reasoning:14b-plus-q8_0, advisor, force_json only
5. deepseek-r1:14b-qwen-distill-q8_0, operator, force_json only
6. deepseek-r1:14b-qwen-distill-q8_0, advisor, force_json only

Per-run output saved to ``data/reasoning_sweep_results/<slug>.txt``.
Summary written to ``data/reasoning_sweep_results/SUMMARY.md``.

Run with: ``python tools/sweep_reasoning_fixes.py``
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    slug: str
    label: str
    model: str
    role: str
    args: tuple[str, ...]


CONFIGS: tuple[Config, ...] = (
    Config(
        slug="phi4-mini_operator_compact_json",
        label="phi4-mini-reasoning operator + compact + force_json",
        model="phi4-mini-reasoning:3.8b-fp16",
        role="operator",
        args=(
            "--prompt-file",
            "config/prompts/operator-compact.md",
            "--force-json",
            "--bypass-suitability-check",
            "--skip-multi-turn",
        ),
    ),
    Config(
        slug="phi4-mini_advisor_json_only",
        label="phi4-mini-reasoning advisor + force_json only (standard quant.md)",
        model="phi4-mini-reasoning:3.8b-fp16",
        role="advisor",
        args=("--force-json",),
    ),
    Config(
        slug="phi4-mini_advisor_compact_json",
        label="phi4-mini-reasoning advisor + compact + force_json",
        model="phi4-mini-reasoning:3.8b-fp16",
        role="advisor",
        args=(
            "--prompt-file",
            "config/prompts/quant-compact.md",
            "--force-json",
        ),
    ),
    Config(
        slug="phi4-reasoning-14b_advisor_json_only",
        label="phi4-reasoning:14b-plus advisor + force_json only (standard quant.md)",
        model="phi4-reasoning:14b-plus-q8_0",
        role="advisor",
        args=("--force-json",),
    ),
    Config(
        slug="deepseek-r1-14b_operator_json_only",
        label="deepseek-r1:14b-qwen-distill operator + force_json",
        model="deepseek-r1:14b-qwen-distill-q8_0",
        role="operator",
        args=("--force-json", "--skip-multi-turn"),
    ),
    Config(
        slug="deepseek-r1-14b_advisor_json_only",
        label="deepseek-r1:14b-qwen-distill advisor + force_json",
        model="deepseek-r1:14b-qwen-distill-q8_0",
        role="advisor",
        args=("--force-json",),
    ),
)


# probe_assistant.py prints one ">>> <msg>" + one "    <result>" per message
# with no aggregate score line. We derive accuracy by counting non-ERROR
# results. probe_advisor.py emits a final "TOTAL: X/Y  errors:Z" line.
_ADVISOR_SCORE_RE = re.compile(r"TOTAL:\s*(\d+)\s*/\s*(\d+)", re.MULTILINE)
_ADVISOR_ERRORS_RE = re.compile(r"errors:\s*(\d+)", re.MULTILINE)
_OPERATOR_MESSAGE_RE = re.compile(r"^>>>\s", re.MULTILINE)
_OPERATOR_ERROR_RE = re.compile(r"^\s+ERROR:", re.MULTILINE)


def extract_score(output: str, role: str) -> tuple[int | None, int | None, int | None]:
    """Return (score, max_score, errors). Any unparseable field is None."""
    if role == "advisor":
        score_match = _ADVISOR_SCORE_RE.search(output)
        errors_match = _ADVISOR_ERRORS_RE.search(output)
        score = int(score_match.group(1)) if score_match else None
        max_score = int(score_match.group(2)) if score_match else None
        errors = int(errors_match.group(1)) if errors_match else None
        return score, max_score, errors
    # operator: count messages + errors; "score" is non-error parses.
    total = len(_OPERATOR_MESSAGE_RE.findall(output))
    errors = len(_OPERATOR_ERROR_RE.findall(output))
    if total == 0:
        return None, None, errors if errors else None
    return total - errors, total, errors


def run_one(config: Config, out_dir: Path) -> tuple[float, str]:
    """Run one probe configuration; return (elapsed_seconds, captured_stdout)."""
    script = "tools/probe_assistant.py" if config.role == "operator" else "tools/probe_advisor.py"
    cmd = [
        sys.executable,
        script,
        "--model",
        config.model,
        *config.args,
    ]
    print(f"\n{'=' * 80}", flush=True)
    print(f"RUN: {config.label}", flush=True)
    print(f"CMD: {' '.join(cmd)}", flush=True)
    print(f"{'=' * 80}", flush=True)
    start = time.monotonic()
    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed = time.monotonic() - start
    combined = result.stdout + ("\n--- STDERR ---\n" + result.stderr if result.stderr else "")
    out_path = out_dir / f"{config.slug}.txt"
    out_path.write_text(combined, encoding="utf-8")
    print(combined, flush=True)
    print(f"\nelapsed: {elapsed:.1f}s -> wrote {out_path}", flush=True)
    return elapsed, combined


def write_summary(
    out_dir: Path,
    rows: list[tuple[Config, float, int | None, int | None, int | None]],
) -> None:
    lines = [
        "# Reasoning-model probe-fix sweep — 2026-05-25",
        "",
        "Configurations run by `tools/sweep_reasoning_fixes.py`. Each row's full",
        "stdout is in the sibling `.txt` file by slug.",
        "",
        "| Model | Role | Config | Score | Errors | Elapsed |",
        "|---|---|---|---|---|---|",
    ]
    for cfg, elapsed, score, max_score, errors in rows:
        score_cell = (
            f"{score}/{max_score}" if score is not None and max_score is not None else "—"
        )
        err_cell = str(errors) if errors is not None else "—"
        config_cell = " ".join(cfg.args) or "(baseline)"
        lines.append(
            f"| `{cfg.model}` | {cfg.role} | `{config_cell}` | {score_cell} | "
            f"{err_cell} | {elapsed:.0f}s |"
        )
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "data" / "reasoning_sweep_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}", flush=True)
    print(f"Total configurations: {len(CONFIGS)}", flush=True)
    rows: list[tuple[Config, float, int | None, int | None, int | None]] = []
    for cfg in CONFIGS:
        elapsed, output = run_one(cfg, out_dir)
        score, max_score, errors = extract_score(output, cfg.role)
        rows.append((cfg, elapsed, score, max_score, errors))
        write_summary(out_dir, rows)  # incremental write so interruption keeps progress
    print("\n" + "=" * 80, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 80, flush=True)
    for cfg, elapsed, score, max_score, errors in rows:
        score_str = (
            f"{score}/{max_score}" if score is not None and max_score is not None else "—"
        )
        err_str = f"{errors} errors" if errors is not None else "errors?"
        print(f"  {cfg.label}: {score_str}, {err_str}, {elapsed:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
