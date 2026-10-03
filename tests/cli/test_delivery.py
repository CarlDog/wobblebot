"""Independent delivery boot, clean failure, and one real SQLite-to-HTTP-fixture cycle."""

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.services.test_delivery import seed
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import delivery
from wobblebot.config.runtime import load_resolved_config

pytestmark = pytest.mark.unit
EXAMPLE = Path(__file__).resolve().parents[2] / "config/settings.example.yml"


@pytest.mark.parametrize("args", [["--config", "does-not-exist.yml"], ["--config", str(EXAMPLE)]])
def test_deprived_startup_never_constructs_transport(args, monkeypatch):
    monkeypatch.setattr("sys.argv", ["delivery", *args])
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(delivery, "load_operator_env", lambda: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("must validate before constructing a transport")

    monkeypatch.setattr(delivery, "DiscordDelivery", forbidden)
    assert delivery.main() == 2


@pytest.mark.asyncio
async def test_sender_runs_without_operator_process(tmp_path, monkeypatch):
    config = load_resolved_config(EXAMPLE, "cpu-only")
    path = str(tmp_path / "operator.db")
    config = config.model_copy(
        update={
            "delivery": config.delivery.model_copy(update={"operator_db": path}),
            "operator": config.operator.model_copy(
                update={
                    "operator_db": path,
                    "auth": config.operator.auth.model_copy(
                        update={"outbound_channel_id": "123", "allowed_channel_ids": ["123"]}
                    ),
                }
            ),
        }
    )
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    await seed(storage)
    await storage.close()
    stops = []
    monkeypatch.setattr(
        delivery, "install_signal_handlers", lambda _loop, stop, **_kwargs: stops.append(stop)
    )
    calls = []

    def send(request):
        calls.append(request)
        stops[0].set()
        return httpx.Response(200, json={"id": "456"})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        delivery.httpx, "AsyncClient", lambda: factory(transport=httpx.MockTransport(send))
    )
    assert await asyncio.wait_for(delivery.run(config, "fixture-token"), timeout=5) == 0
    assert len(calls) == 1
    await storage.connect()
    try:
        (row,) = await storage.get_notifications()
        assert row.forwarded and row.delivery_message_id == "456"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_health_observer_failure_terminates_supervised_daemon(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from tests.services.test_health_response import operator_config

    config = operator_config(tmp_path / "operator.db")
    monkeypatch.setattr(delivery, "install_signal_handlers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(delivery.HealthObserver, "poll", AsyncMock(side_effect=RuntimeError))
    assert await asyncio.wait_for(delivery.run(config, "fixture-token"), timeout=5) == 1
