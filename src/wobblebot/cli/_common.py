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
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, Protocol, TypeVar

from dotenv import find_dotenv, load_dotenv

from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.exceptions import WobbleBotPortError
from wobblebot.ports.notifier import Notification, NotifierPort
from wobblebot.ports.storage import StoragePort

T = TypeVar("T")

_NOTIFY_LOGGER = logging.getLogger("wobblebot.cli.notify")
_HEARTBEAT_LOGGER = logging.getLogger("wobblebot.cli.heartbeat")
_SHUTDOWN_LOGGER = logging.getLogger("wobblebot.cli.shutdown")

ShutdownPhase = tuple[str, Callable[[], Awaitable[None]]]
"""(phase_name, async_callable) pair consumed by ``safe_shutdown``.

``phase_name`` appears verbatim in the shutdown-hang WARNING log so the
operator can immediately see WHICH cleanup hung. Names should be short,
underscore-cased, and stable across releases.
"""


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


class SymbolPartitioner(Protocol):
    """Structural type for adapters that can partition a symbol list.

    Lets ``partition_or_exit`` accept any adapter implementing the
    ``KrakenAdapter.partition_known_symbols`` contract without
    importing the adapter module directly (preserves the
    cli/_common-doesn't-depend-on-adapters layering).
    """

    async def partition_known_symbols(
        self, symbols: Iterable[Symbol]
    ) -> tuple[list[Symbol], list[Symbol]]: ...


async def partition_or_exit(
    adapter: SymbolPartitioner,
    symbols: Iterable[Symbol],
    *,
    logger: logging.Logger,
    cleanups: list[ShutdownPhase],
) -> int | None:
    """Validate ``symbols`` against the adapter's tradeable pairs.

    Hits the adapter's AssetPairs cache once (or its equivalent). On
    success returns ``None`` — the caller proceeds with whatever
    they had. On hard failure (AssetPairs fetch raised, or every
    requested symbol is unknown to the exchange) logs an error,
    drains the supplied ``cleanups`` via :func:`safe_shutdown`, and
    returns ``2`` for the caller to bubble up as the process exit
    code.

    Mid-case (some symbols unknown but at least one is good): logs a
    WARNING naming the bad symbols and returns ``None``. Per-tick
    fault isolation in each daemon's poll loop absorbs the
    subsequent failed polls; the operator updates the config and
    restarts to silence the warning.

    Extracted 2026-05-23 from cli/observe + cli/live + cli/shadow
    where the same 17-line block had just been shipped 3x in
    commit 0007fc3 — the audit's #3 finding.

    Args:
        adapter: Anything implementing the
            ``partition_known_symbols`` Protocol — typically
            ``KrakenAdapter``.
        symbols: The configured symbol list (e.g.
            ``config.observe.symbols``).
        logger: Daemon's logger so the WARN/ERROR lines land in the
            right namespace.
        cleanups: ``(phase_name, async_callable)`` pairs, same shape
            ``safe_shutdown`` accepts. Run only on hard failure;
            successful path leaves resource cleanup to the daemon's
            normal ``finally`` block.

    Returns:
        ``None`` on success, ``2`` on hard failure. Idiomatic usage::

            exit_code = await partition_or_exit(adapter, symbols, ...)
            if exit_code is not None:
                return exit_code
    """
    try:
        known, unknown = await adapter.partition_known_symbols(symbols)
    except WobbleBotPortError as exc:
        logger.error(
            "Kraken AssetPairs fetch failed at startup",
            extra={"error": str(exc)},
        )
        await safe_shutdown(cleanups, logger=logger)
        return 2
    if unknown:
        logger.warning(
            "Kraken does not list these symbols; per-tick polls will fail",
            extra={"unknown": [str(s) for s in unknown]},
        )
    if not known:
        logger.error("no tradeable Kraken symbols remain; exiting")
        await safe_shutdown(cleanups, logger=logger)
        return 2
    return None


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
    *,
    logger: logging.Logger,
) -> None:
    """Wire SIGINT + SIGTERM to ``stop_event.set()`` on ``loop``.

    Shared shape extracted 2026-05-23 — previously every long-running
    daemon hand-rolled the same 8-line function. Centralizing here
    means a fix (e.g. adding SIGHUP, restructuring the
    ``NotImplementedError`` early-out so SIGTERM gets a chance after
    SIGINT fails on Windows) lands once.

    The ``NotImplementedError`` catch is the Windows guard:
    ``add_signal_handler`` raises on the Proactor loop. We ``return``
    after the first miss so SIGTERM isn't attempted either — preserves
    pre-extraction behavior. If Windows signal handling ever matters,
    fix it here once instead of in 8 places.

    Args:
        loop: The running asyncio loop (typically
            ``asyncio.get_running_loop()``).
        stop_event: The shared shutdown event the daemon's poll loop
            checks. Flipped to True on signal receipt.
        logger: Daemon's logger so the "signal received" line lands in
            the right namespace (``wobblebot.cli.live`` vs
            ``wobblebot.cli.observe`` etc.).
    """

    def _set_stop() -> None:
        logger.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


