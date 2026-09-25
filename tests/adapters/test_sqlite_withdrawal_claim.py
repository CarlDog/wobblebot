"""A withdrawal claim must commit before the external request can begin."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.harvester import TransferResult

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _claim(proposal_id: str, amount: str = "60") -> TransferResult:
    return TransferResult(
        proposal_id=proposal_id,
        transaction_id=f"claim-{proposal_id}",
        status="pending",
        submission_state="reserved",
        executed_amount=Decimal(amount),
        direction="exchange_to_bank",
        asset="USD",
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )


@pytest.mark.parametrize(
    ("second_proposal", "cap", "expected_loser"),
    [
        ("first", "1000", "already_claimed"),
        ("second", "100", "unresolved_claim"),
    ],
)
async def test_concurrent_process_connections_only_reserve_once(
    tmp_path: Path, second_proposal: str, cap: str, expected_loser: str
) -> None:
    db_path = tmp_path / "harvest.db"
    first = SQLiteStorageAdapter(db_path)
    second = SQLiteStorageAdapter(db_path)
    await first.connect()
    await second.connect()
    try:
        outcomes = await asyncio.gather(
            first.reserve_withdrawal(_claim("first"), daily_cap=Decimal(cap)),
            second.reserve_withdrawal(_claim(second_proposal), daily_cap=Decimal(cap)),
        )
        assert sorted(outcomes) == sorted(["reserved", expected_loser])
        rows = await first.get_transfer_results()
        assert len(rows) == 1
        assert rows[0].submission_state == "reserved"
    finally:
        await first.close()
        await second.close()


async def test_cap_still_applies_after_a_claim_is_accepted(tmp_path: Path) -> None:
    storage = SQLiteStorageAdapter(tmp_path / "harvest.db")
    await storage.connect()
    try:
        first = _claim("first")
        assert await storage.reserve_withdrawal(first, daily_cap=Decimal("100")) == "reserved"
        await storage.finalize_withdrawal_claim(
            first.transaction_id, transaction_id="kraken-ref", submission_state="accepted"
        )
        assert (
            await storage.reserve_withdrawal(_claim("second"), daily_cap=Decimal("100"))
            == "cap_exceeded"
        )
        assert len(await storage.get_transfer_results()) == 1
    finally:
        await storage.close()


async def test_claim_survives_restart_and_blocks_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "harvest.db"
    first = SQLiteStorageAdapter(db_path)
    await first.connect()
    assert await first.reserve_withdrawal(_claim("first"), daily_cap=Decimal("1000")) == "reserved"
    await first.close()

    restarted = SQLiteStorageAdapter(db_path)
    await restarted.connect()
    try:
        assert (
            await restarted.reserve_withdrawal(_claim("first"), daily_cap=Decimal("1000"))
            == "already_claimed"
        )
        rows = await restarted.get_transfer_results()
        assert rows[0].transaction_id == "claim-first"
        assert rows[0].submission_state == "reserved"
    finally:
        await restarted.close()


async def test_old_unknown_claim_halts_new_withdrawals(tmp_path: Path) -> None:
    storage = SQLiteStorageAdapter(tmp_path / "harvest.db")
    await storage.connect()
    try:
        old = _claim("old").model_copy(
            update={
                "submission_state": "unknown",
                "timestamp": Timestamp(dt=datetime.now(UTC) - timedelta(hours=25)),
            }
        )
        await storage.save_transfer_result(old)
        assert (
            await storage.reserve_withdrawal(_claim("new"), daily_cap=Decimal("1000"))
            == "unresolved_claim"
        )
    finally:
        await storage.close()


async def test_legacy_results_gain_submission_state_without_losing_history(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE transfer_results (id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL, "
            "transaction_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
            "executed_amount TEXT NOT NULL, direction TEXT NOT NULL, asset TEXT NOT NULL, "
            "timestamp TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO transfer_results "
            "(proposal_id, transaction_id, status, executed_amount, direction, asset, timestamp) "
            "VALUES (?, ?, ?, '10', 'exchange_to_bank', 'USD', ?)",
            [
                ("old-ok", "kraken-ref", "pending", datetime.now(UTC).isoformat()),
                ("old-fail", "failed-old", "failed", datetime.now(UTC).isoformat()),
            ],
        )
        conn.commit()
    storage = SQLiteStorageAdapter(db_path)
    await storage.connect()
    try:
        rows = {r.proposal_id: r for r in await storage.get_transfer_results()}
        assert rows["old-ok"].submission_state == "accepted"
        assert rows["old-fail"].submission_state == "rejected"
        assert rows["old-ok"].transaction_id == "kraken-ref"
        assert rows["old-fail"].transaction_id == "failed-old"
    finally:
        await storage.close()
