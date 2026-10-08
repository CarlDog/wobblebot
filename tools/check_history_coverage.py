"""Bounded, read-only timestamp coverage evidence for an operator-supplied snapshot.

No import, scoring, provider access or readiness approval. Missing slots do not
identify retention, exchange inactivity or collection failure as their cause.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

_LOGGER = logging.getLogger("wobblebot.tools.history_coverage")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def _missing_ranges(
    present: bytearray, start: datetime, step: int, limit: int
) -> tuple[list[dict], int]:
    ranges: list[dict] = []
    total = 0
    first: int | None = None
    for slot in range(len(present) + 1):
        missing = slot < len(present) and not present[slot]
        if missing and first is None:
            first = slot
        if not missing and first is not None:
            total += 1
            if len(ranges) < limit:
                ranges.append(
                    {
                        "start": (start + timedelta(seconds=first * step)).isoformat(),
                        "end_exclusive": (start + timedelta(seconds=slot * step)).isoformat(),
                        "missing_slots": slot - first,
                    }
                )
            first = None
    return ranges, total


def coverage_report(
    db: Path,
    *,
    start: datetime,
    end: datetime,
    symbols: list[str],
    intervals: list[int],
    max_rows: int = 1_000_000,
    timeout_seconds: float = 30,
    gap_limit: int = 20,
) -> dict:
    """Inspect one SQLite read transaction; abort, never label partial work complete.

    Each selected pair is scanned in full so malformed and offset timestamps
    cannot silently escape a textual date predicate. Invalid timestamps therefore
    refer to the entire selected pair, not necessarily the requested time window.
    """
    start, end = _utc(start.isoformat()), _utc(end.isoformat())
    if start >= end or start.microsecond or end.microsecond:
        raise ValueError("window must be increasing with whole-second boundaries")
    if not symbols or len(symbols) > 32 or len(set(symbols)) != len(symbols):
        raise ValueError("supply at most 32 distinct symbols")
    pairs = []
    for symbol in symbols:
        parts = symbol.split("/")
        if (
            len(symbol) > 50
            or len(parts) != 2
            or not all(p and p.isascii() and p.isalnum() and p == p.upper() for p in parts)
        ):
            raise ValueError("symbols must be uppercase BASE/QUOTE identifiers")
        pairs.append(parts)
    if (
        not intervals
        or len(set(intervals)) != len(intervals)
        or any(i not in (1, 60) for i in intervals)
    ):
        raise ValueError("G3 coverage supports distinct 1-minute and 60-minute intervals")
    if (
        not 0 < max_rows <= 10_000_000
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
        or not 0 <= gap_limit <= 100
    ):
        raise ValueError("row/time budgets must be positive and gap limit nonnegative")
    for interval in intervals:
        step = interval * 60
        if int(start.timestamp()) % step or int(end.timestamp()) % step:
            raise ValueError("window boundaries must align to every selected interval")
        if (end - start).total_seconds() / step > 1_000_000:
            raise ValueError("window exceeds one million expected slots per pair")
    aggregate_slots = sum(int((end - start).total_seconds()) // (i * 60) for i in intervals) * len(
        symbols
    )
    if aggregate_slots > 2_000_000:
        raise ValueError("request exceeds two million aggregate expected slots")
    deadline = time.monotonic() + timeout_seconds
    scanned = 0
    reports = []
    # No adapter connect: it would migrate/create. mode=ro refuses missing input.
    with closing(
        sqlite3.connect(
            db.resolve().as_uri() + "?mode=ro", uri=True, timeout=min(timeout_seconds, 5)
        )
    ) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN")
        for symbol, (base, quote) in zip(symbols, pairs, strict=True):
            for interval in intervals:
                step = interval * 60
                expected = int((end - start).total_seconds()) // step
                present = bytearray(expected)
                invalid = outside = off_grid = duplicates = rows = 0
                cursor = conn.execute(
                    "SELECT opened_at FROM ohlc_bars WHERE symbol_base=? AND symbol_quote=? AND interval_minutes=?",
                    (base, quote, interval),
                )
                for (raw,) in cursor:
                    scanned += 1
                    rows += 1
                    if scanned > max_rows or time.monotonic() >= deadline:
                        raise ValueError("scan budget exhausted; no complete report produced")
                    try:
                        opened = _utc(str(raw))
                    except (ValueError, OverflowError):
                        invalid += 1
                        continue
                    if not start <= opened < end:
                        outside += 1
                        continue
                    delta = (opened - start).total_seconds()
                    if opened.microsecond or int(delta) % step:
                        off_grid += 1
                        continue
                    slot = int(delta) // step
                    if present[slot]:
                        duplicates += 1
                    present[slot] = 1
                ranges, range_count = _missing_ranges(present, start, step, gap_limit)
                count = sum(present)
                reports.append(
                    {
                        "symbol": symbol,
                        "interval_minutes": interval,
                        "rows_scanned": rows,
                        "outside_window_rows": outside,
                        "invalid_timestamp_rows_entire_pair": invalid,
                        "off_grid_rows_in_window": off_grid,
                        "duplicate_slots_in_window": duplicates,
                        "expected_slots": expected,
                        "present_slots": count,
                        "missing_slots": expected - count,
                        "missing_ranges": ranges,
                        "missing_range_count": range_count,
                        "missing_ranges_truncated": range_count > len(ranges),
                    }
                )
                if time.monotonic() >= deadline:
                    raise ValueError("scan budget exhausted; no complete report produced")
    return {
        "scope": "timestamp_presence_only",
        "source": "operator_supplied_snapshot_not_independently_verified",
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "readiness_gate_cleared": False,
        "absence_cause": "not_determined",
        "rows_scanned": scanned,
        "series": reports,
    }


def main(argv: list[str] | None = None) -> int:
    """Emit JSON only after a complete bounded scan; no report on input failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--interval-minutes", nargs="+", type=int, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        report = coverage_report(
            args.db,
            start=_utc(args.start),
            end=_utc(args.end),
            symbols=args.symbols,
            intervals=args.interval_minutes,
            max_rows=args.max_rows,
            timeout_seconds=args.timeout_seconds,
        )
    except (ValueError, OverflowError, OSError, sqlite3.Error) as exc:
        _LOGGER.error(
            "coverage inspection failed (%s); no complete report produced", type(exc).__name__
        )
        return 2
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return int(
        any(
            s["missing_slots"]
            or s["invalid_timestamp_rows_entire_pair"]
            or s["off_grid_rows_in_window"]
            or s["duplicate_slots_in_window"]
            for s in report["series"]
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
