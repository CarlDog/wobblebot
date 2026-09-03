"""Operator command/query catalog SSOT drift test (P3, wired with ADR-031).

Three copies of the operator vocabulary exist and drift silently:

1. The typed unions in ``ports/operator_intents.py`` (the actual
   contract — what the TypeAdapter will parse),
2. ``_HELP_ENTRIES`` in ``services/operator_service.py`` (what the
   ``help`` query tells the operator exists),
3. ``config/prompts/operator.md`` (what the intent-parsing LLM is
   told it may emit — the NAS's 1.5B model parses against THIS).

A kind present in the union but missing from the prompt is a feature
the operator can't invoke by chat; present in the prompt but not the
union is an LLM emission that fails validation every time. The P3
plan scheduled this test to be wired "when the catalog gains the next
command" — the ADR-031 reanchor command is that moment.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from wobblebot.ports.operator import OperatorCommand, OperatorQuery
from wobblebot.services.operator_intent_fastpath import FAST_PATH_COMMAND_KINDS
from wobblebot.services.operator_service import _HELP_ENTRIES

pytestmark = pytest.mark.unit

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "config" / "prompts" / "operator.md"


def _union_kinds(union: object) -> set[str]:
    """Extract each variant's ``kind`` literal default from a discriminated union."""
    variants = get_args(get_args(union)[0])
    return {v.model_fields["kind"].default for v in variants}


def _prompt_kinds(section_heading: str) -> set[str]:
    """Kinds listed as ``- `{"kind": "<kind>"...`` bullets under one heading."""
    body = _PROMPT_PATH.read_text(encoding="utf-8")
    # Slice from the section heading to the next ### heading.
    match = re.search(rf"^### .*{re.escape(section_heading)}.*?$(.*?)(?=^### )", body, re.M | re.S)
    assert match is not None, f"prompt section {section_heading!r} not found"
    return set(re.findall(r'^- `\{"kind": "(\w+)"', match.group(1), re.M))


class TestCatalogSSOT:
    def test_command_kinds_match_three_ways(self) -> None:
        union = _union_kinds(OperatorCommand)
        help_entries = {e.kind for e in _HELP_ENTRIES if e.category == "command"}
        prompt = _prompt_kinds("Command")
        assert union == help_entries, "union vs _HELP_ENTRIES command drift"
        assert union == prompt, "union vs operator.md command drift"

    def test_query_kinds_match_three_ways(self) -> None:
        union = _union_kinds(OperatorQuery)
        help_entries = {e.kind for e in _HELP_ENTRIES if e.category == "query"}
        prompt = _prompt_kinds("Query")
        assert union == help_entries, "union vs _HELP_ENTRIES query drift"
        assert union == prompt, "union vs operator.md query drift"

    def test_no_kind_in_both_categories(self) -> None:
        commands = _union_kinds(OperatorCommand)
        queries = _union_kinds(OperatorQuery)
        assert not commands & queries

    def test_fast_path_covers_every_engine_command_kind(self) -> None:
        """A fourth copy of the vocabulary (2026-09-03): the deterministic
        fast path in front of the LLM. A new engine command must get a
        fixed-grammar form (or an explicit decision here), and the fast
        path must not name a kind the union no longer has."""
        assert FAST_PATH_COMMAND_KINDS == _union_kinds(OperatorCommand)
