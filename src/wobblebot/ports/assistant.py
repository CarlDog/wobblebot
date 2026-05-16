"""AssistantPort - LLM-side intent parsing for the operator interaction layer.

Per ADR-013, Phase 5 introduces the Operator Interaction Engine. While
``OperatorPort`` (ports/operator.py) is the *engine-side* contract that
dispatches confirmed commands and answers queries, ``AssistantPort`` is
the *LLM-side* contract that turns operator natural-language messages
into typed ``OperatorIntent`` payloads.

Module shape:

- ``SymbolStateSnapshot`` — per-symbol state row inside an engine
  snapshot.
- ``EngineStateSnapshot`` — the read-only view of current engine state
  the assistant uses to ground its replies. Composed by
  ``cli/operator`` (Stage 5.6) before each intent parse.
- ``ConversationTurn`` — one operator-or-assistant turn captured for
  multi-turn context. Stored in ``conversation_turns`` (Stage 5.6).
- ``ConversationContext`` — the assembled prompt input: current
  operator message + recent turn history + engine state snapshot.
  Built fresh per inbound message.
- ``AssistantPort`` — ABC implemented by ``adapters/ollama_assistant.py``
  (Stage 5.3) and the future cloud assistant adapters (Phase 6).

ADR-002 stays intact under this port: ``AssistantPort.parse_intent``
returns a typed ``OperatorIntent``. State-mutating ``Command`` intents
flow through Stage 5.4's confirm-before-execute gate. The assistant
itself never executes anything; it only interprets.

Design decisions ratified in ``stage-5.1-design.md``:

- All types frozen Pydantic; mutation is ``model_copy(update=...)``.
- ``recent_turns: tuple[ConversationTurn, ...]`` — immutable ordered
  sequence. Default cap 10 turns; tunable per ``OperatorConfig`` in
  Stage 5.6.
- Pronoun resolution lives in the prompt (LLM sees prior turns +
  state snapshot); no symbolic dereferencing in code.
- Active conversation TTL (30 min default) lives in Stage 5.6's
  daemon, not on these types. These types just hold the data.
- ``EngineStateSnapshot`` declared here in Stage 5.1.B; composed
  from real engine state by Stage 5.6's ``cli/operator``.
- No SQLite table introduced here. ``conversation_turns`` lands with
  Stage 5.6 (``cli/operator`` daemon).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.operator import OperatorIntent

# --------------------------------------------------------------------- #
# EngineStateSnapshot — what the assistant knows about current state    #
# --------------------------------------------------------------------- #


class SymbolStateSnapshot(BaseModel):
    """One row inside ``EngineStateSnapshot.symbols``."""

    symbol: str = Field(min_length=3)
    state: Literal["active", "paused"]
    open_order_count: int = Field(ge=0)
    latest_price: float | None = None

    class Config:
        frozen = True


class EngineStateSnapshot(BaseModel):
    """Read-only view of current engine state for the assistant prompt.

    Composed by ``cli/operator`` before each ``parse_intent`` call so
    the assistant can ground replies in current reality (e.g. refuse
    ``pause BTC`` if BTC isn't an active symbol). Distinct from
    ``StatusResult`` (which is the typed answer to a ``StatusQuery``)
    — the snapshot is the *assistant's input*, the result is the
    *operator's output*. The two carry overlapping data but live on
    different sides of the parse.
    """

    snapshot_at: Timestamp
    symbols: list[SymbolStateSnapshot] = Field(default_factory=list)
    total_usd_balance: float
    session_pnl: float
    session_runtime_seconds: float = Field(ge=0)
    recent_fill_count: int = Field(ge=0, default=0)
    harvester_band: Literal["deficit", "topup", "hold", "surplus"] | None = None

    class Config:
        frozen = True


# --------------------------------------------------------------------- #
# ConversationTurn — one operator-or-assistant turn                     #
# --------------------------------------------------------------------- #


class ConversationTurn(BaseModel):
    """Single turn in an operator/assistant conversation.

    Stage 5.6 persists these to a ``conversation_turns`` SQLite table
    for both forensic audit and multi-turn prompt assembly. Storing
    ``intent`` alongside ``content`` lets future analysis answer
    questions like "what fraction of operator messages parsed as
    commands vs queries vs chat?" without re-running the LLM.

    Attributes:
        id: Stable identifier; populated by the persistence layer.
        channel_id: Discord channel the turn occurred in.
        user_id: Discord user ID. For ``role='assistant'`` turns this
            is the bot's own user ID.
        role: Speaker — ``operator`` or ``assistant``.
        content: Raw text exchanged.
        intent: Parsed ``OperatorIntent`` for operator turns; ``None``
            for assistant turns and for operator turns pre-parse.
        timestamp: When the turn was emitted (UTC).
    """

    id: UUID
    channel_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    role: Literal["operator", "assistant"]
    content: str = Field(min_length=1)
    intent: OperatorIntent | None = None
    timestamp: Timestamp

    class Config:
        frozen = True


# --------------------------------------------------------------------- #
# ConversationContext — assembled prompt input                          #
# --------------------------------------------------------------------- #


class ConversationContext(BaseModel):
    """Composite input to ``AssistantPort.parse_intent``.

    Built fresh per inbound message by Stage 5.6's ``cli/operator``:
    the current message + recent turn history scoped to
    ``(channel_id, user_id)`` + a fresh engine state snapshot.

    Attributes:
        current_message: The latest operator text awaiting parse.
        channel_id: Discord channel scope.
        user_id: Discord user scope.
        recent_turns: Immutable ordered tuple of prior turns; default
            cap 10 turns enforced by the caller (the type itself
            permits any length so future stages can experiment with
            longer windows without touching this contract).
        engine_state_snapshot: Current engine state grounding.
    """

    current_message: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    recent_turns: tuple[ConversationTurn, ...] = Field(default_factory=tuple)
    engine_state_snapshot: EngineStateSnapshot

    class Config:
        frozen = True


# --------------------------------------------------------------------- #
# AssistantPort — LLM-side ABC                                          #
# --------------------------------------------------------------------- #


class AssistantPort(ABC):
    """Abstract interface for LLM-driven operator-intent parsing.

    Phase 5+ feature — wired to ``adapters/ollama_assistant.py`` in
    Stage 5.3. ``cli/operator`` consumes the port via constructor DI;
    Phase 6 adds cloud variants (anthropic / openai / google).

    Implementations:
    - ``adapters.ollama_assistant.OllamaAssistantAdapter`` (Stage 5.3)
    - Cloud variants — Phase 6

    Error convention:
    - Protocol / transport / schema-validation failure raises
      ``AssistantError`` (LLM backend unreachable, output fails
      ``OperatorIntent`` discriminator resolution, response is
      empty / malformed).
    - There is no "domain miss" surface here — the LLM emits one of
      the four ``OperatorIntent`` variants (Command / Query /
      Conversational / Unparseable), with ``Unparseable`` itself
      being the structured "I don't understand" answer. Anything
      that can't reach that point is a port failure.
    """

    @abstractmethod
    async def parse_intent(self, context: ConversationContext) -> OperatorIntent:
        """Convert the operator's message into a typed ``OperatorIntent``.

        Args:
            context: Current message + conversation history + engine
                state snapshot. The assistant builds its prompt from
                this; ``cli/operator`` is responsible for composing
                a fresh snapshot per call.

        Returns:
            One of the ``OperatorIntent`` variants. Conversational and
            Unparseable variants do not flow through the engine;
            Command and Query variants are picked up by ``cli/operator``
            for confirmation / immediate dispatch respectively.

        Raises:
            AssistantError: LLM unreachable, output fails schema
                validation, or response is malformed in a way that
                cannot be coerced into an ``Unparseable`` intent.
        """
