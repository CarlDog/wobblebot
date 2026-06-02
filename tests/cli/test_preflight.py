"""Tests for cli/preflight's ADR-003 key-scope gate (P0.3).

The full preflight entry point is integration territory (it runs against
live Kraken); these target the ``_audit_trade_key_scope`` helper — the
ADR-003 gate that refuses exit 0 when the trade key can withdraw.
"""

from __future__ import annotations

import pytest

from wobblebot.cli.preflight import _audit_trade_key_scope
from wobblebot.ports.exceptions import ExchangeError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeAdapter:
    """Minimal stand-in exposing only ``has_withdraw_scope``."""

    def __init__(self, *, can_withdraw: bool | None = None, error: Exception | None = None) -> None:
        self._can_withdraw = can_withdraw
        self._error = error

    async def has_withdraw_scope(self) -> bool:
        if self._error is not None:
            raise self._error
        assert self._can_withdraw is not None
        return self._can_withdraw


async def test_no_withdraw_scope_passes() -> None:
    adapter = _FakeAdapter(can_withdraw=False)
    assert await _audit_trade_key_scope(adapter) is None  # type: ignore[arg-type]


async def test_withdraw_scope_is_a_violation_exit_3() -> None:
    # The trade key having withdrawal permission is a hard ADR-003 stop.
    adapter = _FakeAdapter(can_withdraw=True)
    assert await _audit_trade_key_scope(adapter) == 3  # type: ignore[arg-type]


async def test_probe_error_warns_and_continues() -> None:
    # A transient probe failure must NOT block preflight (returns None so
    # the validate run still proceeds) — it can't determine scope, but a
    # network blip shouldn't fail a legitimate diagnostic.
    adapter = _FakeAdapter(error=ExchangeError("transient boom"))
    assert await _audit_trade_key_scope(adapter) is None  # type: ignore[arg-type]
