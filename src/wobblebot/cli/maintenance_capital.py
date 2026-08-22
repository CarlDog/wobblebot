"""The capital-report task behind ``cli/maintenance`` (ADR-040).

Same split rationale as ``cli/maintenance_reconcile``: this task talks
to the exchange and manages auth-halt state, and folding it into the
DB-hygiene module would push that file past the project's file-size
cap. ``cli/maintenance`` keeps the cycle wrapper and scheduling; the
work lives here, and the pure checks live in
``services/capital_reporter.py``.

**Advisory only.** ADR-040 stage 1 is read-only by design: this task
computes findings and notifies. It writes no config, queues no
command, and places no order. The POLICY tier that would let an
operator ACT on a finding is ratified but unimplemented, and ADR-040's
validation plan requires this Reporter to independently reproduce the
three known 2026-08-22 findings before any POLICY value becomes
writable.

Reader key only. Two exchange calls per cycle: one ``get_balances``
(account-wide, not per symbol) and one ``get_pair_limits`` per traded
symbol. It cannot place, cancel, or move anything.

**On sampling the daily-spend check.** ``max_daily_spend_usd``
consumption is a within-day measurement — sampled just after UTC
midnight it reads ~0 and says nothing. The obvious fix, a pre-midnight
wall-clock window, is not expressible here: ``run_poll_loop`` fires on
an interval measured from process start, so its phase drifts with
every restart. Instead the overstatement finding is gated on the
CONDITION that makes it meaningful — the cap being materially consumed
(:data:`_CAP_MATERIAL_FRACTION`). That triggers whenever there is
something to say regardless of when the cycle lands, and stays silent
on a quiet morning rather than reporting a ratio computed from noise.
The sampling hour is included in the finding either way so the
operator can see when it was taken.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import PermanentAuthHalt, notify
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.config.grid import GridConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.safety import SafetyConfig
from wobblebot.domain.cost_basis import replay_average_cost
from wobblebot.domain.value_objects import PairLimits, Symbol
from wobblebot.ports.exceptions import StorageError, WobbleBotPortError
from wobblebot.ports.notifier import NotifierPort
from wobblebot.ports.storage import StoragePort
from wobblebot.services.capital_reporter import (
    CapitalReport,
    check_entry_viability,
    check_exit_viability,
    compute_cap_honesty,
    summarize,
)
from wobblebot.services.exposure import daily_spend_usd

_LOGGER = logging.getLogger("wobblebot.cli.maintenance")

# Enough to cover the whole ledger -- mirrors the sell guard's and the
# inventory caps' fetch-everything posture. A truncated ledger would
# understate both the held position and the day's flow.
_TRADE_FETCH_LIMIT = 100_000

# The daily-spend overstatement finding is only raised once the cap is
# at least this consumed. Below it the ratio is computed from too
# little flow to mean anything (see the module docstring).
_CAP_MATERIAL_FRACTION = Decimal("0.5")


async def run_capital_report_cycle(  # pylint: disable=too-many-return-statements
    # Each return is a distinct precondition (no source db / no symbols
    # / halted / missing file / missing creds / storage open failure)
    # that must skip the cycle without touching Kraken or storage --
    # the same guard-clause shape as run_reconcile_cycle.
    maintenance: MaintenanceConfig,
    grid: GridConfig,
    safety: SafetyConfig,
    symbols: list[Symbol],
    notifier: NotifierPort | None,
    halt: PermanentAuthHalt,
) -> int:
    """One capital-report cycle. Detection only -- never writes.

    Returns the number of findings raised (0 = everything viable).
    """
    if maintenance.reconcile_source_db is None:
        _LOGGER.debug("no reconcile_source_db configured; skipping capital report")
        return 0
    if not symbols:
        _LOGGER.debug("no live.symbols configured; nothing to report on")
        return 0
    if halt.halted:
        _LOGGER.debug("capital report halted (permanent auth failure); skipping")
        return 0
    source_path = Path(maintenance.reconcile_source_db)
    if not source_path.exists():
        _LOGGER.warning(
            "capital report: source db does not exist; skipping (db_path=%s)",
            source_path,
            extra={"db_path": str(source_path)},
        )
        return 0
    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_READER_API_KEY", secret_var="KRAKEN_READER_API_SECRET"
        )
    except ValueError as exc:
        _LOGGER.warning(
            "capital report: missing reader credentials; skipping cycle: %s",
            exc,
            extra={"error": str(exc)},
        )
        return 0

    exchange = KrakenAdapter(config=kraken_config)
    try:
        # read_only -- live.db belongs to cli/live (2026-08-22 review).
        storage = SQLiteStorageAdapter(str(source_path), read_only=True)
        try:
            await storage.connect()
        except StorageError as exc:
            _LOGGER.warning(
                "capital report: failed to open source db (db_path=%s): %s",
                source_path,
                exc,
                extra={"db_path": str(source_path), "error": str(exc)},
            )
            return 0
        try:
            report = await _build_report(exchange, storage, grid, safety, symbols, halt)
            if report is None:
                return 0
            return await _emit(report, notifier)
        finally:
            await storage.close()
    finally:
        await exchange.aclose()


async def _held_quantities(
    exchange: KrakenAdapter,
    storage: StoragePort,
    symbols: list[Symbol],
    halt: PermanentAuthHalt,
) -> dict[Symbol, tuple[Decimal, str]]:
    """Held base quantity per symbol, exchange-first.

    ADR-040 decision: the EXCHANGE balance is authoritative for exit
    viability -- it is what ``ordermin`` is judged against, and the
    2026-08-22 silent-fill-loss incident proved the replayed ledger can
    diverge from it. The replay is the credential-free fallback, and
    every finding carries which source it used so a discrepancy is
    visible rather than silently assumed away.

    One ``get_balances`` call for the whole account, never one per
    symbol.
    """
    held: dict[Symbol, tuple[Decimal, str]] = {}
    balances: dict[str, Decimal] = {}
    try:
        for balance in await exchange.get_balances():
            balances[balance.asset] = balance.total
        halt.note_success()
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "capital report: balance fetch failed; falling back to replayed ledger: %s",
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        if halt.note_failure(exc):
            _LOGGER.error(
                "capital report HALTED after %d consecutive permanent auth failures",
                halt.STRIKES,
                extra={"strikes": halt.STRIKES},
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Daemon isolation: _main_async's gather has no return_exceptions,
        # so an escape here would take the other five tasks down with it.
        # Never a halt strike -- only a confirmed permanent-auth
        # ExchangeError should halt (ADR-037).
        _LOGGER.warning(
            "capital report: balance fetch failed with an unexpected %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )

    for symbol in symbols:
        if symbol.base in balances:
            held[symbol] = (balances[symbol.base], "exchange")
            continue
        try:
            trades = await storage.get_trades(symbol=symbol, limit=_TRADE_FETCH_LIMIT)
        except StorageError as exc:
            _LOGGER.warning(
                "capital report: trade fetch failed for %s; skipping exit check: %s",
                symbol,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue
        held[symbol] = (replay_average_cost(trades).quantity, "replay")
    return held


async def _build_report(  # pylint: disable=too-many-locals
    exchange: KrakenAdapter,
    storage: StoragePort,
    grid: GridConfig,
    safety: SafetyConfig,
    symbols: list[Symbol],
    halt: PermanentAuthHalt,
) -> CapitalReport | None:
    """Gather inputs and run the three checks. ``None`` = cycle skipped."""
    held = await _held_quantities(exchange, storage, symbols, halt)

    entries = []
    exits = []
    for symbol in symbols:
        try:
            limits: PairLimits = await exchange.get_pair_limits(symbol)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "capital report: pair limits unavailable for %s; skipping: %s",
                symbol,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            continue

        # The grid the engine would actually lay out -- its persisted
        # anchor, not the config default. After a re-anchor the two
        # differ, and reporting on the config's ladder would describe a
        # grid that isn't there.
        try:
            state = await storage.get_grid_state(symbol)
        except StorageError as exc:
            _LOGGER.warning(
                "capital report: grid state unavailable for %s; skipping entry check: %s",
                symbol,
                exc,
                extra={"symbol": str(symbol), "error": str(exc)},
            )
            state = None
        coin_cfg = grid.for_coin(symbol.base)
        if state is not None:
            entries.append(
                check_entry_viability(
                    symbol,
                    order_size_usd=coin_cfg.order_size_usd,
                    reference_price=state.reference_price,
                    spacing_percentage=state.spacing_percentage,
                    levels_above=state.levels_above,
                    levels_below=state.levels_below,
                    limits=limits,
                )
            )
        if symbol in held:
            quantity, source = held[symbol]
            exits.append(
                check_exit_viability(
                    symbol,
                    held_quantity=quantity,
                    limits=limits,
                    source="exchange" if source == "exchange" else "replay",
                )
            )

    try:
        charged = await daily_spend_usd(storage)
        trades = await storage.get_trades(limit=_TRADE_FETCH_LIMIT)
    except StorageError as exc:
        _LOGGER.warning(
            "capital report: daily-spend inputs unavailable; skipping cap check: %s",
            exc,
            extra={"error": str(exc)},
        )
        return CapitalReport(entry=tuple(entries), exits=tuple(exits), cap=None)

    cap = compute_cap_honesty(trades, charged_usd=charged, cap_usd=safety.max_daily_spend_usd)
    return CapitalReport(entry=tuple(entries), exits=tuple(exits), cap=cap)


async def _emit(report: CapitalReport, notifier: NotifierPort | None) -> int:
    """Log every finding; notify when there is something to act on."""
    consumed = report.cap.consumed_fraction if report.cap else None
    cap_material = consumed is not None and consumed >= _CAP_MATERIAL_FRACTION
    # Suppress the cap line when the day is too young to have said
    # anything -- see the module docstring on sampling.
    effective = report if cap_material else CapitalReport(report.entry, report.exits, None)
    lines = summarize(effective)

    if not lines:
        _LOGGER.info(
            "capital report clean (symbols_checked=%s, cap_consumed=%s)",
            len(report.entry),
            f"{consumed:.0%}" if consumed is not None else "n/a",
            extra={
                "symbols_checked": len(report.entry),
                "cap_consumed": str(consumed) if consumed is not None else None,
            },
        )
        return 0

    sampled_hour = datetime.now(UTC).strftime("%H:%MZ")
    for line in lines:
        _LOGGER.warning("CAPITAL: %s", line, extra={"finding": line})
    await notify(
        notifier,
        level="warning",
        title=f"Capital report: {len(lines)} finding(s)",
        message="\n".join(lines) + f"\n(sampled {sampled_hour})",
        context={
            "finding_count": len(lines),
            "blocked_entries": len(report.blocked_entries),
            "blocked_exits": len(report.blocked_exits),
            "sampled_at_utc": sampled_hour,
        },
    )
    return len(lines)
