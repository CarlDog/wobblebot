"""Claude 5 rejects ``temperature`` — pin the generation boundary.

Anthropic deprecated the ``temperature`` field starting with the Claude
5 generation. Sending it is a **400 on every call**, so this is not a
quality knob but an availability bug: before this guard, configuring
any Claude 5 model in ``settings.yml`` failed 100% of advisor ticks.
Measured live 2026-08-10 — ``claude-sonnet-5`` and ``claude-opus-5``
both returned ``invalid_request_error: `temperature` is deprecated for
this model``, while ``claude-haiku-4-5`` returned 200.

The boundary is the MAJOR generation, which is why this file exists:
``claude-haiku-4-5`` contains a "5" but is generation 4 and still takes
temperature. A substring check would silently strip the field from
every 4.5-tier model.
"""

from __future__ import annotations

import pytest

from wobblebot.adapters.anthropic import supports_temperature

pytestmark = pytest.mark.unit


class TestGenerationBoundary:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-20250514",
        ],
    )
    def test_generation_4_and_below_keeps_temperature(self, model: str) -> None:
        assert supports_temperature(model) is True

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-5",
            "claude-fable-5",
            "claude-mythos-5",
            "claude-sonnet-5-1",
            "claude-opus-6",
            "claude-opus-10",
        ],
    )
    def test_generation_5_and_above_drops_temperature(self, model: str) -> None:
        assert supports_temperature(model) is False

    def test_the_4_5_trap_specifically(self) -> None:
        """The bug a naive ``"-5" in model`` check would introduce."""
        assert supports_temperature("claude-haiku-4-5") is True
        assert supports_temperature("claude-sonnet-5") is False

    @pytest.mark.parametrize(
        "model",
        ["some-proxy-alias", "claude-instant-1.2", "", "gpt-4o", "claude-opus"],
    )
    def test_unparseable_ids_default_to_sending_temperature(self, model: str) -> None:
        """An unrecognized id is far likelier to be a proxy/test alias for
        an older model than a future Claude. Guessing wrong in this
        direction yields a loud 400, not silent bad output."""
        assert supports_temperature(model) is True


# The wiring — that the request bodies actually consult this helper —
# is asserted against real outbound requests in
# tests/adapters/test_anthropic_advisor.py and
# tests/adapters/test_anthropic_assistant.py, next to their existing
# MockTransport harnesses. A pure-function test cannot catch a caller
# that forgets to ask.
