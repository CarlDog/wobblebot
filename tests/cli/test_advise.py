"""Tests for cli/advise — advisor cycle wiring + fault isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from wobblebot.adapters.moe_advisor import MoEAdvisorAdapter
from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.advise import (
    _build_advisor,
    _moe_model_label,
    _run_cycle,
    _run_loop,
    _summary_to_dict,
)
from wobblebot.config.advisor import (
    AdvisorConfig,
    ArbitratorConfig,
    ExpertConfig,
    InferenceParams,
)
from wobblebot.domain.value_objects import Price, Symbol, Timestamp
from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    CurrentGridParams,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError
from wobblebot.services.summary_builder import SummaryBuilder

pytestmark = pytest.mark.unit


BTC_USD = Symbol(base="BTC", quote="USD")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    """Single in-memory adapter — Slice B's SummaryBuilder defaults
    news_storage to the primary when not supplied, so one adapter
    covers prices + news + suggestions in tests."""
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


async def _seed_prices(storage: SQLiteStorageAdapter) -> None:
    now = datetime.now(UTC)
    for offset, amount in [(20, "100"), (10, "105"), (1, "108")]:
        await storage.save_price_snapshot(
            BTC_USD,
            Price(amount=Decimal(amount), currency="USD"),
            Timestamp(dt=now - timedelta(minutes=offset)),
        )


class _CannedAdvisor(AdvisorPort):
    """Stub AdvisorPort that returns a canned recommendation or raises."""

    def __init__(
        self,
        *,
        recommendation: AdvisorRecommendation | None = None,
        error: AdvisorError | None = None,
    ) -> None:
        self._recommendation = recommendation
        self._error = error
        self.call_count = 0

    async def get_recommendation(self, summary: PerformanceSummary) -> AdvisorRecommendation:
        del summary
        self.call_count += 1
        if self._error is not None:
            raise self._error
        assert self._recommendation is not None
        return self._recommendation

    async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
        del recommendation
        return True


def _make_recommendation(confidence: str = "medium") -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommendation_id="rec-canned",
        timestamp=Timestamp(dt=datetime.now(UTC)),
        role="single",
        recommendations={"spacing_percentage": 1.2},
        rationale="Canned rationale for tests.",
        confidence=confidence,  # type: ignore[arg-type]
    )


def _default_grid() -> CurrentGridParams:
    return CurrentGridParams(
        spacing_percentage=1.0,
        levels_above=3,
        levels_below=3,
        order_size_usd=10.0,
    )


@pytest.mark.asyncio
class TestRunCycleHappyPath:
    async def test_persists_a_suggestion(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        advisor = _CannedAdvisor(recommendation=_make_recommendation())
        builder = SummaryBuilder(storage)

        ok = await _run_cycle(
            advisor,
            builder,
            storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grid=_default_grid(),
            model_name="phi4:14b",
        )

        assert ok is True
        assert advisor.call_count == 1
        suggestions = await storage.get_advisor_suggestions()
        assert len(suggestions) == 1
        persisted = suggestions[0]
        assert persisted.model_name == "phi4:14b"
        assert persisted.recommendation.confidence == "medium"
        assert persisted.recommendation.recommendations == {"spacing_percentage": 1.2}
        # Input summary should round-trip with the symbol baked in
        assert persisted.input_summary["symbol"] == "BTC/USD"
        assert persisted.input_summary["snapshot_count"] == 3

    async def test_grid_carried_into_audit_record(self, storage: SQLiteStorageAdapter) -> None:
        await _seed_prices(storage)
        advisor = _CannedAdvisor(recommendation=_make_recommendation())
        builder = SummaryBuilder(storage)
        grid = CurrentGridParams(spacing_percentage=2.0, levels_above=5, levels_below=5)

        await _run_cycle(
            advisor,
            builder,
            storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grid=grid,
            model_name="phi4:14b",
        )

        persisted = (await storage.get_advisor_suggestions())[0]
        assert persisted.input_summary["current_grid"]["spacing_percentage"] == 2.0
        assert persisted.input_summary["current_grid"]["levels_above"] == 5


@pytest.mark.asyncio
class TestRunCycleFaultIsolation:
    async def test_advisor_error_returns_false(self, storage: SQLiteStorageAdapter) -> None:
        """A bad advisor call doesn't kill the cycle — _run_cycle just
        returns False so the outer loop tries again next tick."""
        await _seed_prices(storage)
        advisor = _CannedAdvisor(error=AdvisorError("LLM offline"))
        builder = SummaryBuilder(storage)

        ok = await _run_cycle(
            advisor,
            builder,
            storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grid=_default_grid(),
            model_name="phi4:14b",
        )

        assert ok is False
        # No suggestion written on advisor failure.
        assert await storage.get_advisor_suggestions() == []

    async def test_empty_observe_db_still_runs(self, storage: SQLiteStorageAdapter) -> None:
        """No price snapshots → summary defaults are safe → advisor still
        gets called → suggestion persists. The "advise before observe has
        data" cold-start case shouldn't fail."""
        advisor = _CannedAdvisor(recommendation=_make_recommendation())
        builder = SummaryBuilder(storage)

        ok = await _run_cycle(
            advisor,
            builder,
            storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grid=_default_grid(),
            model_name="phi4:14b",
        )

        assert ok is True
        persisted = (await storage.get_advisor_suggestions())[0]
        assert persisted.input_summary["snapshot_count"] == 0
        assert persisted.input_summary["latest_price"] is None


