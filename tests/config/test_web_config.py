"""Tests for ``config.cli.WebConfig`` + ``WobbleBotConfig.web`` (Stage 7.1.B)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from wobblebot.config.cli import WebConfig
from wobblebot.config.loader import WobbleBotConfig, load_config

pytestmark = pytest.mark.unit


_BASE_REQUIRED: dict[str, object] = {
    "grid": {
        "default": {
            "spacing_percentage": "1.0",
            "levels_above": 3,
            "levels_below": 3,
            "order_size_usd": "10",
        }
    },
    "safety": {
        "max_total_exposure_usd": "100",
        "max_daily_spend_usd": "100",
        "max_per_coin_exposure_usd": "50",
        "max_orders_per_coin": 10,
        "emergency_stop": {
            "enabled": True,
            "max_loss_percentage": "5",
            "min_exchange_balance_usd": "0",
        },
    },
}


# --------------------------------------------------------------------- #
# WebConfig defaults                                                    #
# --------------------------------------------------------------------- #


class TestWebConfigDefaults:
    def test_empty_dict_uses_defaults(self) -> None:
        cfg = WebConfig()
        assert cfg.bind_host == "127.0.0.1"
        assert cfg.bind_port == 8000
        assert cfg.session_secret_env_var == "WOBBLEBOT_WEB_SESSION_SECRET"
        assert cfg.session_max_age_days == 7
        assert cfg.rate_limit_attempts == 5
        assert cfg.rate_limit_window_seconds == 60
        assert cfg.bcrypt_cost == 12
        assert cfg.htmx_poll_seconds == 15.0
        assert cfg.operator_db == "data/wobblebot-operator.db"
        assert cfg.live_db is None
        assert cfg.advise_db is None
        assert cfg.harvest_db is None
        assert cfg.observe_db is None
        assert cfg.news_db is None
        assert cfg.log_format == "plain"

    def test_frozen(self) -> None:
        cfg = WebConfig()
        with pytest.raises(Exception):
            cfg.bind_port = 9000  # type: ignore[misc]


# --------------------------------------------------------------------- #
# WebConfig validation                                                  #
# --------------------------------------------------------------------- #


class TestWebConfigValidation:
    @pytest.mark.parametrize("invalid_port", [0, -1, 65536, 100000])
    def test_invalid_port_rejected(self, invalid_port: int) -> None:
        with pytest.raises(Exception):
            WebConfig(bind_port=invalid_port)

    @pytest.mark.parametrize("valid_port", [1, 80, 443, 8000, 65535])
    def test_valid_port_accepted(self, valid_port: int) -> None:
        cfg = WebConfig(bind_port=valid_port)
        assert cfg.bind_port == valid_port

    def test_empty_bind_host_rejected(self) -> None:
        with pytest.raises(Exception):
            WebConfig(bind_host="")

    def test_empty_session_secret_env_var_rejected(self) -> None:
        with pytest.raises(Exception):
            WebConfig(session_secret_env_var="")

    def test_session_max_age_bounds(self) -> None:
        # 0 is rejected; 1 is min; 90 is max
        with pytest.raises(Exception):
            WebConfig(session_max_age_days=0)
        with pytest.raises(Exception):
            WebConfig(session_max_age_days=91)
        WebConfig(session_max_age_days=1)
        WebConfig(session_max_age_days=90)

    def test_bcrypt_cost_bounds(self) -> None:
        # Per ADR-017, 12 is default; 10-15 acceptable
        with pytest.raises(Exception):
            WebConfig(bcrypt_cost=9)
        with pytest.raises(Exception):
            WebConfig(bcrypt_cost=16)
        WebConfig(bcrypt_cost=10)
        WebConfig(bcrypt_cost=15)

    def test_rate_limit_attempts_bounds(self) -> None:
        with pytest.raises(Exception):
            WebConfig(rate_limit_attempts=0)
        with pytest.raises(Exception):
            WebConfig(rate_limit_attempts=101)
        WebConfig(rate_limit_attempts=1)
        WebConfig(rate_limit_attempts=100)

    def test_htmx_poll_seconds_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            WebConfig(htmx_poll_seconds=0.0)
        with pytest.raises(Exception):
            WebConfig(htmx_poll_seconds=-1.0)
        cfg = WebConfig(htmx_poll_seconds=0.001)
        assert cfg.htmx_poll_seconds == 0.001

    def test_htmx_poll_seconds_upper_bound(self) -> None:
        with pytest.raises(Exception):
            WebConfig(htmx_poll_seconds=301.0)
        cfg = WebConfig(htmx_poll_seconds=300.0)
        assert cfg.htmx_poll_seconds == 300.0

    def test_invalid_log_format_rejected(self) -> None:
        with pytest.raises(Exception):
            WebConfig(log_format="syslog")  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# WobbleBotConfig.web wiring                                            #
# --------------------------------------------------------------------- #


class TestWobbleBotConfigWebField:
    def test_default_is_none_when_omitted(self) -> None:
        cfg = WobbleBotConfig.model_validate(_BASE_REQUIRED)
        assert cfg.web is None

    def test_block_present_validates(self) -> None:
        data = {
            **_BASE_REQUIRED,
            "web": {
                "bind_host": "0.0.0.0",
                "bind_port": 9000,
                "session_max_age_days": 14,
                "advise_db": "data/wobblebot-advise.db",
            },
        }
        cfg = WobbleBotConfig.model_validate(data)
        assert cfg.web is not None
        assert cfg.web.bind_host == "0.0.0.0"
        assert cfg.web.bind_port == 9000
        assert cfg.web.session_max_age_days == 14
        assert cfg.web.advise_db == "data/wobblebot-advise.db"
        # Unspecified fields use defaults
        assert cfg.web.bcrypt_cost == 12

    def test_empty_block_uses_all_defaults(self) -> None:
        data = {**_BASE_REQUIRED, "web": {}}
        cfg = WobbleBotConfig.model_validate(data)
        assert cfg.web is not None
        assert cfg.web.bind_port == 8000

    def test_invalid_block_propagates(self) -> None:
        data = {**_BASE_REQUIRED, "web": {"bind_port": 99999}}
        with pytest.raises(Exception):
            WobbleBotConfig.model_validate(data)


# --------------------------------------------------------------------- #
# Round-trip through load_config (filesystem)                           #
# --------------------------------------------------------------------- #


class TestLoadConfigRoundTrip:
    def test_no_web_block_loads_with_web_none(self, tmp_path: Path) -> None:
        import yaml

        body = yaml.safe_dump(_BASE_REQUIRED)
        path = tmp_path / "settings.yml"
        path.write_text(body, encoding="utf-8")
        cfg = load_config(path)
        assert cfg.web is None

    def test_web_block_round_trips(self, tmp_path: Path) -> None:
        body = dedent("""\
            grid:
              default:
                spacing_percentage: "1.0"
                levels_above: 3
                levels_below: 3
                order_size_usd: "10"
            safety:
              max_total_exposure_usd: "100"
              max_daily_spend_usd: "100"
              max_per_coin_exposure_usd: "50"
              max_orders_per_coin: 10
              emergency_stop:
                enabled: true
                max_loss_percentage: "5"
                min_exchange_balance_usd: "0"
            web:
              bind_host: "127.0.0.1"
              bind_port: 8123
              session_max_age_days: 14
              rate_limit_attempts: 3
              rate_limit_window_seconds: 30
              bcrypt_cost: 13
              htmx_poll_seconds: 10.0
              operator_db: "/tmp/operator.db"
              advise_db: "/tmp/advise.db"
              harvest_db: "/tmp/harvest.db"
              observe_db: "/tmp/observe.db"
              news_db: "/tmp/news.db"
              live_db: "/tmp/live.db"
              log_format: "json"
            """)
        path = tmp_path / "settings.yml"
        path.write_text(body, encoding="utf-8")
        cfg = load_config(path)
        assert cfg.web is not None
        assert cfg.web.bind_port == 8123
        assert cfg.web.session_max_age_days == 14
        assert cfg.web.rate_limit_attempts == 3
        assert cfg.web.bcrypt_cost == 13
        assert cfg.web.htmx_poll_seconds == 10.0
        assert cfg.web.advise_db == "/tmp/advise.db"
        assert cfg.web.log_format == "json"
