"""Tests for cli/apply — Stage 3.4b dry-run gate evaluation."""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.apply import _run, _select_suggestion
from wobblebot.config.advisor import AdvisorConfig, AutoApplyConfig, InferenceParams
from wobblebot.config.cli import AdviseConfig
from wobblebot.config.grid import CoinGridConfig, GridConfig, GridLevels
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.safety import EmergencyStopConfig, SafetyConfig
from wobblebot.config.schedules import SchedulesConfig
from wobblebot.domain.value_objects import Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def advise_db(tmp_path: Any) -> AsyncIterator[str]:
    """A real on-disk advise.db file so cli/apply's open-by-path flow exercises."""
    path = str(tmp_path / "advise.db")
    adapter = SQLiteStorageAdapter(path)
    await adapter.connect()
    await adapter.close()
    yield path


def _suggestion(
    *,
    rec_id: str = "rec-default",
    role: str = "single",
    recommendations: dict[str, Any] | None = None,
    minutes_ago: int = 1,
    model_name: str = "phi4:14b",
    symbol: str = "BTC/USD",
) -> AdvisorSuggestion:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    rec = AdvisorRecommendation(
        recommendation_id=rec_id,
        timestamp=Timestamp(dt=when),
        role=role,
        recommendations=recommendations or {"spacing_percentage": 1.1},
        rationale="test",
        confidence="medium",
    )
    return AdvisorSuggestion(
        recommendation=rec,
        created_at=Timestamp(dt=when),
        input_summary={"symbol": symbol},
        model_name=model_name,
    )


def _grid_config(
    *,
    spacing: str = "1.0",
    order_size: str = "10",
) -> GridConfig:
    return GridConfig(
        default=GridLevels(
            spacing_percentage=Decimal(spacing),
            levels_above=3,
            levels_below=3,
            order_size_usd=Decimal(order_size),
        ),
        coins={
            "BTC": CoinGridConfig(
                spacing_percentage=Decimal(spacing),
                levels_above=3,
                levels_below=3,
                order_size_usd=Decimal(order_size),
                enabled=True,
            ),
        },
    )


def _safety_config() -> SafetyConfig:
    return SafetyConfig(
        max_total_exposure_usd=Decimal("100"),
        max_daily_spend_usd=Decimal("100"),
        max_per_coin_exposure_usd=Decimal("50"),
        max_orders_per_coin=10,
        emergency_stop=EmergencyStopConfig(
            max_loss_percentage=Decimal("5"),
            min_exchange_balance_usd=Decimal("0"),
        ),
    )


def _full_config(
    *,
    advise_db_path: str,
    grid: GridConfig | None = None,
    auto_apply_enabled: bool = True,
) -> WobbleBotConfig:
    return WobbleBotConfig(
        grid=grid or _grid_config(),
        safety=_safety_config(),
        schedules=SchedulesConfig(root={"advise": timedelta(minutes=15)}),
        advisor=AdvisorConfig(
            type="single",
            provider="ollama",
            model="phi4:14b",
            prompt_file="config/prompts/quant.md",
            inference_params=InferenceParams(),
            auto_apply=AutoApplyConfig(
                enabled=auto_apply_enabled,
                max_spacing_change_percentage=Decimal("20"),
                max_order_size_change_percentage=Decimal("15"),
            ),
        ),
        advise=AdviseConfig(
            symbols=[Symbol(base="BTC", quote="USD")],
            db=advise_db_path,
        ),
    )


def _args(
    *,
    symbol: str | None = None,
    recommendation_id: str | None = None,
    db: str | None = None,
    search_limit: int = 50,
    commit: bool = False,
    settings_path: str | None = None,
    config_path: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        symbol=symbol,
        recommendation_id=recommendation_id,
        db=db,
        search_limit=search_limit,
        commit=commit,
        settings_path=settings_path,
        log_format=None,
        config=config_path,
        profile=None,
    )


class TestSelectSuggestion:
    def test_empty_returns_none(self) -> None:
        assert _select_suggestion([], recommendation_id=None) is None

    def test_latest_is_default(self) -> None:
        first = _suggestion(rec_id="r-new", minutes_ago=1)
        second = _suggestion(rec_id="r-old", minutes_ago=60)
        # get_advisor_suggestions returns DESC by created_at, so [0] is newest.
        chosen = _select_suggestion([first, second], recommendation_id=None)
        assert chosen is not None
        assert chosen.recommendation.recommendation_id == "r-new"

    def test_by_recommendation_id(self) -> None:
        first = _suggestion(rec_id="r-target")
        second = _suggestion(rec_id="r-decoy")
        chosen = _select_suggestion([second, first], recommendation_id="r-target")
        assert chosen is not None
        assert chosen.recommendation.recommendation_id == "r-target"

    def test_recommendation_id_not_found_returns_none(self) -> None:
        first = _suggestion(rec_id="r-1")
        assert _select_suggestion([first], recommendation_id="r-missing") is None


