"""Pull then probe a sequence of Ollama models against the advisor role.

Targets a remote Ollama over HTTP (default the NAS, ``carldog-nas:11434``)
so the sweep measures models where they will actually run — the
CPU-only NAS, not a GPU desktop. For each model:

  1. Skip if already present on the target (idempotent / resumable)
  2. Pull it onto the target via ``POST /api/pull`` (unless ``--no-pull``)
  3. Run ``tools/probe_advisor.py --base-url ... --timeout-seconds ... --json``
  4. Parse the ``JSON_RESULT`` line: score, per-verdict counts, and the
     per-call latency (max + mean) used to size the production timeout
  5. Optionally delete the model afterward (``--rm-after``) to bound disk

Sister of ``tools/pull_and_probe_assistants.py`` (operator-assistant
role). The candidate list adds math specialists (mathstral, wizardmath,
phi4-mini-reasoning) that were rejected for operator-assistant but are
explicit advisor candidates per ``docs/reference/advisor-llm-models.md``.

The advisor is a 4-hourly daemon, so latency is tolerable — the default
per-call budget is generous (600s) so slow-but-accurate models aren't
cut off mid-generation. The point is to find the most ACCURATE +
schema-faithful model and then read off how long it actually needs.

At the end, print a summary sorted by score (desc), then errors (asc),
then max-call latency (asc).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Force UTF-8 in both the child's print() and the parent's capture --
# Windows defaults to cp1252 which can't represent em-dashes, arrows,
# etc. that the advisor prompt + LLM responses routinely contain.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

_DEFAULT_BASE_URL = "http://carldog-nas:11434"
_DEFAULT_TIMEOUT_SECONDS = 600.0

# Candidate manifest for the advisor sweep. q8_0 throughout for
# apples-to-apples accuracy comparison with the operator-assistant
# sweep + the desktop advisor sweep. Accuracy is ~quant-independent, so
# the winner here is deployed as its q4_K_M variant on the NAS (faster,
# same reasoning); see docs/reference/advisor-llm-models.md.
CANDIDATES: list[str] = [
    # ===== Math specialists (advisor-role candidates) ===== #
    "mathstral:7b",
    "wizard-math:7b",
    "wizard-math:13b",
    "phi4-mini-reasoning:3.8b",
    # ===== Newer-gen general-purpose ===== #
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

# Phase-1 "accuracy leaders + controls" subset for a fast actionable
# answer before committing to the full overnight sweep. Pass via
# --tier1 (or just list them as positional args).
TIER1: list[str] = [
    "llama3.1:8b-instruct-q8_0",  # prior desktop winner (14/18)
    "wizard-math:13b",  # prior 13/18
    "mathstral:7b",  # prior 12/18
    "qwen2.5:7b-instruct-q8_0",  # control: qwen family at 7B
    "qwen2.5:3b-instruct-q8_0",  # control: small qwen (current operator family)
    "llama3.1:8b-instruct-q4_K_M",  # control: the NAS-deployed quant of the leader
]

# Dense models too large to be usable on the CPU-only NAS even as a
# latency-tolerant daemon (<1 tok/s floor per ~/.claude/rules/
# nas-ollama-optimization.md). Skipped with a log line, never silently.
_TOO_BIG_MARKERS = ("30b", "32b", "33b", "34b", "65b", "70b", "72b", "120b", "405b")

RESULTS_DIR = Path("data/advisor_probe_results")


def _too_big_for_nas(tag: str) -> bool:
    low = tag.lower()
    return any(m in low for m in _TOO_BIG_MARKERS)


def _probe_interpreter_ready() -> bool:
    """Can the interpreter that will run the probe subprocess import wobblebot?

    The probe is launched with ``sys.executable``; if the sweep is started
    with a non-project Python (no editable ``wobblebot`` install — e.g. a
    bare ``python`` instead of ``.venv\\Scripts\\python.exe``), every probe
    dies on ``import wobblebot``. Check once up front and fail fast with
    guidance, rather than pulling 45 models and failing every probe.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import wobblebot"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _list_remote(client: httpx.Client, base_url: str) -> set[str]:
    """Return the set of model tags already present on the target."""
    try:
        r = client.get(f"{base_url}/api/tags", timeout=30.0)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return set()
    return {m.get("name", "") for m in data.get("models", [])}


