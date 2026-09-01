"""Input-boundary tests for the live Discord webhook probe."""

import pytest
from tools.probe_discord_bot import validate_webhook_url


@pytest.mark.parametrize(
    "url",
    (
        "https://discord.com/api/webhooks/1234567890/a-token",
        "https://discordapp.com/api/webhooks/1234567890/a-token",
        "https://ptb.discord.com/api/webhooks/1234567890/a-token",
        "https://canary.discord.com/api/webhooks/1234567890/a-token",
    ),
)
def test_accepts_discord_webhook_urls(url: str) -> None:
    assert validate_webhook_url(url) == url


@pytest.mark.parametrize(
    "url",
    (
        "http://discord.com/api/webhooks/1234567890/a-token",
        "https://localhost/api/webhooks/1234567890/a-token",
        "https://discord.com@internal.example/api/webhooks/1234567890/a-token",
        "https://discord.com/api/webhooks/1234567890/a-token?redirect=https://internal.example",
        "https://discord.com/api/webhooks/1234567890",
        "https://discord.com/not-api/webhooks/1234567890/a-token",
    ),
)
def test_rejects_non_discord_or_non_webhook_urls(url: str) -> None:
    with pytest.raises(ValueError, match="Discord"):
        validate_webhook_url(url)
