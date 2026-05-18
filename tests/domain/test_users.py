"""Tests for ``domain/users.py`` (Stage 7.1.A, Phase 7 / ADR-017)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wobblebot.domain.users import User, UserCredentials
from wobblebot.domain.value_objects import Timestamp

pytestmark = pytest.mark.unit


def _ts() -> Timestamp:
    return Timestamp(dt=datetime.now(UTC))


def _user(**overrides: object) -> User:
    base: dict[str, object] = {
        "username": "operator",
        "password_hash": "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
        "created_at": _ts(),
    }
    base.update(overrides)
    return User(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# User construction                                                     #
# --------------------------------------------------------------------- #


class TestUserConstruction:
    def test_minimal_user(self) -> None:
        u = _user()
        assert u.id is None  # before insert
        assert u.username == "operator"
        assert u.last_login_at is None  # never logged in

    def test_id_is_optional_default_none(self) -> None:
        u = _user()
        assert u.id is None

    def test_id_can_be_set(self) -> None:
        u = _user(id=42)
        assert u.id == 42

    def test_last_login_at_round_trip(self) -> None:
        when = _ts()
        u = _user(last_login_at=when)
        assert u.last_login_at == when

    def test_user_is_frozen(self) -> None:
        u = _user()
        with pytest.raises((ValidationError, TypeError)):
            u.username = "different"  # type: ignore[misc]


# --------------------------------------------------------------------- #
# User validation                                                       #
# --------------------------------------------------------------------- #


class TestUserValidation:
    def test_empty_username_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _user(username="")

    def test_long_username_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _user(username="a" * 65)

    def test_username_at_max_accepted(self) -> None:
        u = _user(username="a" * 64)
        assert len(u.username) == 64

    def test_empty_password_hash_rejected(self) -> None:
        """Pydantic min_length=1 is the primary defense; the SQL
        CHECK constraint is belt-and-suspenders."""
        with pytest.raises(ValidationError):
            _user(password_hash="")


# --------------------------------------------------------------------- #
# JSON round-trip                                                       #
# --------------------------------------------------------------------- #


class TestUserRoundTrip:
    def test_dump_and_validate_round_trips(self) -> None:
        u = _user(id=7, last_login_at=_ts())
        payload = u.model_dump_json()
        restored = User.model_validate_json(payload)
        assert restored == u

    def test_never_logged_in_round_trips(self) -> None:
        u = _user(id=7, last_login_at=None)
        restored = User.model_validate_json(u.model_dump_json())
        assert restored.last_login_at is None


# --------------------------------------------------------------------- #
# UserCredentials                                                       #
# --------------------------------------------------------------------- #


class TestUserCredentials:
    def test_minimal_credentials(self) -> None:
        c = UserCredentials(username="operator", password="hunter2")
        assert c.username == "operator"
        assert c.password == "hunter2"

    def test_credentials_are_frozen(self) -> None:
        c = UserCredentials(username="operator", password="x")
        with pytest.raises((ValidationError, TypeError)):
            c.password = "different"  # type: ignore[misc]

    def test_empty_username_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UserCredentials(username="", password="x")

    def test_empty_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UserCredentials(username="operator", password="")

    def test_long_username_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UserCredentials(username="a" * 65, password="x")