def _pull_remote(
    client: httpx.Client, base_url: str, model: str, timeout: float
) -> tuple[bool, str]:
    """Pull a model onto the target via streaming ``/api/pull``.

    Streams NDJSON progress so a long pull doesn't trip a single
    blocking-read timeout; watches for an ``error`` event or the final
    ``status: success``.
    """
    t0 = time.monotonic()
    # Read-timeout is per-event; Ollama emits progress continuously, so
    # a generous per-read window covers slow disks without a hard cap on
    # total pull time.
    pull_timeout = httpx.Timeout(connect=15.0, read=max(300.0, timeout), write=15.0, pool=15.0)
    last_status = ""
    try:
        with client.stream(
            "POST",
            f"{base_url}/api/pull",
            json={"model": model, "stream": True},
            timeout=pull_timeout,
        ) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in evt:
                    return False, str(evt["error"])[:200]
                last_status = evt.get("status", last_status)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]
    elapsed = time.monotonic() - t0
    if last_status == "success":
        return True, f"pulled in {elapsed:.0f}s"
    # A stream that ends WITHOUT a terminal "success" (connection drop,
    # graceful server shutdown, disk-full close, proxy cut at a chunk
    # boundary) left the model missing/partial. Treat as failure — never
    # report an incomplete pull as success, or the caller probes a
    # missing model and launders 12 ERRORs into a "0/36, status OK" row.
    return False, f"incomplete pull after {elapsed:.0f}s (last status: {last_status or 'none'})"


def _delete_remote(client: httpx.Client, base_url: str, model: str) -> None:
    """Best-effort delete a model from the target (for --rm-after)."""
    for key in ("model", "name"):  # newer Ollama uses "model", older "name"
        try:
            r = client.request("DELETE", f"{base_url}/api/delete", json={key: model}, timeout=60.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            continue


def run_probe(
    model: str, base_url: str, timeout_seconds: float, prompt_file: str | None = None
) -> dict:
    """Run probe_advisor.py against the model on the target; parse JSON_RESULT."""
    print(f"  probing {model}...", flush=True)
    t0 = time.monotonic()
    # Generous subprocess budget: up to ~14 fixtures' worth of per-call
    # timeout plus margin, so a slow model isn't killed before its
    # latency is recorded.
    subprocess_timeout = int(timeout_seconds * 14 + 120)
    cmd = [
        sys.executable,
        "tools/probe_advisor.py",
        "--model",
        model,
        "--base-url",
        base_url,
        "--timeout-seconds",
        str(timeout_seconds),
        "--json",
    ]
    if prompt_file:
        cmd += ["--prompt-file", prompt_file]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_UTF8_ENV,
            timeout=subprocess_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "probe subprocess timed out", "elapsed": -1}
    elapsed = time.monotonic() - t0
    output = (r.stdout or "") + ("\n--- stderr ---\n" + r.stderr if r.stderr else "")
    out_path = RESULTS_DIR / f"{model.replace(':', '_').replace('/', '_')}.txt"
    out_path.write_text(output, encoding="utf-8")
    parsed = _parse_probe_output(output)
    parsed["elapsed"] = elapsed
    return parsed


def _parse_probe_output(output: str) -> dict:
    """Extract the JSON_RESULT blob emitted by probe_advisor.py --json."""
    marker = "JSON_RESULT: "
    line = next(
        (ln for ln in reversed(output.splitlines()) if ln.startswith(marker)),
        None,
    )
    if line is None:
        return {"ok": False, "error": "no JSON_RESULT line in probe output"}
    try:
        blob = json.loads(line[len(marker) :])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"bad JSON_RESULT: {exc}"}
    blob["ok"] = True
    return blob


