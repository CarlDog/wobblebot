"""Bind the daemon entry point to the shared sweep-budget tracker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fixtures import grid_config, safety_config
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import advise
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.services.llm_cost_gate import SessionCostTracker

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("cloud_enabled", [True, False])
async def test_main_threads_builder_tracker_into_sweep_loop(
    monkeypatch: pytest.MonkeyPatch, cloud_enabled: bool
) -> None:
    """A loop-only reset test cannot catch a missing production tracker argument."""
    config = WobbleBotConfig.model_validate(
        {
            "grid": grid_config(),
            "safety": safety_config(),
            "schedules": {"advise": "4h"},
            "advise": {"symbols": ["BTC/USD"]},
            "advisor": {
                "type": "single",
                "provider": "anthropic" if cloud_enabled else "ollama",
                "model": "fixture-model",
                "prompt_file": "config/prompts/quant.md",
            },
            "operator": {
                "auth": {"outbound_channel_id": "fixture-channel"},
                "assistant": {"model": "fixture-assistant"},
            },
            "llm": {} if cloud_enabled else None,
        }
    )
    storage = MagicMock(spec=SQLiteStorageAdapter)
    storage.connect = AsyncMock()
    storage.close = AsyncMock()
    monkeypatch.setattr(advise, "SQLiteStorageAdapter", MagicMock(return_value=storage))
    monkeypatch.setattr(advise, "install_signal_handlers", MagicMock())
    builder_wirings = []
    advisor = MagicMock()
    advisor.aclose = AsyncMock()

    def build(_config, model_name_out, *, cloud_wiring):
        model_name_out.append("fixture-model")
        builder_wirings.append(cloud_wiring)
        return advisor

    def build_gremlin(_config, cloud_wiring):
        builder_wirings.append(cloud_wiring)

    monkeypatch.setattr(advise, "_build_advisor", build)
    monkeypatch.setattr(advise, "_build_gremlin_advisor", build_gremlin)
    run_loop = AsyncMock(return_value=0)
    monkeypatch.setattr(advise, "_run_loop", run_loop)

    assert await advise._main_async(config) == 0
    run_loop.assert_awaited_once()
    loop_tracker = run_loop.await_args.kwargs["session_tracker"]
    if cloud_enabled:
        assert builder_wirings[0] is builder_wirings[1]
        assert isinstance(loop_tracker, SessionCostTracker)
        assert loop_tracker is builder_wirings[0].session_tracker
        assert builder_wirings[0].llm_config is config.llm
    else:
        assert builder_wirings == [None, None]
        assert loop_tracker is None
    advisor.aclose.assert_awaited_once()
