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
    PauseAllCommand,
    PauseCommand,
    ReanchorCommand,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StopCommand,
)
from wobblebot.services.operator_intent_fastpath import classify_fast, matching_symbols

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
        assert classify_fast(text, ACTIVE).intent == IntentCommand(
            command=ReanchorCommand(symbol=SOL)
        )

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
        assert classify_fast(text, ACTIVE).intent == expected


class TestBareBaseResolution:
    def test_bare_base_resolves_when_exactly_one_active_symbol_has_it(self) -> None:
        assert matching_symbols("sol", ACTIVE) == (SOL,)
        assert matching_symbols("SOL/USD", ACTIVE) == (SOL,)

    def test_ambiguous_bare_base_defers_to_the_model(self) -> None:
        active = (BTC, Symbol(base="BTC", quote="EUR"))
        assert len(matching_symbols("BTC", active)) == 2
        # Defer, do not refuse — the model grounds against the same set.
        assert classify_fast("pause BTC", active).intent is None
        # Fully-qualified still resolves in the ambiguous set.
        assert classify_fast("pause BTC/EUR", active).intent == IntentCommand(
            command=PauseCommand(symbol=Symbol(base="BTC", quote="EUR"))
        )

    def test_inactive_symbol_defers_to_the_model(self) -> None:
        assert classify_fast("reanchor XRP", ACTIVE).intent is None
        assert classify_fast("pause XRP/USD", ACTIVE).intent is None

    def test_wrong_quote_does_not_resolve(self) -> None:
        assert matching_symbols("SOL/EUR", ACTIVE) == ()


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
        assert classify_fast(text, ACTIVE).intent is None

    def test_no_active_symbol_set_means_the_fast_path_abstains(self) -> None:
        # Nothing to ground a symbol against — the model keeps the whole
        # conversation, exactly as before this module existed.
        assert classify_fast("reanchor SOL/USD", ()).intent is None
        assert classify_fast("stop", ()).intent is None


class TestVerbPlusOrdinaryWordFallsThrough:
    """2026-09-03 review, finding 1. ``_SYMBOL`` matches any 2-10 character
    word, so a verb followed by ordinary English used to be hard-refused
    with a false reason while the model never saw the message. Every one of
    these must now reach the model instead."""

    @pytest.mark.parametrize(
        "text",
        [
            "pause everything",
            "resume everything",
            "pause held",
            "resume rest",
            "pause both",
            "cancel orders for everything",
            "reanchor whatever",
        ],
    )
    def test_verb_plus_prose_defers_instead_of_refusing(self, text: str) -> None:
        assert classify_fast(text, ACTIVE).intent is None

    @pytest.mark.parametrize(
        "text",
        [
            "cancel orders on all",
            "cancel orders for all",
            "cancel open orders on all",
            "cancel open orders for all",
            "cancel all orders",
            "Cancel All Open Orders",
        ],
    )
    def test_documented_cancel_all_phrasings_queue_the_all_symbols_form(self, text: str) -> None:
        # operator.md and the help catalog both advertise the all-symbols
        # form (``symbol`` omitted / null). It has no ``*_all`` sibling kind,
        # so it needs its own grammar or ``all`` gets read as a symbol.
        assert classify_fast(text, ACTIVE).intent == IntentCommand(
            command=CancelOpenOrdersCommand(symbol=None)
        )

    def test_scoped_cancel_still_binds_its_symbol(self) -> None:
        assert classify_fast("cancel orders on SOL", ACTIVE).intent == IntentCommand(
            command=CancelOpenOrdersCommand(symbol=SOL)
        )


class TestDecisionReasons:
    """Review follow-up 2: the fast path must say WHY it declined. Only the hit
    used to be observable, so an inert fast path looked exactly like the 1.5B
    mis-parse it replaced."""

    def test_hit_names_the_verb(self) -> None:
        d = classify_fast("re-anchor SOL", ACTIVE)
        assert d.reason == "hit"
        assert d.verb == "reanchor"
        assert d.intent is not None

    def test_symbolless_hit_names_its_verb_too(self) -> None:
        assert classify_fast("pause all", ACTIVE).verb == "pause_all"
        assert classify_fast("cancel orders on all", ACTIVE).verb == "cancel_all"

    def test_empty_active_set_reports_not_armed(self) -> None:
        d = classify_fast("reanchor SOL/USD", ())
        assert (d.intent, d.reason) == (None, "not_armed")

    def test_ordinary_chat_reports_no_match(self) -> None:
        d = classify_fast("hey, how are things looking?", ACTIVE)
        assert (d.intent, d.reason, d.verb) == (None, "no_match", "")

    def test_verb_with_untraded_symbol_reports_symbol_unknown_and_the_token(self) -> None:
        d = classify_fast("reanchor XRP", ACTIVE)
        assert (d.intent, d.reason, d.verb, d.token) == (None, "symbol_unknown", "reanchor", "XRP")

    def test_verb_with_ambiguous_base_is_distinguished_from_unknown(self) -> None:
        # The two used to collapse to one reason, and the operator-facing
        # wording ("not in the active symbol set") was false for this one.
        active = (BTC, Symbol(base="BTC", quote="EUR"))
        d = classify_fast("pause BTC", active)
        assert (d.intent, d.reason, d.token) == (None, "symbol_ambiguous", "BTC")

    def test_prose_after_a_verb_is_a_decline_not_a_hit(self) -> None:
        d = classify_fast("pause everything", ACTIVE)
        assert d.intent is None and d.reason == "symbol_unknown"
