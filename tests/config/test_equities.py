"""The equities flag cannot activate a guessed broker or crypto substitute."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from wobblebot.cli import sandbox
from wobblebot.config.equities import EquitiesConfig
from wobblebot.config.loader import WobbleBotConfig, load_config
from wobblebot.config.runtime import load_resolved_config

pytestmark = pytest.mark.unit


def example() -> dict:
    return yaml.safe_load(Path("config/settings.example.yml").read_text(encoding="utf-8"))


def test_omitted_and_explicit_false_preserve_crypto_config():
    raw = example()
    explicit = WobbleBotConfig.model_validate(raw)
    raw.pop("equities")
    omitted = WobbleBotConfig.model_validate(raw)
    assert explicit == omitted
    assert not omitted.equities.enabled
    assert omitted.live == explicit.live
    assert omitted.grid == explicit.grid


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_activation_requires_a_boolean(value):
    with pytest.raises(ValidationError):
        EquitiesConfig(enabled=value)


@pytest.mark.parametrize("loader", [load_config, load_resolved_config])
def test_enabled_unavailable_fails_at_both_loader_boundaries(tmp_path, loader):
    raw = example()
    raw["equities"]["enabled"] = True
    path = tmp_path / "settings.yml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="Equities support unavailable"):
        loader(path)


def test_profile_cannot_bypass_capability_check(tmp_path):
    raw = example()
    raw["profiles"]["equity-request"] = {"equities": {"enabled": True}}
    path = tmp_path / "settings.yml"
    path.write_text(yaml.safe_dump(raw))
    assert not load_resolved_config(path).equities.enabled
    with pytest.raises(ValidationError, match="stock/ETF adapter"):
        load_resolved_config(path, profile_name="equity-request")
    with pytest.raises(ValidationError, match="stock/ETF adapter"):
        load_resolved_config(path, cli_overrides={"equities": {"enabled": True}})


def test_crypto_adapter_cannot_be_declared_as_equities():
    with pytest.raises(ValidationError):
        EquitiesConfig(enabled=False, adapter="kraken-spot")


@pytest.mark.parametrize("enabled, expected_code", [(False, 0), (True, 2)])
def test_cli_checks_capability_before_tasks_and_provider_wiring(
    tmp_path, monkeypatch, capsys, enabled, expected_code
):
    raw = example()
    raw["equities"]["enabled"] = enabled
    path = tmp_path / "settings.yml"
    path.write_text(yaml.safe_dump(raw))
    run_crypto = AsyncMock(return_value=0)
    monkeypatch.setattr(sandbox, "_run", run_crypto)
    monkeypatch.setattr("sys.argv", ["sandbox", "--config", str(path)])
    assert sandbox.main() == expected_code
    if enabled:
        run_crypto.assert_not_called()
        text = capsys.readouterr().err
        assert "Equities support unavailable" in text
        assert "equities.enabled=false" in text
        assert "Traceback" not in text
    else:
        run_crypto.assert_awaited_once()
        assert not run_crypto.call_args.args[0].equities.enabled
