"""Tests for cli/preflight's ADR-003 key-scope gate (P0.3).

The full preflight entry point is integration territory (it runs against
live Kraken); these target the ``_audit_trade_key_scope`` helper — the
ADR-003 gate that refuses exit 0 when the trade key can withdraw.

v1.1 test-hardening (test-honesty audit, P9 "preflight gate
orchestration") adds ``TestRunOrchestration``: ``_audit_trade_key_scope``
itself was solid, but nothing drove ``_run`` to prove the gate's
early-return actually pre-empts the validate run at the CLI-wiring
level -- a regression moving the gate call after ``engine.step``, or
skipping it for a specific ``dry_run`` value, would still pass every
existing test.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.cli import preflight as preflight_module
from wobblebot.cli.preflight import _audit_trade_key_scope
from wobblebot.config.cli import PreflightConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.domain.value_objects import Price, Symbol
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


async def test_violation_message_names_both_causes(caplog: pytest.LogCaptureFixture) -> None:
    """Mirrors cli/harvest's gate: the probe answers for whatever key the
    env var holds, so the refusal must offer both the scope cause and the
    wrong-key cause (a stale deployment env store handing the trade slot
    the Harvester key) instead of sending the operator to the Kraken UI
    with a single diagnosis the probe cannot actually support.
    """
    adapter = _FakeAdapter(can_withdraw=True)
    with caplog.at_level(logging.ERROR, logger="wobblebot.cli.preflight"):
        assert await _audit_trade_key_scope(adapter) == 3  # type: ignore[arg-type]
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "ADR-003 VIOLATION" in message
    assert "Funds permissions - Withdraw" in message  # cause (a)
    assert "DIFFERENT key" in message  # cause (b)
    assert "NOT just the Kraken UI" in message  # the discriminating step
    assert "EAPI:Invalid key" in message  # the narrowing auth-validity fact
    assert "KRAKEN_TRADER_API_KEY" in message


async def test_violation_message_logs_no_credential_material(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Names the env VAR, never any part of its value — not even a fragment.

    Sentinel is low-entropy on purpose: a realistic ``sk-…`` fixture trips
    the pre-commit gitleaks scan, and an allowlist entry to keep a prettier
    fixture would weaken a load-bearing check.
    """
    monkeypatch.setenv("KRAKEN_TRADER_API_KEY", "placeholder-trader-do-not-log")
    adapter = _FakeAdapter(can_withdraw=True)
    with caplog.at_level(logging.ERROR, logger="wobblebot.cli.preflight"):
        await _audit_trade_key_scope(adapter)  # type: ignore[arg-type]
    emitted = "\n".join(f"{r.getMessage()} {getattr(r, 'key_var', '')}" for r in caplog.records)
    assert "placeholder-trader" not in emitted
    assert "do-not-log" not in emitted


async def test_probe_error_warns_and_continues() -> None:
    # A transient probe failure must NOT block preflight (returns None so
    # the validate run still proceeds) — it can't determine scope, but a
    # network blip shouldn't fail a legitimate diagnostic.
    adapter = _FakeAdapter(error=ExchangeError("transient boom"))
    assert await _audit_trade_key_scope(adapter) is None  # type: ignore[arg-type]


class _StubKrakenAdapter:
    """Stands in for ``KrakenAdapter`` at the ``_run`` orchestration level.

    ``get_current_price`` raising proves ``_run`` never reaches the
    reference-price fetch when the key-scope gate fires first.
    """

    def __init__(self, *, config: object = None, dry_run: bool = True) -> None:
        del config, dry_run

    async def has_withdraw_scope(self) -> bool:
        return True  # the ADR-003 violation this test class exercises

    async def get_current_price(self, symbol: object) -> Price:
        raise AssertionError("gate should have pre-empted the reference-price fetch")

    async def aclose(self) -> None:
        return None


class _NeverStepEngine:
    """Stands in for ``GridEngine``; ``.step`` raising proves the gate
    pre-empts the validate run itself, not just the price fetch."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def step(self, symbol: object) -> Any:
        raise AssertionError("gate should have pre-empted engine.step")


class TestRunOrchestration:
    """Drives ``_run`` itself (not just ``_audit_trade_key_scope``) to prove
    the ADR-003 gate's early-return actually pre-empts the validate run at
    the CLI-wiring level."""

    async def test_withdraw_scope_violation_short_circuits_before_price_fetch_and_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KRAKEN_TRADER_API_KEY", "test-key")
        monkeypatch.setenv("KRAKEN_TRADER_API_SECRET", "c2VjcmV0")  # base64("secret")
        monkeypatch.setattr(preflight_module, "KrakenAdapter", _StubKrakenAdapter)
        monkeypatch.setattr(preflight_module, "GridEngine", _NeverStepEngine)

        config = WobbleBotConfig(
            grid=_grid_config(),
            safety=_safety_config(),
            preflight=PreflightConfig(symbol=Symbol(base="BTC", quote="USD")),
        )

        exit_code = await preflight_module._run(config)  # pylint: disable=protected-access

        assert exit_code == 3
