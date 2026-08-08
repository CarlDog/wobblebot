"""One-shot import of the local Kraken OHLCVT dump into the observe DB (P2 slice 2).

Streams the on-disk historical dump (``data/kraken-history/`` — Kraken's
downloadable OHLCVT CSVs, 2013 onward, plus quarterly incremental
subdirectories like ``2026Q1/``) into ``ohlc_bars`` and synthesized
``price_snapshots`` through the same idempotent write paths the live
backfill uses. This is the ONLY deep-history path: Kraken's live OHLC
endpoint retains just ~720 bars per interval (verified 2026-08-07), so
anything older than that horizon must come from this dump.

Deliberately a standalone tool, NOT ``backfill_range`` — that service
fetches live. Deliberately NOT resident code — a one-shot operator
event, run per symbol/interval as needed.

CSV schema (headerless): ``time,open,high,low,close,volume,trades``.
There is no vwap column; imported bars carry ``vwap=0``, mirroring
Kraken's own 0-for-no-vwap sentinel on empty live bars — TA consumers
must not treat 0 as a price. Rows that fail the ``OHLCBar`` validator
(garbled 2013-era data) are skipped and counted, never imported —
fail-hard would abort an hours-long import on one bad row.

Usage::

    python tools/import_kraken_history.py --symbols BTC/USD
    python tools/import_kraken_history.py --symbols BTC/USD,ETH/USD --intervals 1m,1h
    python tools/import_kraken_history.py --symbols SOL/USD \
        --dir data/kraken-history --db data/wobblebot-observe.db
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from wobblebot.adapters.kraken_exchange import symbol_to_kraken_altname
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import parse_intervals_arg, parse_symbol_csv
from wobblebot.config.logging import configure_logging
from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.backfill import synthesize_snapshots

_LOGGER = logging.getLogger("wobblebot.tools.import_kraken_history")

_DEFAULT_DIR = Path("data") / "kraken-history"
_DEFAULT_DB = Path("data") / "wobblebot-observe.db"

# Rows accumulated before each bulk write. save_ohlc_bars/-snapshots
# are executemany-per-call, so this is the commit granularity too.
_BATCH_ROWS = 5_000

# Log a progress line every N batches (~250k rows) so multi-million-row
# 1m files aren't a silent terminal, without flooding the log.
_PROGRESS_EVERY_BATCHES = 50

# Per-file cap on individually-logged bad rows; the rest roll up into
# the file's skipped count.
_MAX_ROW_WARNINGS = 5


@dataclass
class ImportStats:
    """Running totals for one (symbol, interval) import."""

    files: int = 0
    rows_read: int = 0
    rows_skipped: int = 0
    bars_inserted: int = 0
    snapshots_inserted: int = 0


def _parse_row(line: str, symbol: Symbol, interval_minutes: int) -> OHLCBar:
    """One CSV line -> OHLCBar. Raises ValueError-family on any bad row."""
    cols = line.split(",")
    if len(cols) != 7:
        raise ValueError(f"expected 7 columns, got {len(cols)}")
    return OHLCBar(
        symbol=symbol,
        interval_minutes=interval_minutes,
        opened_at=datetime.fromtimestamp(int(cols[0]), tz=UTC),
        open=Decimal(cols[1]),
        high=Decimal(cols[2]),
        low=Decimal(cols[3]),
        close=Decimal(cols[4]),
        vwap=Decimal("0"),  # OHLCVT CSVs carry no vwap; 0 = Kraken's own no-vwap sentinel
        volume=Decimal(cols[5]),
        count=int(cols[6]),
    )


def _candidate_files(root: Path, pair: str, interval_minutes: int) -> tuple[Path, list[Path]]:
    """Base file + chronologically-sorted quarterly incrementals.

    Kraken ships the bulk dump as ``<PAIR>_<minutes>.csv`` at the root
    and continuation files in ``<YYYY>Q<N>/`` subdirectories. Lexical
    sort of the quarter dirs is chronological. The base may end mid-
    stream of a quarter file's window — harmless, the UNIQUE constraint
    dedups the overlap.
    """
    name = f"{pair}_{interval_minutes}.csv"
    quarters = sorted(root.glob(f"[0-9][0-9][0-9][0-9]Q[0-9]/{name}"))
    return root / name, quarters


async def _flush(storage: SQLiteStorageAdapter, batch: list[OHLCBar], stats: ImportStats) -> None:
    if not batch:
        return
    stats.bars_inserted += await storage.save_ohlc_bars(batch)
    stats.snapshots_inserted += await storage.save_price_snapshots(synthesize_snapshots(batch))
    batch.clear()


async def _import_file(
    storage: SQLiteStorageAdapter,
    path: Path,
    symbol: Symbol,
    interval_minutes: int,
    stats: ImportStats,
) -> None:
    """Stream one CSV into storage. Bad rows skip-and-log; IO/storage errors raise."""
    started = time.monotonic()
    file_rows = 0
    file_skipped = 0
    batch: list[OHLCBar] = []
    batches_flushed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            file_rows += 1
            try:
                batch.append(_parse_row(line, symbol, interval_minutes))
            except (ValueError, InvalidOperation, ValidationError, OSError) as exc:
                file_skipped += 1
                if file_skipped <= _MAX_ROW_WARNINGS:
                    _LOGGER.warning(
                        "skipping bad row %s:%d: %s",
                        path.name,
                        line_no,
                        exc,
                        extra={"file": str(path), "line": line_no, "error": str(exc)},
                    )
                continue
            if len(batch) >= _BATCH_ROWS:
                await _flush(storage, batch, stats)
                batches_flushed += 1
                if batches_flushed % _PROGRESS_EVERY_BATCHES == 0:
                    _LOGGER.info(
                        "import %s: %d rows so far, %.1fs elapsed",
                        path.name,
                        file_rows,
                        time.monotonic() - started,
                    )
    await _flush(storage, batch, stats)
    stats.files += 1
    stats.rows_read += file_rows
    stats.rows_skipped += file_skipped
    _LOGGER.info(
        "imported %s: %d rows, %d skipped, %.1fs",
        path.name,
        file_rows,
        file_skipped,
        time.monotonic() - started,
        extra={
            "file": str(path),
            "rows": file_rows,
            "skipped": file_skipped,
            "elapsed_seconds": round(time.monotonic() - started, 1),
        },
    )


async def _import_pair_interval(
    storage: SQLiteStorageAdapter,
    root: Path,
    symbol: Symbol,
    interval_minutes: int,
) -> bool:
    """Import base + quarterly files for one (symbol, interval).

    Returns True when the item errored (missing base file, unreadable
    file, or storage failure). A missing *quarterly* file is normal.
    """
    pair = symbol_to_kraken_altname(symbol)
    base, quarters = _candidate_files(root, pair, interval_minutes)
    if not base.is_file():
        _LOGGER.error(
            "no dump file %s for %s @ %dm (looked in %s)",
            base.name,
            symbol,
            interval_minutes,
            root,
            extra={"symbol": str(symbol), "interval_minutes": interval_minutes},
        )
        return True
    stats = ImportStats()
    try:
        for path in [base, *quarters]:
            await _import_file(storage, path, symbol, interval_minutes, stats)
    except (OSError, StorageError) as exc:
        _LOGGER.error(
            "import failed for %s @ %dm: %s",
            symbol,
            interval_minutes,
            exc,
            extra={"symbol": str(symbol), "interval_minutes": interval_minutes},
        )
        return True
    _LOGGER.info(
        "import %s @ %dm complete: %d files, %d rows read, %d bars inserted, "
        "%d snapshots, %d skipped",
        symbol,
        interval_minutes,
        stats.files,
        stats.rows_read,
        stats.bars_inserted,
        stats.snapshots_inserted,
        stats.rows_skipped,
        extra={
            "symbol": str(symbol),
            "interval_minutes": interval_minutes,
            "files": stats.files,
            "rows_read": stats.rows_read,
            "bars_inserted": stats.bars_inserted,
            "snapshots_inserted": stats.snapshots_inserted,
            "rows_skipped": stats.rows_skipped,
        },
    )
    return False


async def _run(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        _LOGGER.error("dump directory not found: %s", root)
        return 2
    symbols = [Symbol.from_string(s) for s in parse_symbol_csv(args.symbols)]
    if not symbols:
        _LOGGER.error("--symbols resolved to an empty list")
        return 2
    storage = SQLiteStorageAdapter(str(args.db))
    await storage.connect()
    try:
        any_error = False
        for symbol in symbols:
            for interval in args.intervals:
                errored = await _import_pair_interval(storage, root, symbol, interval)
                any_error = any_error or errored
        return 1 if any_error else 0
    finally:
        await storage.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated trading pairs to import (e.g. BTC/USD,ETH/USD).",
    )
    parser.add_argument(
        "--intervals",
        type=parse_intervals_arg,
        default=[60],
        help=(
            "Comma list of bar intervals to import (1m/5m/.../1h/4h/1d/1w or "
            "bare minutes). Default 1h — the blueprint's default; 1m files "
            "run to millions of rows, import them deliberately."
        ),
    )
    parser.add_argument(
        "--dir",
        default=str(_DEFAULT_DIR),
        help=f"Dump directory (default {_DEFAULT_DIR}).",
    )
    parser.add_argument(
        "--db",
        default=str(_DEFAULT_DB),
        help=f"Target observe DB (default {_DEFAULT_DB}).",
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
