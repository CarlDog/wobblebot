class TestSymbolPriorityConfig:
    """`screener` ordering needs OHLC bars, so it needs observe_db. Caught
    at config load with a message naming the fix, rather than degrading
    silently at runtime — an operator who selected screener ordering and
    quietly got the biased order back would have no way to notice."""

    def test_default_is_the_historical_behaviour(self) -> None:
        from wobblebot.config.cli import LiveConfig

        cfg = LiveConfig(symbols=["BTC/USD"])
        assert cfg.symbol_priority == "config_order"
        assert cfg.observe_db is None

    def test_screener_without_observe_db_is_rejected(self) -> None:
        import pytest as _pytest

        from wobblebot.config.cli import LiveConfig

        with _pytest.raises(ValueError, match="requires live.observe_db"):
            LiveConfig(symbols=["BTC/USD"], symbol_priority="screener")

    def test_screener_with_observe_db_is_accepted(self) -> None:
        from wobblebot.config.cli import LiveConfig

        cfg = LiveConfig(
            symbols=["BTC/USD"], symbol_priority="screener", observe_db="data/observe.db"
        )
        assert cfg.symbol_priority == "screener"

    def test_round_robin_needs_no_observe_db(self) -> None:
        from wobblebot.config.cli import LiveConfig

        assert LiveConfig(symbols=["BTC/USD"], symbol_priority="round_robin").observe_db is None

    def test_unknown_strategy_is_rejected(self) -> None:
        import pytest as _pytest

        from wobblebot.config.cli import LiveConfig

        with _pytest.raises(ValueError):
            LiveConfig(symbols=["BTC/USD"], symbol_priority="shuffle")