def _write_summary(summary: list[dict]) -> None:
    """Persist the running summary atomically so an interrupted sweep isn't lost.

    Write to a temp file then os.replace() so a kill mid-write can't leave
    summary.json truncated — a truncated file would make _load_prior_summary
    silently drop every prior result on the next resume.
    """
    path = RESULTS_DIR / "summary.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_prior_summary() -> list[dict]:
    """Load a prior run's summary.json for resume, or [] if absent/unreadable.

    Makes the sweep genuinely resumable + --rm-after-compatible: a
    restart preserves prior rows and skips any model already scored OK,
    independent of whether --rm-after deleted it from disk. (Without
    this the prior summary was overwritten on every start, silently
    dropping earlier scorers from the final ranking.)
    """
    path = RESULTS_DIR / "summary.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Distinguish "present but unreadable" from "absent" — loudly, so a
        # truncated file isn't silently mistaken for a fresh start.
        print(
            f"  WARNING: summary.json present but unreadable ({exc}); "
            "starting without prior results",
            flush=True,
        )
        return []
    return data if isinstance(data, list) else []


def _print_summary(summary: list[dict]) -> None:
    print(f"\n\n{'='*96}\nSUMMARY\n{'='*96}", flush=True)
    header = (
        f"{'Model':46s}  {'Score':>7s}  "
        f"{'OK':>3s} {'OVR':>3s} {'OTR':>3s} {'MIS':>3s} {'WR':>3s} {'ERR':>3s}  "
        f"{'MaxCall':>8s} {'MeanCall':>8s}"
    )
    print(header)
    print("-" * len(header))
    success = [s for s in summary if s.get("status") == "OK"]
    success.sort(key=lambda s: (-s["score"], s["errors"], s.get("max_call_seconds", 1e9)))
    failed = [s for s in summary if s.get("status") != "OK"]
    for s in success:
        v = s["verdicts"]
        score_str = f"{s['score']}/{s['max_score']}"
        print(
            f"{s['model']:46s}  {score_str:>7s}  "
            f"{v.get('OK',0):>3d} {v.get('OVERSHOOT',0):>3d} {v.get('OVERTRADE',0):>3d} "
            f"{v.get('MISS',0):>3d} {v.get('WRONG',0):>3d} {v.get('ERROR',0):>3d}  "
            f"{s.get('max_call_seconds',0):>7.0f}s {s.get('mean_call_seconds',0):>7.0f}s"
        )
    for s in failed:
        print(f"{s['model']:46s}  {s['status']}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep Ollama advisor candidates against the 12-fixture accuracy "
            "battery on a remote (default NAS) Ollama. No positional args runs "
            "the full ~45-model CANDIDATES list; pass tags for a subset, or "
            "--tier1 for the accuracy-leaders-plus-controls fast pass."
        ),
    )
    parser.add_argument(
        "models",
        nargs="*",
        help="Explicit Ollama tags to probe. Overrides CANDIDATES.",
    )
    parser.add_argument(
        "--tier1",
        action="store_true",
        help=f"Run the accuracy-leaders + controls subset ({len(TIER1)} models) for a fast answer.",
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"Target Ollama base URL. Default: {_DEFAULT_BASE_URL}.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-call probe timeout. Default: {_DEFAULT_TIMEOUT_SECONDS:.0f}s.",
    )
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="Assume candidates are already present on the target; skip pulling.",
    )
    parser.add_argument(
        "--rm-after",
        action="store_true",
        help="Delete each model from the target after probing, to bound disk use.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any existing summary.json and start a fresh sweep (default: resume).",
    )
    parser.add_argument(
        "--prompt-file",
        default=None,
        help=(
            "System-prompt path passed through to every probe, to A/B a "
            "prompt variant against the battery. "
            "Default: the probe's own default (config/prompts/quant.md)."
        ),
    )
    parser.add_argument(
        "--results-dir",
        default=str(RESULTS_DIR),
        help=(
            "Directory for summary.json + per-model .txt. Use a separate dir to run an "
            "isolated experiment (e.g. a desktop quant comparison against --base-url "
            f"http://localhost:11434) without clobbering another sweep. Default: {RESULTS_DIR}."
        ),
    )
    return parser.parse_args(argv)


