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
import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from dotenv import find_dotenv, load_dotenv

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.ports.notifier import Notification, NotifierPort
from wobblebot.ports.storage import StoragePort

T = TypeVar("T")

_NOTIFY_LOGGER = logging.getLogger("wobblebot.cli.notify")
_HEARTBEAT_LOGGER = logging.getLogger("wobblebot.cli.heartbeat")


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


async def emit_heartbeat(operator_storage: StoragePort | None, daemon_name: str) -> None:
    """Best-effort heartbeat emit. Failures logged, never raised.

    Stage 8.4.E follow-up — backs the ``/health`` page's per-daemon
    liveness view. Each long-running daemon calls this at the top of
    its tick/poll loop body so the operator.db ``daemon_heartbeats``
    table has a "loop ran" timestamp for the freshness reader.

    Args:
        operator_storage: Where to write the row. ``None`` is a no-op
            (operator ran without ``operator_db`` configured).
        daemon_name: Canonical name (e.g. ``"cli/live"``) — must match
            what ``services/daemon_health.fetch_daemon_freshness``
            looks for.
    """
    if operator_storage is None:
        return
    try:
        await operator_storage.upsert_daemon_heartbeat(daemon_name, datetime.now(UTC))
    except WobbleBotPortError as exc:
        _HEARTBEAT_LOGGER.warning(
            "heartbeat emit failed; continuing",
            extra={"daemon": daemon_name, "error": str(exc)},
        )


async def notify(
    notifier: NotifierPort | None,
    *,
    level: str,
    title: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Best-effort outbound notification emit. Failures logged, never raised.

    Phase 5 notifications are forensic ledger entries — losing one
    must NEVER break the trading or harvester loop. ``cli/live`` and
    ``cli/harvest`` call this from their session / fill / cap-trip /
    proposal / withdrawal hooks; ``cli/operator`` (Stage 5.6) reads
    the persisted rows and forwards them to Discord.

    Args:
        notifier: Where to write the row. ``None`` is a no-op (operator
            ran without ``operator_db`` configured).
        level: ``info | warning | error | critical``.
        title: Short human label; appears as the Discord embed title.
        message: Longer human message; appears as the embed description.
        context: Optional structured context dict (rendered as embed
            fields by the cli/operator forwarder).
    """
    if notifier is None:
        return
    try:
        await notifier.send_notification(
            Notification(
                level=level,
                title=title,
                message=message,
                timestamp=Timestamp(dt=datetime.now(UTC)),
                context=context or {},
            )
        )
    except WobbleBotPortError as exc:
        _NOTIFY_LOGGER.warning(
            "notification emit failed; continuing",
            extra={"title": title, "error": str(exc)},
        )


async def run_poll_loop(
    do_one_cycle: Callable[[], Any],
    *,
    interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """Run ``do_one_cycle`` on a fixed interval until ``stop_event`` is set.

    Shared shape for the five long-running CLI daemons (``cli/observe``,
    ``cli/news``, ``cli/advise``, ``cli/harvest``, plus ``cli/operator``'s
    notification forwarder + TTL expirer). Each daemon previously
    hand-rolled the same body — Stage 8.0.C consolidates so shutdown
    discipline lives in one place.

    Loop semantics:

    - Outer condition: ``while not stop_event.is_set()``.
    - Per-cycle: ``await do_one_cycle()`` — any exception propagates
      back to the caller. The caller's ``try/finally`` handles
      session-end logging; per-cycle fault isolation is the
      caller's job (e.g. ``cli/observe`` catches ``WobbleBotPortError``
      inside ``_poll_prices`` itself).
    - Interruptible sleep: ``await asyncio.wait_for(stop_event.wait(),
      timeout=interval_seconds)``. SIGINT/SIGTERM sets the event;
      the sleep returns immediately and the next loop check exits.
      Without the interruptible sleep an operator pressing Ctrl-C
      mid-interval would wait up to ``interval_seconds`` for clean
      shutdown — unacceptable for a 1-hour harvest interval.

    Signal handler installation stays at the CLI level — each daemon
    owns its own ``signal.SIGINT`` / ``signal.SIGTERM`` wiring that
    flips the shared ``stop_event``.

    Args:
        do_one_cycle: Async callable that does one tick of work. Its
            return value is ignored; raising propagates.
        interval_seconds: Seconds between consecutive cycle starts
            (measured from cycle-finish to next cycle-start, since
            the sleep happens after the work).
        stop_event: External shutdown signal. Set by the daemon's
            signal handler.
    """
    while not stop_event.is_set():
        await do_one_cycle()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
