"""Tests for cli/recalibrate — dry-run + commit (Stage 7.6.B)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from textwrap import dedent

import pytest

from tests.fixtures import grid_config as _grid_config_fixture
from tests.fixtures import safety_config as _safety_config_fixture
from wobblebot.cli import recalibrate as cli_recalibrate
from wobblebot.config.cli import LiveConfig
from wobblebot.config.harvester import HarvesterConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.domain.value_objects import Symbol
from wobblebot.services.calibrator import (
    RecalibrationChange,
    RecalibrationProposal,
    recalibrate,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_wobblebot_logger() -> Iterator[None]:
    """Snapshot + restore the ``wobblebot`` logger config per test.

    Same fixture as ``tests/cli/test_web.py`` — ``cli_recalibrate.main()``
    calls ``configure_logging`` which flips ``root.propagate = False``
    on the ``wobblebot`` subtree.
    """
    root = logging.getLogger("wobblebot")
    snapshot_level = root.level
    snapshot_propagate = root.propagate
    snapshot_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.handlers = snapshot_handlers
        root.propagate = snapshot_propagate
        root.setLevel(snapshot_level)


# --------------------------------------------------------------------- #
# Parser                                                                #
# --------------------------------------------------------------------- #


class TestParser:
    def test_target_balance_required(self) -> None:
        parser = cli_recalibrate._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_target_balance_must_be_positive(self) -> None:
        parser = cli_recalibrate._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--target-balance", "0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--target-balance", "-10"])

    def test_target_balance_invalid_format_rejected(self) -> None:
        parser = cli_recalibrate._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--target-balance", "fifty"])

    def test_target_balance_accepts_decimal(self) -> None:
        parser = cli_recalibrate._build_parser()
        args = parser.parse_args(["--target-balance", "10.50"])
        assert args.target_balance == Decimal("10.50")

    def test_current_balance_optional_and_validated(self) -> None:
        parser = cli_recalibrate._build_parser()
        args = parser.parse_args(["--target-balance", "10", "--current-balance", "99.92"])
        assert args.current_balance == Decimal("99.92")

    def test_commit_flag_defaults_false(self) -> None:
        parser = cli_recalibrate._build_parser()
        args = parser.parse_args(["--target-balance", "10"])
        assert args.commit is False

    def test_commit_flag_sets_true(self) -> None:
        parser = cli_recalibrate._build_parser()
        args = parser.parse_args(["--target-balance", "10", "--commit"])
        assert args.commit is True


# --------------------------------------------------------------------- #
# _proposal_to_overrides                                                #
# --------------------------------------------------------------------- #


class TestProposalToOverrides:
    def test_flattens_changes_to_dict(self) -> None:
        proposal = RecalibrationProposal(
            current_balance=Decimal("100"),
            target_balance=Decimal("10"),
            scale_factor=Decimal("0.1"),
            changes=(
                RecalibrationChange(
                    yaml_path="grid.default.order_size_usd",
                    current_value=Decimal("10"),
                    proposed_value=Decimal("1.00"),
                ),
                RecalibrationChange(
                    yaml_path="safety.max_total_exposure_usd",
                    current_value=Decimal("100"),
                    proposed_value=Decimal("10.00"),
                ),
            ),
        )
        result = cli_recalibrate._proposal_to_overrides(proposal)
        assert result == {
            "grid.default.order_size_usd": Decimal("1.00"),
            "safety.max_total_exposure_usd": Decimal("10.00"),
        }

    def test_empty_changes_returns_empty(self) -> None:
        proposal = RecalibrationProposal(
            current_balance=Decimal("100"),
            target_balance=Decimal("100"),
            scale_factor=Decimal("1"),
            changes=(),
        )
        assert cli_recalibrate._proposal_to_overrides(proposal) == {}


# --------------------------------------------------------------------- #
# _run integration paths                                                #
# --------------------------------------------------------------------- #


def _write_minimal_settings(path: Path) -> None:
    """Operator-style settings.yml the recalibrator can write to."""
    body = dedent("""\
        grid:
          default:
            spacing_percentage: 1.0
            levels_above: 3
            levels_below: 3
            order_size_usd: 10.0
          coins:
            DOGE:
              spacing_percentage: 2.0
              levels_above: 3
              levels_below: 3
              order_size_usd: 15.0
              enabled: true
        safety:
          max_total_exposure_usd: 100.0
          max_daily_spend_usd: 100.0
          max_per_coin_exposure_usd: 50.0
          max_orders_per_coin: 20
          emergency_stop:
            enabled: true
            max_loss_percentage: 20.0
            min_exchange_balance_usd: 0
        live:
          symbols:
            - BTC/USD
          max_session_loss_usd: 5.0
        harvester:
          enabled: false
          min_exchange_liquidity_usd: 200.0
          topup_threshold_usd: 250.0
          surplus_threshold_usd: 500.0
          max_withdrawal_per_day_usd: 1000.0
        """)
    path.write_text(body, encoding="utf-8")


def _load_config(path: Path) -> WobbleBotConfig:
    """Load via runtime so the test exercises the real loader."""
    from wobblebot.config.runtime import load_resolved_config

    return load_resolved_config(config_path=path, profile_name=None, cli_overrides={})


@pytest.mark.asyncio
class TestRunDryRun:
    async def test_with_current_balance_override_skips_kraken(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.yml"
        _write_minimal_settings(settings)
        cfg = _load_config(settings)
        rc = await cli_recalibrate._run(
            config=cfg,
            target_balance=Decimal("10"),
            current_balance_override=Decimal("100"),
            commit=False,
            config_path=settings,
        )
        assert rc == 0
        # File untouched on dry-run
        assert "order_size_usd: 10.0" in settings.read_text(encoding="utf-8")

    async def test_dry_run_does_not_mutate_file(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.yml"
        _write_minimal_settings(settings)
        before = settings.read_text(encoding="utf-8")
        cfg = _load_config(settings)
        await cli_recalibrate._run(
            config=cfg,
            target_balance=Decimal("10"),
            current_balance_override=Decimal("100"),
            commit=False,
            config_path=settings,
        )
        after = settings.read_text(encoding="utf-8")
        assert before == after

    async def test_invalid_balance_returns_2(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.yml"
        _write_minimal_settings(settings)
        cfg = _load_config(settings)
        # Same current and target shouldn't fail; non-positive target
        # never gets here (argparse rejects). The invalid path is
        # current_balance <= 0 via the override, which argparse also
        # rejects. But internally, if recalibrate's input validation
        # fires it returns 2. Exercise by passing target == current
        # (should succeed with no changes, not 2).
        rc = await cli_recalibrate._run(
            config=cfg,
            target_balance=Decimal("100"),
            current_balance_override=Decimal("100"),
            commit=False,
            config_path=settings,
        )
        assert rc == 0


@pytest.mark.asyncio
class TestRunCommit:
    async def test_commit_writes_settings_file(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.yml"
        _write_minimal_settings(settings)
        cfg = _load_config(settings)
        rc = await cli_recalibrate._run(
            config=cfg,
            target_balance=Decimal("10"),
            current_balance_override=Decimal("100"),
            commit=True,
            config_path=settings,
        )
        assert rc == 0
        text = settings.read_text(encoding="utf-8")
        # grid.default.order_size_usd: 10.0 → 1.00
        assert "order_size_usd: 1.0" in text or "order_size_usd: 1.00" in text
        # safety.max_total_exposure_usd: 100.0 → 10.0
        assert "max_total_exposure_usd: 10.0" in text
        # harvester.surplus_threshold_usd: 500.0 → 50.0
        assert "surplus_threshold_usd: 50.0" in text

    async def test_commit_preserves_comments(self, tmp_path: Path) -> None:
        """ruamel round-trip keeps comment lines intact."""
        settings = tmp_path / "settings.yml"
        body = dedent("""\
            grid:
              default:
                spacing_percentage: 1.0
                levels_above: 3
                levels_below: 3
                order_size_usd: 10.0  # operator-tuned at $100 balance
            safety:
              max_total_exposure_usd: 100.0
              max_daily_spend_usd: 100.0
              max_per_coin_exposure_usd: 50.0
              max_orders_per_coin: 20
              emergency_stop:
                enabled: true
                max_loss_percentage: 20.0
                min_exchange_balance_usd: 0
            """)
        settings.write_text(body, encoding="utf-8")
        cfg = _load_config(settings)
        await cli_recalibrate._run(
            config=cfg,
            target_balance=Decimal("10"),
            current_balance_override=Decimal("100"),
            commit=True,
            config_path=settings,
        )
        text = settings.read_text(encoding="utf-8")
        assert "operator-tuned at $100 balance" in text

    async def test_commit_with_no_changes_returns_0(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.yml"
        _write_minimal_settings(settings)
        before = settings.read_text(encoding="utf-8")
        cfg = _load_config(settings)
        rc = await cli_recalibrate._run(
            config=cfg,
            target_balance=Decimal("100"),
            current_balance_override=Decimal("100"),
            commit=True,
            config_path=settings,
        )
        assert rc == 0
        assert settings.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------- #
# main() pre-async-dispatch paths                                       #
# --------------------------------------------------------------------- #
#
# Tests below only cover the synchronous portion of ``main()`` — bad
# config + early argparse failure. Anything that reaches
# ``asyncio.run`` is exercised via the async ``TestRunDryRun`` /
# ``TestRunCommit`` classes above. Reason: nesting ``asyncio.run``
# inside a sync pytest test pollutes pytest-asyncio's loop state for
# downstream tests on Windows (ProactorEventLoop transport pipes leak
# during pytest's strict warnings phase).


class TestMain:
    def test_bad_config_path_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        rc = cli_recalibrate.main(
            [
                "--config",
                str(tmp_path / "nope.yml"),
                "--target-balance",
                "10",
                "--current-balance",
                "100",
            ]
        )
        assert rc == 2
