"""Exercise the daemon's actual task construction and supervision roles offline."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.fixtures import grid_config, safety_config
from wobblebot.cli import operator

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

REQUIRED = {
    "operator-forwarder",
    "operator-ttl-expirer",
    "operator-heartbeat-alerts",
    "operator-gateway",
}
ONE_SHOT = {"operator-history-backfill"}


@pytest.mark.parametrize("failed_name", [None, *sorted(REQUIRED)])
async def test_real_main_constructs_and_supervises_every_task(monkeypatch, failed_name):
    closed = asyncio.Event()
    observed = []

    class Transport:
        def __init__(self, config):
            pass

        def set_confirm_handler(self, handler):
            pass

        def on_message(self, handler):
            pass

        async def start(self):
            if failed_name == "operator-gateway":
                raise RuntimeError("synthetic required-task failure")
            await closed.wait()

        async def close(self):
            closed.set()

    async def loop(**kwargs):
        if asyncio.current_task().get_name() == failed_name:
            raise RuntimeError("synthetic required-task failure")
        await kwargs["stop_event"].wait()

    async def once(**kwargs):
        return None

    supervise = operator._supervise_background_tasks

    async def record_supervision(*, must_run, one_shot, stop_event):
        created = {task for task in asyncio.all_tasks() if task.get_name().startswith("operator-")}
        assert {task.get_name() for task in created} == REQUIRED | ONE_SHOT
        assert set(must_run) | set(one_shot) == created
        assert {task.get_name() for task in must_run} == REQUIRED
        assert {task.get_name() for task in one_shot} == ONE_SHOT
        observed.extend(created)
        if failed_name is None:
            stop_event.set()
        return await supervise(must_run=must_run, one_shot=one_shot, stop_event=stop_event)

    monkeypatch.setattr(operator, "DiscordTransport", Transport)
    monkeypatch.setattr(operator, "load_prompt", lambda *_: object())
    monkeypatch.setattr(operator, "_build_assistant", lambda *_: SimpleNamespace())
    monkeypatch.setattr(operator, "derive_thresholds_from_config", lambda *_: object())
    monkeypatch.setattr(operator, "install_signal_handlers", lambda *_, **__: None)
    monkeypatch.setattr(operator, "_forwarder_loop", loop)
    monkeypatch.setattr(operator, "_ttl_expirer_loop", loop)
    monkeypatch.setattr(operator, "_heartbeat_alert_loop", loop)
    monkeypatch.setattr(operator, "_backfill_history_task", once)
    monkeypatch.setattr(operator, "_supervise_background_tasks", record_supervision)
    config = SimpleNamespace(
        grid=grid_config(),
        safety=safety_config(),
        live=None,
        harvester=None,
        operator=SimpleNamespace(
            operator_db=":memory:",
            live_db=None,
            observe_db=None,
            advise_db=None,
            news_db=None,
            harvest_db=None,
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
    exit_code = await asyncio.wait_for(operator._main_async(config), timeout=5)
    assert exit_code == (1 if failed_name else 0)
    assert closed.is_set()
    assert len(observed) == 5 and all(task.done() for task in observed)
