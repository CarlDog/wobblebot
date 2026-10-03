"""Outbound-only Discord REST client for independently delivered alerts."""

import math
from collections.abc import Sequence
from typing import Any

import httpx

from wobblebot.ports.delivery import DeliveryError


class DiscordDelivery:
    """No Gateway, LLM, exchange client or command handler; token stays in headers."""

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def send_embed(  # pylint: disable=too-many-arguments
        # Same explicit embed fields as the existing Gateway transport.
        self,
        channel_id: str,
        *,
        title: str,
        description: str,
        color: int = 0,
        fields: Sequence[tuple[str, str] | tuple[str, str, bool]] | None = None,
        footer: str | None = None,
    ) -> str:
        """Classify rejected/pre-connect sends separately from ambiguous outcomes."""
        if not channel_id.isdecimal():
            raise DeliveryError("Invalid Discord channel", permanent=True)
        embed: dict[str, Any] = {"title": title, "description": description, "color": color}
        if fields:
            embed["fields"] = [
                {"name": item[0], "value": item[1], "inline": item[2] if len(item) == 3 else False}
                for item in fields
            ]
        if footer:
            embed["footer"] = {"text": footer}
        try:
            response = await self._client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {self._token}"},
                json={"embeds": [embed], "allowed_mentions": {"parse": []}},
                timeout=10,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise DeliveryError("Discord connection unavailable", retryable=True) from None
        except httpx.HTTPError:
            raise DeliveryError("Discord send outcome uncertain") from None
        if response.status_code == 429:
            try:
                delay = float(response.headers.get("Retry-After") or response.json()["retry_after"])
                if not math.isfinite(delay) or not 0 <= delay <= 86400:
                    raise ValueError("invalid delay")
            except (ValueError, KeyError, TypeError):
                delay = 60.0
            raise DeliveryError(
                "Discord rejected rate-limited send", retryable=True, retry_after_seconds=delay
            )
        if response.status_code >= 400:
            raise DeliveryError(
                f"Discord HTTP {response.status_code}", permanent=response.status_code < 500
            )
        try:
            identifier = response.json()["id"]
            if not isinstance(identifier, str) or not identifier.isdecimal():
                raise ValueError("invalid receipt")
        except (ValueError, KeyError, TypeError):
            raise DeliveryError("Discord receipt unavailable; send outcome uncertain") from None
        return identifier
