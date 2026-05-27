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
_KEY_ENV = "KRAKEN_READER_API_KEY"
_SECRET_ENV = "KRAKEN_READER_API_SECRET"
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
    def from_env(
        cls,
        key_var: str = _KEY_ENV,
        secret_var: str = _SECRET_ENV,
        base_url_var: str = _BASE_URL_ENV,
    ) -> KrakenConfig:
        """Load credentials from environment variables.

        Default vars: ``KRAKEN_READER_API_KEY``, ``KRAKEN_READER_API_SECRET``,
        ``KRAKEN_BASE_URL``. Per ADR-003-style separation, callers can
        load a *different* key (e.g. the Stage 2.3 trading key) by
        passing alternate var names — this lets the project keep
        multiple Kraken keys in ``.env`` without overwriting each
        other.

        Args:
            key_var: Env var holding the public API key. Default
                ``KRAKEN_READER_API_KEY``.
            secret_var: Env var holding the base64-encoded API secret.
                Default ``KRAKEN_READER_API_SECRET``.
            base_url_var: Env var holding the API base URL. Default
                ``KRAKEN_BASE_URL`` (falls back to the public Kraken
                endpoint when unset).

        Raises:
            ValueError: If either credential env var is unset or empty.
        """
        api_key = os.environ.get(key_var)
        api_secret = os.environ.get(secret_var)
        missing = [name for name, val in ((key_var, api_key), (secret_var, api_secret)) if not val]
        if missing:
            raise ValueError(f"Missing required Kraken credential env vars: {', '.join(missing)}")
        # mypy: the comprehension above proves api_key/api_secret are truthy here.
        assert api_key is not None
        assert api_secret is not None
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            base_url=os.environ.get(base_url_var, _DEFAULT_BASE_URL),
        )
