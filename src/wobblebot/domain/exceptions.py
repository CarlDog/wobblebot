"""Domain-specific exceptions for WobbleBot.

These exceptions represent business rule violations and should be raised
by domain models when invariants are broken.
"""

from decimal import Decimal


class WobbleBotDomainError(Exception):
    """Base exception for all domain errors."""

    pass


class ExposureLimitExceeded(WobbleBotDomainError):
    """Raised when an operation would exceed configured exposure limits."""

    def __init__(self, current: Decimal, limit: Decimal, message: str | None = None):
        self.current = current
        self.limit = limit
        default_msg = f"Exposure limit exceeded: {current} > {limit}"
        super().__init__(message or default_msg)


class DailySpendCapExceeded(WobbleBotDomainError):
    """Raised when daily spend cap is exceeded."""

    def __init__(self, spent_today: Decimal, cap: Decimal, message: str | None = None):
        self.spent_today = spent_today
        self.cap = cap
        default_msg = f"Daily spend cap exceeded: {spent_today} > {cap}"
        super().__init__(message or default_msg)


class InvalidOrderState(WobbleBotDomainError):
    """Raised when attempting an invalid state transition for an order."""

    def __init__(self, current_state: str, attempted_transition: str):
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        msg = f"Invalid transition from '{current_state}' via '{attempted_transition}'"
        super().__init__(msg)


class InvalidGridConfiguration(WobbleBotDomainError):
    """Raised when grid parameters are invalid or inconsistent."""

    pass


class InsufficientBalance(WobbleBotDomainError):
    """Raised when attempting an operation with insufficient funds."""

    def __init__(self, required: Decimal, available: Decimal, asset: str):
        self.required = required
        self.available = available
        self.asset = asset
        msg = f"Insufficient {asset}: required {required}, available {available}"
        super().__init__(msg)


class InvalidPriceRange(WobbleBotDomainError):
    """Raised when price range is invalid (e.g., min > max)."""

    pass


class InvalidAmount(WobbleBotDomainError):
    """Raised when amount validation fails."""

    pass


class LLMRetryExhausted(WobbleBotDomainError):
    """Raised when an LLM call exhausts its retry budget per ADR-015.

    Carries the attempt count + the last exception so callers can
    surface both to the operator notification path.

    Attributes:
        attempts: Total attempts made (initial + retries).
        last_error: The exception from the final attempt.
    """

    def __init__(self, attempts: int, last_error: Exception, message: str | None = None):
        self.attempts = attempts
        self.last_error = last_error
        default_msg = f"LLM call failed after {attempts} attempts; last error: {last_error}"
        super().__init__(message or default_msg)


class LLMCostCapExceeded(WobbleBotDomainError):
    """Raised when a cloud-LLM call would exceed an ADR-014 cost cap.

    The exception carries the budget state so the caller's
    notification path can render a self-explanatory message without
    re-querying.

    Attributes:
        cap_kind: Which cap tripped — ``daily`` or ``session``.
        cap_value_usd: The configured cap.
        daily_spent_usd: 24-hour sliding-window total at check time.
        session_spent_usd: Running session total at check time.
    """

    def __init__(
        self,
        cap_kind: str,
        cap_value_usd: Decimal,
        daily_spent_usd: Decimal,
        session_spent_usd: Decimal,
        message: str | None = None,
    ):
        self.cap_kind = cap_kind
        self.cap_value_usd = cap_value_usd
        self.daily_spent_usd = daily_spent_usd
        self.session_spent_usd = session_spent_usd
        default_msg = (
            f"LLM {cap_kind} cap ${cap_value_usd} exceeded "
            f"(daily=${daily_spent_usd}, session=${session_spent_usd})"
        )
        super().__init__(message or default_msg)
