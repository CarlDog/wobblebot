"""SQLiteStorageAdapter tests for the transfer-proposals persistence (Stage 4.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError
from wobblebot.ports.harvester import TransferProposal

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _proposal(
    *,
    proposal_id: str | None = None,
    direction: str = "exchange_to_bank",
    asset: str = "USD",
    amount: str = "100",
    rationale: str = "test rationale",
    current_balance: str = "600",
    target_balance: str = "500",
    minutes_ago: int = 1,
) -> TransferProposal:
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return TransferProposal(
        proposal_id=proposal_id or str(uuid4()),
        direction=direction,  # type: ignore[arg-type]
        asset=asset,
        amount=Decimal(amount),
        rationale=rationale,
        current_exchange_balance=Decimal(current_balance),
        target_exchange_balance=Decimal(target_balance),
        created_at=Timestamp(dt=created),
    )


async def test_save_then_read(storage: SQLiteStorageAdapter) -> None:
    proposal = _proposal()
    await storage.save_transfer_proposal(proposal)
    result = await storage.get_transfer_proposals()
    assert len(result) == 1
    got = result[0]
    assert got.proposal_id == proposal.proposal_id
    assert got.direction == "exchange_to_bank"
    assert got.amount == Decimal("100")
    assert got.current_exchange_balance == Decimal("600")
    assert got.target_exchange_balance == Decimal("500")


async def test_decimal_precision_round_trips(storage: SQLiteStorageAdapter) -> None:
    """Decimals must round-trip exactly through the TEXT column."""
    proposal = _proposal(amount="225.0040")
    await storage.save_transfer_proposal(proposal)
    got = (await storage.get_transfer_proposals())[0]
    assert got.amount == Decimal("225.0040")


async def test_default_order_is_created_desc(storage: SQLiteStorageAdapter) -> None:
    for offset, asset_tag in [(30, "old"), (10, "mid"), (1, "new")]:
        await storage.save_transfer_proposal(
            _proposal(
                proposal_id=f"p-{asset_tag}",
                minutes_ago=offset,
            ),
        )
    result = await storage.get_transfer_proposals()
    assert [p.proposal_id for p in result] == ["p-new", "p-mid", "p-old"]


async def test_direction_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_transfer_proposal(
        _proposal(proposal_id="p-out", direction="exchange_to_bank"),
    )
    await storage.save_transfer_proposal(
        _proposal(proposal_id="p-in", direction="bank_to_exchange"),
    )
    result = await storage.get_transfer_proposals(direction="exchange_to_bank")
    assert len(result) == 1
    assert result[0].proposal_id == "p-out"


async def test_asset_filter(storage: SQLiteStorageAdapter) -> None:
    await storage.save_transfer_proposal(
        _proposal(proposal_id="p-usd", asset="USD"),
    )
    # The schema doesn't constrain asset; future per-asset coverage
    # would let an operator save BTC proposals here. For now we just
    # use a non-USD tag to verify the filter wires through.
    await storage.save_transfer_proposal(
        _proposal(proposal_id="p-eur", asset="EUR"),
    )
    result = await storage.get_transfer_proposals(asset="USD")
    assert len(result) == 1
    assert result[0].asset == "USD"


async def test_since_filter_inclusive(storage: SQLiteStorageAdapter) -> None:
    base = datetime.now(UTC) - timedelta(hours=2)
    for off, tag in [(0, "oldest"), (30, "mid"), (60, "newer"), (90, "newest")]:
        proposal = TransferProposal(
            proposal_id=f"p-{tag}",
            direction="exchange_to_bank",
            asset="USD",
            amount=Decimal("100"),
            rationale="x",
            current_exchange_balance=Decimal("600"),
            target_exchange_balance=Decimal("500"),
            created_at=Timestamp(dt=base + timedelta(minutes=off)),
        )
        await storage.save_transfer_proposal(proposal)
    cutoff = base + timedelta(minutes=30)
    result = await storage.get_transfer_proposals(since=cutoff)
    assert len(result) == 3
    assert all(p.created_at.dt >= cutoff for p in result)


async def test_limit_caps_rows(storage: SQLiteStorageAdapter) -> None:
    for i in range(5):
        await storage.save_transfer_proposal(
            _proposal(proposal_id=f"p-{i}", minutes_ago=i),
        )
    result = await storage.get_transfer_proposals(limit=2)
    assert len(result) == 2


async def test_empty_returns_empty_list(storage: SQLiteStorageAdapter) -> None:
    assert await storage.get_transfer_proposals() == []


async def test_duplicate_proposal_id_rejected(storage: SQLiteStorageAdapter) -> None:
    """The DB-level UNIQUE constraint catches accidental double-inserts —
    e.g. a daemon retry after a network blip that already wrote the row.
    The error wraps as StorageError so the caller sees a typed failure."""
    proposal = _proposal(proposal_id="p-unique")
    await storage.save_transfer_proposal(proposal)
    with pytest.raises(StorageError, match="p-unique"):
        await storage.save_transfer_proposal(proposal)


async def test_direction_check_constraint(storage: SQLiteStorageAdapter) -> None:
    """The CHECK constraint on direction guards against direct-INSERT
    paths that bypass Pydantic. We can't construct an invalid value
    through the model (Literal blocks it), so this just verifies the
    happy-path values both work."""
    for direction in ("exchange_to_bank", "bank_to_exchange"):
        await storage.save_transfer_proposal(
            _proposal(proposal_id=f"p-{direction}", direction=direction),
        )
    result = await storage.get_transfer_proposals()
    assert {p.direction for p in result} == {"exchange_to_bank", "bank_to_exchange"}


async def test_long_rationale_survives(storage: SQLiteStorageAdapter) -> None:
    long_rationale = (
        "Balance $1200 above surplus_threshold $500; scrape $825 to bank "
        "(target post-scrape balance $375); constrained by max_withdrawal_per_day_usd "
        "(today's total $0 + $825 = cap $1000). Operator should manually verify "
        "the bank destination label is current before approving 4.4 execution."
    )
    await storage.save_transfer_proposal(_proposal(rationale=long_rationale))
    got = (await storage.get_transfer_proposals())[0]
    assert got.rationale == long_rationale
