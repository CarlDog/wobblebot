"""The reconcile task behind ``cli/maintenance`` (2026-08-22).

Split from ``cli/maintenance.py`` (same shape as the ``cli/harvest`` /
``cli/harvest_execute`` split): the reconcile task is the only
maintenance task that talks to the exchange and manages auth-halt
state, and folding it into the DB-hygiene module pushed that file to
~2.4x the project's ~300-400-line cap. ``cli/maintenance`` keeps the
cycle wrapper and scheduling; the work lives here.

What the task does: once per ``schedules.maintenance_reconcile``
cadence, fetch Kraken's account-wide ``TradesHistory`` ONCE, then diff
it per traded symbol against the locally recorded trades in
``maintenance.reconcile_source_db`` (opened read-only — that DB
belongs to ``cli/live``). Any Kraken trade with no local row and no
locally-open order notifies at ``critical``: it means the SellGuard's
cost-basis replay for that symbol no longer matches what actually
happened on the exchange. Trades whose order is still locally open are
*deferred* (persistence legitimately pending; logged, never paged) —
see ``services/trade_reconciliation.py`` for the full rationale.

Symbols come from ``live.symbols`` — the actually-traded set — NOT
from ``grid.coins``, which is a per-coin *override* map that neither
lists every traded symbol nor excludes untraded ones (2026-08-22
review: with the example config, a ``grid.coins``-derived set checked
never-traded coins and silently skipped actively-traded BTC/USD,
recreating the exact silent-miss class this task exists to close).

Reader key only; the single exchange call is ``get_trade_history``.
This task cannot place, cancel, or move anything.
"""

from __future__ import annotations

import logging
from pathlib import Path

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import PermanentAuthHalt, notify
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.config.kraken import KrakenConfig
from wobblebot.domain.models import Trade
from wobblebot.domain.value_objects import Symbol, fmt_decimal
from wobblebot.ports.exceptions import StorageError, WobbleBotPortError
from wobblebot.ports.notifier import NotifierPort
from wobblebot.services.trade_reconciliation import TRADE_FETCH_LIMIT, reconcile_symbol_trades

_LOGGER = logging.getLogger("wobblebot.cli.maintenance")


async def run_reconcile_cycle(  # pylint: disable=too-many-return-statements
    # Each return is a distinct guard clause (not configured / no
    # symbols / halted / missing db / missing creds / storage open
    # failure) that must skip the cycle WITHOUT touching Kraken or
    # storage -- nesting these into one if/else would obscure exactly
    # which precondition failed, which is the point of guard clauses.
    maintenance: MaintenanceConfig,
    symbols: list[Symbol],
    notifier: NotifierPort | None,
    halt: PermanentAuthHalt,
) -> int:
    """One reconcile cycle. Detection only -- never writes, never
    backfills.

    Returns count of symbols that reconciled clean. A symbol whose
    check itself fails (storage error) counts as neither clean nor
    dirty -- logged and retried next cycle, same fail-soft shape as
    the other four maintenance tasks. A failed Kraken fetch skips the
    whole cycle (there is exactly one, account-wide).
    """
    if maintenance.reconcile_source_db is None:
        _LOGGER.debug("no reconcile_source_db configured; skipping reconcile cycle")
        return 0
    if not symbols:
        _LOGGER.debug("no live.symbols configured; nothing to reconcile")
        return 0
    if halt.halted:
        _LOGGER.debug("reconcile halted (permanent auth failure); skipping")
        return 0
    source_path = Path(maintenance.reconcile_source_db)
    if not source_path.exists():
        _LOGGER.warning(
            "reconcile_source_db does not exist; skipping (db_path=%s)",
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
            "reconcile: missing reader credentials; skipping cycle: %s",
            exc,
            extra={"error": str(exc)},
        )
        return 0

    exchange = KrakenAdapter(config=kraken_config)
    try:
        # read_only=True -- live.db belongs to cli/live, which writes
        # fills/orders to it continuously; this task must never open a
        # write-capable connection against it (2026-08-22 review fix).
        storage = SQLiteStorageAdapter(str(source_path), read_only=True)
        try:
            await storage.connect()
        except StorageError as exc:
            _LOGGER.warning(
                "reconcile: failed to open source db (db_path=%s): %s",
                source_path,
                exc,
                extra={"db_path": str(source_path), "error": str(exc)},
            )
            return 0
        try:
            return await reconcile_symbols(exchange, storage, symbols, notifier, halt)
        finally:
            await storage.close()
    finally:
        await exchange.aclose()


