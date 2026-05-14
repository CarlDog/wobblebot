"""KrakenConfig — credentials and connection parameters for the Kraken adapter.

Credentials are sourced from environment variables in production
(``KrakenConfig.from_env``) so they never land in a committed file.
The Phase 2 read-only path uses a Kraken API key with only the
"Query Funds" / "Query Open Orders & Trades" permissions — no trading,
no withdrawals. Phase 4's Harvester gets a separate key with withdraw
permission (per ADR-003).
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

_DEFAULT_BASE_URL = "https://api.kraken.com"
_DEFAULT_TIMEOUT = 10.0
_KEY_ENV = "KRAKEN_API_KEY"
_SECRET_ENV = "KRAKEN_API_SECRET"
_BASE_URL_ENV = "KRAKEN_BASE_URL"


class KrakenConfig(BaseModel):
    """Kraken adapter configuration.

    Args:
        api_key: Kraken API key string (the public half).
        api_secret: Base64-encoded private key Kraken hands you on key
            creation. Pass it through verbatim — the signing routine
            base64-decodes it as the HMAC key.
        base_url: API base URL. Override for staging or a mock server.
        request_timeout_seconds: Per-request HTTP timeout.
    """

    api_key: str
    api_secret: str
    base_url: str = _DEFAULT_BASE_URL
    request_timeout_seconds: float = Field(default=_DEFAULT_TIMEOUT, gt=0)

    class Config:
        frozen = True

    @classmethod
    def from_env(cls) -> KrakenConfig:
        """Load credentials from environment variables.

        Required: ``KRAKEN_API_KEY``, ``KRAKEN_API_SECRET``.
        Optional: ``KRAKEN_BASE_URL`` (overrides default; useful for
        pointing tests at a local mock).

        Raises:
            ValueError: If either credential env var is unset or empty.
        """
        api_key = os.environ.get(_KEY_ENV)
        api_secret = os.environ.get(_SECRET_ENV)
        missing = [
            name for name, val in ((_KEY_ENV, api_key), (_SECRET_ENV, api_secret)) if not val
        ]
        if missing:
            raise ValueError(f"Missing required Kraken credential env vars: {', '.join(missing)}")
        # mypy: the comprehension above proves api_key/api_secret are truthy here.
        assert api_key is not None
        assert api_secret is not None
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            base_url=os.environ.get(_BASE_URL_ENV, _DEFAULT_BASE_URL),
        )
