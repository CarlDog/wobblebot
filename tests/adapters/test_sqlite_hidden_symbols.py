"""Tests for the hidden_symbols table (Group 3, dashboard card visibility).

UI-local state in the same posture as ``reanchor_snoozes``: hiding a card
moves no money and touches no engine state, so it never crosses the
ADR-002 firewall.

Its own table rather than a column on ``user_preferences`` — that shape
is a silent-data-loss trap, and ``test_a_timezone_save_cannot_blank_the_
hidden_set`` below is the regression test for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.users import UserPreferences
from wobblebot.domain.value_objects import Symbol, Timestamp

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_BTC = Symbol(base="BTC", quote="USD")
_BABY = Symbol(base="BABY", quote="USD")
_USER = 1
_OTHER_USER = 2


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


class TestHideAndReveal:
    async def test_nothing_hidden_by_default(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_hidden_symbols(_USER) == frozenset()

    async def test_hide_then_read(self, storage: SQLiteStorageAdapter) -> None:
        await storage.set_symbol_hidden(_USER, _BABY, True)
        assert await storage.get_hidden_symbols(_USER) == frozenset({_BABY})

    async def test_reveal_removes_the_row(self, storage: SQLiteStorageAdapter) -> None:
        await storage.set_symbol_hidden(_USER, _BABY, True)
        await storage.set_symbol_hidden(_USER, _BABY, False)
        assert await storage.get_hidden_symbols(_USER) == frozenset()

    async def test_hiding_twice_is_a_no_op(self, storage: SQLiteStorageAdapter) -> None:
        """Absence is visible, so the table only ever holds what was
        actively hidden — a double-click must not error or duplicate."""
        await storage.set_symbol_hidden(_USER, _BABY, True)
        await storage.set_symbol_hidden(_USER, _BABY, True)
        assert await storage.get_hidden_symbols(_USER) == frozenset({_BABY})

    async def test_revealing_something_visible_is_a_no_op(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        await storage.set_symbol_hidden(_USER, _BABY, False)
        assert await storage.get_hidden_symbols(_USER) == frozenset()

    async def test_hidden_sets_are_per_user(self, storage: SQLiteStorageAdapter) -> None:
        """Two operators must not fight over one visibility list."""
        await storage.set_symbol_hidden(_USER, _BABY, True)
        assert await storage.get_hidden_symbols(_OTHER_USER) == frozenset()
        await storage.set_symbol_hidden(_OTHER_USER, _BTC, True)
        assert await storage.get_hidden_symbols(_USER) == frozenset({_BABY})
        assert await storage.get_hidden_symbols(_OTHER_USER) == frozenset({_BTC})

    async def test_storage_applies_no_eligibility_policy(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """Only the caller knows which symbols are configured. Storage
        reports what was written; the reader neutralizes a symbol that has
        since become ineligible, so a config change cannot strand a card
        permanently out of sight."""
        await storage.set_symbol_hidden(_USER, _BTC, True)
        assert await storage.get_hidden_symbols(_USER) == frozenset({_BTC})


class TestNotAUserPreferencesColumn:
    async def test_a_timezone_save_cannot_blank_the_hidden_set(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The reason this is a table and not a preferences column.

        ``update_user_preferences`` is a full-row upsert rebuilt from the
        timezone form alone, so a ``hidden_symbols`` field on that model
        would be blanked by every timezone save — a silent data loss no
        naive test of the hide feature would catch.
        """
        user = await storage.create_user("operator", "x" * 60)
        await storage.set_symbol_hidden(user.id, _BABY, True)
        await storage.update_user_preferences(
            UserPreferences(
                user_id=user.id,
                timezone="America/Chicago",
                updated_at=Timestamp(dt=datetime.now(UTC)),
            )
        )
        assert await storage.get_hidden_symbols(user.id) == frozenset({_BABY})
