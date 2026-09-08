"""Cloud-check and ledger-query entry points expose the new provider safely."""

import argparse
from decimal import Decimal
from pathlib import Path

import pytest

from tools import run_cloud_check, show_llm_costs
from wobblebot.config.advisor import GremlinConfig
from wobblebot.config.prompts import load_prompt
from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
from wobblebot.services.llm_pricing import get_price_point
from wobblebot.services.llm_retry import LLMRetryConfig

pytestmark = pytest.mark.unit


def test_cloud_provider_is_not_advertised_for_unsupported_gremlin():
    with pytest.raises(ValueError):
        GremlinConfig(provider="ollama_cloud")


@pytest.mark.asyncio
async def test_operator_role_rejected_without_opening_database(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-only")
    assert (
        await run_cloud_check._run(argparse.Namespace(provider="ollama_cloud", role="operator"))
        == 2
    )


@pytest.mark.asyncio
async def test_missing_key_rejected_without_opening_database(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert (
        await run_cloud_check._run(argparse.Namespace(provider="ollama_cloud", role="quant")) == 2
    )


def test_cloud_smoke_builder_uses_native_adapter(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-only")
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return "native-cloud-adapter"

    monkeypatch.setattr(run_cloud_check, "OllamaCloudAdvisorAdapter", build)
    result = run_cloud_check._build_advisor(
        argparse.Namespace(
            provider="ollama_cloud", role="quant", model="gpt-oss:20b", max_tokens=4000
        ),
        object(),
        load_prompt(Path("config/prompts/quant.md")),
        LLMCostConfig(),
        LLMRetryConfig(),
        SessionCostTracker(),
    )
    assert result == "native-cloud-adapter"
    assert captured["api_key"] == "test-only" and captured["max_tokens"] == 4000
    assert "ollama_cloud" in show_llm_costs._VALID_PROVIDERS


@pytest.mark.parametrize(
    "model,input_rate,cache_rate,output_rate",
    [
        ("gpt-oss:120b", "0.15", "0.014", "0.60"),
        ("gpt-oss:20b", "0.07", "0.035", "0.30"),
        ("qwen3.5:397b", "0.60", None, "3.60"),
        ("gemma4:31b", "0.14", "0.05", "0.40"),
    ],
)
def test_verified_initial_rates(model, input_rate, cache_rate, output_rate):
    point = get_price_point("ollama_cloud", model)
    assert point.input_per_million_usd == Decimal(input_rate)
    assert point.output_per_million_usd == Decimal(output_rate)
    assert point.cached_input_per_million_usd == (Decimal(cache_rate) if cache_rate else None)
