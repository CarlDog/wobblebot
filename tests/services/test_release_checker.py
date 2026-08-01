"""Unit tests for services.release_checker (v1.1 footer update indicator)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from wobblebot.services.release_checker import _parse_version_tuple, check_for_update

pytestmark = pytest.mark.unit


def _release(
    *,
    tag_name: str = "v1.1.0",
    html_url: str = "https://github.com/CarlDog/wobblebot/releases/tag/v1.1.0",
) -> dict[str, Any]:
    return {"tag_name": tag_name, "html_url": html_url, "name": "v1.1.0"}


class TestParseVersionTuple:
    def test_simple_version(self) -> None:
        assert _parse_version_tuple("1.2.3") == (1, 2, 3)

    def test_two_part_version(self) -> None:
        assert _parse_version_tuple("1.0") == (1, 0)

    def test_multi_digit_components_order_correctly(self) -> None:
        """The whole reason for tuple comparison over string comparison:
        "1.10.0" < "1.9.0" lexicographically, but 1.10.0 is newer."""
        assert _parse_version_tuple("1.10.0") > _parse_version_tuple("1.9.0")  # type: ignore[operator]

    def test_pre_release_suffix_returns_none(self) -> None:
        assert _parse_version_tuple("1.1.0-rc1") is None

    def test_non_numeric_returns_none(self) -> None:
        assert _parse_version_tuple("not-a-version") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_version_tuple("") is None


@pytest.mark.asyncio
class TestCheckForUpdateHappyPath:
    async def test_newer_release_available(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release(tag_name="v1.1.0"))

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is True
        assert result.current_version == "1.0.0"
        assert result.latest_version == "1.1.0"
        assert result.release_url == "https://github.com/CarlDog/wobblebot/releases/tag/v1.1.0"

    async def test_same_version_no_update(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release(tag_name="v1.0.0"))

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False
        assert result.latest_version == "1.0.0"

    async def test_older_release_no_update(self) -> None:
        """Defensive: a rollback or a stale GitHub cache must never
        claim an update when the running version is actually newer."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release(tag_name="v0.9.0"))

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False

    async def test_multi_digit_minor_version_compares_correctly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release(tag_name="v1.10.0"))

        result = await check_for_update(
            "1.9.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is True

    async def test_tag_without_v_prefix(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release(tag_name="1.1.0"))

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.latest_version == "1.1.0"
        assert result.update_available is True

    async def test_request_url_and_headers(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["accept"] = request.headers.get("accept")
            return httpx.Response(200, json=_release())

        await check_for_update(
            "1.0.0",
            repo="CarlDog/wobblebot",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert captured["url"] == "https://api.github.com/repos/CarlDog/wobblebot/releases/latest"
        assert captured["accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
class TestCheckForUpdateFailurePaths:
    """check_for_update never raises -- every failure degrades to
    update_available=False."""

    async def test_http_404_degrades_to_no_update(self) -> None:
        """A repo with no releases yet returns 404 -- must not raise."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False
        assert result.latest_version is None

    async def test_connection_error_degrades_to_no_update(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns refused")

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False

    async def test_malformed_json_degrades_to_no_update(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False

    async def test_missing_tag_name_degrades_to_no_update(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"html_url": "https://example.com"})

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False
        assert result.latest_version is None

    async def test_unparseable_current_version_does_not_claim_update(self) -> None:
        """current_version comes from wobblebot.__version__ so this is
        mostly defensive, but a malformed version string must never
        crash or false-positive an update."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release(tag_name="v1.1.0"))

        result = await check_for_update(
            "not-a-version",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert result.update_available is False
        # Still surfaces the latest version + link even though it can't compare.
        assert result.latest_version == "1.1.0"

    async def test_non_dict_envelope_degrades_to_no_update(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "a", "dict"])

        result = await check_for_update(
            "1.0.0", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert result.update_available is False


@pytest.mark.asyncio
class TestClientLifecycle:
    async def test_owned_client_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no client is passed, check_for_update must not leak
        the throwaway client it creates. Patches AsyncClient
        construction to a mocked-transport client so this stays a
        real unit test -- no actual network call to GitHub."""
        import wobblebot.services.release_checker as module

        closed_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release())

        real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        original_aclose = real_client.aclose

        async def _tracked_aclose() -> None:
            closed_calls["n"] += 1
            await original_aclose()

        real_client.aclose = _tracked_aclose  # type: ignore[method-assign]

        def _fake_async_client(*, timeout: float) -> httpx.AsyncClient:
            del timeout
            return real_client

        monkeypatch.setattr(module.httpx, "AsyncClient", _fake_async_client)

        result = await check_for_update("1.0.0")

        assert result.update_available is True
        assert closed_calls["n"] == 1

    async def test_external_client_not_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_release())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await check_for_update("1.0.0", client=client)
        assert not client.is_closed
        await client.aclose()
