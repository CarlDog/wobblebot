"""Tests for ``services/daemon_health.py``.

Builds tiny SQLite DBs at fixture-scoped temp paths, runs the
freshness reader against them with controlled wallclock + thresholds,
verifies every classification branch (FRESH / STALE / UNKNOWN with
its detail variants).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wobblebot.services.daemon_health import (
    DaemonHealth,
    DaemonStatus,
    fetch_daemon_freshness,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# --------------------------------------------------------------------- #
# DB builders — minimal schema with just the column the reader queries  #
# --------------------------------------------------------------------- #


def _build_observe_db(path: Path, *, observed_at: datetime | None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE price_snapshots ("
            " id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL"
            ")"
        )
        if observed_at is not None:
            conn.execute(
                "INSERT INTO price_snapshots(observed_at) VALUES (?)",
                (observed_at.isoformat(),),
            )
        conn.commit()
    finally:
        conn.close()


def _build_news_db(path: Path, *, fetched_at: datetime | None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE news_items (" " id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL" ")"
        )
        if fetched_at is not None:
            conn.execute(
                "INSERT INTO news_items(fetched_at) VALUES (?)",
                (fetched_at.isoformat(),),
            )
        conn.commit()
    finally:
        conn.close()


def _build_advise_db(path: Path, *, created_at: datetime | None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE advisor_suggestions ("
            " id INTEGER PRIMARY KEY, created_at TEXT NOT NULL"
            ")"
        )
        if created_at is not None:
            conn.execute(
                "INSERT INTO advisor_suggestions(created_at) VALUES (?)",
                (created_at.isoformat(),),
            )
        conn.commit()
    finally:
        conn.close()


def _build_empty_observe_db(path: Path) -> None:
    """price_snapshots table exists but has zero rows."""
    _build_observe_db(path, observed_at=None)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db_paths(tmp_path: Path) -> Iterator[dict[str, Path]]:
    yield {
        "observe": tmp_path / "observe.db",
        "news": tmp_path / "news.db",
        "advise": tmp_path / "advise.db",
    }


def _by_name(rows: list[DaemonHealth]) -> dict[str, DaemonHealth]:
    return {r.name: r for r in rows}


# --------------------------------------------------------------------- #
# Happy path — every daemon fresh                                       #
# --------------------------------------------------------------------- #


class TestAllFresh:
    async def test_all_three_fresh_under_threshold(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        # observe wrote 30s ago (threshold 120s) — fresh
        _build_observe_db(db_paths["observe"], observed_at=now - timedelta(seconds=30))
        # news wrote 5min ago (threshold 900s) — fresh
        _build_news_db(db_paths["news"], fetched_at=now - timedelta(seconds=300))
        # advise wrote 20min ago (threshold 3600s) — fresh
        _build_advise_db(db_paths["advise"], created_at=now - timedelta(seconds=1200))

        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )

        by_name = _by_name(rows)
        assert by_name["cli/observe"].status is DaemonStatus.FRESH
        assert by_name["cli/news"].status is DaemonStatus.FRESH
        assert by_name["cli/advise"].status is DaemonStatus.FRESH

    async def test_order_is_observe_news_advise(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        _build_observe_db(db_paths["observe"], observed_at=now - timedelta(seconds=30))
        _build_news_db(db_paths["news"], fetched_at=now - timedelta(seconds=300))
        _build_advise_db(db_paths["advise"], created_at=now - timedelta(seconds=1200))

        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )

        assert [r.name for r in rows] == ["cli/observe", "cli/news", "cli/advise"]


# --------------------------------------------------------------------- #
# STALE — last-seen exceeds threshold                                   #
# --------------------------------------------------------------------- #


class TestStale:
    async def test_observe_just_past_threshold_is_stale(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        # 121s old, threshold 120s.
        _build_observe_db(db_paths["observe"], observed_at=now - timedelta(seconds=121))
        _build_news_db(db_paths["news"], fetched_at=now - timedelta(seconds=10))
        _build_advise_db(db_paths["advise"], created_at=now - timedelta(seconds=10))

        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )
        by_name = _by_name(rows)
        assert by_name["cli/observe"].status is DaemonStatus.STALE
        assert by_name["cli/news"].status is DaemonStatus.FRESH
        assert by_name["cli/advise"].status is DaemonStatus.FRESH

    async def test_at_exact_threshold_still_fresh(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        """Boundary: age == threshold should classify as fresh."""
        _build_observe_db(db_paths["observe"], observed_at=now - timedelta(seconds=120))
        _build_news_db(db_paths["news"], fetched_at=now)
        _build_advise_db(db_paths["advise"], created_at=now)

        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )
        assert _by_name(rows)["cli/observe"].status is DaemonStatus.FRESH


# --------------------------------------------------------------------- #
# UNKNOWN — each detail variant                                         #
# --------------------------------------------------------------------- #


class TestUnknown:
    async def test_none_path_yields_unknown(self, now: datetime) -> None:
        rows = await fetch_daemon_freshness(observe_db=None, news_db=None, advise_db=None, now=now)
        for r in rows:
            assert r.status is DaemonStatus.UNKNOWN
            assert r.detail == "db path not configured"
            assert r.last_seen is None

    async def test_missing_file_yields_unknown_with_filename_detail(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        # No files created at the paths.
        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )
        for r in rows:
            assert r.status is DaemonStatus.UNKNOWN
            assert r.detail is not None
            assert "db file missing" in r.detail

    async def test_empty_table_yields_unknown(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        # Tables exist but no rows.
        _build_empty_observe_db(db_paths["observe"])
        _build_news_db(db_paths["news"], fetched_at=None)
        _build_advise_db(db_paths["advise"], created_at=None)

        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )
        for r in rows:
            assert r.status is DaemonStatus.UNKNOWN
            assert r.detail == "no rows yet"

    async def test_query_failure_yields_unknown(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        # File exists but isn't a SQLite database — opening with mode=ro
        # works for any file, but the SELECT will fail with "file is not
        # a database" once it tries to read pages.
        db_paths["observe"].write_bytes(b"this is not a sqlite database")
        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=None,
            advise_db=None,
            now=now,
        )
        obs = _by_name(rows)["cli/observe"]
        assert obs.status is DaemonStatus.UNKNOWN
        assert obs.detail is not None
        assert "query failed" in obs.detail


# --------------------------------------------------------------------- #
# Mixed states — typical operator scenario                              #
# --------------------------------------------------------------------- #


class TestMixed:
    async def test_one_fresh_one_stale_one_unknown(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        _build_observe_db(db_paths["observe"], observed_at=now - timedelta(seconds=60))
        _build_news_db(db_paths["news"], fetched_at=now - timedelta(hours=2))
        # advise_db deliberately not created — UNKNOWN

        rows = await fetch_daemon_freshness(
            observe_db=db_paths["observe"],
            news_db=db_paths["news"],
            advise_db=db_paths["advise"],
            now=now,
        )
        by_name = _by_name(rows)
        assert by_name["cli/observe"].status is DaemonStatus.FRESH
        assert by_name["cli/news"].status is DaemonStatus.STALE
        assert by_name["cli/advise"].status is DaemonStatus.UNKNOWN

    async def test_threshold_seconds_surfaces_on_every_row(
        self, db_paths: dict[str, Path], now: datetime
    ) -> None:
        rows = await fetch_daemon_freshness(observe_db=None, news_db=None, advise_db=None, now=now)
        # Per-daemon thresholds documented in design; UI shows them
        # alongside age. Just verify they're present + positive.
        for r in rows:
            assert r.threshold_seconds > 0
