"""Engine -> writer -> SQLite -> authenticated status-card starvation reporting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.fixtures import grid_config, safety_config
from tests.web._helpers import login_as
from tests.web.test_status import _build_client, _make_order
from tests.web.test_status import live_storage as live_storage
from tests.web.test_status import operator_storage as operator_storage
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.cli.live import _emit_engine_states, _restore_engine_state
from wobblebot.domain.engine_state import EngineStateRow
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.grid_starvation import STARVED_RETRY_EVERY_TICKS

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
BTC = Symbol(base="BTC", quote="USD")


def _exchange(usd="0"):
    return MockExchangeAdapter(
        starting_balances={"USD": Decimal(usd), "BTC": Decimal("0")},
        starting_prices={BTC: Decimal("50000")},
    )


def _card(operator, live, *, tick=5.0):
    with _build_client(operator, live, live_tick_seconds=tick, live_symbols=(BTC,)) as client:
        login_as(client)
        response = client.get("/status/card")
        assert response.status_code == 200
        return " ".join(response.text.split())


async def test_engine_to_card_without_any_orders_fills_or_balances(live_storage, operator_storage):
    engine = GridEngine(_exchange(), live_storage, grid_config(), safety_config())
    result = await engine.step(BTC)
    assert (result.placed, result.refusals) == (0, 6)
    await _emit_engine_states(engine, [BTC], live_storage, operator_storage)
    [row] = await operator_storage.get_engine_states()
    assert row.starved_ticks == 1
    assert row.starved_reasons == {"insufficient_balance": 6}
    body = _card(operator_storage, live_storage)
    assert ">STARVED</span>" in body
    assert "0/6 placed; 6 refused; 0 sells deferred by the cost-basis guard" in body
    assert "insufficient_balance x6" in body
    assert "Most frequent reported cap:" not in body
    assert f"Retry every {STARVED_RETRY_EVERY_TICKS} eligible engine ticks" in body
    assert "elapsed starvation time is unknown" in body
    assert "No open orders for this symbol." not in body


@pytest.mark.parametrize("state", ["paused", "offside", "recovered", "restarted"])
async def test_state_transitions_clear_persisted_diagnostics(live_storage, operator_storage, state):
    exchange = _exchange()
    engine = GridEngine(exchange, live_storage, grid_config(), safety_config())
    await engine.step(BTC)
    await _emit_engine_states(engine, [BTC], live_storage, operator_storage)
    if state == "paused":
        engine.pause_symbol(BTC)
    elif state == "offside":
        exchange.set_price(BTC, Decimal("48000"))
        await engine.step(BTC)
    elif state == "recovered":
        exchange._balances["USD"] = Decimal("100")
        engine._starved[BTC] = replace(engine.starvation(BTC), ticks=59)
        result = await engine.step(BTC)
        assert result.placed == 3
    else:
        engine = GridEngine(exchange, live_storage, grid_config(), safety_config())
        await _restore_engine_state(engine, [BTC], operator_storage)
        assert engine.starvation(BTC) is None
    await _emit_engine_states(engine, [BTC], live_storage, operator_storage)
    [row] = await operator_storage.get_engine_states()
    assert (row.starved_ticks, row.starved_target, row.starved_refusals) == (0, 0, 0)
    assert row.starved_sells_deferred == 0 and row.starved_reasons == {}
    body = _card(operator_storage, live_storage)
    assert ">STARVED</span>" not in body
    if state in {"paused", "offside"}:
        assert f">{state.upper()}</span>" in body


def _row(**changes):
    return replace(
        EngineStateRow(
            symbol=BTC,
            paused=False,
            offside=False,
            offside_ticks=0,
            reference_price=None,
            anchored_at=None,
            updated_at=datetime.now(UTC),
            starved_ticks=121,
            starved_target=6,
            starved_refusals=4,
            starved_sells_deferred=2,
            starved_reasons={"max_daily_spend_usd": 3, "exchange_error": 1},
        ),
        **changes,
    )


async def test_complete_breakdown_and_cost_basis_deferrals(live_storage, operator_storage):
    await operator_storage.save_engine_state(_row())
    body = _card(operator_storage, live_storage, tick=17.0)
    assert "0/6 placed; 4 refused; 2 sells deferred by the cost-basis guard" in body
    assert "max_daily_spend_usd x3, exchange_error x1" in body
    assert "Most frequent reported cap: max_daily_spend_usd. Other limits may also bind." in body
    assert "Retry every 60 eligible engine ticks" in body
    assert "121 consecutive starved ticks in this process" in body
    assert "elapsed starvation time is unknown" in body


@pytest.mark.parametrize("case", ["paused", "offside", "stale", "open_order"])
async def test_no_badge_from_frozen_stale_or_contradicted_state(
    live_storage, operator_storage, case
):
    changes = {}
    if case in {"paused", "offside"}:
        changes[case] = True
    elif case == "stale":
        changes["updated_at"] = datetime.now(UTC) - timedelta(hours=1)
    else:
        await live_storage.save_order(_make_order())
    await operator_storage.save_engine_state(_row(**changes))
    body = _card(operator_storage, live_storage)
    assert ">STARVED</span>" not in body
    assert "0/6 placed" not in body


async def test_partial_layout_never_reports_starvation(live_storage, operator_storage):
    engine = GridEngine(_exchange("100"), live_storage, grid_config(), safety_config())
    result = await engine.step(BTC)
    assert result.placed == 3
    await _emit_engine_states(engine, [BTC], live_storage, operator_storage)
    assert ">STARVED</span>" not in _card(operator_storage, live_storage)


async def test_deferral_only_layout_has_no_refusal_reason_claim(live_storage, operator_storage):
    await operator_storage.save_engine_state(
        _row(
            starved_refusals=0,
            starved_sells_deferred=6,
            starved_reasons={},
        )
    )
    body = _card(operator_storage, live_storage)
    assert ">STARVED</span>" in body
    assert "0/6 placed; 0 refused; 6 sells deferred by the cost-basis guard" in body
    assert "Refusal reasons:" not in body
    assert "Most frequent reported cap:" not in body


async def test_changed_retry_reasons_replace_previous_counts(live_storage, operator_storage):
    await operator_storage.save_engine_state(_row())
    await operator_storage.save_engine_state(
        _row(
            starved_ticks=180,
            starved_refusals=6,
            starved_sells_deferred=0,
            starved_reasons={"exchange_error": 6},
        )
    )
    body = _card(operator_storage, live_storage)
    assert "exchange_error x6" in body
    assert "max_daily_spend_usd" not in body
    assert "Most frequent reported cap:" not in body
    assert "180 consecutive starved ticks in this process" in body
