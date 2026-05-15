"""Tests for the NewsConfig schema (Stage 3.2.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wobblebot.config.cli import CryptoCompareSpec, NewsConfig, RssFeedSpec

pytestmark = pytest.mark.unit


class TestRssFeedSpec:
    def test_minimal_valid(self) -> None:
        spec = RssFeedSpec(source_id="rss:coindesk", url="https://example.com/feed")
        assert spec.enabled is True

    def test_empty_source_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RssFeedSpec(source_id="", url="https://example.com/feed")

    def test_empty_url_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RssFeedSpec(source_id="rss:x", url="")

    def test_disabled_flag(self) -> None:
        spec = RssFeedSpec(
            source_id="rss:noisy",
            url="https://example.com/feed",
            enabled=False,
        )
        assert spec.enabled is False

    def test_frozen(self) -> None:
        spec = RssFeedSpec(source_id="rss:x", url="https://x")
        with pytest.raises(ValidationError):
            spec.enabled = False  # type: ignore[misc]


class TestCryptoCompareSpec:
    def test_disabled_by_default(self) -> None:
        spec = CryptoCompareSpec()
        assert spec.enabled is False
        assert spec.lang == "EN"
        assert spec.categories is None

    def test_can_enable_with_lang(self) -> None:
        spec = CryptoCompareSpec(enabled=True, lang="ES", categories="BTC|ETH")
        assert spec.enabled is True
        assert spec.lang == "ES"
        assert spec.categories == "BTC|ETH"

    def test_frozen(self) -> None:
        spec = CryptoCompareSpec()
        with pytest.raises(ValidationError):
            spec.enabled = True  # type: ignore[misc]


class TestNewsConfig:
    def test_empty_valid_with_defaults(self) -> None:
        """NewsConfig no longer carries interval; cadence lives in
        the top-level `schedules.news` per Stage 3.3 Slice C.0."""
        cfg = NewsConfig()
        assert cfg.db == "data/wobblebot-news.db"
        assert cfg.rss_feeds == []
        assert cfg.cryptocompare.enabled is False
        assert cfg.log_format == "plain"

    def test_with_multiple_feeds(self) -> None:
        cfg = NewsConfig(
            rss_feeds=[
                RssFeedSpec(source_id="rss:a", url="https://a.example.com/feed"),
                RssFeedSpec(source_id="rss:b", url="https://b.example.com/feed", enabled=False),
            ],
        )
        assert len(cfg.rss_feeds) == 2
        assert cfg.rss_feeds[1].enabled is False

    def test_feeds_accept_dict_form_for_yaml(self) -> None:
        """Pydantic parses YAML-loaded dicts into RssFeedSpec instances."""
        cfg = NewsConfig.model_validate(
            {
                "rss_feeds": [
                    {"source_id": "rss:y", "url": "https://y", "enabled": True},
                ],
            }
        )
        assert cfg.rss_feeds[0].source_id == "rss:y"

    def test_frozen(self) -> None:
        cfg = NewsConfig()
        with pytest.raises(ValidationError):
            cfg.poll_interval_minutes = 60.0  # type: ignore[misc]
