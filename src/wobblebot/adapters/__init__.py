"""
Adapters layer - Concrete implementations of ports.

This layer contains concrete implementations of ports including:
- Exchange adapters (Kraken API, mock exchange)
- Storage adapters (SQLite)
- LLM adapters (Strategy Advisor)
- Notification adapters

Note: Harvester uses ExchangePort for withdrawals (ADR-004); there is no
separate banking adapter.
"""

from wobblebot.adapters.cryptocompare_news import CryptoCompareAdapter
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.ollama import OllamaAdapter
from wobblebot.adapters.rss_news import RssNewsAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter

__all__ = [
    "CryptoCompareAdapter",
    "MockExchangeAdapter",
    "OllamaAdapter",
    "RssNewsAdapter",
    "SQLiteStorageAdapter",
]
