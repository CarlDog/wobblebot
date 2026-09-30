"""Operator setup failures release every database opened before the daemon loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.fixtures import grid_config, safety_config
from wobblebot.cli import operator

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_DATABASES = ("operator", "live", "observe", "advise", "news", "harvest")


@pytest.mark.parametrize(
    "failure_point",
    [
        "primary_connect",
        "optional_connect",
        "prompt",
        "assistant",
        "warmup",
        "transport",
        "task_setup",
    ],
)
async def test_early_startup_failure_closes_all_open_resources(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    opened: list[str] = []
    closed: list[str] = []
    assistant_closed: list[str] = []

    class Storage:
        def __init__(self, path: str) -> None:
            self.path = path

        async def connect(self) -> None:
            opened.append(self.path)
            if failure_point == "primary_connect" and self.path == "operator":
                raise PermissionError("synthetic primary connect failure")
            if failure_point == "optional_connect" and self.path == "news":
                raise PermissionError("synthetic optional connect failure")

        async def close(self) -> None:
            closed.append(self.path)

    class Assistant:
        async def warmup(self) -> None:
            if failure_point == "warmup":
                raise RuntimeError("synthetic warmup failure")

        async def aclose(self) -> None:
            assistant_closed.append("assistant")

    class Transport:
        def __init__(self, _config: object) -> None:
            if failure_point == "transport":
                raise RuntimeError("synthetic transport failure")

        def set_confirm_handler(self, _handler: object) -> None:
            pass

        def on_message(self, _handler: object) -> None:
            pass

    async def idle_loop(**kwargs: object) -> None:
        await kwargs["stop_event"].wait()

    def fail_task_setup(_config: object) -> object:
        raise RuntimeError("synthetic task setup failure")

    def load_prompt(_path: object) -> object:
        if failure_point == "prompt":
            raise FileNotFoundError("synthetic missing prompt")
        return object()

    monkeypatch.setattr(operator, "SQLiteStorageAdapter", Storage)
    monkeypatch.setattr(operator, "load_prompt", load_prompt)
    monkeypatch.setattr(operator, "DiscordTransport", Transport)
    monkeypatch.setattr(operator, "install_signal_handlers", lambda *_, **__: None)
    monkeypatch.setattr(operator, "_forwarder_loop", idle_loop)
    monkeypatch.setattr(operator, "_ttl_expirer_loop", idle_loop)
    if failure_point == "task_setup":
        monkeypatch.setattr(operator, "derive_thresholds_from_config", fail_task_setup)
    monkeypatch.setattr(
        operator,
        "_build_assistant",
        lambda *_: None if failure_point == "assistant" else Assistant(),
    )
    config = SimpleNamespace(
        grid=grid_config(),
        safety=safety_config(),
        live=None,
        harvester=None,
        operator=SimpleNamespace(
            operator_db="operator",
            live_db="live",
            observe_db="observe",
            advise_db="advise",
            news_db="news",
            harvest_db="harvest",
            assistant=SimpleNamespace(prompt_file="unused", model="stub"),
            auth=SimpleNamespace(
                outbound_channel_id="100",
                allowed_channel_ids=frozenset({"100"}),
                allowed_user_ids=frozenset({"42"}),
                bot_token_env_var="UNUSED",
            ),
            forwarder_poll_seconds=2,
            ttl_expirer_poll_seconds=2,
            heartbeat_alert_mute=[],
            history_backfill_messages=0,
            context_window_turns=10,
            confirm_ttl_seconds=300,
        ),
    )
    active_before = {
        task for task in asyncio.all_tasks() if task.get_name().startswith("operator-")
    }

    if failure_point in {"primary_connect", "optional_connect"}:
        message = f"synthetic {failure_point.replace('_', ' ')} failure"
        with pytest.raises(PermissionError, match=message):
            await operator._main_async(config)
    elif failure_point in {"warmup", "transport", "task_setup"}:
        message = f"synthetic {failure_point.replace('_', ' ')} failure"
        with pytest.raises(RuntimeError, match=message):
            await operator._main_async(config)
    else:
        assert await operator._main_async(config) == 2

    attempted = (
        _DATABASES[:1]
        if failure_point == "primary_connect"
        else _DATABASES[:5] if failure_point == "optional_connect" else _DATABASES
    )
    assert opened == list(attempted)
    assert closed == list(attempted)
    assert assistant_closed == (
        []
        if failure_point in {"primary_connect", "optional_connect", "prompt", "assistant"}
        else ["assistant"]
    )
    assert {
        task for task in asyncio.all_tasks() if task.get_name().startswith("operator-")
    } == active_before
