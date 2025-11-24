"""Domain-specific exceptions for WobbleBot.

These exceptions represent business rule violations and should be raised
by domain models when invariants are broken.
"""


class WobbleBotDomainError(Exception):
    """Base exception for all domain errors."""

    pass


class ExposureLimitExceeded(WobbleBotDomainError):
    """Raised when an operation would exceed configured exposure limits."""

    def __init__(self, current: float, limit: float, message: str | None = None):
        self.current = current
        self.limit = limit
        default_msg = f"Exposure limit exceeded: {current} > {limit}"
        super().__init__(message or default_msg)


class DailySpendCapExceeded(WobbleBotDomainError):
    """Raised when daily spend cap is exceeded."""

    def __init__(self, spent_today: float, cap: float, message: str | None = None):
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

    def __init__(self, required: float, available: float, asset: str):
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
