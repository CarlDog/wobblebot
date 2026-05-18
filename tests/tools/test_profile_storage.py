"""Unit tests for tools/profile_storage.py (Stage 8.3.C)."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter


def _load_profile_storage() -> ModuleType:
    """Load ``tools/profile_storage.py`` as a module by path.

    ``tools/`` is intentionally not a package (the convention is one-shot
    operator scripts), so importlib by-path is the cleanest way to
    test the module's helper functions.
    """
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "tools" / "profile_storage.py"
    spec = importlib.util.spec_from_file_location("profile_storage", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["profile_storage"] = module
    spec.loader.exec_module(module)
    return module


profile_storage = _load_profile_storage()

pytestmark = pytest.mark.unit


class TestPercentileMs:
    """percentile_ms is the load-bearing math; tests pin its behavior."""

    def test_empty_list_returns_zero(self) -> None:
        assert profile_storage.percentile_ms([], 50.0) == 0.0

    def test_single_sample_returns_that_sample(self) -> None:
        # 2_000_000 ns = 2 ms.
        assert profile_storage.percentile_ms([2_000_000], 50.0) == pytest.approx(2.0)
        assert profile_storage.percentile_ms([2_000_000], 99.0) == pytest.approx(2.0)

    def test_p50_is_median_for_odd_count(self) -> None:
        # Five samples: 1, 2, 3, 4, 5 ms. Median = 3 ms.
        samples = [1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]
        assert profile_storage.percentile_ms(samples, 50.0) == pytest.approx(3.0)

    def test_p99_returns_near_max(self) -> None:
        # 100 samples 1..100ms; p99 should be ~99ms.
        samples = [int(i * 1_000_000) for i in range(1, 101)]
        result = profile_storage.percentile_ms(samples, 99.0)
        # Linear interpolation across rank 99 of [1..100] -> 99.01ms.
        assert 99.0 <= result <= 100.0

    def test_invalid_percentile_raises(self) -> None:
        with pytest.raises(ValueError):
            profile_storage.percentile_ms([1_000_000], -1.0)
        with pytest.raises(ValueError):
            profile_storage.percentile_ms([1_000_000], 101.0)


class TestSummarize:
    def test_emits_expected_keys(self) -> None:
        record = profile_storage.summarize("get_open_orders", [1_000_000, 2_000_000])
        assert set(record.keys()) == {
            "operation",
            "n",
            "p50_ms",
            "p99_ms",
            "mean_ms",
            "total_seconds",
        }
        assert record["operation"] == "get_open_orders"
        assert record["n"] == 2

    def test_empty_samples_returns_zero_metrics(self) -> None:
        record = profile_storage.summarize("save_order", [])
        assert record["n"] == 0
        assert record["p50_ms"] == 0.0
        assert record["p99_ms"] == 0.0
        assert record["mean_ms"] == 0.0
        assert record["total_seconds"] == 0.0


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestProfileOp:
    """End-to-end timing helpers run against a real in-memory DB."""

    @pytest.mark.asyncio
    async def test_get_open_orders_returns_n_samples(self, storage: SQLiteStorageAdapter) -> None:
        samples = await profile_storage._profile_op(  # pylint: disable=protected-access
            storage, "get_open_orders", iterations=10
        )
        assert len(samples) == 10
        assert all(s >= 0 for s in samples)

    @pytest.mark.asyncio
    async def test_save_order_writes_n_rows(self, storage: SQLiteStorageAdapter) -> None:
        samples = await profile_storage._profile_op(  # pylint: disable=protected-access
            storage, "save_order", iterations=5
        )
        assert len(samples) == 5
        # Confirm the writes actually landed.
        rows = await storage.get_orders()
        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_unknown_operation_raises(self, storage: SQLiteStorageAdapter) -> None:
        with pytest.raises(ValueError, match="unknown operation"):
            await profile_storage._profile_op(  # pylint: disable=protected-access
                storage, "nonexistent_op", iterations=1
            )


class TestSeedFixtures:
    @pytest.mark.asyncio
    async def test_seeds_expected_counts(self, storage: SQLiteStorageAdapter) -> None:
        await profile_storage._seed_fixtures(  # pylint: disable=protected-access
            storage, closed_orders=3, open_orders=2, trades=4
        )
        all_orders = await storage.get_orders()
        assert len(all_orders) == 5
        trades = await storage.get_trades()
        assert len(trades) == 4
