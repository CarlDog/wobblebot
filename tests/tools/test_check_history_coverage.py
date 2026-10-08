"""Offline timestamp evidence must remain bounded and never certify readiness."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tools.check_history_coverage import coverage_report, main

pytestmark = pytest.mark.unit
START = datetime(2026, 4, 1, tzinfo=UTC)
END = START + timedelta(hours=4)


def _database(path, rows):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE ohlc_bars(symbol_base TEXT, symbol_quote TEXT, interval_minutes INTEGER, opened_at TEXT)"
        )
        db.executemany("INSERT INTO ohlc_bars VALUES (?, ?, ?, ?)", rows)
    return path


def _report(path, **kwargs):
    return coverage_report(
        path, start=START, end=END, symbols=["BTC/USD"], intervals=[60], **kwargs
    )


def test_gaps_offsets_duplicates_bad_rows_and_half_open_window(tmp_path):
    path = _database(
        tmp_path / "snapshot #100%.db",
        [
            ("BTC", "USD", 60, START.isoformat()),
            ("BTC", "USD", 60, "2026-04-01T01:00:00+01:00"),
            ("BTC", "USD", 60, (START + timedelta(hours=2)).isoformat()),
            ("BTC", "USD", 60, (START + timedelta(minutes=1)).isoformat()),
            ("BTC", "USD", 60, END.isoformat()),
            ("BTC", "USD", 60, "2026-04-01T03:00:00"),
            ("BTC", "USD", 60, "garbled"),
            ("ETH", "USD", 60, START.isoformat()),
        ],
    )
    before = path.read_bytes()
    report = _report(path)
    row = report["series"][0]
    assert row["present_slots"] == 2
    assert row["missing_slots"] == 2
    assert row["duplicate_slots_in_window"] == 1
    assert row["off_grid_rows_in_window"] == 1
    assert row["invalid_timestamp_rows_entire_pair"] == 2
    assert row["outside_window_rows"] == 1
    assert row["rows_scanned"] == report["rows_scanned"] == 7
    assert row["missing_ranges"] == [
        {
            "start": "2026-04-01T01:00:00+00:00",
            "end_exclusive": "2026-04-01T02:00:00+00:00",
            "missing_slots": 1,
        },
        {
            "start": "2026-04-01T03:00:00+00:00",
            "end_exclusive": "2026-04-01T04:00:00+00:00",
            "missing_slots": 1,
        },
    ]
    assert report["readiness_gate_cleared"] is False
    assert report["absence_cause"] == "not_determined"
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_missing_series_and_bounded_ranges_remain_visible(tmp_path):
    path = _database(tmp_path / "snapshot.db", [("BTC", "USD", 60, START.isoformat())])
    report = _report(path, gap_limit=0)
    row = report["series"][0]
    assert row["missing_slots"] == 3
    assert row["missing_range_count"] == 1
    assert row["missing_ranges"] == []
    assert row["missing_ranges_truncated"] is True
    empty = coverage_report(path, start=START, end=END, symbols=["ETH/USD"], intervals=[1])
    assert empty["series"][0]["missing_slots"] == 240


def test_readonly_refuses_missing_file_and_missing_schema(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.Error):
        _report(path)
    assert not path.exists()
    with closing(sqlite3.connect(path)):
        pass
    before = path.read_bytes()
    with pytest.raises(sqlite3.Error):
        _report(path)
    assert path.read_bytes() == before


def test_budget_exhaustion_has_no_partial_success(tmp_path, capsys):
    path = _database(tmp_path / "snapshot.db", [("BTC", "USD", 60, START.isoformat())] * 3)
    args = [
        "--db",
        str(path),
        "--symbols",
        "BTC/USD",
        "--interval-minutes",
        "60",
        "--start",
        START.isoformat(),
        "--end",
        END.isoformat(),
        "--max-rows",
        "2",
    ]
    assert main(args) == 2
    assert capsys.readouterr().out == ""


def test_cli_complete_grid_is_not_readiness_approval(tmp_path, capsys):
    path = _database(
        tmp_path / "snapshot.db",
        [("BTC", "USD", 60, (START + timedelta(hours=i)).isoformat()) for i in range(4)],
    )
    args = [
        "--db",
        str(path),
        "--symbols",
        "BTC/USD",
        "--interval-minutes",
        "60",
        "--start",
        START.isoformat(),
        "--end",
        END.isoformat(),
    ]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "timestamp_presence_only"
    assert report["readiness_gate_cleared"] is False
    assert report["series"][0]["missing_slots"] == 0
    assert main([*args[:-1], (END + timedelta(hours=1)).isoformat()]) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_rows": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
        {"gap_limit": 101},
    ],
)
def test_invalid_limits_refused_before_open(tmp_path, kwargs):
    with pytest.raises(ValueError):
        _report(tmp_path / "absent.db", **kwargs)


def test_invalid_grid_and_aggregate_bound(tmp_path):
    for params in [
        {
            "start": START + timedelta(seconds=1),
            "end": END,
            "symbols": ["BTC/USD"],
            "intervals": [60],
        },
        {"start": START, "end": END, "symbols": ["BTC/USD"], "intervals": [5]},
        {
            "start": START,
            "end": START + timedelta(days=365),
            "symbols": ["A/USD", "B/USD", "C/USD", "D/USD"],
            "intervals": [1],
        },
    ]:
        with pytest.raises(ValueError):
            coverage_report(tmp_path / "absent.db", **params)


def test_deadline_interrupts_sql_work(tmp_path, monkeypatch):
    from tools import check_history_coverage as tool

    path = _database(tmp_path / "snapshot.db", [("ETH", "USD", 60, START.isoformat())] * 5000)
    clock = iter([0, 100])
    monkeypatch.setattr(tool.time, "monotonic", lambda: next(clock, 100))
    with pytest.raises(sqlite3.OperationalError) as caught:
        _report(path, timeout_seconds=1)
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT


def test_row_budget_is_global_across_selected_series(tmp_path):
    path = _database(
        tmp_path / "snapshot.db",
        [
            ("BTC", "USD", 60, START.isoformat()),
            ("ETH", "USD", 60, START.isoformat()),
        ],
    )
    with pytest.raises(ValueError, match="budget"):
        coverage_report(
            path, start=START, end=END, symbols=["BTC/USD", "ETH/USD"], intervals=[60], max_rows=1
        )


def test_one_read_snapshot_across_symbols_with_concurrent_wal_writer(tmp_path, monkeypatch):
    from tools import check_history_coverage as tool

    path = _database(tmp_path / "snapshot.db", [("BTC", "USD", 60, START.isoformat())])
    connect = sqlite3.connect
    with closing(connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")

        class ReaderConnection(sqlite3.Connection):
            changed = False

            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if sql.startswith("SELECT opened_at") and not self.changed:
                    self.changed = True
                    writer.execute(
                        "INSERT INTO ohlc_bars VALUES (?, ?, ?, ?)",
                        ("ETH", "USD", 60, START.isoformat()),
                    )
                    writer.commit()
                return cursor

        monkeypatch.setattr(
            tool.sqlite3,
            "connect",
            lambda *args, **kwargs: connect(*args, factory=ReaderConnection, **kwargs),
        )
        report = coverage_report(
            path, start=START, end=END, symbols=["BTC/USD", "ETH/USD"], intervals=[60]
        )
        assert [row["present_slots"] for row in report["series"]] == [1, 0]
        assert writer.execute("SELECT COUNT(*) FROM ohlc_bars").fetchone()[0] == 2
