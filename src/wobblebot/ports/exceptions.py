"""Exception hierarchy for adapter-layer failures.

Every port has a single error type that adapters raise on
protocol/transport failures (network error, DB unreachable, malformed
upstream response, etc). All inherit from ``WobbleBotPortError`` so
service-layer code can catch any port failure uniformly.

These live in the ``ports`` package rather than ``domain.exceptions``
because they represent infrastructure failures, not business-rule
violations.

Convention enforced via docstrings on each port:

- **Domain-data miss returns ``None``** (e.g. "no order with that id",
  "asset never held"). The caller decides how to interpret absence.
- **Protocol or transport failure raises** the port's error type. The
  caller distinguishes transient/retriable failures from terminal
  ones via the cause chain (``raise ... from exc``).
"""


class WobbleBotPortError(Exception):
    """Base class for all adapter-layer failures.

    Catch this when service code needs to handle any port failure
    uniformly (e.g. retry logic, structured logging).
    """


class ExchangeError(WobbleBotPortError):
    """Raised when an ``ExchangePort`` operation fails.

    Examples: Kraken returns 5xx, request times out, response is
    malformed. Insufficient-funds responses are *not* this error —
    they raise ``InsufficientBalance`` (a domain exception) instead.
    """


class StorageError(WobbleBotPortError):
    """Raised when a ``StoragePort`` operation fails.

    Examples: database unreachable, write violates a constraint that
    the domain layer cannot prevent, partial-write rollback fired.
    """


class AdvisorError(WobbleBotPortError):
    """Raised when an ``AdvisorPort`` operation fails.

    Examples: LLM backend unreachable, output fails JSON-schema
    validation, recommendation violates configured safety bounds.
    """


class HarvesterError(WobbleBotPortError):
    """Raised when a harvester operation fails.

    Examples: Kraken withdrawal endpoint rejects the request, bank
    address book lookup fails, safety-cap validation fails.
    """


class NotifierError(WobbleBotPortError):
    """Raised when a ``NotifierPort`` operation fails.

    Examples: Slack webhook returns non-2xx, email gateway times out.
    """


class DataCollectorError(WobbleBotPortError):
    """Raised when a ``DataCollectorPort`` operation fails.

    Examples: upstream price feed unreachable, derived metric
    calculation fails, cache stampede.
    """


class NewsError(WobbleBotPortError):
    """Raised when a ``NewsPort`` operation fails.

    Examples: RSS feed unreachable, news API returns 5xx, response
    body is malformed XML/JSON, the feed exists but is empty in a way
    that suggests an upstream change rather than legitimate quiet.
    """


class OperatorError(WobbleBotPortError):
    """Raised when an ``OperatorPort`` operation fails.

    Examples: dispatching an approved command fails because the engine
    refused (symbol unknown, already paused, cancel failed at the
    exchange); answering a query fails because a backing storage call
    raises. Per ADR-013 / stage-5.1-design.md the port returns typed
    ``CommandResult`` / ``QueryResult`` for domain misses — exceptions
    are reserved for protocol / transport / unrecoverable conditions.
    """


class AssistantError(WobbleBotPortError):
    """Raised when an ``AssistantPort`` operation fails.

    Examples: LLM backend unreachable, output fails ``OperatorIntent``
    schema validation, model returns an empty or malformed response.
    Per ADR-013 the conversational LLM is NOT in the money path — an
    ``AssistantError`` only affects the Discord chat surface; engine
    code in ``cli/live`` cannot observe it. ``cli/operator`` catches
    this, logs structurally, and posts a graceful "I couldn't parse
    that" reply rather than dropping the message.
    """
