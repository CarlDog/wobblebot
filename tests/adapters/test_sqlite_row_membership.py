"""Real SQLite rows must retain column-presence checks across lint migrations."""

import sqlite3

import pytest

from wobblebot.adapters.sqlite_storage_rowmap import row_to_transfer_result


@pytest.mark.parametrize("submission_state", ["reserved", "rejected", None])
def test_transfer_state_uses_column_names_not_row_values(submission_state: str | None) -> None:
    """Keep persisted state and legacy fallback even when a value resembles a key."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        # A legacy row deliberately contains the missing column's name as DATA.
        # Replacing the adapter's key check with row membership would then try
        # to read a nonexistent column. Modern rows expose the converse bug:
        # a present column's name is absent from values, losing persisted state.
        proposal_id = "submission_state" if submission_state is None else "proposal-1"
        state_column = ", ? AS submission_state" if submission_state is not None else ""
        parameters = (proposal_id, submission_state) if submission_state else (proposal_id,)
        row = connection.execute(
            "SELECT ? AS proposal_id, 'claim-1' AS transaction_id, 'pending' AS status, "
            "'0' AS executed_amount, 'exchange_to_bank' AS direction, 'USD' AS asset, "
            "'2026-10-03T00:00:00+00:00' AS timestamp" + state_column,
            parameters,
        ).fetchone()

        # The rule's unsafe rewrite is precisely the regression under test.
        assert ("submission_state" in row.keys()) is (submission_state is not None)  # noqa: SIM118
        assert ("submission_state" in row) is (submission_state is None)
        result = row_to_transfer_result(row)
        assert result.submission_state == (submission_state or "accepted")
        assert result.proposal_id == proposal_id
    finally:
        connection.close()