@pytest.mark.asyncio
class TestSummaryToDict:
    async def test_serializes_recent_news(self) -> None:
        """The persisted input_summary must be JSON-safe — no Pydantic
        objects, no Decimals, no Timestamps as bare datetime."""
        summary = PerformanceSummary(
            symbol="BTC/USD",
            lookback_hours=6.0,
            snapshot_count=10,
            volatility=0.0004,
            max_drawdown=-0.03,
            flatness=0.97,
            cycle_count=0,
            win_rate=0.0,
        )
        result = _summary_to_dict(summary)
        # Must be a plain dict, JSON-serializable
        import json

        roundtrip = json.loads(json.dumps(result))
        assert roundtrip["symbol"] == "BTC/USD"
        assert roundtrip["lookback_hours"] == 6.0


class TestBuildAdvisorDispatch:
    """``_build_advisor`` is the single + MoE dispatch seam in cli/advise.

    These tests don't talk to a real Ollama server — they just verify
    that the right concrete adapter type comes back and the resolved
    model name lands in the audit slot. The OllamaAdapter constructor
    is cheap (creates an httpx.AsyncClient but doesn't connect).
    """

    @pytest.fixture
    def quant_prompt_path(self) -> str:
        return "config/prompts/quant.md"

    @pytest.fixture
    def risk_prompt_path(self) -> str:
        return "config/prompts/risk.md"

    @pytest.fixture
    def news_prompt_path(self) -> str:
        return "config/prompts/news.md"

    @pytest.fixture
    def arbitrator_prompt_path(self) -> str:
        return "config/prompts/arbitrator.md"

    def test_single_mode_returns_ollama(self, quant_prompt_path: str) -> None:
        config = AdvisorConfig(
            type="single",
            provider="ollama",
            model="phi4:14b",
            prompt_file=quant_prompt_path,
            inference_params=InferenceParams(),
        )
        out: list[str] = []
        advisor = _build_advisor(config, out)
        assert isinstance(advisor, OllamaAdapter)
        assert out == ["phi4:14b"]

    def test_moe_mode_returns_moe_adapter(
        self,
        quant_prompt_path: str,
        risk_prompt_path: str,
        news_prompt_path: str,
    ) -> None:
        config = AdvisorConfig(
            type="moe",
            aggregator="voting",
            experts=[
                ExpertConfig(
                    name="q",
                    provider="ollama",
                    model="phi4:14b",
                    role="quant",
                    prompt_file=quant_prompt_path,
                ),
                ExpertConfig(
                    name="r",
                    provider="ollama",
                    model="qwen3:8b",
                    role="risk",
                    prompt_file=risk_prompt_path,
                ),
                ExpertConfig(
                    name="n",
                    provider="ollama",
                    model="deepseek-r1:8b",
                    role="news",
                    prompt_file=news_prompt_path,
                ),
            ],
        )
        out: list[str] = []
        advisor = _build_advisor(config, out)
        assert isinstance(advisor, MoEAdvisorAdapter)
        # The label captures the aggregator + every expert role:model pair.
        assert "voting" in out[0]
        assert "quant:phi4:14b" in out[0]
        assert "risk:qwen3:8b" in out[0]
        assert "news:deepseek-r1:8b" in out[0]
        # No arbitrator suffix when aggregator != arbitrator.
        assert "arb=" not in out[0]

    def test_moe_mode_with_arbitrator(
        self,
        quant_prompt_path: str,
        risk_prompt_path: str,
        news_prompt_path: str,
        arbitrator_prompt_path: str,
    ) -> None:
        config = AdvisorConfig(
            type="moe",
            aggregator="arbitrator",
            arbitrator=ArbitratorConfig(
                provider="ollama",
                model="phi4:14b-q8_0",
                prompt_file=arbitrator_prompt_path,
            ),
            experts=[
                ExpertConfig(
                    name="q",
                    provider="ollama",
                    model="phi4:14b",
                    role="quant",
                    prompt_file=quant_prompt_path,
                ),
                ExpertConfig(
                    name="r",
                    provider="ollama",
                    model="qwen3:8b",
                    role="risk",
                    prompt_file=risk_prompt_path,
                ),
                ExpertConfig(
                    name="n",
                    provider="ollama",
                    model="deepseek-r1:8b",
                    role="news",
                    prompt_file=news_prompt_path,
                ),
            ],
        )
        out: list[str] = []
        advisor = _build_advisor(config, out)
        assert isinstance(advisor, MoEAdvisorAdapter)
        assert "arb=phi4:14b-q8_0" in out[0]

    def test_anthropic_without_cloud_wiring_rejected(self, quant_prompt_path: str) -> None:
        """Stage 6.2: anthropic provider needs an `llm:` block. Without
        cloud_wiring threaded through, _build_advisor raises clearly."""
        config = AdvisorConfig(
            type="single",
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_file=quant_prompt_path,
            inference_params=InferenceParams(),
        )
        with pytest.raises(ValueError, match="no `llm:` block"):
            _build_advisor(config, [])

    def test_unimplemented_cloud_provider_rejected(self, quant_prompt_path: str) -> None:
        """openai / google adapters land in Stages 6.3 / 6.4 — until then
        the dispatcher raises 'not implemented' at construction time."""
        config = AdvisorConfig(
            type="single",
            provider="openai",
            model="gpt-4o",
            prompt_file=quant_prompt_path,
            inference_params=InferenceParams(),
        )
        with pytest.raises(ValueError, match="not implemented"):
            _build_advisor(config, [])

    def test_moe_label_format(self, quant_prompt_path: str, risk_prompt_path: str) -> None:
        """The compact model_name label is operator-readable and machine-grep-able."""
        config = AdvisorConfig(
            type="moe",
            aggregator="weighted_confidence",
            experts=[
                ExpertConfig(
                    name="q",
                    provider="ollama",
                    model="m1",
                    role="quant",
                    prompt_file=quant_prompt_path,
                ),
                ExpertConfig(
                    name="r",
                    provider="ollama",
                    model="m2",
                    role="risk",
                    prompt_file=risk_prompt_path,
                ),
                ExpertConfig(
                    name="n",
                    provider="ollama",
                    model="m3",
                    role="news",
                    prompt_file="config/prompts/news.md",
                ),
            ],
        )
        label = _moe_model_label(config)
        assert label == "moe[weighted_confidence:quant:m1/risk:m2/news:m3]"


