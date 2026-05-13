"""
Ports layer - Abstract interfaces defining contracts.

This layer defines abstract ports (interfaces) that domain and application layers
depend on. Adapters implement these ports to provide concrete functionality.
"""

from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    PerformanceSummary,
)
from wobblebot.ports.data_collector import DataCollectorPort, MarketSnapshot
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.harvester import HarvesterPort, TransferProposal, TransferResult
from wobblebot.ports.notifier import Notification, NotifierPort
from wobblebot.ports.storage import StorageError, StoragePort

__all__ = [
    # Core ports
    "ExchangePort",
    "StoragePort",
    "StorageError",
    "DataCollectorPort",
    # Phase 3+ ports
    "AdvisorPort",
    "PerformanceSummary",
    "AdvisorRecommendation",
    # Phase 4+ ports
    "HarvesterPort",
    "TransferProposal",
    "TransferResult",
    # Future ports
    "NotifierPort",
    "Notification",
    # Data Collector types
    "MarketSnapshot",
]
