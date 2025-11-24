"""
Pytest configuration and shared fixtures.

This module provides shared test fixtures, hooks, and configuration
used across the entire test suite.
"""

import pytest


@pytest.fixture
def sample_fixture() -> str:
    """
    Example fixture - replace or remove as needed.

    Returns:
        A sample string for testing.
    """
    return "sample"


# Add more shared fixtures here as the project grows
