"""Tests for the proactive-notification embed renderer (P3 renderers slice).

Mirrors ``test_discord_embed_render``: every typed event gets a
bespoke embed asserted on the load-bearing parts (title intent, color
semantics, key fields), plus the legacy fallback path for rows
without a typed event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.notification_events import (
    CommandResultEvent,
    FillEvent,
    HarvestProposalEvent,
    LossCapEvent,
    NotificationEvent,
    SessionEndEvent,
    SessionStartEvent,
    WithdrawalFailedEvent,
    WithdrawalSubmittedEvent,
)
from wobblebot.ports.notifier import Notification
from wobblebot.services.notification_embed_render import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    render_context_fields,
    render_notification_embed,
)

pytestmark = pytest.mark.unit


def _wrap(event: NotificationEvent | None, **kwargs: object) -> Notification:
    defaults: dict[str, object] = {
        "level": "info",
        "title": "legacy title",
        "message": "legacy message",
        "timestamp": Timestamp(dt=datetime.now(UTC)),
    }
    defaults.update(kwargs)
    return Notification(event=event, **defaults)  # type: ignore[arg-type]


class TestPerEventEmbeds:
    def test_session_start(self) -> None:
        embed = render_notification_embed(
            _wrap(
                SessionStartEvent(
                    symbols=("BTC/USD", "ETH/USD"),
                    tick_seconds=5.0,
                    max_runtime_seconds=None,
                    max_session_loss_usd=Decimal("150.00"),
                    starting_usd=Decimal("17.81"),
                    starting_value_usd=Decimal("241.53"),
                )
            ),
            row_id=1,
        )
        assert "session started" in embed["title"].lower()
        assert "2 symbol(s)" in embed["title"]
        assert embed["color"] == COLOR_INFO
        assert "BTC/USD, ETH/USD" == embed["description"]
        # Four short counters ride inline so Discord packs them
        # three-per-row rather than stacking eight lines above the
        # symbol list (P3 slice 18 follow-up).
        assert ("Loss cap", "$150.00", True) in embed["fields"]
        assert any("unlimited" in field[1] for field in embed["fields"])
        assert all(field[2] is True for field in embed["fields"])
        assert embed["footer"] == "level=info • id=1"

    def test_fill(self) -> None:
        embed = render_notification_embed(
            _wrap(FillEvent(symbol="BTC/USD", fills=2, counters_placed=2, tick=17)),
            row_id=7,
        )
        assert "BTC/USD" in embed["title"]
        assert embed["color"] == COLOR_SUCCESS
        assert "2 order(s) filled" in embed["description"]
        assert ("Tick", "17") in embed["fields"]

    def test_loss_cap_is_red_and_loud(self) -> None:
        embed = render_notification_embed(
            _wrap(
                LossCapEvent(session_pnl_usd=Decimal("-5.12"), limit_usd=Decimal("5.00"), tick=99)
            ),
            row_id=2,
        )
        assert embed["color"] == COLOR_ERROR
        assert "Loss cap" in embed["title"]
        assert "-5.12" in embed["description"]
        assert "5.00" in embed["description"]

    def test_session_end_clean_is_green(self) -> None:
        embed = render_notification_embed(
            _wrap(
                SessionEndEvent(
                    ticks=300,
                    duration_seconds=1761.0,
                    starting_usd=Decimal("17.81"),
                    ending_usd=Decimal("17.81"),
                    starting_value_usd=Decimal("241.49"),
                    ending_value_usd=Decimal("241.52"),
                    session_pnl_usd=Decimal("0.0336"),
                    open_orders_cancelled=11,
                    open_orders_cancel_failed=0,
                    exit_code=0,
                )
            ),
            row_id=3,
        )
        assert embed["color"] == COLOR_SUCCESS
        assert "cleanly" in embed["title"]
        assert ("Session PnL", "$0.0336") in embed["fields"]
        assert ("Open orders", "11 cancelled") in embed["fields"]

    def test_session_end_dirty_exit_is_red_with_unknowns(self) -> None:
        """exit != 0 → red; None balances render 'unknown', not a crash
        (the blueprint's fix for the old 'unknown' sentinel strings)."""
        embed = render_notification_embed(
            _wrap(
                SessionEndEvent(
                    ticks=5,
                    duration_seconds=30.0,
                    starting_usd=Decimal("17.81"),
                    ending_usd=None,
                    starting_value_usd=Decimal("241.49"),
                    ending_value_usd=None,
                    session_pnl_usd=None,
                    open_orders_cancelled=3,
                    open_orders_cancel_failed=2,
                    exit_code=1,
                )
            ),
            row_id=4,
        )
        assert embed["color"] == COLOR_ERROR
        assert "exit 1" in embed["title"]
        assert ("Session PnL", "unknown") in embed["fields"]
        assert any("2 FAILED" in v for _, v in embed["fields"])

    def test_harvest_proposal_carries_execute_hint(self) -> None:
        embed = render_notification_embed(
            _wrap(
                HarvestProposalEvent(
                    proposal_id="P-123",
                    direction="withdraw",
                    asset="USD",
                    amount=Decimal("25.00"),
                    current_exchange_balance=Decimal("260.00"),
                    target_exchange_balance=Decimal("235.00"),
                    rationale="Balance above target.",
                )
            ),
            row_id=5,
        )
        assert embed["color"] == COLOR_INFO
        assert "withdraw 25.00 USD" in embed["title"]
        assert "--execute P-123" in embed["description"]

    def test_withdrawal_failed_states_no_money_moved(self) -> None:
        embed = render_notification_embed(
            _wrap(
                WithdrawalFailedEvent(
                    proposal_id="P-9",
                    asset="USD",
                    amount=Decimal("25.00"),
                    destination="bank-1",
                    error="EFunding:Insufficient",
                    error_type="ExchangeError",
                )
            ),
            row_id=6,
        )
        assert embed["color"] == COLOR_ERROR
        assert "No money moved" in embed["description"]

    def test_withdrawal_submitted_is_amber_money_moved(self) -> None:
        embed = render_notification_embed(
            _wrap(
                WithdrawalSubmittedEvent(
                    proposal_id="P-9",
                    transaction_id="REF-1",
                    asset="USD",
                    amount=Decimal("25.00"),
                    destination="bank-1",
                    status="pending",
                )
            ),
            row_id=8,
        )
        assert embed["color"] == COLOR_WARNING
        assert "left the exchange" in embed["description"]
        assert ("refid", "REF-1") in embed["fields"]

    def test_command_result_success_and_failure(self) -> None:
        ok = render_notification_embed(
            _wrap(
                CommandResultEvent(
                    command_kind="reanchor",
                    symbol="BTC/USD",
                    success=True,
                    message="re-anchored BTC/USD: 74769.80 -> 65193.50; " "cancelled 0, placed 0/6",
                )
            ),
            row_id=9,
        )
        assert ok["color"] == COLOR_SUCCESS
        assert "reanchor BTC/USD" in ok["title"]
        assert "placed 0/6" in ok["description"]
        bad = render_notification_embed(
            _wrap(
                CommandResultEvent(
                    command_kind="cancel_open_orders",
                    symbol=None,
                    success=False,
                    message="couldn't read open orders",
                )
            ),
            row_id=10,
        )
        assert bad["color"] == COLOR_ERROR
        assert "FAILED" in bad["title"]


class TestLegacyFallback:
    def test_no_event_renders_title_message_context(self) -> None:
        embed = render_notification_embed(
            _wrap(None, level="warning", context={"daemon": "cli/harvest", "age": 3600}),
            row_id=42,
        )
        assert embed["title"] == "legacy title"
        assert embed["description"] == "legacy message"
        assert embed["color"] == COLOR_WARNING
        assert ("daemon", "cli/harvest") in embed["fields"]
        assert embed["footer"] == "level=warning • id=42"

    def test_context_fields_cap_and_truncate(self) -> None:
        fields = render_context_fields({f"k{i}": "v" for i in range(20)}, max_fields=8)
        assert len(fields) == 8
        long = render_context_fields({"k": "x" * 500})
        assert len(long[0][1]) == 200
        assert long[0][1].endswith("...")
