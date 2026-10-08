"""N1: real persistence-to-web-reader contract with synthetic HTTP only."""

from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.provider_health import ProviderHealthSnapshot, ProviderObservation
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.llm_health import LLMEndpoint
from wobblebot.services.provider_health import PersistedProviderHealth, publish_provider_health

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_probe_persists_safe_result_and_reader_needs_no_credentials(tmp_path):
    owner = SQLiteStorageAdapter(tmp_path / "operator.db")
    await owner.connect()
    reader = SQLiteStorageAdapter(tmp_path / "operator.db", read_only=True)
    await reader.connect()
    try:
        assert (await PersistedProviderHealth(reader).get())[0].ok is None

        def handler(request):
            assert request.headers["Authorization"] == "test-secret"
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await publish_provider_health(
                owner,
                client,
                [
                    LLMEndpoint(
                        "OpenAI",
                        "https://fixture.invalid/models",
                        (("Authorization", "test-secret"),),
                    )
                ],
            )
        (result,) = await PersistedProviderHealth(reader).get()
        assert result.ok is False and "401" in result.detail
        (stored,) = await reader.get_provider_health()
        assert "test-secret" not in stored.model_dump_json()
        with pytest.raises(StorageError):
            await reader.save_provider_health(stored)
        # Complete replacement eliminates endpoints removed from configuration.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await publish_provider_health(owner, client, [])
        assert "No endpoints configured" in (await PersistedProviderHealth(reader).get())[0].detail
    finally:
        await reader.close()
        await owner.close()


@pytest.mark.parametrize("age", [timedelta(minutes=10), timedelta(minutes=-10)])
async def test_old_and_future_observations_never_report_current_health(tmp_path, age):
    owner = SQLiteStorageAdapter(tmp_path / "operator.db")
    await owner.connect()
    try:
        at = datetime.now(UTC) - age
        await owner.save_provider_health(
            ProviderHealthSnapshot(
                producer="operator",
                checked_at=at,
                observations=(
                    ProviderObservation(name="OpenAI", ok=True, detail="HTTP 200", checked_at=at),
                ),
            )
        )
        (result,) = await PersistedProviderHealth(owner).get()
        assert result.ok is None and "Stale" in result.detail
        assert result.checked_at == at
    finally:
        await owner.close()


async def test_transport_error_message_cannot_persist_sensitive_url(tmp_path):
    owner = SQLiteStorageAdapter(tmp_path / "operator.db")
    await owner.connect()
    try:

        def handler(request):
            raise httpx.ConnectError("sensitive-query-or-header", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await publish_provider_health(
                owner, client, [LLMEndpoint("OpenAI", "https://fixture.invalid/models")]
            )
        (stored,) = await owner.get_provider_health()
        assert "sensitive" not in stored.model_dump_json()
        assert stored.observations[0].detail == "ConnectError"
    finally:
        await owner.close()


async def test_missing_table_is_unknown_and_not_created_by_reader(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE old_table (id INTEGER)")
    reader = SQLiteStorageAdapter(path, read_only=True)
    await reader.connect()
    try:
        (result,) = await PersistedProviderHealth(reader).get()
        assert result.ok is None and result.checked_at is None
        with closing(sqlite3.connect(path)) as conn:
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='provider_health'"
                ).fetchall()
                == []
            )
    finally:
        await reader.close()