@pytest.mark.asyncio
class TestMoEExpertOpinionsRoundTrip:
    async def test_expert_opinions_persist_through_cycle(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """When the advisor is a MoE-style adapter (or any AdvisorPort
        returning a recommendation with populated ``expert_opinions``),
        ``_run_cycle`` must persist those opinions so ``tools/show_suggestions``
        and downstream consumers see the per-expert audit trail."""
        await _seed_prices(storage)
        expert_opinions = [
            AdvisorRecommendation(
                recommendation_id="op-quant",
                timestamp=Timestamp(dt=datetime.now(UTC)),
                role="quant",
                recommendations={"spacing_percentage": 1.2},
                rationale="vol spiking",
                confidence="high",
            ),
            AdvisorRecommendation(
                recommendation_id="op-risk",
                timestamp=Timestamp(dt=datetime.now(UTC)),
                role="risk",
                recommendations={"spacing_percentage": 1.5},
                rationale="drawdown widening",
                confidence="medium",
            ),
        ]
        aggregated = AdvisorRecommendation(
            recommendation_id="rec-aggregated",
            timestamp=Timestamp(dt=datetime.now(UTC)),
            role="aggregated",
            recommendations={"spacing_percentage": 1.35},
            rationale="MoE consensus",
            confidence="medium",
            expert_opinions=expert_opinions,
        )
        advisor = _CannedAdvisor(recommendation=aggregated)
        builder = SummaryBuilder(storage)

        ok = await _run_cycle(
            advisor,
            builder,
            storage,
            symbol=BTC_USD,
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grid=_default_grid(),
            model_name="moe[voting:quant:phi4/risk:qwen3]",
        )
        assert ok is True

        persisted = (await storage.get_advisor_suggestions())[0]
        assert persisted.recommendation.role == "aggregated"
        assert persisted.model_name == "moe[voting:quant:phi4/risk:qwen3]"
        assert len(persisted.recommendation.expert_opinions) == 2
        by_role = {op.role: op for op in persisted.recommendation.expert_opinions}
        assert by_role["quant"].confidence == "high"
        assert by_role["risk"].rationale == "drawdown widening"


@pytest.mark.asyncio
class TestMultiSymbolSweep:
    """Stage 3.6b: cli/advise iterates serial per symbol within each tick.
    Each LLM call sees one coin's PerformanceSummary — no cross-
    contamination of opinions. Per-symbol cycle errors are swallowed at
    the daemon layer (matching cli/live's Stage 2.4 discipline)."""

    async def test_sweep_visits_every_symbol(self, storage: SQLiteStorageAdapter) -> None:
        """One sweep should produce one AdvisorSuggestion per configured
        symbol — and each suggestion's input_summary should carry its
        own symbol tag (proves per-symbol isolation)."""
        eth_usd = Symbol(base="ETH", quote="USD")
        doge_usd = Symbol(base="DOGE", quote="USD")
        symbols = [BTC_USD, eth_usd, doge_usd]
        # Seed a few snapshots so SummaryBuilder doesn't degenerate to
        # snapshot_count=0 — not strictly required but matches reality.
        await _seed_prices(storage)

        advisor = _CannedAdvisor(recommendation=_make_recommendation())
        builder = SummaryBuilder(storage)
        current_grids = {s.base: _default_grid() for s in symbols}

        # Stop event fires after the first tick — single sweep, no
        # second sweep, no infinite loop in the test.
        import asyncio as _asyncio

        stop_event = _asyncio.Event()

        async def _stop_after_one_sweep() -> None:
            # Wait long enough for the sweep across 3 symbols to complete
            # (3× ~5ms per cycle with the canned advisor); then signal stop.
            await _asyncio.sleep(0.5)
            stop_event.set()

        stopper = _asyncio.create_task(_stop_after_one_sweep())
        rc = await _run_loop(
            advisor=advisor,
            summary_builder=builder,
            advise_storage=storage,
            symbols=symbols,
            interval=timedelta(seconds=60),  # cadence; doesn't matter — we stop early
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grids=current_grids,
            model_name="canned",
            stop_event=stop_event,
        )
        await stopper
        assert rc == 0

        # The advisor was invoked once per symbol — 3 calls total in one sweep.
        # (May have more if the sweep finished before stop and rolled into a
        # second iteration; we just assert the minimum.)
        assert advisor.call_count >= 3

        # Persisted suggestions should cover every symbol from the sweep.
        # Use input_summary.symbol to verify per-symbol isolation in the
        # builder's PerformanceSummary.
        suggestions = await storage.get_advisor_suggestions()
        symbols_seen = {s.input_summary["symbol"] for s in suggestions}
        assert "BTC/USD" in symbols_seen
        assert "ETH/USD" in symbols_seen
        assert "DOGE/USD" in symbols_seen

    async def test_per_symbol_error_does_not_kill_sweep(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """One bad coin (advisor raising) must not stop the remaining
        symbols from running this tick. Matches cli/live's Stage 2.4
        per-symbol error-swallow discipline."""
        eth_usd = Symbol(base="ETH", quote="USD")
        symbols = [BTC_USD, eth_usd]
        await _seed_prices(storage)

        class _FlakeyAdvisor(AdvisorPort):
            """Raises on BTC, succeeds on everything else."""

            def __init__(self) -> None:
                self.call_count = 0

            async def get_recommendation(
                self, summary: PerformanceSummary
            ) -> AdvisorRecommendation:
                self.call_count += 1
                if summary.symbol == "BTC/USD":
                    raise AdvisorError("simulated BTC outage")
                return _make_recommendation()

            async def validate_recommendation(self, recommendation: AdvisorRecommendation) -> bool:
                del recommendation
                return True

        advisor = _FlakeyAdvisor()
        builder = SummaryBuilder(storage)
        current_grids = {s.base: _default_grid() for s in symbols}

        import asyncio as _asyncio

        stop_event = _asyncio.Event()

        async def _stop_after_one_sweep() -> None:
            await _asyncio.sleep(0.5)
            stop_event.set()

        stopper = _asyncio.create_task(_stop_after_one_sweep())
        await _run_loop(
            advisor=advisor,
            summary_builder=builder,
            advise_storage=storage,
            symbols=symbols,
            interval=timedelta(seconds=60),
            metrics_lookback=timedelta(hours=1),
            news_lookback=None,
            news_limit=20,
            news_match_coin=False,
            current_grids=current_grids,
            model_name="canned",
            stop_event=stop_event,
        )
        await stopper

        # Both symbols got tried; BTC raised but ETH still produced a
        # suggestion. The ETH suggestion must be persisted.
        assert advisor.call_count >= 2
        suggestions = await storage.get_advisor_suggestions()
        eth_suggestions = [s for s in suggestions if s.input_summary["symbol"] == "ETH/USD"]
        assert eth_suggestions, "ETH suggestion must persist despite BTC failure"
