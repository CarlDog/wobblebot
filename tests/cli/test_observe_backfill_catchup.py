"""Tests for cli/observe._resolve_catchup_since (P2 slice 1, item 2).

``--catchup`` / ``--since auto`` resolves each symbol's backfill lower
bound from its latest stored ``price_snapshots.observed_at`` — the same
cursor the startup auto-gap-fill reads. Per-symbol decision matrix:

- No prior history          -> None (WARN: seed with --since/--days)
- Latest >= until           -> None (INFO: already current)
- Latest < until            -> that latest timestamp
- Storage failure           -> WobbleBotPortError propagates (caller
                               marks the symbol errored and continues)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.observe import _resolve_catchup_since
from wobblebot.domain.value_objects import Price, Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_UNTIL = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _seed_snapshot(storage: SQLiteStorageAdapter, symbol: Symbol, at: datetime) -> None:
    await storage.save_price_snapshot(
        symbol,
        Price(amount=Decimal("79000"), currency=symbol.quote),
        Timestamp(dt=at),
    )


class TestResolveCatchupSince:
    async def test_no_history_returns_none_with_warning(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.observe"):
            resolved = await _resolve_catchup_since(storage, _BTC, until=_UNTIL)
        assert resolved is None
        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "no prior history" in rendered
        assert "BTC/USD" in rendered

    async def test_stale_history_returns_latest_observed_at(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        latest = datetime(2026, 8, 5, 9, 30, 0, tzinfo=UTC)
        await _seed_snapshot(storage, _BTC, datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC))
        await _seed_snapshot(storage, _BTC, latest)
        resolved = await _resolve_catchup_since(storage, _BTC, until=_UNTIL)
        assert resolved == latest

    async def test_already_current_returns_none_with_info(
        self, storage: SQLiteStorageAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        await _seed_snapshot(storage, _BTC, _UNTIL)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.observe"):
            resolved = await _resolve_catchup_since(storage, _BTC, until=_UNTIL)
        assert resolved is None
        assert any("already current" in r.getMessage() for r in caplog.records)

    async def test_other_symbols_history_does_not_leak(self, storage: SQLiteStorageAdapter) -> None:
        """ETH history must not resolve a BTC catchup — the cursor is
        strictly per-symbol."""
        await _seed_snapshot(
            storage, Symbol(base="ETH", quote="USD"), datetime(2026, 8, 5, tzinfo=UTC)
        )
        resolved = await _resolve_catchup_since(storage, _BTC, until=_UNTIL)
        assert resolved is None