def run_with_clean_exit(
    coro: Coroutine[Any, Any, int],
    *,
    logger: logging.Logger,
) -> NoReturn:
    """Run ``coro`` via ``asyncio.run``; on completion ``os._exit`` the rc.

    Shared shape extracted 2026-05-23 — replaces the boilerplate at
    the bottom of every long-running daemon's ``main()``: try/except
    ``KeyboardInterrupt`` + flush stdio + ``os._exit(rc)``. Without
    ``os._exit`` non-daemon library threads (httpx connection pool,
    discord.py heartbeat, uvicorn worker) can keep the interpreter
    alive after the asyncio loop has finished, leaving the operator's
    terminal hung. The cli/web Ctrl-C hang surfaced 2026-05-23 (commit
    ``e3a11ce``) was this exact failure mode.

    DO NOT use for one-shot CLIs that return a value (cli/apply,
    cli/preflight, cli/status, cli/sandbox, cli/recalibrate). Those
    don't spawn long-running connection pools and ``os._exit`` would
    bypass any pending atexit hooks unnecessarily.

    Args:
        coro: The ``_main_async(config)`` coroutine the daemon would
            otherwise pass to ``asyncio.run``.
        logger: Daemon's logger for the KeyboardInterrupt info line.

    Does not return — always exits via ``os._exit``.
    """
    try:
        rc = asyncio.run(coro)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt at top level; exiting clean")
        rc = 0
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


async def safe_shutdown(
    cleanups: list[ShutdownPhase],
    *,
    timeout_seconds: float = 10.0,
    logger: logging.Logger | None = None,
) -> None:
    """Run a list of cleanup phases with a wall-clock timeout escape valve.

    Each cleanup is a ``(phase_name, async_callable)`` tuple. Phases run
    sequentially in list order. If the whole sequence doesn't complete
    within ``timeout_seconds``, the helper logs a WARNING naming the
    in-progress phase, flushes stdio, and calls ``os._exit(1)`` to
    release the terminal. This is the escape valve for cleanup steps
    that hang on stuck file descriptors / sockets — ``asyncio.wait_for``
    tries to cancel the inner task but cancellation alone doesn't
    unblock a stuck ``close()``. ``os._exit`` does.

    Per-phase exceptions are logged at WARNING and swallowed; subsequent
    phases still run. This matches the existing "best-effort cleanup"
    contract in each daemon's finally block (e.g. ``cli/web``'s
    ``_close_storages`` swallows ``StorageError``).

    The current phase is tracked via a single-element list (atomic read
    from the outer scope) so the timeout's WARNING accurately names
    whichever phase was in flight when the deadline fired.

    Args:
        cleanups: Phases to run in order. Each callable is invoked with
            no arguments and awaited. Empty list is a no-op.
        timeout_seconds: Wall-clock cap on the whole sequence. Default
            10s; matches the v1.1.A proposal.
        logger: Logger for both per-phase exception logs and the
            timeout WARNING. Falls back to the shared shutdown logger
            if not provided so callers can stay one-line.

    Returns normally on success. **Does not return** on timeout —
    calls ``os._exit(1)`` directly.
    """
    log = logger or _SHUTDOWN_LOGGER
    current_phase: list[str] = ["init"]

    async def _run_all() -> None:
        for name, cleanup in cleanups:
            current_phase[0] = name
            try:
                await cleanup()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log.warning(
                    "shutdown phase raised; continuing",
                    extra={"phase": name, "error": str(exc)},
                )
        current_phase[0] = "done"

    try:
        await asyncio.wait_for(_run_all(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log.warning(
            "shutdown hung beyond timeout; forcing exit",
            extra={"phase": current_phase[0], "timeout_seconds": timeout_seconds},
        )
        # Flush stdio so the WARNING actually reaches the operator's
        # terminal before _exit bypasses normal shutdown.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
