"""Tests for the QueryResult -> Discord embed renderer.

Exercises every variant of the discriminated union plus the empty /
overflow paths so the dispatch table stays exhaustive.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.operator_results import (
    FillEntry,
    GridConfigResult,
    HarvesterStatusResult,
    HelpEntry,
    HelpResult,
    NewsEntry,
    OpenOrderEntry,
    OpenOrdersResult,
    ProposalEntry,
    RecentFillsResult,
    RecentNewsResult,
    RecentProposalsResult,
    RecentSuggestionsResult,
    StatusReportResult,
    StatusReportTally,
    StatusResult,
    SuggestionEntry,
    SymbolStatusEntry,
)
from wobblebot.services.discord_embed_render import (
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    render_query_embed,
)

pytestmark = pytest.mark.unit


def _ts() -> Timestamp:
    return Timestamp(dt=datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC))


class TestRenderStatus:
    def test_all_active_uses_success_color(self) -> None:
        result = StatusResult(
            symbols=[
                SymbolStatusEntry(symbol="BTC/USD", state="active", open_order_count=3),
                SymbolStatusEntry(symbol="ETH/USD", state="active", open_order_count=5),
            ],
            total_usd_balance=89.92,
            session_pnl=0.1025,
            session_runtime_seconds=3725.0,
            recent_fill_count=12,
        )

        out = render_query_embed(result)

        assert out["title"] == "Engine status"
        assert out["color"] == COLOR_SUCCESS
        assert "89.92" in out["description"]
        assert "+0.1025" in out["description"]
        assert "1h 02m 05s" in out["description"]
        assert len(out["fields"]) == 2
        assert out["fields"][0][0].startswith("▶")
        assert "BTC/USD" in out["fields"][0][0]

    def test_paused_symbol_uses_warning_color(self) -> None:
        result = StatusResult(
            symbols=[
                SymbolStatusEntry(symbol="BTC/USD", state="paused", open_order_count=0),
                SymbolStatusEntry(symbol="ETH/USD", state="active", open_order_count=1),
            ],
            total_usd_balance=100.0,
            session_pnl=0.0,
            session_runtime_seconds=60.0,
            recent_fill_count=0,
        )

        out = render_query_embed(result)

        assert out["color"] == COLOR_WARNING
        assert any(name.startswith("⏸") for name, _ in out["fields"])

    def test_empty_symbols(self) -> None:
        result = StatusResult(
            symbols=[],
            total_usd_balance=0.0,
            session_pnl=0.0,
            session_runtime_seconds=0.0,
            recent_fill_count=0,
        )

        out = render_query_embed(result)

        assert out["color"] == COLOR_SUCCESS
        assert out["fields"] == []


class TestRenderOpenOrders:
    def test_empty_orders_message(self) -> None:
        result = OpenOrdersResult(symbol="BTC/USD", orders=[])

        out = render_query_embed(result)

        assert "BTC/USD" in out["title"]
        assert "No open orders" in out["description"]
        assert out["fields"] == []

    def test_orders_render_as_fields(self) -> None:
        result = OpenOrdersResult(
            symbol="BTC/USD",
            orders=[
                OpenOrderEntry(
                    order_id="ABCDEF123456789",
                    symbol="BTC/USD",
                    side="buy",
                    price=76000.0,
                    amount=0.00013,
                    created_at=_ts(),
                ),
                OpenOrderEntry(
                    order_id="ZYX0987654321QR",
                    symbol="BTC/USD",
                    side="sell",
                    price=77000.0,
                    amount=0.00013,
                    created_at=_ts(),
                ),
            ],
        )

        out = render_query_embed(result)

        assert len(out["fields"]) == 2
        assert "BUY BTC/USD" in out["fields"][0][0]
        assert "76,000" in out["fields"][0][1]
        assert "created" in out["fields"][0][1]
        assert "2026-05-24" in out["fields"][0][1]

    def test_overflow_truncates_with_marker(self) -> None:
        orders = [
            OpenOrderEntry(
                order_id=f"ORDER{i:03d}1234567",
                symbol="BTC/USD",
                side="buy",
                price=76000.0 + i,
                amount=0.0001,
                created_at=_ts(),
            )
            for i in range(15)
        ]
        result = OpenOrdersResult(symbol="BTC/USD", orders=orders)

        out = render_query_embed(result)

        last_name, last_value = out["fields"][-1]
        assert last_name == "…"
        assert "5 more" in last_value


class TestRenderRecentFills:
    def test_empty_fills(self) -> None:
        result = RecentFillsResult(symbol=None, lookback_hours=24, fills=[])

        out = render_query_embed(result)

        assert "all symbols" in out["title"]
        assert "No fills" in out["description"]

    def test_fill_with_pnl_renders_value(self) -> None:
        result = RecentFillsResult(
            symbol="BTC/USD",
            lookback_hours=24,
            fills=[
                FillEntry(
                    order_id="FILL1234567890",
                    symbol="BTC/USD",
                    side="sell",
                    price=77000.0,
                    amount=0.00013,
                    pnl=0.05,
                    filled_at=_ts(),
                )
            ],
        )

        out = render_query_embed(result)

        assert out["color"] == COLOR_INFO
        assert "PnL" in out["fields"][0][1]
        assert "+0.0500" in out["fields"][0][1]

    def test_fill_without_pnl_omits_pnl_label(self) -> None:
        result = RecentFillsResult(
            symbol="BTC/USD",
            lookback_hours=24,
            fills=[
                FillEntry(
                    order_id="FILL1234567890",
                    symbol="BTC/USD",
                    side="buy",
                    price=77000.0,
                    amount=0.00013,
                    pnl=None,
                    filled_at=_ts(),
                )
            ],
        )

        out = render_query_embed(result)

        assert "PnL" not in out["fields"][0][1]


class TestRenderRecentSuggestions:
    def test_empty_suggestions(self) -> None:
        result = RecentSuggestionsResult(symbol=None, suggestions=[])

        out = render_query_embed(result)

        assert "No suggestions" in out["description"]

    def test_suggestion_renders_confidence_and_rationale(self) -> None:
        result = RecentSuggestionsResult(
            symbol="BTC/USD",
            suggestions=[
                SuggestionEntry(
                    recommendation_id="REC12345678ABCD",
                    symbol="BTC/USD",
                    model_name="claude-sonnet-4-6",
                    confidence="high",
                    recommendations={"spacing": 1.1},
                    rationale="Volume up, range stable.",
                    created_at=_ts(),
                )
            ],
        )

        out = render_query_embed(result)

        assert "claude-sonnet-4-6" in out["fields"][0][0]
        assert "high" in out["fields"][0][1]
        assert "Volume up" in out["fields"][0][1]
        assert "2026-05-24" in out["fields"][0][1]
        assert "created" in out["fields"][0][1]


class TestRenderRecentNews:
    def test_empty_news(self) -> None:
        result = RecentNewsResult(lookback_hours=24, items=[])

        out = render_query_embed(result)

        assert "No news" in out["description"]

    def test_news_renders_with_sentiment(self) -> None:
        result = RecentNewsResult(
            lookback_hours=24,
            items=[
                NewsEntry(
                    source="cryptocompare",
                    headline="BTC surges past 77k",
                    published_at=_ts(),
                    sentiment_score=0.5,
                    mentioned_coins=["BTC"],
                )
            ],
        )

        out = render_query_embed(result)

        name, value = out["fields"][0]
        assert "BTC surges past 77k" in name
        assert "+0.50" in value
        assert "BTC" in value


class TestRenderHarvesterStatus:
    def test_hold_band_uses_success_color(self) -> None:
        result = HarvesterStatusResult(
            enabled=True,
            asset="USD",
            current_balance=150.0,
            band="hold",
        )

        out = render_query_embed(result)

        assert out["color"] == COLOR_SUCCESS
        assert "USD" in out["description"]
        assert "hold" in out["description"]
        assert out["fields"] == []

    def test_surplus_band_uses_warning_and_renders_proposal(self) -> None:
        result = HarvesterStatusResult(
            enabled=True,
            asset="USD",
            current_balance=500.0,
            band="surplus",
            latest_proposal_id="PROP1234567890",
            latest_proposal_amount=250.0,
            latest_proposal_direction="exchange_to_bank",
        )

        out = render_query_embed(result)

        assert out["color"] == COLOR_WARNING
        assert len(out["fields"]) == 1
        assert "exchange_to_bank" in out["fields"][0][1]


class TestRenderRecentProposals:
    def test_empty_proposals(self) -> None:
        result = RecentProposalsResult(
            direction=None,
            lookback_hours=24,
            proposals=[],
        )

        out = render_query_embed(result)

        assert "No proposals" in out["description"]

    def test_proposal_renders(self) -> None:
        result = RecentProposalsResult(
            direction="exchange_to_bank",
            lookback_hours=48,
            proposals=[
                ProposalEntry(
                    proposal_id="PROP123ABC4567",
                    direction="exchange_to_bank",
                    asset="USD",
                    amount=250.0,
                    rationale="Surplus band hit.",
                    created_at=_ts(),
                )
            ],
        )

        out = render_query_embed(result)

        assert "exchange_to_bank" in out["title"]
        assert "Surplus" in out["fields"][0][1]


class TestRenderGridConfig:
    def test_default_tier(self) -> None:
        result = GridConfigResult(
            symbol=None,
            spacing_percentage=1.0,
            levels_above=3,
            levels_below=3,
            order_size_usd=10.0,
        )

        out = render_query_embed(result)

        assert "default tier" in out["title"]
        assert "1.0%" in out["description"]
        assert "$10.00" in out["description"]

    def test_per_symbol(self) -> None:
        result = GridConfigResult(
            symbol="BTC/USD",
            spacing_percentage=0.8,
            levels_above=4,
            levels_below=4,
            order_size_usd=20.0,
        )

        out = render_query_embed(result)

        assert "BTC/USD" in out["title"]
        assert "$20.00" in out["description"]


class TestRenderHelp:
    def test_empty_help(self) -> None:
        result = HelpResult(entries=[])

        out = render_query_embed(result)

        assert "No help" in out["description"]

    def test_groups_commands_and_queries(self) -> None:
        result = HelpResult(
            entries=[
                HelpEntry(kind="pause", category="command", description="Pause a symbol"),
                HelpEntry(kind="resume", category="command", description="Resume a symbol"),
                HelpEntry(kind="status", category="query", description="Show engine state"),
            ]
        )

        out = render_query_embed(result)

        field_names = [name for name, _ in out["fields"]]
        assert "Commands" in field_names
        assert "Queries" in field_names
        commands_value = next(v for n, v in out["fields"] if n == "Commands")
        assert "pause" in commands_value
        assert "resume" in commands_value


class TestRenderStatusReport:
    def test_renders_narrative_as_description_and_tallies_as_fields(self) -> None:
        result = StatusReportResult(
            lookback_hours=4,
            since=_ts(),
            narrative="Quiet four hours. Two fills, no news. Harvester holding.",
            tallies=[
                StatusReportTally(label="Balance", value="$89.92"),
                StatusReportTally(label="Open orders", value="5"),
                StatusReportTally(label="Fills (last 4h)", value="2"),
            ],
        )

        out = render_query_embed(result)

        assert "4h" in out["title"]
        assert "Quiet four hours" in out["description"]
        assert ("Balance", "$89.92") in out["fields"]
        assert ("Open orders", "5") in out["fields"]
        assert "since" in out["footer"]

    def test_long_narrative_is_truncated_under_discord_cap(self) -> None:
        long_text = "x" * 5000
        result = StatusReportResult(
            lookback_hours=24,
            since=_ts(),
            narrative=long_text,
            tallies=[],
        )

        out = render_query_embed(result)

        # Discord caps description at 4096; we truncate well under.
        assert len(out["description"]) <= 4000
