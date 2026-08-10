"""ADR-034 — ExecuteProposalCommand's type-level containment.

The whole point of putting this command OUTSIDE ``OperatorCommand`` is
that ADR-002 ("the LLM is advisory only") stops being a runtime check
and becomes a property of the schema: the assistant's output type
simply has no branch that can produce a withdrawal. These tests pin
that, because the failure mode of a regression is invisible — a new
variant quietly added to the wrong union would look fine and parse
fine, and only show up as an LLM-originated money movement.
"""

from __future__ import annotations

from decimal import Decimal
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from wobblebot.ports.operator import (
    ExecuteProposalCommand,
    OperatorCommand,
    OperatorIntent,
    QueueableCommand,
)

pytestmark = pytest.mark.unit


def _kinds(union: object) -> set[str]:
    """The set of ``kind`` discriminators a union can produce."""
    return {variant.model_fields["kind"].default for variant in get_args(get_args(union)[0])}


class TestUnionMembership:
    def test_execute_proposal_is_not_an_operator_command(self) -> None:
        """The LLM's output schema must not contain the money command."""
        assert "execute_proposal" not in _kinds(OperatorCommand)

    def test_execute_proposal_is_queueable(self) -> None:
        assert "execute_proposal" in _kinds(QueueableCommand)

    def test_queueable_is_a_strict_superset(self) -> None:
        """Adding a queue-only kind must not remove an engine kind."""
        assert _kinds(OperatorCommand) < _kinds(QueueableCommand)
        assert _kinds(QueueableCommand) - _kinds(OperatorCommand) == {"execute_proposal"}


class TestLlmCannotEmitIt:
    def test_intent_parse_rejects_an_execute_proposal_command(self) -> None:
        """A crafted assistant payload naming the money kind must fail.

        This is the adversarial case: prompt injection, a jailbreak, or
        simply a confused model emitting the kind by name. Validation
        has to reject it at the boundary rather than construct it.
        """
        adapter: TypeAdapter[OperatorIntent] = TypeAdapter(OperatorIntent)
        payload = {
            "kind": "command",
            "command": {
                "kind": "execute_proposal",
                "proposal_id": "p-1",
                "amount_usd": "100",
                "destination": "attacker-wallet",
            },
        }
        with pytest.raises(ValidationError):
            adapter.validate_python(payload)


class TestFieldValidation:
    def test_amount_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ExecuteProposalCommand(
                proposal_id="p-1",
                amount_usd=Decimal("0"),
                destination="bank",
            )

    def test_destination_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            ExecuteProposalCommand(
                proposal_id="p-1",
                amount_usd=Decimal("1"),
                destination="",
            )

    def test_is_frozen(self) -> None:
        command = ExecuteProposalCommand(
            proposal_id="p-1",
            amount_usd=Decimal("1"),
            destination="bank",
        )
        with pytest.raises(ValidationError):
            command.amount_usd = Decimal("999")  # type: ignore[misc]
