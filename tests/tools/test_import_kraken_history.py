"""Tests for tools/import_kraken_history.py (P2 slice 2).

The dump importer is the ONLY deep-history path (Kraken's live OHLC
endpoint retains ~720 bars/interval). Pins: CSV row parsing incl. the
skip-and-log posture for garbled 2013-era rows, base+quarterly file
discovery in chronological order, the altname filename mapping
(BTC/USD -> XBTUSD_*.csv), idempotent re-runs, and the vwap=0
synthesis (OHLCVT CSVs carry no vwap column).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError

from tools.import_kraken_history import (
    ImportStats,
    _candidate_files,
    _import_pair_interval,
    _parse_row,
)
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit

_BTC = Symbol(base="BTC", quote="USD")

# 2013-10-06T21:00:00Z — a real-shaped epoch from the dump's early era.
_GOOD_ROW = "1381093200,122.0,122.5,121.5,122.2,0.1,1"


class TestParseRow:
    def test_good_row_parses(self) -> None:
        bar = _parse_row(_GOOD_ROW, _BTC, 60)
        assert bar.opened_at == datetime(2013, 10, 6, 21, 0, 0, tzinfo=UTC)
        assert bar.open == Decimal("122.0")
        assert bar.high == Decimal("122.5")
        assert bar.low == Decimal("121.5")
        assert bar.close == Decimal("122.2")
        assert bar.volume == Decimal("0.1")
        assert bar.count == 1
        assert bar.interval_minutes == 60

    def test_vwap_synthesized_as_zero(self) -> None:
        """The OHLCVT CSV has no vwap column; 0 mirrors Kraken's own
        no-vwap sentinel on empty live bars."""
        assert _parse_row(_GOOD_ROW, _BTC, 60).vwap == Decimal("0")

    def test_flat_single_trade_row_parses(self) -> None:
        bar = _parse_row("1381093200,122.0,122.0,122.0,122.0,0.1,1", _BTC, 60)
        assert bar.high == bar.low == Decimal("122.0")

    def test_wrong_column_count_raises(self) -> None:
        with pytest.raises(ValueError, match="expected 7 columns"):
            _parse_row("1381093200,122.0,122.5", _BTC, 60)

    def test_non_numeric_price_raises(self) -> None:
        with pytest.raises(Exception):
            _parse_row("1381093200,abc,122.5,121.5,122.2,0.1,1", _BTC, 60)

    def test_validator_violation_raises(self) -> None:
        """low > high — the OHLCBar validator refuses; the caller's
        skip-and-log turns this into a counted skip, never a persisted
        corrupt bar."""
        with pytest.raises(ValidationError):
            _parse_row("1381093200,122.0,121.0,123.0,122.2,0.1,1", _BTC, 60)


class TestCandidateFiles:
    def test_base_plus_quarters_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "XBTUSD_60.csv").write_text(_GOOD_ROW + "\n")
        for quarter in ("2026Q1", "2025Q4"):
            qdir = tmp_path / quarter
            qdir.mkdir()
            (qdir / "XBTUSD_60.csv").write_text(_GOOD_ROW + "\n")
        base, quarters = _candidate_files(tmp_path, "XBTUSD", 60)
        assert base == tmp_path / "XBTUSD_60.csv"
        assert [q.parent.name for q in quarters] == ["2025Q4", "2026Q1"]

    def test_no_quarters_is_fine(self, tmp_path: Path) -> None:
        _, quarters = _candidate_files(tmp_path, "XBTUSD", 60)
        assert quarters == []


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _write_dump(tmp_path: Path) -> Path:
    """Base file (3 rows, one garbled) + a 2026Q1 continuation (2 rows,
    one overlapping the base)."""
    (tmp_path / "XBTUSD_60.csv").write_text(
        "1381093200,122.0,122.5,121.5,122.2,0.1,1\n"
        "1381096800,122.2,123.0,122.0,122.8,0.5,3\n"
        "1381100400,122.8,121.0,124.0,122.9,0.2,2\n"  # low>high: must skip
    )
    qdir = tmp_path / "2026Q1"
    qdir.mkdir()
    (qdir / "XBTUSD_60.csv").write_text(
        "1381096800,122.2,123.0,122.0,122.8,0.5,3\n"  # dup of base row 2
        "1381104000,122.9,123.5,122.5,123.1,0.3,4\n"
    )
    return tmp_path


class TestImportPairInterval:
    @pytest.mark.asyncio
    async def test_end_to_end_import(
        self,
        storage: SQLiteStorageAdapter,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _write_dump(tmp_path)
        with caplog.at_level(logging.WARNING, logger="wobblebot.tools.import_kraken_history"):
            errored = await _import_pair_interval(storage, root, _BTC, 60)
        assert errored is False
        bars = await storage.get_ohlc_bars(_BTC, 60)
        # 5 rows total, 1 garbled skip, 1 cross-file duplicate -> 3 unique bars.
        assert len(bars) == 3
        assert [b.opened_at for b in bars] == sorted(b.opened_at for b in bars)
        assert any("skipping bad row" in r.getMessage() for r in caplog.records)
        snaps = await storage.get_price_snapshots(symbol=_BTC)
        assert len(snaps) == 3  # synthesized alongside, same dedup

    @pytest.mark.asyncio
    async def test_rerun_is_idempotent(self, storage: SQLiteStorageAdapter, tmp_path: Path) -> None:
        root = _write_dump(tmp_path)
        await _import_pair_interval(storage, root, _BTC, 60)
        errored = await _import_pair_interval(storage, root, _BTC, 60)
        assert errored is False
        assert len(await storage.get_ohlc_bars(_BTC, 60)) == 3
        assert len(await storage.get_price_snapshots(symbol=_BTC)) == 3

    @pytest.mark.asyncio
    async def test_missing_base_file_errors(
        self,
        storage: SQLiteStorageAdapter,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="wobblebot.tools.import_kraken_history"):
            errored = await _import_pair_interval(storage, tmp_path, _BTC, 60)
        assert errored is True
        assert any("no dump file" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_uses_altname_filename(
        self, storage: SQLiteStorageAdapter, tmp_path: Path
    ) -> None:
        """BTC/USD must resolve to XBTUSD_*.csv, not BTCUSD_*.csv."""
        (tmp_path / "BTCUSD_60.csv").write_text(_GOOD_ROW + "\n")
        errored = await _import_pair_interval(storage, tmp_path, _BTC, 60)
        assert errored is True  # the XBTUSD file is absent

    @pytest.mark.asyncio
    async def test_interval_scoping_in_storage(
        self, storage: SQLiteStorageAdapter, tmp_path: Path
    ) -> None:
        _write_dump(tmp_path)
        await _import_pair_interval(storage, tmp_path, _BTC, 60)
        assert await storage.get_ohlc_bars(_BTC, 1) == []


class TestImportStats:
    def test_defaults_zeroed(self) -> None:
        stats = ImportStats()
        assert (stats.files, stats.rows_read, stats.rows_skipped) == (0, 0, 0)
        assert (stats.bars_inserted, stats.snapshots_inserted) == (0, 0)