@pytest.mark.asyncio
class TestRunHappyPath:
    async def test_logs_applied_keys(
        self, advise_db: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(rec_id="r-1", recommendations={"spacing_percentage": 1.1}),
            )
        finally:
            await storage.close()

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.apply"):
            rc = await _run(_args(), config)
        assert rc == 0
        applied_messages = [r for r in caplog.records if "APPLIED" in r.message]
        assert applied_messages, "expected at least one APPLIED log line"
        assert any("spacing_percentage" in r.message for r in applied_messages)

    async def test_logs_rejected_with_reason(
        self, advise_db: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A proposal over the magnitude cap shows up as REJECTED with
        the delta in the reason — operator can see why."""
        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(recommendations={"spacing_percentage": 1.5}),  # +50% > 20% cap
            )
        finally:
            await storage.close()

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.apply"):
            rc = await _run(_args(), config)
        assert rc == 0
        rejected = [r for r in caplog.records if "REJECTED" in r.message]
        assert rejected
        assert any("+50.00%" in r.message for r in rejected)


@pytest.mark.asyncio
class TestRunFailureModes:
    async def test_missing_advise_section_exits_2(self) -> None:
        config = WobbleBotConfig(
            grid=_grid_config(),
            safety=_safety_config(),
            schedules=SchedulesConfig(root={}),
            advisor=AdvisorConfig(
                type="single",
                provider="ollama",
                model="phi4:14b",
                prompt_file="config/prompts/quant.md",
            ),
            # advise=None — missing section
        )
        rc = await _run(_args(), config)
        assert rc == 2

    async def test_missing_advisor_section_exits_2(self, advise_db: str) -> None:
        config = WobbleBotConfig(
            grid=_grid_config(),
            safety=_safety_config(),
            schedules=SchedulesConfig(root={}),
            advise=AdviseConfig(symbols=[Symbol(base="BTC", quote="USD")], db=advise_db),
            # advisor=None — missing section
        )
        rc = await _run(_args(), config)
        assert rc == 2

    async def test_empty_db_exits_2(self, advise_db: str) -> None:
        """No suggestions in the db → can't gate anything."""
        config = _full_config(advise_db_path=advise_db)
        rc = await _run(_args(), config)
        assert rc == 2

    async def test_recommendation_id_not_found_exits_2(
        self, advise_db: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(_suggestion(rec_id="r-exists"))
        finally:
            await storage.close()

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.apply"):
            rc = await _run(_args(recommendation_id="r-missing"), config)
        assert rc == 2
        assert any("no suggestion matched" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestSymbolOverride:
    async def test_symbol_flag_overrides_config_default(self, advise_db: str) -> None:
        """--symbol picks a coin's grid distinct from advise.symbol — useful
        when an operator's advise daemon runs on BTC but they want to
        evaluate the same recommendation pattern against ETH."""
        # Seed one BTC-tagged and one ETH-tagged suggestion. Under
        # Stage 3.6b's symbol filter, cli/apply --symbol BTC/USD must
        # pick the BTC row and apply against BTC's grid; --symbol
        # ETH/USD picks the ETH row and applies against ETH's grid.
        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(
                    rec_id="r-btc",
                    symbol="BTC/USD",
                    recommendations={"spacing_percentage": 1.1},
                ),
            )
            await storage.save_advisor_suggestion(
                _suggestion(
                    rec_id="r-eth",
                    symbol="ETH/USD",
                    recommendations={"spacing_percentage": 2.1},
                ),
            )
        finally:
            await storage.close()

        eth_grid = GridConfig(
            default=GridLevels(
                spacing_percentage=Decimal("2.0"),
                levels_above=3,
                levels_below=3,
                order_size_usd=Decimal("10"),
            ),
            coins={
                "BTC": CoinGridConfig(
                    spacing_percentage=Decimal("1.0"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("10"),
                    enabled=True,
                ),
                "ETH": CoinGridConfig(
                    spacing_percentage=Decimal("2.0"),
                    levels_above=3,
                    levels_below=3,
                    order_size_usd=Decimal("10"),
                    enabled=True,
                ),
            },
        )
        config = _full_config(advise_db_path=advise_db, grid=eth_grid)
        # --symbol BTC/USD picks the BTC suggestion (spacing 1.1 vs
        # 1.0 baseline = +10%) and applies against BTC's grid: within cap.
        rc = await _run(_args(symbol="BTC/USD"), config)
        assert rc == 0
        # --symbol ETH/USD picks the ETH suggestion (spacing 2.1 vs
        # 2.0 baseline = +5%) and applies against ETH's grid: within cap.
        rc2 = await _run(_args(symbol="ETH/USD"), config)
        assert rc2 == 0


@pytest.mark.asyncio
class TestNewsRoleSafetyEndToEnd:
    """End-to-end: a news-role suggestion in the DB must NOT apply
    regardless of the auto_apply.enabled flag — ADR-007 safety property
    is enforced at the gate, surfaced through cli/apply."""

    async def test_news_role_rejected(
        self, advise_db: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(role="news", recommendations={"spacing_percentage": 1.1}),
            )
        finally:
            await storage.close()

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.apply"):
            rc = await _run(_args(), config)
        assert rc == 0
        applied = [r for r in caplog.records if "APPLIED" in r.message]
        rejected = [r for r in caplog.records if "REJECTED" in r.message]
        assert not applied, "news-role suggestion must not apply"
        assert rejected
        assert any("ADR-007" in r.message for r in rejected)


_COMMIT_FIXTURE_SETTINGS = """\
# header comment
grid:
  default:
    spacing_percentage: 1.0
    levels_above: 3
    levels_below: 3
    order_size_usd: 10
  coins:
    BTC:
      enabled: true
      spacing_percentage: 1.0
      levels_above: 3
      levels_below: 3
      order_size_usd: 10
safety:
  emergency_stop:
    enabled: true
"""


@pytest.mark.asyncio
class TestCommit:
    async def test_commit_rewrites_settings_and_writes_audit(
        self,
        advise_db: str,
        tmp_path: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """--commit on a clean apply must: rewrite settings.yml,
        persist an AppliedSuggestion audit row, and log the path."""
        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(_COMMIT_FIXTURE_SETTINGS, encoding="utf-8")

        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(
                    rec_id="rec-commit",
                    recommendations={"spacing_percentage": 1.1},
                ),
            )
        finally:
            await storage.close()

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.INFO, logger="wobblebot.cli.apply"):
            rc = await _run(
                _args(commit=True, settings_path=str(settings_path)),
                config,
            )
        assert rc == 0

        # settings.yml was rewritten.
        contents = settings_path.read_text(encoding="utf-8")
        assert "spacing_percentage: 1.1" in contents
        # Audit row persisted.
        audit_storage = SQLiteStorageAdapter(advise_db)
        await audit_storage.connect()
        try:
            applied_rows = await audit_storage.get_applied_suggestions()
        finally:
            await audit_storage.close()
        assert len(applied_rows) == 1
        row = applied_rows[0]
        assert row.recommendation_id == "rec-commit"
        assert row.symbol == "BTC"
        assert row.applied_keys[0]["key"] == "spacing_percentage"
        # Log emitted the "commit complete" event.
        assert any("commit complete" in r.message for r in caplog.records)

    async def test_commit_without_applied_keys_exits_1(
        self,
        advise_db: str,
        tmp_path: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A suggestion where every key got rejected (e.g. all level
        keys) should NOT rewrite settings.yml or write an audit row —
        exit 1 with a clear log line."""
        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(_COMMIT_FIXTURE_SETTINGS, encoding="utf-8")
        before = settings_path.read_text(encoding="utf-8")

        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(recommendations={"levels_above": 5}),  # not whitelisted
            )
        finally:
            await storage.close()

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.apply"):
            rc = await _run(
                _args(commit=True, settings_path=str(settings_path)),
                config,
            )
        assert rc == 1
        assert settings_path.read_text(encoding="utf-8") == before
        # No audit row written.
        audit_storage = SQLiteStorageAdapter(advise_db)
        await audit_storage.connect()
        try:
            applied_rows = await audit_storage.get_applied_suggestions()
        finally:
            await audit_storage.close()
        assert applied_rows == []
        assert any("no keys cleared" in r.message for r in caplog.records)

    async def test_commit_handles_missing_settings_file(
        self,
        advise_db: str,
        tmp_path: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = SQLiteStorageAdapter(advise_db)
        await storage.connect()
        try:
            await storage.save_advisor_suggestion(
                _suggestion(recommendations={"spacing_percentage": 1.1}),
            )
        finally:
            await storage.close()
        bad_path = tmp_path / "does-not-exist.yml"

        config = _full_config(advise_db_path=advise_db)
        with caplog.at_level(logging.ERROR, logger="wobblebot.cli.apply"):
            rc = await _run(
                _args(commit=True, settings_path=str(bad_path)),
                config,
            )
        assert rc == 1
        assert any("rewrite failed" in r.message for r in caplog.records)
        # No audit row written when the rewrite fails — the audit
        # represents what hit disk, and nothing hit disk.
        audit_storage = SQLiteStorageAdapter(advise_db)
        await audit_storage.connect()
        try:
            applied_rows = await audit_storage.get_applied_suggestions()
        finally:
            await audit_storage.close()
        assert applied_rows == []
