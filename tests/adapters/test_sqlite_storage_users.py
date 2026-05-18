"""Tests for the ``users`` adapter methods (Stage 7.1.A, ADR-017)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.exceptions import StorageError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


_TEST_HASH = "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
_OTHER_HASH = "$2b$12$ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"


# --------------------------------------------------------------------- #
# create_user                                                           #
# --------------------------------------------------------------------- #


class TestCreateUser:
    async def test_creates_with_id_and_timestamps(self, storage: SQLiteStorageAdapter) -> None:
        user = await storage.create_user("operator", _TEST_HASH)
        assert user.id is not None
        assert user.id > 0
        assert user.username == "operator"
        assert user.password_hash == _TEST_HASH
        assert user.last_login_at is None
        # created_at is set to roughly now
        delta = abs((datetime.now(UTC) - user.created_at.dt).total_seconds())
        assert delta < 5.0

    async def test_round_trips_via_get_by_username(self, storage: SQLiteStorageAdapter) -> None:
        created = await storage.create_user("operator", _TEST_HASH)
        fetched = await storage.get_user_by_username("operator")
        assert fetched is not None
        assert fetched == created

    async def test_duplicate_username_raises(self, storage: SQLiteStorageAdapter) -> None:
        await storage.create_user("operator", _TEST_HASH)
        with pytest.raises(StorageError, match="operator"):
            await storage.create_user("operator", _OTHER_HASH)

    async def test_multiple_distinct_users(self, storage: SQLiteStorageAdapter) -> None:
        u1 = await storage.create_user("alice", _TEST_HASH)
        u2 = await storage.create_user("bob", _OTHER_HASH)
        assert u1.id != u2.id
        assert u2.id > u1.id  # AUTOINCREMENT monotonic


# --------------------------------------------------------------------- #
# get_user_by_username                                                  #
# --------------------------------------------------------------------- #


class TestGetUserByUsername:
    async def test_returns_none_when_missing(self, storage: SQLiteStorageAdapter) -> None:
        assert await storage.get_user_by_username("nope") is None

    async def test_returns_user_when_present(self, storage: SQLiteStorageAdapter) -> None:
        await storage.create_user("operator", _TEST_HASH)
        fetched = await storage.get_user_by_username("operator")
        assert fetched is not None
        assert fetched.username == "operator"
        assert fetched.password_hash == _TEST_HASH

    async def test_case_sensitive_lookup(self, storage: SQLiteStorageAdapter) -> None:
        """SQLite default collation is case-sensitive — Operator != operator."""
        await storage.create_user("operator", _TEST_HASH)
        assert await storage.get_user_by_username("Operator") is None
        assert await storage.get_user_by_username("OPERATOR") is None


# --------------------------------------------------------------------- #
# update_user_last_login                                                #
# --------------------------------------------------------------------- #


class TestUpdateUserLastLogin:
    async def test_sets_timestamp_on_first_call(self, storage: SQLiteStorageAdapter) -> None:
        user = await storage.create_user("operator", _TEST_HASH)
        assert user.id is not None
        when = Timestamp(dt=datetime.now(UTC))
        await storage.update_user_last_login(user.id, when)
        fetched = await storage.get_user_by_username("operator")
        assert fetched is not None
        assert fetched.last_login_at is not None
        # ISO round-trip preserves the moment within microsecond precision
        assert abs((fetched.last_login_at.dt - when.dt).total_seconds()) < 0.001

    async def test_idempotent_overwrites(self, storage: SQLiteStorageAdapter) -> None:
        from datetime import timedelta

        user = await storage.create_user("operator", _TEST_HASH)
        assert user.id is not None
        first = Timestamp(dt=datetime.now(UTC))
        second = Timestamp(dt=datetime.now(UTC) + timedelta(seconds=10))
        await storage.update_user_last_login(user.id, first)
        await storage.update_user_last_login(user.id, second)
        fetched = await storage.get_user_by_username("operator")
        assert fetched is not None
        assert fetched.last_login_at is not None
        assert abs((fetched.last_login_at.dt - second.dt).total_seconds()) < 0.001

    async def test_missing_user_raises(self, storage: SQLiteStorageAdapter) -> None:
        when = Timestamp(dt=datetime.now(UTC))
        with pytest.raises(StorageError, match="9999"):
            await storage.update_user_last_login(9999, when)


# --------------------------------------------------------------------- #
# Schema constraints                                                    #
# --------------------------------------------------------------------- #


class TestSchemaGuards:
    async def test_unique_index_enforced(self, storage: SQLiteStorageAdapter) -> None:
        """The UNIQUE(username) constraint backstops the application-
        level duplicate-check. Verified above via test_duplicate_username_raises
        — this test confirms the same shape against the empty case
        (creating immediately after duplicate-failure should still work
        with a different username)."""
        await storage.create_user("alice", _TEST_HASH)
        with pytest.raises(StorageError):
            await storage.create_user("alice", _OTHER_HASH)
        # Different username still works
        bob = await storage.create_user("bob", _OTHER_HASH)
        assert bob.username == "bob"


# --------------------------------------------------------------------- #
# Row-mapping symmetry                                                  #
# --------------------------------------------------------------------- #


async def test_password_hash_round_trips_with_bcrypt_prefix(
    storage: SQLiteStorageAdapter,
) -> None:
    """Real bcrypt hashes contain ``$`` separators + base64 chars. The
    string column round-trips byte-for-byte."""
    realistic_hash = "$2b$12$ZqXi6F8aB7y0KZGz5e7DyukoYJC9Y7pZGqkqyJpyJ7E.5kT4jHnDi"
    await storage.create_user("operator", realistic_hash)
    fetched = await storage.get_user_by_username("operator")
    assert fetched is not None
    assert fetched.password_hash == realistic_hash
