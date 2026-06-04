"""Valid-pairs guard for ``config/settings.example.yml``.

The key-only schema-drift test (``test_schema_drift.py``) cannot see a
retired/dead trading pair, because ``grid.coins.*`` is an exempt subtree and
symbol *values* aren't compared at all. This is exactly how a dead ``MATIC/USD``
(Polygon migrated to POL in 2024) lingered in the committed example across
several commits. This test asserts every Kraken pair the example references is
in a maintained set of currently-valid pairs.

The live ``/0/public/AssetPairs`` endpoint is the source of truth;
``tests/integration/test_kraken_api_health.py`` checks against it on the
quarterly audit. This CI-safe unit test (no network) is the gate that stops a
retired pair silently re-entering the committed example.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.example.yml"

# Maintained snapshot of valid Kraken spot USD pairs the example may reference.
# Update this set IN THE SAME COMMIT when you add a new pair to the example.
KNOWN_KRAKEN_USD_PAIRS: frozenset[str] = frozenset(
    {
        "BTC/USD",
        "ETH/USD",
        "SOL/USD",
        "XRP/USD",
        "DOGE/USD",
        "ADA/USD",
        "AVAX/USD",
        "LINK/USD",
        "DOT/USD",
        "POL/USD",
        "LTC/USD",
        "BCH/USD",
    }
)


def _collect_symbols(node: object) -> set[str]:
    """Gather every pair under a ``symbols`` list or a scalar ``symbol`` key,
    recursively, anywhere in the config tree (covers profiles too)."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "symbols" and isinstance(value, list):
                found |= {s for s in value if isinstance(s, str)}
            elif key == "symbol" and isinstance(value, str):
                found.add(value)
            else:
                found |= _collect_symbols(value)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_symbols(item)
    return found


def _collect_grid_coin_pairs(raw: object) -> set[str]:
    """``grid.coins`` keys are bare bases (``DOGE``); map to ``DOGE/USD``."""
    if not isinstance(raw, dict):
        return set()
    grid = raw.get("grid")
    if not isinstance(grid, dict):
        return set()
    coins = grid.get("coins")
    if not isinstance(coins, dict):
        return set()
    return {f"{base}/USD" for base in coins}


def test_example_symbols_are_known_pairs() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    referenced = _collect_symbols(raw) | _collect_grid_coin_pairs(raw)
    unknown = referenced - KNOWN_KRAKEN_USD_PAIRS
    assert not unknown, (
        f"settings.example.yml references pairs not in KNOWN_KRAKEN_USD_PAIRS: "
        f"{sorted(unknown)}. If a pair is valid + current, add it to the fixture; "
        f"if retired (e.g. MATIC->POL), remove it from the example."
    )


def test_known_pairs_fixture_is_non_empty() -> None:
    # Guard against an accidental wipe that would make the check vacuous.
    assert KNOWN_KRAKEN_USD_PAIRS
