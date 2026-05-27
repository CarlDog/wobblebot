"""
Pytest configuration and shared fixtures.

This module provides shared test fixtures, hooks, and configuration
used across the entire test suite.
"""

import pytest
from dotenv import load_dotenv

# Load .env once at session start so live integration tests can read
# KRAKEN_READER_API_KEY / KRAKEN_READER_API_SECRET. Idempotent — no-op if .env is
# absent. Unit tests use ``monkeypatch.setenv/delenv`` and remain
# isolated from whatever .env happens to set.
load_dotenv()


@pytest.fixture
def sample_fixture() -> str:
    """
    Example fixture - replace or remove as needed.

    Returns:
        A sample string for testing.
    """
    return "sample"


# Add more shared fixtures here as the project grows
