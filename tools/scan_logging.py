"""Audit log-call quality against docs/implementation/logging-conventions.md.

Two independent checks. They look similar and are not — the second
exists because the first is structurally blind to it.

**rule1 — missing data.** A *static* message string paired with a
non-empty ``extra=``. The plain formatter renders message-only, so
everything in ``extra`` is invisible to an operator tailing the
container log. This has a mechanical signature, which is what makes the
audit a greppable scan rather than a matter of taste.

**decimal — unreadable data.** A money-ish value interpolated into a
message without a formatter. These lines PASS ``rule1`` — the data is
right there — and are still unreadable, because a ``Decimal`` division
keeps 28 significant digits and a round ``Decimal`` renders as ``1E+2``.
Found live in production on 2026-08-10:

    below avg cost 73390.78543435964243143764881 (8.742335…% loss)

Heuristic by necessity (no type information at scan time), so this one
is a **review list, not a gate** — ints and ``.total_seconds()`` floats
trip it. Judge each hit; wrap the real Decimals with
``domain.value_objects.fmt_decimal``.

Usage::

    python -m tools.scan_logging                # rule1, exits 1 on any hit
    python -m tools.scan_logging --check decimal
    python -m tools.scan_logging --check all --verbose

``rule1`` exits non-zero so it can gate; ``decimal`` always exits 0.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections.abc import Iterable, Sequence

# Logging methods worth auditing. ``exception`` included: it renders the
# same message the operator reads.
_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})

# Substrings that mark a name as a logger. Deliberately loose — a
# false positive here only means an extra line to review.
_LOGGER_HINTS = ("LOGGER", "LOG")

# Names suggesting a value whose raw Decimal rendering hurts.
_MONEY_HINTS = (
    "price",
    "amount",
    "cost",
    "balance",
    "pnl",
    "total",
    "usd",
    "spacing",
    "percentage",
    "fee",
    "notional",
    "profit",
    "loss",
)

# Wrappers that already make a value display-safe.
_FORMATTERS = ("fmt_decimal", "_fmt_money", "_fmt_pct", "quantize", "round", "len", "str")

_DEFAULT_ROOT = pathlib.Path("src/wobblebot")


class Finding:
    """One audited log call."""

    def __init__(self, path: pathlib.Path, line: int, level: str, detail: str) -> None:
        self.path = path
        self.line = line
        self.level = level
        self.detail = detail

    def __str__(self) -> str:
        rel = str(self.path).replace("\\", "/")
        return f"{rel}:{self.line} [{self.level}] {self.detail}"


def _is_logger_call(node: ast.Call) -> str | None:
    """Return the level name if ``node`` looks like a logger call."""
    fn = node.func
    if not isinstance(fn, ast.Attribute) or fn.attr not in _LEVELS:
        return None
    owner = str(getattr(fn.value, "id", "") or getattr(fn.value, "attr", "")).upper()
    if not any(hint in owner for hint in _LOGGER_HINTS):
        return None
    return fn.attr


def _extra_keys(node: ast.Call) -> list[str]:
    for kw in node.keywords:
        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
            return [str(k.value) for k in kw.value.keys if isinstance(k, ast.Constant)]
    return []


def _iter_python_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    yield from sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def scan_rule1(root: pathlib.Path) -> list[Finding]:
    """Static message + non-empty ``extra=`` — data the operator can't see."""
    findings: list[Finding] = []
    for path in _iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            level = _is_logger_call(node)
            if level is None or not node.args:
                continue
            message = node.args[0]
            # More than one arg means the message interpolates something.
            if len(node.args) > 1:
                continue
            if not isinstance(message, ast.Constant) or not isinstance(message.value, str):
                continue
            keys = _extra_keys(node)
            if not keys:
                continue
            findings.append(
                Finding(path, node.lineno, level, f"extra={keys} never reaches the message")
            )
    return findings


def scan_decimal(root: pathlib.Path) -> list[Finding]:
    """Money-ish value interpolated without a display formatter."""
    findings: list[Finding] = []
    for path in _iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            level = _is_logger_call(node)
            if level is None:
                continue
            for arg in node.args[1:]:  # the message itself is args[0]
                expr = ast.unparse(arg)
                lowered = expr.lower()
                if not any(hint in lowered for hint in _MONEY_HINTS):
                    continue
                if any(f in lowered for f in _FORMATTERS):
                    continue
                findings.append(Finding(path, node.lineno, level, expr))
    return findings


def _report(title: str, findings: Sequence[Finding], *, verbose: bool) -> None:
    print(f"\n=== {title}: {len(findings)} ===")
    if not findings:
        return
    by_file: dict[str, int] = {}
    for f in findings:
        key = str(f.path).replace("\\", "/")
        by_file[key] = by_file.get(key, 0) + 1
    for name, count in sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{count:4d}  {name}")
    if verbose:
        print()
        for f in findings:
            print(f"  {f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        choices=("rule1", "decimal", "all"),
        default="rule1",
        help="Which audit to run (default: rule1, the gate-able one).",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=_DEFAULT_ROOT,
        help=f"Package root to scan (default: {_DEFAULT_ROOT}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List every finding, not just per-file counts.",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"root does not exist: {args.root}", file=sys.stderr)
        return 2

    exit_code = 0
    if args.check in ("rule1", "all"):
        findings = scan_rule1(args.root)
        _report("rule 1 — data stranded in extra= (GATE)", findings, verbose=args.verbose)
        if findings:
            print(
                "\nEach message must answer what/which/how-much on its own; "
                "extra= duplicates for JSON consumers, it never replaces.",
            )
            exit_code = 1
    if args.check in ("decimal", "all"):
        findings = scan_decimal(args.root)
        _report("decimal readability — review list, NOT a gate", findings, verbose=args.verbose)
        if findings:
            print(
                "\nJudge each: wrap real Decimals with fmt_decimal (add "
                "max_significant= for division results). Ints and float "
                "durations trip this heuristic and are fine as-is.",
            )
    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