def _select_models(args: argparse.Namespace) -> list[str]:
    if args.models:
        return args.models
    if args.tier1:
        return TIER1
    return CANDIDATES


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(argv)
    models = _select_models(args)

    global RESULTS_DIR
    RESULTS_DIR = Path(args.results_dir)

    if not _probe_interpreter_ready():
        print(
            f"error: this interpreter ({sys.executable}) cannot import 'wobblebot', "
            "which the probe subprocess requires. Run the sweep with the project venv:\n"
            "    .venv\\Scripts\\python.exe tools/pull_and_probe_advisors.py ...",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Resume: keep prior rows (keyed by model) and skip anything already
    # scored OK, so a restart neither loses earlier results nor re-probes
    # completed models — works even when --rm-after deleted them on disk.
    prior = [] if args.fresh else _load_prior_summary()
    summary_by_model: dict[str, dict] = {e["model"]: e for e in prior if "model" in e}
    done = {m for m, e in summary_by_model.items() if e.get("status") == "OK"}

    def snapshot() -> list[dict]:
        return list(summary_by_model.values())

    client = httpx.Client()
    print(f"# target: {args.base_url}  per-call timeout: {args.timeout_seconds:.0f}s")
    print(
        f"# models: {len(models)}  pull: {not args.no_pull}  rm-after: {args.rm_after}  "
        f"resumed: {len(done)} already scored"
    )
    try:
        present = set() if args.no_pull else _list_remote(client, args.base_url)
        for i, model in enumerate(models, 1):
            print(f"\n[{i}/{len(models)}] {model}", flush=True)
            if model in done:
                print("  -> skip (already scored in summary.json; --fresh to redo)", flush=True)
                continue
            if _too_big_for_nas(model):
                print("  -> SKIP (too large for CPU-only NAS; <1 tok/s floor)", flush=True)
                summary_by_model[model] = {"model": model, "status": "SKIP_TOO_BIG"}
                _write_summary(snapshot())
                continue
            if not args.no_pull and model not in present:
                ok, msg = _pull_remote(client, args.base_url, model, args.timeout_seconds)
                if not ok:
                    print(f"  -> SKIP (pull failed: {msg})", flush=True)
                    summary_by_model[model] = {"model": model, "status": f"PULL_FAILED: {msg}"}
                    _write_summary(snapshot())
                    continue
                present.add(model)
                print(f"  {msg}", flush=True)
            else:
                print("  (already present)" if not args.no_pull else "  (pull skipped)", flush=True)

            result = run_probe(model, args.base_url, args.timeout_seconds, args.prompt_file)
            if args.rm_after:
                _delete_remote(client, args.base_url, model)
                present.discard(model)
                print("  (removed from target)", flush=True)
            if not result.get("ok"):
                print(f"  -> PROBE FAILED: {result.get('error')}", flush=True)
                summary_by_model[model] = {
                    "model": model,
                    "status": f"PROBE_FAILED: {result.get('error')}",
                }
                _write_summary(snapshot())
                continue
            print(
                f"  -> {result['score']}/{result['max_score']} score, "
                f"{result['errors']} errors, max_call={result.get('max_call_seconds',0):.0f}s, "
                f"{result['elapsed']:.0f}s total",
                flush=True,
            )
            summary_by_model[model] = {
                "model": model,
                "status": "OK",
                "score": result["score"],
                "max_score": result["max_score"],
                "errors": result["errors"],
                "verdicts": result["verdicts"],
                "max_call_seconds": result.get("max_call_seconds", 0),
                "mean_call_seconds": result.get("mean_call_seconds", 0),
                "elapsed": result["elapsed"],
            }
            done.add(model)
            _write_summary(snapshot())
    finally:
        client.close()
        _print_summary(snapshot())
        _write_summary(snapshot())
        print(f"\nFull per-model outputs: {RESULTS_DIR}/*.txt")
        print(f"Summary JSON: {RESULTS_DIR}/summary.json")


if __name__ == "__main__":
    main()
