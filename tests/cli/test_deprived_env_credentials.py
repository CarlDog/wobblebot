"""Real CLI entry-point checks for the two C4 missing-credential defects."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("operator", None),
        ("operator", ""),
        ("operator", " \t\n"),
        ("recalibrate", None),
        ("recalibrate", ""),
    ],
)
def test_missing_credentials_exit_before_io(
    name: str,
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = importlib.import_module(f"wobblebot.cli.{name}")
    settings = Path(__file__).resolve().parents[2] / "config/settings.example.yml"
    argv = ["wobblebot", "--config", str(settings)]
    if name == "recalibrate":
        argv += ["--target-balance", "100"]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(module, "load_operator_env", lambda: None)
    monkeypatch.setattr(module, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(logging.getLogger("wobblebot"), "propagate", True)
    credentials = (
        ["DISCORD_BOT_TOKEN"]
        if name == "operator"
        else ["KRAKEN_READER_API_KEY", "KRAKEN_READER_API_SECRET"]
    )
    for key in credentials:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    forbidden = Mock(side_effect=AssertionError("startup reached external work"))
    if name == "operator":
        monkeypatch.setattr(module, "_main_async", forbidden)
    else:
        monkeypatch.setattr(module, "KrakenAdapter", forbidden)

    assert module.main() == 2
    forbidden.assert_not_called()
    assert credentials[0] in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("custom_token", [None, "fixture-custom-token"])
def test_operator_uses_configured_token_variable(
    custom_token: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from wobblebot.cli import operator
    from wobblebot.config.runtime import load_resolved_config

    settings = Path(__file__).resolve().parents[2] / "config/settings.example.yml"
    config = load_resolved_config(
        settings,
        cli_overrides={"operator": {"auth": {"bot_token_env_var": "C4_CUSTOM_DISCORD_TOKEN"}}},
    )
    monkeypatch.setattr("sys.argv", ["wobblebot"])
    monkeypatch.setattr(operator, "load_operator_env", lambda: None)
    monkeypatch.setattr(operator, "load_resolved_config", lambda **kwargs: config)
    monkeypatch.setattr(operator, "configure_logging", lambda **kwargs: None)
    # The real runner deliberately uses os._exit after flushing; keep pytest alive.
    monkeypatch.setattr(operator.os, "_exit", sys.exit)
    monkeypatch.setattr(logging.getLogger("wobblebot"), "propagate", True)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fixture-standard-token-must-not-be-used")
    startup = AsyncMock(return_value=0)
    monkeypatch.setattr(operator, "_main_async", startup)
    if custom_token is None:
        monkeypatch.delenv("C4_CUSTOM_DISCORD_TOKEN", raising=False)
        assert operator.main() == 2
        startup.assert_not_called()
        assert "C4_CUSTOM_DISCORD_TOKEN" in caplog.text
    else:
        monkeypatch.setenv("C4_CUSTOM_DISCORD_TOKEN", custom_token)
        with pytest.raises(SystemExit) as exc:
            operator.main()
        assert exc.value.code == 0
        startup.assert_awaited_once_with(config)
        assert custom_token not in caplog.text
    assert "fixture-standard-token-must-not-be-used" not in caplog.text


@pytest.mark.asyncio
async def test_recalibrate_exchange_failure_still_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from wobblebot.cli import recalibrate
    from wobblebot.config.runtime import load_resolved_config
    from wobblebot.ports.exceptions import ExchangeError

    settings = Path(__file__).resolve().parents[2] / "config/settings.example.yml"
    config = load_resolved_config(settings)
    monkeypatch.setenv("KRAKEN_READER_API_KEY", "fixture-reader")
    monkeypatch.setenv("KRAKEN_READER_API_SECRET", "fixture-secret")
    adapter = Mock()
    adapter.get_balance = AsyncMock(side_effect=ExchangeError("fixture upstream failure"))
    adapter.aclose = AsyncMock()
    monkeypatch.setattr(recalibrate, "KrakenAdapter", lambda **kwargs: adapter)

    assert (
        await recalibrate._run(
            config=config,
            target_balance=Decimal("100"),
            current_balance_override=None,
            commit=False,
            config_path=settings,
        )
        == 1
    )
    adapter.get_balance.assert_awaited_once_with("USD")
    adapter.aclose.assert_awaited_once()
