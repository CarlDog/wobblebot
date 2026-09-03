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

A matched verb whose symbol does NOT resolve is returned as
``IntentUnparseable`` with the same wording the prompt teaches the
model ("X is not in the active symbol set") — deterministic, and it
keeps the model from guessing a symbol the engine is not trading.
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

from wobblebot.domain.value_objects import Symbol
from wobblebot.ports.operator_intents import (
    CancelOpenOrdersCommand,
    IntentCommand,
    IntentQuery,
    IntentUnparseable,
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
    kind for kind, _ in _PATTERNS if kind != "status"
)
"""Command kinds the fast path can emit. Pinned against the typed union
by ``tests/config/test_operator_catalog_ssot.py`` so a new engine command
cannot silently miss the deterministic path (or name a kind that no
longer exists)."""


def resolve_symbol(token: str, active_symbols: Sequence[Symbol]) -> Symbol | None:
    """Ground ``token`` (``BTC`` or ``BTC/USD``) against the active set.

    Returns the matching active ``Symbol``, or ``None`` when the token is
    not active or a bare base is ambiguous (two active symbols share it).
    """
    if "/" in token:
        base, quote = token.upper().split("/", 1)
        for sym in active_symbols:
            if sym.base.upper() == base and sym.quote.upper() == quote:
                return sym
        return None
    base = token.upper()
    matches = [sym for sym in active_symbols if sym.base.upper() == base]
    return matches[0] if len(matches) == 1 else None


def parse_fast(text: str, active_symbols: Sequence[Symbol]) -> OperatorIntent | None:
    """Parse ``text`` deterministically, or return ``None`` to defer to the LLM.

    Args:
        text: The operator's raw Discord message.
        active_symbols: The engine's configured trading set
            (``config.live.symbols``). Empty means "unknown" and makes
            this function abstain.

    Returns:
        An ``IntentCommand`` / ``IntentQuery`` when the message is exactly
        one of the fixed forms; ``IntentUnparseable`` when the verb
        matched but the symbol is not in the active set; ``None`` for
        everything else.
    """
    if not active_symbols:
        return None
    for kind, pattern in _PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        if kind in _NO_SYMBOL:
            return _NO_SYMBOL[kind]()
        token = match.group("symbol")
        symbol = resolve_symbol(token, active_symbols)
        if symbol is None:
            return IntentUnparseable(reason=f"{token.upper()} is not in the active symbol set")
        return _WITH_SYMBOL[kind](symbol)
    return None


__all__ = ("FAST_PATH_COMMAND_KINDS", "parse_fast", "resolve_symbol")
