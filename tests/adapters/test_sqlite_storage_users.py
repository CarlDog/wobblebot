"""Tests for the ``users`` adapter methods (Stage 7.1.A, ADR-017)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.users import UserPreferences
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

    async def test_case_insensitive_lookup(self, storage: SQLiteStorageAdapter) -> None:
        """Lookups are case-insensitive — preserved-casing storage,
        type-anything login. Pairs with the COLLATE NOCASE on
        get_user_by_username + the idx_users_username_nocase
        UNIQUE index that prevents case-variant collisions."""
        created = await storage.create_user("CarlDog", _TEST_HASH)
        assert created.username == "CarlDog"  # stored as typed
        # Any casing at the lookup layer finds the same row.
        for variant in ("CarlDog", "carldog", "CARLDOG", "carlDOG"):
            got = await storage.get_user_by_username(variant)
            assert got is not None, f"lookup failed for variant {variant!r}"
            assert got.username == "CarlDog"  # display value unchanged
            assert got.id == created.id

    async def test_case_insensitive_collision_raises(self, storage: SQLiteStorageAdapter) -> None:
        """Creating "carldog" when "CarlDog" exists must fail —
        case-variant collisions are blocked at the DB layer."""
        await storage.create_user("CarlDog", _TEST_HASH)
        with pytest.raises(StorageError):
            await storage.create_user("carldog", _OTHER_HASH)
        with pytest.raises(StorageError):
            await storage.create_user("CARLDOG", _OTHER_HASH)


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


# --------------------------------------------------------------------- #
# Stage 8.4 follow-up: user_preferences                                 #
# --------------------------------------------------------------------- #


class TestUserPreferences:
    """Storage round-trip for the per-user UI prefs table.

    Auto-create on first read returns a UTC default; subsequent
    update persists; subsequent read sees the new value.
    """

    async def test_first_read_auto_creates_default_utc(self, storage: SQLiteStorageAdapter) -> None:
        user = await storage.create_user("op", _TEST_HASH)
        assert user.id is not None
        prefs = await storage.get_user_preferences(user.id)
        assert prefs.user_id == user.id
        assert prefs.timezone == "UTC"
        # updated_at is recent (within a few seconds).
        delta = abs((datetime.now(UTC) - prefs.updated_at.dt).total_seconds())
        assert delta < 5.0

    async def test_subsequent_read_returns_same_row(self, storage: SQLiteStorageAdapter) -> None:
        user = await storage.create_user("op", _TEST_HASH)
        assert user.id is not None
        first = await storage.get_user_preferences(user.id)
        # Sleep not needed — second read against existing row should
        # see the same updated_at (not bumped on read).
        second = await storage.get_user_preferences(user.id)
        assert second.updated_at.dt == first.updated_at.dt
        assert second.timezone == first.timezone

    async def test_update_changes_timezone(self, storage: SQLiteStorageAdapter) -> None:
        user = await storage.create_user("op", _TEST_HASH)
        assert user.id is not None
        # Auto-create default.
        await storage.get_user_preferences(user.id)
        # Update to Chicago tz.
        new_prefs = UserPreferences(
            user_id=user.id,
            timezone="America/Chicago",
            updated_at=Timestamp(dt=datetime.now(UTC)),
        )
        await storage.update_user_preferences(new_prefs)
        # Read back; should see new tz.
        roundtrip = await storage.get_user_preferences(user.id)
        assert roundtrip.timezone == "America/Chicago"

    async def test_update_is_upsert(self, storage: SQLiteStorageAdapter) -> None:
        """update_user_preferences should ON CONFLICT update, not raise,
        when called multiple times for the same user_id."""
        user = await storage.create_user("op", _TEST_HASH)
        assert user.id is not None
        first = UserPreferences(
            user_id=user.id,
            timezone="Europe/London",
            updated_at=Timestamp(dt=datetime.now(UTC)),
        )
        await storage.update_user_preferences(first)
        second = UserPreferences(
            user_id=user.id,
            timezone="Asia/Tokyo",
            updated_at=Timestamp(dt=datetime.now(UTC)),
        )
        await storage.update_user_preferences(second)
        roundtrip = await storage.get_user_preferences(user.id)
        assert roundtrip.timezone == "Asia/Tokyo"

    async def test_per_user_isolation(self, storage: SQLiteStorageAdapter) -> None:
        """Each user has their own preferences row; one's update
        doesn't affect another."""
        user_a = await storage.create_user("alice", _TEST_HASH)
        user_b = await storage.create_user("bob", _OTHER_HASH)
        assert user_a.id is not None
        assert user_b.id is not None
        await storage.update_user_preferences(
            UserPreferences(
                user_id=user_a.id,
                timezone="America/New_York",
                updated_at=Timestamp(dt=datetime.now(UTC)),
            )
        )
        await storage.update_user_preferences(
            UserPreferences(
                user_id=user_b.id,
                timezone="Australia/Sydney",
                updated_at=Timestamp(dt=datetime.now(UTC)),
            )
        )
        prefs_a = await storage.get_user_preferences(user_a.id)
        prefs_b = await storage.get_user_preferences(user_b.id)
        assert prefs_a.timezone == "America/New_York"
        assert prefs_b.timezone == "Australia/Sydney"
