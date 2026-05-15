"""
Ports layer - Abstract interfaces defining contracts.

This layer defines abstract ports (interfaces) that domain and application layers
depend on. Adapters implement these ports to provide concrete functionality.
"""

from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    AdvisorSuggestion,
    ConfidenceLevel,
    CurrentGridParams,
    NewsItemSummary,
    PerformanceSummary,
)
from wobblebot.ports.data_collector import DataCollectorPort, MarketSnapshot
from wobblebot.ports.exceptions import (
    AdvisorError,
    DataCollectorError,
    ExchangeError,
    HarvesterError,
    NewsError,
    NotifierError,
    StorageError,
    WobbleBotPortError,
)
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.harvester import HarvesterPort, TransferProposal, TransferResult
from wobblebot.ports.news import NewsPort
from wobblebot.ports.notifier import Notification, NotifierPort
from wobblebot.ports.storage import StoragePort

__all__ = [
    # Core ports
    "ExchangePort",
    "StoragePort",
    "DataCollectorPort",
    # Phase 3+ ports
    "AdvisorPort",
    "PerformanceSummary",
    "CurrentGridParams",
    "NewsItemSummary",
    "AdvisorRecommendation",
    "AdvisorSuggestion",
    "ConfidenceLevel",
    # Phase 4+ ports
    "HarvesterPort",
    "TransferProposal",
    "TransferResult",
    # Future ports
    "NotifierPort",
    "Notification",
    # Phase 3.2.5 — news ingestion
    "NewsPort",
    # Data Collector types
    "MarketSnapshot",
    # Port-layer exceptions
    "WobbleBotPortError",
    "ExchangeError",
    "StorageError",
    "AdvisorError",
    "HarvesterError",
    "NotifierError",
    "DataCollectorError",
    "NewsError",
]
