"""Deterministic operator-command fast path (2026-09-03 recovery ergonomics).

The two real messages from the incident anchor this table: the 1.5B
model parsed ``reanchor SOL/USD`` as a status query and ``re-anchor SOL``
as the command. Both must now resolve identically, before any model
sees them.
"""

from __future__ import annotations

import pytest

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator_intents import (
    CancelOpenOrdersCommand,
    IntentCommand,
    IntentQuery,
    IntentUnparseable,
    PauseAllCommand,
    PauseCommand,
    ReanchorCommand,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StopCommand,
)
from wobblebot.services.operator_intent_fastpath import parse_fast, resolve_symbol

pytestmark = pytest.mark.unit

BTC = Symbol(base="BTC", quote="USD")
SOL = Symbol(base="SOL", quote="USD")
DOGE = Symbol(base="DOGE", quote="USD")
ACTIVE = (BTC, SOL, DOGE)


class TestVerbSpellingsAreSynonyms:
    @pytest.mark.parametrize(
        "text",
        [
            "reanchor SOL/USD",  # 2026-09-03 15:24Z — the model read this as a status query
            "re-anchor SOL",  # 2026-09-03 15:30Z — the prompt's example wording, worked
            "re anchor SOL",
            "Reanchor sol/usd",
            "RE-ANCHOR SOL.",
            "  reanchor   SOL/USD  ",
        ],
    )
    def test_every_reanchor_spelling_is_the_same_command(self, text: str) -> None:
        assert parse_fast(text, ACTIVE) == IntentCommand(command=ReanchorCommand(symbol=SOL))

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("pause BTC", IntentCommand(command=PauseCommand(symbol=BTC))),
            ("pause BTC/USD", IntentCommand(command=PauseCommand(symbol=BTC))),
            ("resume doge", IntentCommand(command=ResumeCommand(symbol=DOGE))),
            ("Resume DOGE/USD", IntentCommand(command=ResumeCommand(symbol=DOGE))),
            ("cancel orders on SOL", IntentCommand(command=CancelOpenOrdersCommand(symbol=SOL))),
            (
                "cancel open orders for SOL/USD",
                IntentCommand(command=CancelOpenOrdersCommand(symbol=SOL)),
            ),
            ("pause all", IntentCommand(command=PauseAllCommand())),
            ("Resume ALL", IntentCommand(command=ResumeAllCommand())),
            ("stop", IntentCommand(command=StopCommand())),
            ("stop.", IntentCommand(command=StopCommand())),
            ("status", IntentQuery(query=StatusQuery())),
            ("Status?", IntentQuery(query=StatusQuery())),
        ],
    )
    def test_fixed_grammar(self, text: str, expected: object) -> None:
        assert parse_fast(text, ACTIVE) == expected


class TestBareBaseResolution:
    def test_bare_base_resolves_when_exactly_one_active_symbol_has_it(self) -> None:
        assert resolve_symbol("sol", ACTIVE) == SOL
        assert resolve_symbol("SOL/USD", ACTIVE) == SOL

    def test_bare_base_is_refused_when_ambiguous(self) -> None:
        active = (BTC, Symbol(base="BTC", quote="EUR"))
        assert resolve_symbol("BTC", active) is None
        # The verb matched, so the operator gets a deterministic refusal —
        # not a model guessing which BTC pair they meant.
        assert parse_fast("pause BTC", active) == IntentUnparseable(
            reason="BTC is not in the active symbol set"
        )
        # Fully-qualified still works in the ambiguous set.
        assert parse_fast("pause BTC/EUR", active) == IntentCommand(
            command=PauseCommand(symbol=Symbol(base="BTC", quote="EUR"))
        )

    def test_inactive_symbol_is_unparseable_with_the_prompts_wording(self) -> None:
        assert parse_fast("reanchor XRP", ACTIVE) == IntentUnparseable(
            reason="XRP is not in the active symbol set"
        )
        assert parse_fast("pause XRP/USD", ACTIVE) == IntentUnparseable(
            reason="XRP/USD is not in the active symbol set"
        )

    def test_wrong_quote_does_not_resolve(self) -> None:
        assert resolve_symbol("SOL/EUR", ACTIVE) is None


class TestAbstention:
    @pytest.mark.parametrize(
        "text",
        [
            "hello",
            "what's the weather like",
            "pause",  # verb with no symbol
            "reanchor",  # verb with no symbol
            "please pause BTC for me",  # not the exact form — the model handles prose
            "stop that",
            "status report",  # a DIFFERENT query kind; leave it to the model
            "cancel orders",  # no scope — leave the all-symbols reading to the model
            "",
        ],
    )
    def test_anything_outside_the_grammar_defers_to_the_model(self, text: str) -> None:
        assert parse_fast(text, ACTIVE) is None

    def test_no_active_symbol_set_means_the_fast_path_abstains(self) -> None:
        # Nothing to ground a symbol against — the model keeps the whole
        # conversation, exactly as before this module existed.
        assert parse_fast("reanchor SOL/USD", ()) is None
        assert parse_fast("stop", ()) is None
