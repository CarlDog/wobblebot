"""Exchange-ledger ingest for cli/maintenance (ADR-040 follow-up).

Persists every non-trade balance movement Kraken records -- staking
rewards, deposits, withdrawals, adjustments -- so income the trades
table cannot explain stops being invisible.

**The gap this closes.** On 2026-08-22 SOL, ETH and ADA balances each
exceeded their replayed quantity by exactly their net staking income
(gross minus Kraken's 30% cut) while unstaked BTC/XRP/DOGE matched to
the digit. Nothing was lost; the account was simply earning money no
accounting surface recorded.

**Account-wide, not per traded symbol.** One call covers every asset,
which is cheaper -- and correct. Keying the ingest off ``live.symbols``
would have silently skipped ``BABY``, an asset this account stakes but
has never traded, along with the four USD deposits that record the
operator's own capital contributions. The same class of miss as the
2026-08-22 reconcile bug that nearly keyed off ``grid.coins``.

**Writes operator.db, never live.db.** Following ADR-014 decision 5's
precedent for ``llm_calls``: a shared ledger read by several daemons
belongs in the cross-daemon store. It also keeps this task clear of
live.db, which ``cli/maintenance`` opens strictly read-only because
``cli/live`` owns it.

Reader key only; one credentialed call per cycle. Cannot place, cancel,
or move anything.
"""

from __future__ import annotations

import logging

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.cli._common import PermanentAuthHalt, notify
from wobblebot.config.kraken import KrakenConfig
from wobblebot.ports.exceptions import StorageError, WobbleBotPortError
from wobblebot.ports.notifier import NotifierPort
from wobblebot.ports.storage import StoragePort

_LOGGER = logging.getLogger("wobblebot.cli.maintenance")

# Bounded well above this account's ~410 lifetime entries. The ingest
# re-fetches this window every cycle and upserts on the exchange's own
# ledger id rather than tracking a watermark: a watermark that drifts
# (or a page boundary that shifts under it) would double-count or skip
# income, and re-reading a few hundred rows daily costs nothing.
LEDGER_FETCH_LIMIT = 5000


async def run_ledger_sync_cycle(  # pylint: disable=too-many-return-statements
    # Each return is a distinct precondition or fail-soft path (no
    # operator_db / halted / missing creds / fetch failed / unexpected
    # fetch error / persist failed) that must skip the cycle cleanly.
    # Same guard-clause shape as the reconcile and capital tasks.
    operator_storage: StoragePort | None,
    notifier: NotifierPort | None,
    halt: PermanentAuthHalt,
) -> int:
    """One ledger-ingest cycle. Returns the number of entries written."""
    if operator_storage is None:
        _LOGGER.debug("no operator_db configured; skipping ledger sync")
        return 0
    if halt.halted:
        _LOGGER.debug("ledger sync halted (permanent auth failure); skipping")
        return 0
    try:
        kraken_config = KrakenConfig.from_env(
            key_var="KRAKEN_READER_API_KEY", secret_var="KRAKEN_READER_API_SECRET"
        )
    except ValueError as exc:
        _LOGGER.warning(
            "ledger sync: missing reader credentials; skipping cycle: %s",
            exc,
            extra={"error": str(exc)},
        )
        return 0

    exchange = KrakenAdapter(config=kraken_config)
    try:
        try:
            entries = await exchange.get_ledger_entries(limit=LEDGER_FETCH_LIMIT)
        except WobbleBotPortError as exc:
            _LOGGER.warning(
                "ledger sync: fetch failed; will retry next interval: %s: %s",
                type(exc).__name__,
                exc,
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            if halt.note_failure(exc):
                _LOGGER.error(
                    "ledger sync HALTED after %d consecutive permanent auth failures; "
                    "fix KRAKEN_READER_API_KEY/_SECRET and redeploy",
                    halt.STRIKES,
                    extra={"strikes": halt.STRIKES},
                )
                await notify(
                    notifier,
                    level="critical",
                    title="Reader key dead — ledger sync halted",
                    message=(
                        f"{halt.STRIKES} consecutive permanent auth failures on the reader "
                        "key during exchange-ledger ingest. Staking income and deposits are "
                        "no longer being recorded. Fix KRAKEN_READER_API_KEY/_SECRET in the "
                        "deployment env and redeploy."
                    ),
                    context={"strikes": halt.STRIKES, "task": halt.task_name},
                )
            return 0
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Daemon isolation: _main_async's gather has no
            # return_exceptions, so an escape here would take the other
            # maintenance tasks down with it. Never a halt strike --
            # only a confirmed permanent-auth ExchangeError halts
            # (ADR-037).
            _LOGGER.warning(
                "ledger sync: fetch failed with an unexpected %s; will retry: %s",
                type(exc).__name__,
                exc,
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return 0
        halt.note_success()

        try:
            written = await operator_storage.save_ledger_entries(entries)
        except StorageError as exc:
            _LOGGER.warning(
                "ledger sync: persist failed; will retry next interval: %s",
                exc,
                extra={"error": str(exc)},
            )
            return 0
        _LOGGER.info(
            "ledger sync complete (fetched=%s, written=%s)",
            len(entries),
            written,
            extra={"fetched": len(entries), "written": written},
        )
        return written
    finally:
        await exchange.aclose()
