"""
Smoke tests - verify basic package structure and imports.

These tests ensure the package is properly installed and basic imports work.
"""

import pytest


def test_wobblebot_import() -> None:
    """Test that the main wobblebot package can be imported."""
    import wobblebot

    assert wobblebot.__version__ == "0.1.0"
    assert wobblebot.__author__ == "WobbleBot Team"


def test_domain_import() -> None:
    """Test that the domain layer can be imported."""
    import wobblebot.domain  # noqa: F401


def test_ports_import() -> None:
    """Test that the ports layer can be imported."""
    import wobblebot.ports  # noqa: F401


def test_adapters_import() -> None:
    """Test that the adapters layer can be imported."""
    import wobblebot.adapters  # noqa: F401


def test_services_import() -> None:
    """Test that the services layer can be imported."""
    import wobblebot.services  # noqa: F401


def test_cli_import() -> None:
    """Test that the CLI layer can be imported."""
    import wobblebot.cli  # noqa: F401


def test_config_import() -> None:
    """Test that the config layer can be imported."""
    import wobblebot.config  # noqa: F401


@pytest.mark.unit
def test_package_structure() -> None:
    """Verify expected package structure exists."""
    import wobblebot.adapters
    import wobblebot.cli
    import wobblebot.config
    import wobblebot.domain
    import wobblebot.ports
    import wobblebot.services

    # All layers should have __all__ defined (even if empty)
    assert hasattr(wobblebot.domain, "__all__")
    assert hasattr(wobblebot.ports, "__all__")
    assert hasattr(wobblebot.adapters, "__all__")
    assert hasattr(wobblebot.services, "__all__")
    assert hasattr(wobblebot.cli, "__all__")
    assert hasattr(wobblebot.config, "__all__")