async def _fetch_account_snapshot(
    exchange: KrakenAdapter,
    notifier: NotifierPort | None,
    halt: PermanentAuthHalt,
) -> list[Trade] | None:
    """The cycle's single credentialed Kraken call, with ADR-037 halt
    accounting. ``None`` means the fetch failed and the cycle should
    skip (retried next interval)."""
    try:
        snapshot = await exchange.get_trade_history(symbol=None, limit=TRADE_FETCH_LIMIT)
    except WobbleBotPortError as exc:
        _LOGGER.warning(
            "reconcile: account trade-history fetch failed; will retry next interval: %s: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        if halt.note_failure(exc):
            _LOGGER.error(
                "reconcile HALTED after %d consecutive permanent auth failures; "
                "fix KRAKEN_READER_API_KEY/_SECRET and redeploy",
                halt.STRIKES,
                extra={"strikes": halt.STRIKES},
            )
            await notify(
                notifier,
                level="critical",
                title="Reader key dead — trade reconciliation halted",
                message=(
                    f"{halt.STRIKES} consecutive permanent auth failures on the reader "
                    "key during trade-history reconciliation. Fix "
                    "KRAKEN_READER_API_KEY/_SECRET in the deployment env and redeploy. "
                    "Continuing to retry would re-arm Kraken's account-wide lockout."
                ),
                context={"strikes": halt.STRIKES, "task": halt.task_name},
            )
        return None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Belt-and-suspenders daemon isolation, NOT a known adapter gap:
        # since the 2026-08-22 parse hardening the adapter wraps
        # malformed-response builtins into ExchangeError itself, so this
        # fallback covers only the genuinely unforeseen. It exists
        # because _main_async's asyncio.gather has no return_exceptions
        # -- an escape here would take vacuum/prune/backup/verify down
        # with it. Never a halt strike: only a confirmed permanent-auth
        # ExchangeError should ever halt (ADR-037).
        _LOGGER.warning(
            "reconcile: account trade-history fetch failed with an unexpected %s; "
            "will retry next interval: %s",
            type(exc).__name__,
            exc,
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None
    halt.note_success()
    return snapshot


async def reconcile_symbols(
    exchange: KrakenAdapter,
    storage: SQLiteStorageAdapter,
    symbols: list[Symbol],
    notifier: NotifierPort | None,
    halt: PermanentAuthHalt,
) -> int:
    """Fetch the account snapshot once, then diff every symbol against
    it. Per-symbol failures (storage reads) are fail-soft; they touch
    neither the halt (not an auth path) nor the other symbols."""
    snapshot = await _fetch_account_snapshot(exchange, notifier, halt)
    if snapshot is None:
        return 0

    clean = 0
    for symbol in symbols:
        try:
            result = await reconcile_symbol_trades(
                exchange, storage, symbol, account_trades=snapshot
            )
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "reconcile failed for %s; will retry next interval: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
                extra={
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            continue
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Same belt-and-suspenders rationale as the fetch above.
            _LOGGER.warning(
                "reconcile failed for %s with an unexpected %s; will retry next interval: %s",
                symbol,
                type(exc).__name__,
                exc,
                extra={
                    "symbol": str(symbol),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            continue
        if result.deferred_open:
            _LOGGER.info(
                "reconcile: %s has %d Kraken trade(s) on still-open orders; "
                "persistence pending, not a gap",
                symbol,
                len(result.deferred_open),
                extra={
                    "symbol": str(symbol),
                    "deferred_count": len(result.deferred_open),
                    "deferred_trade_ids": [t.id for t in result.deferred_open],
                },
            )
        if result.is_clean:
            clean += 1
            continue
        missing_summary = "; ".join(
            f"{t.side.value} {fmt_decimal(t.amount.value)} @ {fmt_decimal(t.price.amount)} "
            f"({t.executed_at.dt.isoformat()})"
            for t in result.missing_locally
        )
        _LOGGER.error(
            "TRADE RECONCILIATION GAP: %s has %d Kraken trade(s) missing from local storage "
            "(kraken_count=%s, local_count=%s): %s",
            symbol,
            len(result.missing_locally),
            result.kraken_trade_count,
            result.local_trade_count,
            missing_summary,
            extra={
                "symbol": str(symbol),
                "missing_count": len(result.missing_locally),
                "kraken_trade_count": result.kraken_trade_count,
                "local_trade_count": result.local_trade_count,
                "missing_trade_ids": [t.id for t in result.missing_locally],
            },
        )
        await notify(
            notifier,
            level="critical",
            title=f"Trade reconciliation gap: {symbol}",
            message=(
                f"{len(result.missing_locally)} Kraken trade(s) for {symbol} are missing from "
                f"local storage (Kraken reports {result.kraken_trade_count}, local has "
                f"{result.local_trade_count}). This corrupts the SellGuard's cost-basis replay "
                "for this symbol until investigated and backfilled -- run "
                "tools/reconcile_trade_history.py for the deeper diagnostic and follow its "
                "backfill runbook."
            ),
            context={
                "symbol": str(symbol),
                "missing_count": len(result.missing_locally),
                "missing_trade_ids": [t.id for t in result.missing_locally],
            },
        )
    return clean


__all__ = ["reconcile_symbols", "run_reconcile_cycle"]
