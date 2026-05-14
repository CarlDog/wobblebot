"""Tests for KrakenConfig — env-var loading and validation."""

from __future__ import annotations

import pytest

from wobblebot.config.kraken import KrakenConfig

pytestmark = pytest.mark.unit


class TestFromEnv:
    def test_loads_required_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRAKEN_API_KEY", "key-abc")
        monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")  # base64("secret")
        monkeypatch.delenv("KRAKEN_BASE_URL", raising=False)

        cfg = KrakenConfig.from_env()

        assert cfg.api_key == "key-abc"
        assert cfg.api_secret == "c2VjcmV0"
        assert cfg.base_url == "https://api.kraken.com"

    def test_base_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRAKEN_API_KEY", "k")
        monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")
        monkeypatch.setenv("KRAKEN_BASE_URL", "https://mock.kraken.test")

        cfg = KrakenConfig.from_env()

        assert cfg.base_url == "https://mock.kraken.test"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
        monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")

        with pytest.raises(ValueError, match="KRAKEN_API_KEY"):
            KrakenConfig.from_env()

    def test_missing_secret_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRAKEN_API_KEY", "k")
        monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)

        with pytest.raises(ValueError, match="KRAKEN_API_SECRET"):
            KrakenConfig.from_env()

    def test_empty_credentials_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRAKEN_API_KEY", "")
        monkeypatch.setenv("KRAKEN_API_SECRET", "")

        with pytest.raises(ValueError, match="KRAKEN_API_KEY.*KRAKEN_API_SECRET"):
            KrakenConfig.from_env()

    def test_both_missing_lists_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
        monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)

        with pytest.raises(ValueError) as exc_info:
            KrakenConfig.from_env()

        msg = str(exc_info.value)
        assert "KRAKEN_API_KEY" in msg
        assert "KRAKEN_API_SECRET" in msg


class TestValidation:
    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            KrakenConfig(
                api_key="k",
                api_secret="c2VjcmV0",
                request_timeout_seconds=0.0,
            )

    def test_frozen_after_construction(self) -> None:
        cfg = KrakenConfig(api_key="k", api_secret="c2VjcmV0")
        with pytest.raises(ValueError):
            cfg.api_key = "different"  # type: ignore[misc]
