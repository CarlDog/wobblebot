"""Deterministic fast path for the operator's fixed command grammar.

``cli/operator`` hands every inbound Discord message to
``AssistantPort.parse_intent`` — a local 1.5B model on the NAS. That is
the right tool for free text and the wrong tool for a six-verb grammar:
on 2026-09-03, during recovery from a dead-man's-switch purge, the model
parsed ``reanchor SOL/USD`` (the exact command kind its prompt teaches,
fully-qualified symbol) as a *status query* and answered with the engine
status, so nothing was queued and the symbol stayed held; six minutes
later ``re-anchor SOL`` — the prompt's own example wording — parsed
correctly. Same intent, opposite outcomes, decided by a hyphen.

This module is the regex the grammar deserves. :func:`parse_fast` runs
BEFORE the model and returns an intent only when the message is
unambiguously one of the fixed commands; anything else returns ``None``
and falls through to the LLM unchanged. Two normalisation rules the
operator expects to hold:

1. **Verb spellings are synonyms.** ``reanchor``, ``re-anchor`` and
   ``re anchor`` all produce the same ``ReanchorCommand``.
2. **A bare base resolves when unambiguous.** ``SOL`` means ``SOL/USD``
   whenever exactly one active symbol has that base. A fully-qualified
   ``BASE/QUOTE`` must match an active symbol exactly.

A matched verb whose symbol does NOT resolve **defers to the model**
(returns ``None``) rather than refusing. The first cut returned
``IntentUnparseable`` there, which broke the contract this docstring
states: ``_SYMBOL`` matches any 2-10 character word, so ``cancel orders
on all`` — the phrasing the help catalog and ``operator.md`` both
advertise — captured ``all``, failed to resolve, and was hard-refused
with the false reason "ALL is not in the active symbol set" while the
model that parses it correctly never saw the message. Same for ``pause
everything`` and every other verb-plus-ordinary-word phrasing. Deferring
costs nothing: the model has the active set in its snapshot and refuses
an unknown symbol on its own. (2026-09-03 review, finding 1.)

When no active-symbol set is known (empty tuple) the fast path abstains
entirely: it has nothing to ground a symbol against, so the model keeps
the whole conversation.

Pure function, no I/O, no config. ADR-002 is untouched: the returned
``IntentCommand`` goes through the same ``pending_commands`` confirm flow
as an LLM-parsed one — this module only decides *what* the operator
said, never whether it happens.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator_intents import (
    CancelOpenOrdersCommand,
    IntentCommand,
    IntentQuery,
    OperatorIntent,
    PauseAllCommand,
    PauseCommand,
    ReanchorCommand,
    ResumeAllCommand,
    ResumeCommand,
    StatusQuery,
    StopCommand,
)

# ``BTC`` or ``BTC/USD``; case-insensitive, no whitespace inside.
_SYMBOL = r"(?P<symbol>[A-Za-z0-9]{2,10}(?:/[A-Za-z0-9]{2,10})?)"
# Optional trailing punctuation ("status?", "stop.") and whitespace.
_END = r"\s*[.!?]?\s*$"

# Ordered: the ``*_all`` forms must be tried before their symbol-taking
# siblings, because ``all`` also matches the symbol pattern.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pause_all", re.compile(r"^\s*pause\s+all" + _END, re.IGNORECASE)),
    ("resume_all", re.compile(r"^\s*resume\s+all" + _END, re.IGNORECASE)),
    ("pause", re.compile(r"^\s*pause\s+" + _SYMBOL + _END, re.IGNORECASE)),
    ("resume", re.compile(r"^\s*resume\s+" + _SYMBOL + _END, re.IGNORECASE)),
    ("reanchor", re.compile(r"^\s*re[\s-]?anchor\s+" + _SYMBOL + _END, re.IGNORECASE)),
    # The all-symbols form of cancel_open_orders (``symbol=None``). MUST
    # precede the symbol-taking pattern below, for the same reason
    # pause_all precedes pause: ``all`` also matches _SYMBOL.
    (
        "cancel_all",
        re.compile(r"^\s*cancel\s+(?:open\s+)?orders\s+(?:on|for)\s+all" + _END, re.IGNORECASE),
    ),
    ("cancel_all", re.compile(r"^\s*cancel\s+all\s+(?:open\s+)?orders" + _END, re.IGNORECASE)),
    (
        "cancel_open_orders",
        re.compile(
            r"^\s*cancel\s+(?:open\s+)?orders\s+(?:on|for)\s+" + _SYMBOL + _END,
            re.IGNORECASE,
        ),
    ),
    ("stop", re.compile(r"^\s*stop" + _END, re.IGNORECASE)),
    ("status", re.compile(r"^\s*status" + _END, re.IGNORECASE)),
)

# Builders keyed by pattern kind. Symbol-less forms first, then the
# forms that need a grounded ``Symbol``. Keeping them as tables (rather
# than an if-chain) is what lets ``parse_fast`` stay a short function.
_NO_SYMBOL: dict[str, Callable[[], OperatorIntent]] = {
    "pause_all": lambda: IntentCommand(command=PauseAllCommand()),
    "resume_all": lambda: IntentCommand(command=ResumeAllCommand()),
    "cancel_all": lambda: IntentCommand(command=CancelOpenOrdersCommand(symbol=None)),
    "stop": lambda: IntentCommand(command=StopCommand()),
    "status": lambda: IntentQuery(query=StatusQuery()),
}
_WITH_SYMBOL: dict[str, Callable[[Symbol], OperatorIntent]] = {
    "pause": lambda s: IntentCommand(command=PauseCommand(symbol=s)),
    "resume": lambda s: IntentCommand(command=ResumeCommand(symbol=s)),
    "reanchor": lambda s: IntentCommand(command=ReanchorCommand(symbol=s)),
    "cancel_open_orders": lambda s: IntentCommand(command=CancelOpenOrdersCommand(symbol=s)),
}

FAST_PATH_COMMAND_KINDS: frozenset[str] = frozenset(
    # ``cancel_all`` is a second grammar for the cancel_open_orders KIND
    # (its ``symbol=None`` form), not a command kind of its own.
    ("cancel_open_orders" if kind == "cancel_all" else kind)
    for kind, _ in _PATTERNS
    if kind != "status"
)
"""Command kinds the fast path can emit. Pinned against the typed union
by ``tests/config/test_operator_catalog_ssot.py`` so a new engine command
cannot silently miss the deterministic path (or name a kind that no
longer exists)."""


def matching_symbols(token: str, active_symbols: Sequence[Symbol]) -> tuple[Symbol, ...]:
    """Every active symbol ``token`` (``BTC`` or ``BTC/USD``) could mean.

    Returned as a tuple so the caller can tell the two failure modes apart:
    empty means the token is not traded, more than one means a bare base is
    ambiguous. Collapsing both to ``None`` produced a refusal message that
    was false in the ambiguous case (2026-09-03 review follow-up 2).
    """
    if "/" in token:
        base, quote = token.upper().split("/", 1)
        return tuple(
            s for s in active_symbols if s.base.upper() == base and s.quote.upper() == quote
        )
    base = token.upper()
    return tuple(s for s in active_symbols if s.base.upper() == base)


@dataclass(frozen=True)
class FastPathDecision:
    """What the fast path decided, and why — the ``why`` is the point.

    Before this existed only the HIT was logged: abstain, miss and
    never-armed all logged nothing, so a fast path that had gone silently
    inert was indistinguishable from the 1.5B mis-parse it replaced. That
    is the failure that cost six minutes on 2026-09-03 (review follow-up 2).
    """

    intent: OperatorIntent | None
    reason: str
    """One of: ``hit``, ``not_armed`` (no active symbol set — the fast path
    is inert), ``no_match`` (not one of the fixed forms), ``symbol_unknown``
    or ``symbol_ambiguous`` (a command VERB matched but its symbol did not
    ground). The last two are the interesting declines: the operator was
    issuing a command and the fast path handed it to the model."""

    verb: str = ""
    """The pattern that matched, when one did. Empty otherwise."""

    token: str = ""
    """The symbol token as typed, when a verb matched but did not ground."""


def classify_fast(text: str, active_symbols: Sequence[Symbol]) -> FastPathDecision:
    """Parse ``text`` deterministically, reporting WHY when it declines.

    Args:
        text: The operator's raw Discord message.
        active_symbols: The engine's configured trading set
            (``config.live.symbols``). Empty means "unknown" and makes the
            fast path abstain entirely.

    Returns:
        A :class:`FastPathDecision`. ``intent`` is non-None only for
        ``reason == "hit"``; every other reason defers to the model,
        INCLUDING a matched verb whose symbol does not ground (2026-09-03
        review, finding 1 — refusing there broke ``cancel orders on all``).
    """
    if not active_symbols:
        return FastPathDecision(intent=None, reason="not_armed")
    for kind, pattern in _PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        if kind in _NO_SYMBOL:
            return FastPathDecision(intent=_NO_SYMBOL[kind](), reason="hit", verb=kind)
        token = match.group("symbol")
        candidates = matching_symbols(token, active_symbols)
        if len(candidates) != 1:
            return FastPathDecision(
                intent=None,
                reason="symbol_unknown" if not candidates else "symbol_ambiguous",
                verb=kind,
                token=token,
            )
        return FastPathDecision(intent=_WITH_SYMBOL[kind](candidates[0]), reason="hit", verb=kind)
    return FastPathDecision(intent=None, reason="no_match")


__all__ = ("FAST_PATH_COMMAND_KINDS", "FastPathDecision", "classify_fast", "matching_symbols")
