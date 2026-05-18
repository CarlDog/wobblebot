"""Storage-layer latency harness (Stage 8.3.C).

Times the hot reads + writes against an operator-specified (or in-memory)
SQLite DB and reports p50/p99 latency in milliseconds. The operator runs
this on their actual deployment hardware (e.g. a Synology NAS) to find
their actual hotspots; Stage 8.4's soak test then has a baseline to
compare against.

Why not bench against synthetic numbers? The query planner sees the
schema, not the row counts. The harness pre-populates the DB with a
configurable fixture set (1000 closed orders, 200 trades, etc.) so
index-vs-scan choices reflect realistic load.

Usage::

    # Default: in-memory DB, 1000 iterations of every operation.
    python tools/profile_storage.py

    # Against a live operator DB (read-only ops; writes go to a temp DB).
    python tools/profile_storage.py --db data/wobblebot.db --iterations 5000

    # Just one operation:
    python tools/profile_storage.py --operations get_open_orders

Output is one structured log line per operation::

    {operation, n, p50_ms, p99_ms, total_seconds}

Safe to run against a live DB (read ops are concurrent-reader-safe with
WAL mode from Stage 8.3.B). Write ops always run against a fresh
temp DB so they can't pollute the operator's data.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.logging import configure_logging
from wobblebot.domain.models import Order, PriceSnapshot, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp

_LOGGER = logging.getLogger("wobblebot.tools.profile_storage")

_READ_OPS = {"get_open_orders", "get_trades", "get_orders"}
_WRITE_OPS = {"save_order", "save_trade", "save_price_snapshot"}
_ALL_OPS = sorted(_READ_OPS | _WRITE_OPS)


def percentile_ms(samples_ns: list[int], pct: float) -> float:
    """Return the requested percentile of ``samples_ns`` in milliseconds.

    ``pct`` is in [0, 100]. Empty samples return 0.0. The harness
    converts ns -> ms here so the rest of the code never juggles units.
    Linear interpolation between adjacent rank-ordered samples; matches
    numpy.percentile's default but without the numpy dependency.
    """
    if not samples_ns:
        return 0.0
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"percentile must be in [0, 100], got {pct}")
    ordered = sorted(samples_ns)
    if len(ordered) == 1:
        return ordered[0] / 1_000_000.0
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo] / 1_000_000.0
    frac = rank - lo
    interpolated = ordered[lo] + (ordered[hi] - ordered[lo]) * frac
    return interpolated / 1_000_000.0


def summarize(operation: str, samples_ns: list[int]) -> dict[str, Any]:
    """Build the structured output record for one operation."""
    return {
        "operation": operation,
        "n": len(samples_ns),
        "p50_ms": round(percentile_ms(samples_ns, 50.0), 3),
        "p99_ms": round(percentile_ms(samples_ns, 99.0), 3),
        "mean_ms": (round(statistics.mean(samples_ns) / 1_000_000.0, 3) if samples_ns else 0.0),
        "total_seconds": round(sum(samples_ns) / 1e9, 3),
    }


def _make_order(*, status: str = "open", side: str = "buy") -> Order:
    return Order(
        symbol=Symbol(base="BTC", quote="USD"),
        side=OrderSide(side),
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        status=status,
        created_at=Timestamp(dt=datetime.now(UTC)),
    )


def _make_trade(*, executed_at: datetime | None = None) -> Trade:
    return Trade(
        id=f"TRADE-{uuid4().hex[:12]}",
        order_id=f"ORDER-{uuid4().hex[:12]}",
        symbol=Symbol(base="BTC", quote="USD"),
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000.12"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset="BTC"),
        fee=Decimal("0.01"),
        cost=Decimal("50.00"),
        executed_at=Timestamp(dt=executed_at or datetime.now(UTC)),
    )


def _make_snapshot() -> PriceSnapshot:
    return PriceSnapshot(
        symbol=Symbol(base="BTC", quote="USD"),
        price=Price(amount=Decimal("50000"), currency="USD"),
        observed_at=Timestamp(dt=datetime.now(UTC)),
    )


async def _seed_fixtures(
    storage: SQLiteStorageAdapter,
    *,
    closed_orders: int,
    open_orders: int,
    trades: int,
) -> None:
    """Pre-populate the DB so query plans face realistic row counts.

    Without seeding, in-memory queries land in O(1) territory regardless
    of index quality and the timing tells the operator nothing about
    their actual deployment.
    """
    for _ in range(closed_orders):
        await storage.save_order(_make_order(status="closed"))
    for _ in range(open_orders):
        await storage.save_order(_make_order(status="open"))
    for _ in range(trades):
        await storage.save_trade(_make_trade())


async def _time(coro_factory: Callable[[], Awaitable[Any]]) -> int:
    """Time one operation in nanoseconds via perf_counter_ns."""
    start = time.perf_counter_ns()
    await coro_factory()
    return time.perf_counter_ns() - start


async def _profile_op(
    storage: SQLiteStorageAdapter,
    op: str,
    *,
    iterations: int,
) -> list[int]:
    """Run ``iterations`` of one operation; return ns timings."""
    symbol = Symbol(base="BTC", quote="USD")
    cutoff = datetime.now(UTC) - timedelta(days=7)
    samples: list[int] = []
    for _ in range(iterations):
        if op == "get_open_orders":
            samples.append(await _time(lambda: storage.get_open_orders(symbol)))
        elif op == "get_trades":
            samples.append(await _time(lambda: storage.get_trades(symbol, start_time=cutoff)))
        elif op == "get_orders":
            samples.append(await _time(lambda: storage.get_orders(symbol=symbol)))
        elif op == "save_order":
            order = _make_order()
            samples.append(await _time(lambda: storage.save_order(order)))
        elif op == "save_trade":
            trade = _make_trade()
            samples.append(await _time(lambda: storage.save_trade(trade)))
        elif op == "save_price_snapshot":
            snap = _make_snapshot()
            samples.append(
                await _time(
                    lambda: storage.save_price_snapshot(snap.symbol, snap.price, snap.observed_at)
                )
            )
        else:
            raise ValueError(f"unknown operation: {op}")
    return samples


def _resolve_db_path(arg_db: str | None) -> tuple[str, Path | None]:
    """Pick the DB path to profile against.

    Operator can pass ``--db <path>`` to point at their live DB, or omit
    for an in-memory DB. To avoid polluting a live DB with seeded
    fixtures + write-op rows, we always copy the operator's DB to a
    temp file first. Returns ``(db_path, tmp_dir_to_clean)``.
    """
    if not arg_db:
        return ":memory:", None
    src = Path(arg_db).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"db not found: {src}")
    tmp_dir = Path(tempfile.mkdtemp(prefix="wobblebot-profile-"))
    dst = tmp_dir / src.name
    shutil.copy2(src, dst)
    return str(dst), tmp_dir


async def _run(args: argparse.Namespace) -> int:
    try:
        db_path, tmp_dir = _resolve_db_path(args.db)
    except FileNotFoundError as exc:
        _LOGGER.error("db missing", extra={"error": str(exc)})
        return 2

    operations = (
        _ALL_OPS if args.operations == "all" else [op.strip() for op in args.operations.split(",")]
    )
    unknown = [op for op in operations if op not in _ALL_OPS]
    if unknown:
        _LOGGER.error(
            "unknown operations",
            extra={"unknown": unknown, "valid": _ALL_OPS},
        )
        return 2

    storage = SQLiteStorageAdapter(db_path)
    await storage.connect()
    try:
        if args.seed:
            _LOGGER.info(
                "seeding fixtures",
                extra={
                    "closed_orders": args.seed_closed_orders,
                    "open_orders": args.seed_open_orders,
                    "trades": args.seed_trades,
                },
            )
            await _seed_fixtures(
                storage,
                closed_orders=args.seed_closed_orders,
                open_orders=args.seed_open_orders,
                trades=args.seed_trades,
            )
        for op in operations:
            samples = await _profile_op(storage, op, iterations=args.iterations)
            record = summarize(op, samples)
            _LOGGER.info(
                "profiled %s p50=%sms p99=%sms",
                record["operation"],
                record["p50_ms"],
                record["p99_ms"],
                extra=record,
            )
    finally:
        await storage.close()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite DB to copy + profile against. Default: fresh in-memory DB.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="Iterations per operation (default 1000).",
    )
    parser.add_argument(
        "--operations",
        default="all",
        help=(f"Comma-separated ops or 'all'. Valid: {','.join(_ALL_OPS)} " "(default all)."),
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        default=True,
        help="Seed fixture rows before profiling (default on).",
    )
    parser.add_argument(
        "--no-seed",
        action="store_false",
        dest="seed",
        help="Skip the fixture seed (when --db already has data).",
    )
    parser.add_argument(
        "--seed-closed-orders",
        type=int,
        default=1000,
        help="Closed-order fixture rows (default 1000).",
    )
    parser.add_argument(
        "--seed-open-orders",
        type=int,
        default=20,
        help="Open-order fixture rows (default 20).",
    )
    parser.add_argument(
        "--seed-trades",
        type=int,
        default=200,
        help="Trade fixture rows (default 200).",
    )
    parser.add_argument(
        "--log-format",
        choices=("plain", "json"),
        default="plain",
        help="Output format. json emits one structured record per operation.",
    )
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
