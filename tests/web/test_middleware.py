"""Tests for ``web.middleware`` — CSRF + LoginRateLimit (Stage 7.1.C)."""

from __future__ import annotations

import asyncio
import time
from typing import cast

import pytest
from fastapi import HTTPException

from wobblebot.web.middleware import (
    CSRF_FORM_FIELD,
    CSRF_SESSION_KEY,
    LoginRateLimit,
    get_or_create_csrf_token,
    require_csrf_token,
    rotate_csrf_token,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- #
# CSRF helpers — pure function tests via a fake Request                 #
# --------------------------------------------------------------------- #


class _FakeRequest:
    """Just enough Request surface for the CSRF helpers."""

    def __init__(self, session: dict[str, object], form: dict[str, str] | None = None) -> None:
        self.session = session
        self._form = form or {}

    async def form(self) -> dict[str, str]:
        return self._form


class TestGetOrCreateCsrfToken:
    def test_mints_token_when_absent(self) -> None:
        session: dict[str, object] = {}
        req = cast("object", _FakeRequest(session=session))
        token = get_or_create_csrf_token(req)  # type: ignore[arg-type]
        assert isinstance(token, str)
        assert len(token) >= 32
        assert session[CSRF_SESSION_KEY] == token

    def test_reuses_existing_token(self) -> None:
        session: dict[str, object] = {CSRF_SESSION_KEY: "preset"}
        req = cast("object", _FakeRequest(session=session))
        token = get_or_create_csrf_token(req)  # type: ignore[arg-type]
        assert token == "preset"

    def test_mints_token_when_existing_is_empty_string(self) -> None:
        session: dict[str, object] = {CSRF_SESSION_KEY: ""}
        req = cast("object", _FakeRequest(session=session))
        token = get_or_create_csrf_token(req)  # type: ignore[arg-type]
        assert token != ""
        assert len(token) >= 32

    def test_mints_token_when_existing_is_wrong_type(self) -> None:
        session: dict[str, object] = {CSRF_SESSION_KEY: 12345}
        req = cast("object", _FakeRequest(session=session))
        token = get_or_create_csrf_token(req)  # type: ignore[arg-type]
        assert isinstance(token, str)


class TestRotateCsrfToken:
    def test_replaces_existing_token(self) -> None:
        session: dict[str, object] = {CSRF_SESSION_KEY: "preset"}
        req = cast("object", _FakeRequest(session=session))
        new_token = rotate_csrf_token(req)  # type: ignore[arg-type]
        assert new_token != "preset"
        assert session[CSRF_SESSION_KEY] == new_token


@pytest.mark.asyncio
class TestRequireCsrfToken:
    async def test_raises_when_session_has_no_token(self) -> None:
        req = cast("object", _FakeRequest(session={}, form={CSRF_FORM_FIELD: "x"}))
        with pytest.raises(HTTPException) as exc_info:
            await require_csrf_token(req)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 403

    async def test_raises_when_form_missing_token(self) -> None:
        req = cast(
            "object",
            _FakeRequest(session={CSRF_SESSION_KEY: "expected"}, form={}),
        )
        with pytest.raises(HTTPException) as exc_info:
            await require_csrf_token(req)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 403

    async def test_raises_when_tokens_mismatch(self) -> None:
        req = cast(
            "object",
            _FakeRequest(
                session={CSRF_SESSION_KEY: "expected"},
                form={CSRF_FORM_FIELD: "different"},
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            await require_csrf_token(req)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 403

    async def test_passes_when_tokens_match(self) -> None:
        req = cast(
            "object",
            _FakeRequest(
                session={CSRF_SESSION_KEY: "match-me"},
                form={CSRF_FORM_FIELD: "match-me"},
            ),
        )
        # No exception → success
        await require_csrf_token(req)  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# LoginRateLimit                                                        #
# --------------------------------------------------------------------- #


class TestLoginRateLimitConstruction:
    def test_max_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            LoginRateLimit(max_attempts=0, window_seconds=60)

    def test_window_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            LoginRateLimit(max_attempts=5, window_seconds=0)


@pytest.mark.asyncio
class TestLoginRateLimitBehavior:
    async def test_first_attempts_allowed(self) -> None:
        gate = LoginRateLimit(max_attempts=3, window_seconds=60)
        assert await gate.allow("1.2.3.4") is True
        assert await gate.allow("1.2.3.4") is True
        assert await gate.allow("1.2.3.4") is True

    async def test_blocks_after_max(self) -> None:
        gate = LoginRateLimit(max_attempts=2, window_seconds=60)
        assert await gate.allow("1.2.3.4") is True
        assert await gate.allow("1.2.3.4") is True
        assert await gate.allow("1.2.3.4") is False

    async def test_reset_clears_bucket(self) -> None:
        gate = LoginRateLimit(max_attempts=2, window_seconds=60)
        await gate.allow("1.2.3.4")
        await gate.allow("1.2.3.4")
        assert await gate.allow("1.2.3.4") is False
        await gate.reset("1.2.3.4")
        assert await gate.allow("1.2.3.4") is True

    async def test_per_ip_isolated(self) -> None:
        gate = LoginRateLimit(max_attempts=1, window_seconds=60)
        assert await gate.allow("1.1.1.1") is True
        assert await gate.allow("1.1.1.1") is False
        # Different IP unaffected.
        assert await gate.allow("2.2.2.2") is True

    async def test_window_expiry_releases_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gate = LoginRateLimit(max_attempts=2, window_seconds=60)
        base = time.monotonic()

        # Pin the monotonic clock so attempts pile up at t=0.
        monkeypatch.setattr("time.monotonic", lambda: base)
        assert await gate.allow("1.1.1.1") is True
        assert await gate.allow("1.1.1.1") is True
        assert await gate.allow("1.1.1.1") is False

        # Advance past the window; old attempts age out.
        monkeypatch.setattr("time.monotonic", lambda: base + 61)
        assert await gate.allow("1.1.1.1") is True

    async def test_attempts_for_introspection(self) -> None:
        gate = LoginRateLimit(max_attempts=5, window_seconds=60)
        assert await gate.attempts_for("1.1.1.1") == 0
        await gate.allow("1.1.1.1")
        await gate.allow("1.1.1.1")
        assert await gate.attempts_for("1.1.1.1") == 2

    async def test_concurrent_attempts_serialized(self) -> None:
        """Lock prevents both racing attempts from passing past the cap."""
        gate = LoginRateLimit(max_attempts=1, window_seconds=60)
        results = await asyncio.gather(
            gate.allow("1.1.1.1"),
            gate.allow("1.1.1.1"),
            gate.allow("1.1.1.1"),
        )
        # Exactly one should have been allowed past the cap.
        assert results.count(True) == 1
        assert results.count(False) == 2
