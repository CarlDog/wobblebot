"""Historical OHLC backfill orchestrator (v1.1).

Pure-logic service: takes an ExchangePort + a StoragePort and walks
Kraken's ``/0/public/OHLC`` endpoint from ``since`` to ``until``,
writing both:

- ``ohlc_bars`` (canonical, idempotent via UNIQUE(symbol, interval,
  opened_at))
- ``price_snapshots`` (synthesized as ``(opened_at, open)`` per bar,
  matching what cli/observe's poll would have written at that
  instant)

Pagination follows Kraken's 720-bar response cap: the cursor for the
next page is the last returned bar's ``opened_at`` (Kraken's ``since``
is exclusive, so this naturally avoids duplicates across page
boundaries).

Rate limiting: ``rate_limit_seconds`` (default 1.0) sleeps between
requests. Kraken's free-tier public-API limit is one call per second;
the default is safe and predictable.

Failure handling: on an ``ExchangeError`` from the adapter, the
service stops the loop, populates ``BackfillResult.error`` with the
exception message, and returns. The operator can re-run with
``--since <last_opened_at>`` to resume — ``ohlc_bars``'s UNIQUE
constraint makes any overlap a no-op.

v1.1 limitation: ``price_snapshots`` has no UNIQUE constraint, so
re-running an overlapping window produces duplicate snapshots. The
``ohlc_bars`` half stays idempotent. A future cleanup will add a
UNIQUE constraint + migration; for now operators should re-run from
the cursor returned in ``BackfillResult.last_opened_at`` rather than
re-running from the original ``--since``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wobblebot.domain.value_objects import OHLCBar, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError, StorageError
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.storage import StoragePort

# Kraken's public-API free-tier limit is roughly 1 call/second. 1.0s
# is the safe default; operators with a paid tier or a more permissive
# venue can lower this through the kwarg.
_DEFAULT_RATE_LIMIT_SECONDS: float = 1.0

# Internal safety cap on iteration count. Each iteration fetches up to
# 720 bars, so ~10,000 iterations covers 7,200,000 bars — well beyond
# anything an operator would reasonably request. Above this we assume
# something is wrong (server returning the same `last` cursor in a loop,
# clock skew, etc.) and bail.
_MAX_ITERATIONS: int = 10_000


@dataclass(frozen=True)
class BackfillResult:  # pylint: disable=too-many-instance-attributes
    """Summary of one ``backfill_range`` invocation.

    Returned regardless of whether the run finished cleanly or stopped
    on an error; ``error`` is populated only on failure. The
    ``last_opened_at`` cursor is the resume hint for a re-run.
    """

    symbol: Symbol
    interval_minutes: int
    requested_since: datetime
    requested_until: datetime
    bars_fetched: int
    bars_inserted: int  # ohlc_bars rows actually written (post-dedup)
    snapshots_inserted: int  # price_snapshots rows written
    requests_made: int
    elapsed_seconds: float
    last_opened_at: datetime | None
    error: str | None = None


ProgressCallback = Callable[[BackfillResult], Awaitable[None]]


def _synthesize_snapshots(
    bars: list[OHLCBar],
) -> list[tuple[Symbol, Price, Timestamp]]:
    """Translate each bar's ``(opened_at, open)`` into a price snapshot.

    Per the slice-3 design decision (workshopped 2026-05-25): the
    synthesized snapshot's timestamp is the bar's ``opened_at`` and
    the price is ``open`` — modelling what cli/observe's poll would
    have observed at that exact instant.
    """
    return [
        (
            ohlc.symbol,
            Price(amount=ohlc.open, currency=ohlc.symbol.quote),
            Timestamp(dt=ohlc.opened_at),
        )
        for ohlc in bars
    ]


def _filter_to_window(bars: list[OHLCBar], since: datetime, until: datetime) -> list[OHLCBar]:
    """Drop bars whose opened_at falls outside ``[since, until]``.

    Kraken returns bars STRICTLY AFTER `since` so the lower bound is
    redundant here in practice, but the upper bound matters: the
    final page may overshoot ``until`` and we trim it.
    """
    return [ohlc for ohlc in bars if since < ohlc.opened_at <= until]


async def backfill_range(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    adapter: ExchangePort,
    storage: StoragePort,
    *,
    symbol: Symbol,
    since: datetime,
    until: datetime | None = None,
    interval_minutes: int = 1,
    rate_limit_seconds: float = _DEFAULT_RATE_LIMIT_SECONDS,
    progress_callback: ProgressCallback | None = None,
) -> BackfillResult:
    """Walk ``[since, until]`` for ``symbol``, writing OHLC + snapshots.

    Args:
        adapter: An ExchangePort with a working ``get_ohlc``. For the
            backfill use case this is always KrakenAdapter (live) or
            ShadowExchangeAdapter (forwards to live).
        storage: A StoragePort that knows how to persist
            ``ohlc_bars`` + ``price_snapshots``.
        symbol: Trading pair to backfill.
        since: Exclusive lower bound — fetched bars are STRICTLY AFTER
            this timestamp. Must be tz-aware (the adapter rejects
            naive inputs).
        until: Upper bound (inclusive). ``None`` defaults to
            ``datetime.now(UTC)`` resolved at the start of the call.
        interval_minutes: One of ``OHLCBar.ALLOWED_INTERVALS``. Default
            1 (max-fidelity).
        rate_limit_seconds: Sleep between adapter requests. Default
            1s. Set to 0.0 in tests.
        progress_callback: Optional async callback invoked AFTER each
            successful chunk with a partial ``BackfillResult``
            reflecting the running totals. Useful for CLI dot-printing
            or web-UI progress events.

    Returns:
        A ``BackfillResult`` capturing what was fetched / written /
        skipped. On error, ``error`` carries the exception message;
        ``last_opened_at`` carries the resume cursor.
    """
    started = time.monotonic()
    until_resolved = until if until is not None else datetime.now(UTC)

    bars_fetched = 0
    bars_inserted = 0
    snapshots_inserted = 0
    requests_made = 0
    cursor = since
    last_opened_at: datetime | None = None
    error: str | None = None

    if cursor >= until_resolved:
        return BackfillResult(
            symbol=symbol,
            interval_minutes=interval_minutes,
            requested_since=since,
            requested_until=until_resolved,
            bars_fetched=0,
            bars_inserted=0,
            snapshots_inserted=0,
            requests_made=0,
            elapsed_seconds=time.monotonic() - started,
            last_opened_at=None,
        )

    for _ in range(_MAX_ITERATIONS):
        try:
            page = await adapter.get_ohlc(symbol, interval_minutes, since=cursor)
        except ExchangeError as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        requests_made += 1

        in_window = _filter_to_window(page, since=cursor, until=until_resolved)
        bars_fetched += len(in_window)

        if in_window:
            try:
                bars_inserted += await storage.save_ohlc_bars(in_window)
                snapshots_inserted += await storage.save_price_snapshots(
                    _synthesize_snapshots(in_window)
                )
            except StorageError as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            last_opened_at = in_window[-1].opened_at

        # Termination conditions: either the page returned no bars at
        # all (cursor is at or past the head of available data), or
        # the last bar in the page is at/after until (we've reached
        # the requested ceiling).
        if not page or not in_window:
            break
        if in_window[-1].opened_at >= until_resolved:
            break

        # Kraken's ``since`` is exclusive — passing last opened_at as
        # the next page's cursor avoids fetching the same bar twice.
        cursor = in_window[-1].opened_at

        if progress_callback is not None:
            await progress_callback(
                BackfillResult(
                    symbol=symbol,
                    interval_minutes=interval_minutes,
                    requested_since=since,
                    requested_until=until_resolved,
                    bars_fetched=bars_fetched,
                    bars_inserted=bars_inserted,
                    snapshots_inserted=snapshots_inserted,
                    requests_made=requests_made,
                    elapsed_seconds=time.monotonic() - started,
                    last_opened_at=last_opened_at,
                )
            )

        if rate_limit_seconds > 0:
            await asyncio.sleep(rate_limit_seconds)

    return BackfillResult(
        symbol=symbol,
        interval_minutes=interval_minutes,
        requested_since=since,
        requested_until=until_resolved,
        bars_fetched=bars_fetched,
        bars_inserted=bars_inserted,
        snapshots_inserted=snapshots_inserted,
        requests_made=requests_made,
        elapsed_seconds=time.monotonic() - started,
        last_opened_at=last_opened_at,
        error=error,
    )


__all__ = ("BackfillResult", "ProgressCallback", "backfill_range")


# Helpers exposed for unit testing -- keep _private to discourage
# accidental cross-module use.
_DECIMAL_ZERO = Decimal("0")
_ONE_MINUTE = timedelta(minutes=1)
