"""
Ports layer - Abstract interfaces defining contracts.

This layer defines abstract ports (interfaces) that domain and application layers
depend on. Adapters implement these ports to provide concrete functionality.
"""

from wobblebot.ports.advisor import (
    AdvisorPort,
    AdvisorRecommendation,
    AdvisorSuggestion,
    AppliedSuggestion,
    ConfidenceLevel,
    CurrentGridParams,
    NewsItemSummary,
    PerformanceSummary,
)
from wobblebot.ports.assistant import (
    AssistantPort,
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
    SymbolStateSnapshot,
)
from wobblebot.ports.data_collector import DataCollectorPort, MarketSnapshot
from wobblebot.ports.exceptions import (
    AdvisorError,
    AssistantError,
    DataCollectorError,
    ExchangeError,
    HarvesterError,
    NewsError,
    NotifierError,
    OperatorError,
    StorageError,
    WobbleBotPortError,
)
from wobblebot.ports.exchange import ExchangePort
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.news import NewsPort
from wobblebot.ports.notifier import Notification, NotifierPort
from wobblebot.ports.operator import (
    CancelOpenOrdersCommand,
    CommandResult,
    FillEntry,
    GridConfigQuery,
    GridConfigResult,
    HarvesterStatusQuery,
    HarvesterStatusResult,
    HelpEntry,
    HelpQuery,
    HelpResult,
    IntentCommand,
    IntentConversational,
    IntentQuery,
    IntentUnparseable,
    NewsEntry,
    OpenOrderEntry,
    OpenOrdersQuery,
    OpenOrdersResult,
    OperatorCommand,
    OperatorIntent,
    OperatorPort,
    OperatorQuery,
    PauseAllCommand,
    PauseCommand,
    PendingCommand,
    PendingCommandStatus,
    ProposalEntry,
    QueryResult,
    RecentFillsQuery,
    RecentFillsResult,
    RecentNewsQuery,
    RecentNewsResult,
    RecentProposalsQuery,
    RecentProposalsResult,
    RecentSuggestionsQuery,
    RecentSuggestionsResult,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StatusResult,
    StopCommand,
    SuggestionEntry,
    SymbolStatusEntry,
)
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
    "AppliedSuggestion",
    "ConfidenceLevel",
    # Phase 4+ ports
    "TransferProposal",
    "TransferResult",
    # Phase 5 — operator interaction (ADR-013)
    "OperatorPort",
    "AssistantPort",
    "OperatorIntent",
    "OperatorCommand",
    "OperatorQuery",
    "QueryResult",
    "CommandResult",
    "PendingCommand",
    "PendingCommandStatus",
    "ConversationTurn",
    "ConversationContext",
    "EngineStateSnapshot",
    "SymbolStateSnapshot",
    # Intent variants
    "IntentCommand",
    "IntentQuery",
    "IntentConversational",
    "IntentUnparseable",
    # Command variants
    "PauseCommand",
    "ResumeCommand",
    "PauseAllCommand",
    "ResumeAllCommand",
    "CancelOpenOrdersCommand",
    "StopCommand",
    # Query variants
    "StatusQuery",
    "OpenOrdersQuery",
    "RecentFillsQuery",
    "RecentSuggestionsQuery",
    "RecentNewsQuery",
    "HarvesterStatusQuery",
    "RecentProposalsQuery",
    "GridConfigQuery",
    "HelpQuery",
    # Result variants
    "StatusResult",
    "OpenOrdersResult",
    "RecentFillsResult",
    "RecentSuggestionsResult",
    "RecentNewsResult",
    "HarvesterStatusResult",
    "RecentProposalsResult",
    "GridConfigResult",
    "HelpResult",
    # Result-row helpers
    "SymbolStatusEntry",
    "OpenOrderEntry",
    "FillEntry",
    "SuggestionEntry",
    "NewsEntry",
    "ProposalEntry",
    "HelpEntry",
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
    "OperatorError",
    "AssistantError",
]
