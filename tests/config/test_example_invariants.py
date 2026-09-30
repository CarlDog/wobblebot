"""Value-invariant guard for ``config/settings.example.yml``.

The key-only schema-drift test compares *keys*, never *values* — so a stale
example value that contradicts a code constant (the kind of drift that bit us
2026-06-04) slips through. These tests guard code invariants and the documented
starting response budget for the example's thinking advisor. They do NOT enforce
value-equality generally: most example values are template choices an operator
is expected to change.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wobblebot.adapters.ollama import is_thinking_model
from wobblebot.config.grid import KRAKEN_MAKER_FEE_RATE, KRAKEN_TAKER_FEE_RATE
from wobblebot.config.loader import load_config

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.example.yml"


def _load() -> dict:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_default_spacing_clears_fee_floor() -> None:
    """``grid.default.spacing_percentage`` must exceed 2x the maker fee.

    The ``GridConfig`` validator rejects sub-floor spacing at load time; this
    pins the *example's* documented default above the floor so the shipped
    template can never demonstrate a money-losing grid.
    """
    raw = _load()
    spacing = Decimal(str(raw["grid"]["default"]["spacing_percentage"]))
    floor = KRAKEN_MAKER_FEE_RATE * Decimal("2") * Decimal("100")
    assert spacing > floor, (
        f"example grid.default.spacing_percentage={spacing}% is not above the "
        f"fee floor {floor}% (2 x maker {KRAKEN_MAKER_FEE_RATE * 100}%)"
    )


def test_shadow_fee_rates_match_code_constants() -> None:
    """``shadow.{maker,taker}_fee_rate`` document Kraken's base-tier rates and
    must match the ``KRAKEN_*_FEE_RATE`` constants the engine/validator use —
    ``grid.py`` explicitly says to update both together."""
    shadow = _load()["shadow"]
    assert Decimal(str(shadow["maker_fee_rate"])) == KRAKEN_MAKER_FEE_RATE
    assert Decimal(str(shadow["taker_fee_rate"])) == KRAKEN_TAKER_FEE_RATE


@pytest.mark.parametrize(("field", "minimum"), [("max_tokens", 2048), ("timeout_seconds", 180)])
def test_copied_thinking_advisor_has_starting_response_budget(
    tmp_path: Path, field: str, minimum: int
) -> None:
    """A fresh copy must allow the documented reasoning + final JSON budget.

    This is the InferenceParams starting floor, not a model-completion or host
    latency guarantee; operators still need complete-response measurements.
    """
    copied_config = tmp_path / "settings.yml"
    copied_config.write_bytes(_EXAMPLE.read_bytes())
    advisor = load_config(copied_config).advisor
    assert advisor is not None
    if advisor.provider == "ollama" and is_thinking_model(advisor.model):
        actual = getattr(advisor.inference_params, field)
        assert actual >= minimum, (
            f"copied example thinking advisor {advisor.model}: {field}={actual} "
            f"is below the documented starting floor {minimum}"
        )
