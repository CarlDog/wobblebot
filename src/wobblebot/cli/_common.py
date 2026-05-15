"""Internal CLI helpers shared across cli/live, cli/shadow, etc.

Underscore prefix marks this as a layer-internal module — operators
should not invoke it. The audit's slice-4b scaffolding ended up here:
helpers for converting argparse flags to YAML override dicts so each
CLI's wiring stays declarative.

Pattern usage in a CLI's ``main()``::

    parser = argparse.ArgumentParser(...)
    add_config_args(parser)  # adds --config and --profile
    parser.add_argument("--tick-seconds", type=float, default=None)
    parser.add_argument("--symbols", default=None)
    args = parser.parse_args()

    overrides = collect_overrides(args, "live", {
        "tick_seconds": ("tick_seconds", _identity),
        "symbols":      ("symbols",      _parse_symbol_csv),
    })
    config = load_resolved_config(
        config_path=args.config,
        profile_name=args.profile,
        cli_overrides=overrides,
    )

Every CLI flag whose default is ``None`` is treated as "not passed."
The collector skips those, so operator-omitted flags inherit from
YAML / profile / Pydantic defaults rather than being clobbered by
sentinel ``None`` values.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from dotenv import find_dotenv, load_dotenv

T = TypeVar("T")


def load_operator_env() -> None:
    """Load ``.env`` from the operator's working directory (or ancestors).

    Calls ``find_dotenv(usecwd=True)`` so python-dotenv walks UP from
    the operator's cwd rather than from this source file's location.
    The default behavior surprised the deprived-env walkthrough: a CLI
    run from ``/tmp/`` was still picking up the dev repo's ``.env``
    because python-dotenv defaults to traversing the call-frame's
    source-file directory. Explicit cwd-based discovery matches what
    most operators expect ("I'm in this dir, look for .env from here").

    Safe to call multiple times; ``load_dotenv`` is idempotent and
    won't override env vars already set.
    """
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(dotenv_path=found)


def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--config`` and ``--profile`` to ``parser``.

    Both default to ``None`` so the runtime layer's discovery (and the
    resolver's "no profile" path) take over when unset.
    """
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to settings YAML file. Defaults to config/settings.yml "
        "(falling back to config/settings.example.yml if not present).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Named profile from the YAML's `profiles:` block to apply.",
    )


def collect_overrides(
    args: argparse.Namespace,
    section: str,
    field_map: dict[str, tuple[str, Callable[[Any], Any]]],
) -> dict[str, Any]:
    """Build a ``{section: {yaml_field: value}}`` dict from explicit args.

    ``field_map`` maps argparse attr names to ``(yaml_field, converter)``
    tuples. The converter transforms the raw argparse value into the
    YAML-compatible form (e.g. comma-separated string → list of strings
    for ``--symbols``). Values of ``None`` are treated as "not passed"
    and skipped, regardless of converter.
    """
    section_overrides: dict[str, Any] = {}
    for arg_attr, (yaml_field, convert) in field_map.items():
        raw = getattr(args, arg_attr, None)
        if raw is None:
            continue
        section_overrides[yaml_field] = convert(raw)
    return {section: section_overrides} if section_overrides else {}


def parse_symbol_csv(raw: str) -> list[str]:
    """``"BTC/USD, ETH/USD"`` → ``["BTC/USD", "ETH/USD"]``.

    Trims whitespace, drops empty entries (e.g. from a trailing comma).
    Returns ``list[str]`` because Pydantic ``LiveConfig.symbols`` accepts
    strings and parses to ``Symbol`` via its field validator.
    """
    return [s.strip() for s in raw.split(",") if s.strip()]


def identity(value: T) -> T:
    """No-op converter — passes the argparse value through unchanged."""
    return value
