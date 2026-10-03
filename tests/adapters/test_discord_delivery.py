"""Outbound REST contract; no real messages or credentials."""

import httpx
import pytest

from wobblebot.adapters.discord_delivery import DiscordDelivery
from wobblebot.ports.delivery import DeliveryError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "status,retryable,permanent", [(429, True, False), (403, False, True), (500, False, False)]
)
async def test_known_rejections_vs_uncertain_effects(status, retryable, permanent):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        with pytest.raises(DeliveryError) as error:
            await DiscordDelivery(client, "fixture-secret").send_embed(
                "123", title="a", description="b"
            )
        assert error.value.retryable is retryable
        assert error.value.permanent is permanent
        assert "fixture-secret" not in str(error.value)


async def test_receipt_and_no_mentions_or_credential_in_url():
    def send(request):
        assert request.url == "https://discord.com/api/v10/channels/123/messages"
        assert request.headers["Authorization"] == "Bot fixture-secret"
        assert b'"allowed_mentions":{"parse":[]}' in request.content
        return httpx.Response(200, json={"id": "456"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        assert (
            await DiscordDelivery(client, "fixture-secret").send_embed(
                "123", title="a", description="b"
            )
            == "456"
        )


async def test_retry_after_is_preserved_without_exposing_response_body():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429, headers={"Retry-After": "123.5"}, json={"message": "private fixture body"}
            )
        )
    ) as client:
        with pytest.raises(DeliveryError) as error:
            await DiscordDelivery(client, "fixture-secret").send_embed(
                "123", title="a", description="b"
            )
        assert error.value.retry_after_seconds == 123.5
        assert "private" not in str(error.value)
