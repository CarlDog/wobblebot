# Stage 5.1 — Operator Domain & Ports: Design and Slicing

*Drafted 2026-05-16 alongside ADR-013 at the kickoff of Phase 5, before any 5.1 code was written. Living document — actual slicing may adjust during implementation, but the principles below are load-bearing and should not be relitigated without an ADR.*

## What Stage 5.1 delivers

The type contracts every other Phase 5 stage consumes. Pure domain + port code: no I/O, no Discord, no LLM call, no SQLite table, no CLI. The deliverable is a set of strongly-typed Python modules in `ports/` whose import graph is `pydantic + domain.value_objects` and nothing else.

At the end of Stage 5.1:

- A new `ports/operator.py` defines `OperatorCommand` (sum of typed commands), `OperatorQuery` (sum of typed queries), the per-query `*Result` types, `OperatorIntent` (the outermost sum including `Conversational` and `Unparseable`), `PendingCommand`, and the `OperatorPort` ABC.
- A new `ports/assistant.py` defines `ConversationTurn`, `ConversationContext`, and the `AssistantPort` ABC.
- New port-layer exceptions `OperatorError` and `AssistantError` in `ports/exceptions.py`.
- `ports/__init__.py` re-exports the new symbols.
- Unit tests cover construction, discriminator resolution, validation errors, JSON round-trip, and frozenness for every new type.

The stage closes once mypy + pylint + black + isort + pytest are all clean and the unit count has grown by ~50.

## Critical separation: Stage 5.1 ≠ later Phase 5 stages

Stage 5.1 produces **types and contracts only**. Stage 5.2 introduces Discord transport. Stage 5.3 introduces an LLM-backed `AssistantPort` implementation. Stage 5.4 wires `OperatorPort` to the engine. Stage 5.5 introduces the `notifications` table and SqliteNotifierAdapter. Stage 5.6 brings the `cli/operator` daemon online.

**Do not conflate them.** If a Stage 5.1 PR touches `adapters/`, `services/`, `cli/`, or `config/`, something is wrong. The single allowed exception is `ports/__init__.py` re-exports.

## What's already in place

- **`Notification`** value object and `NotifierPort` ABC in `ports/notifier.py` (Phase 1.2). The `NotifierPort.send_notification` signature is reused by Stage 5.5's `SqliteNotifierAdapter`; **no changes to `NotifierPort` in Stage 5.1**.
- **`Symbol`** value object in `domain/value_objects.py` — used throughout the command/query typed args. No raw strings for trading pairs.
- **`Timestamp`** value object — used for all timestamped fields.
- **Port-layer exception convention** established by `AdvisorError`, `HarvesterError`, etc. Stage 5.1's new exceptions extend the same `WobbleBotPortError` base.
- **Pydantic discriminator pattern** used by `MoEAdvisorAdapter` for expert-opinion lists. Stage 5.1's `OperatorIntent` follows the same idiom.

## Proposed slicing

| Slice | Scope | Estimated size |
|-------|-------|----------------|
| **5.1.A — Operator types + port + results** | `ports/operator.py`: `OperatorCommand` typed sum (Pause, Resume, PauseAll, ResumeAll, CancelOpenOrders, Stop), `OperatorQuery` typed sum (Status, OpenOrders, RecentFills, RecentSuggestions, RecentNews, HarvesterStatus, RecentProposals, GridConfig, Help), `OperatorIntent` (Command \| Query \| Conversational \| Unparseable), `CommandResult`, per-query `*Result` types, `PendingCommand` (with status enum: awaiting_confirmation, approved, rejected, expired, dispatched, failed), the `OperatorPort` ABC (`dispatch_command`, `answer_query` methods), and `OperatorError` in `ports/exceptions.py`. Tests: type construction, discriminator resolution, frozenness, JSON round-trip, validation errors. | ~2–3 hours |
| **5.1.B — Assistant types + port** | `ports/assistant.py`: `ConversationTurn` (role: operator \| assistant, content, intent, timestamp), `ConversationContext` (recent turns + engine state snapshot + current operator message), `AssistantPort` ABC (`parse_intent(context) -> OperatorIntent`), and `AssistantError` in `ports/exceptions.py`. Tests: context construction, turn-list immutability, snapshot composition. | ~1.5 hours |
| **5.1.C — Stage close** | `ports/__init__.py` re-exports updated; **no other code changes**. `docs/planning/roadmap.md` ✅, `CLAUDE.md` Project Status bump, `CHANGELOG.md` entry, `project_state` memory updated, `MEMORY.md` index touched if needed. mypy + pylint 10.00/10 + black/isort/pytest all clean. | ~30 min |

**Total: ~4–5 hours of focused implementation.** Comfortably a single-evening stage. The deliberate boundary keeps Stage 5.1 a "types only" foundation that any subsequent slice can be wedged in against.

## Design decisions to ratify

ADR-013 ratifies the architecture; the items below are *implementation-level* decisions that should land at the start of Slice 5.1.A and stay stable through the stage.

### 1. Use Pydantic discriminated unions for `OperatorIntent`, `OperatorCommand`, and `OperatorQuery`

**Decision:** Each sum type uses `Annotated[Union[...], Field(discriminator='kind')]` with a `Literal[str]` `kind` field on each variant.

**Reason:** Same pattern Pydantic uses elsewhere in the project; first-class validation; emits cleanly to JSON for the LLM's structured-output channel; round-trips losslessly through `model_validate` / `model_dump_json`. The MoE advisor's `expert_opinions` list already uses the discriminator idiom.

**Implication:** Every concrete `Command`, `Query`, and top-level `Intent` variant has a unique `kind` string. Naming convention: snake_case verb (`pause`, `resume`, `pause_all`, `cancel_open_orders`, `status`, `open_orders`, `recent_fills`, etc.). The `kind` is what the LLM emits; it's what the engine dispatches on.

### 2. Frozen Pydantic models for every value object

**Decision:** Every type in `ports/operator.py` and `ports/assistant.py` is `frozen=True`. Mutation requires `.model_copy(update={...})` — explicit, audited, never accidental.

**Reason:** These objects flow through long-lived per-channel conversation state and persisted SQLite rows. Mutating one in place would invalidate the in-memory cache and the persisted row simultaneously. Same discipline as `Balance`, `Order`, `Trade`, `AdvisorSuggestion`.

### 3. `OperatorCommand` is small and bounded; no live config edits in v1

**v1 command catalog:**

- `PauseCommand(symbol: Symbol)` — sets a per-symbol pause flag. `cli/live` skips that symbol on the next tick. Engine state and open orders preserved.
- `ResumeCommand(symbol: Symbol)` — clears the pause flag.
- `PauseAllCommand()` — pauses every active symbol.
- `ResumeAllCommand()` — resumes every paused symbol.
- `CancelOpenOrdersCommand(symbol: Symbol | None)` — cancels all open grid orders for a symbol (or all symbols if `None`). Does NOT stop the engine; the next tick will re-lay grid orders unless the symbol is also paused.
- `StopCommand()` — soft-stop request. `cli/live` finishes its current tick, cancels open orders, and exits cleanly. Same path as SIGINT.

**Explicitly NOT in v1:**

- Live grid-param edits (spacing, levels, order size). ADR-012 routes those through `cli/apply` + restart. Allowing them via Discord would bypass the auto-apply gate and the audit trail. Defer to a much later stage if ever.
- Withdrawals. Per ADR-003 the Harvester is the only module with transfer authority; allowing Discord to trigger `cli/harvest --execute` would route money through the conversational layer. Hard no.
- `KillAllCommand` (force-exit without cleanup). The `Stop` flow is already a clean shutdown; an emergency kill is the operator pressing Ctrl-C on the host. Discord is not the right path for that.

**Reason:** every command in v1 is reversible (pause/resume, cancel/re-lay) or is the same clean-shutdown path the engine already does correctly. No new state-mutation surface; no new failure modes the engine doesn't already handle.

### 4. `OperatorQuery` is read-only and bounded

**v1 query catalog:**

- `StatusQuery()` — per-symbol active/paused state, USD balance, session PnL since start, runtime elapsed, recent fill count.
- `OpenOrdersQuery(symbol: Symbol | None)` — open grid orders.
- `RecentFillsQuery(symbol: Symbol | None, lookback_hours: int = 24, limit: int = 20)` — recent filled orders + cycles.
- `RecentSuggestionsQuery(symbol: Symbol | None, limit: int = 5)` — last N advisor suggestions from `advise.db`.
- `RecentNewsQuery(lookback_hours: int = 24, limit: int = 10)` — recent ingested news items from `news.db`.
- `HarvesterStatusQuery()` — current USD balance, band classification, latest proposal.
- `RecentProposalsQuery(direction: Literal['exchange_to_bank', 'bank_to_exchange'] | None, lookback_hours: int = 24, limit: int = 10)`.
- `GridConfigQuery(symbol: Symbol | None)` — current grid parameters in effect.
- `HelpQuery()` — returns a structured help payload listing available commands and queries with one-line descriptions.

**Each query has a matching `*Result` type** (`StatusResult`, `OpenOrdersResult`, etc.) carrying structured fields the assistant can summarize into prose. Results are also frozen Pydantic.

**Reason:** typed-input + typed-output discipline; the assistant can format any result for Discord without ad-hoc parsing. New queries can be added cheaply in later stages — adding a query type does not require touching the LLM (it'll pick the new kind up from the JSON schema).

### 5. `OperatorPort` returns typed results, not raw dicts

**Decision:**

```python
class OperatorPort(ABC):
    @abstractmethod
    async def dispatch_command(self, command: OperatorCommand) -> CommandResult: ...

    @abstractmethod
    async def answer_query(self, query: OperatorQuery) -> QueryResult: ...
```

Where `QueryResult` is itself a typed union (`Annotated[Union[StatusResult, OpenOrdersResult, ...], Field(discriminator='kind')]`). The `kind` on the result echoes the query's kind, so downstream code can pattern-match.

**Reason:** same wire discipline as commands. The bot formats results uniformly; tests assert on result structure rather than freeform strings.

### 6. `PendingCommand` carries the full audit trail

**Decision:** `PendingCommand` is a frozen Pydantic model that captures:

- `id: UUID` — primary key.
- `command: OperatorCommand` — the parsed command (nested model).
- `status: Literal['awaiting_confirmation', 'approved', 'rejected', 'expired', 'dispatched', 'failed']`.
- `channel_id: str`, `requesting_user_id: str` — who asked.
- `confirming_user_id: str | None`, `confirmed_at: Timestamp | None` — who clicked ✅.
- `dispatched_at: Timestamp | None`, `result: CommandResult | None`.
- `ttl_expires_at: Timestamp`.
- `created_at: Timestamp`.

**Reason:** every status transition is queryable for audit ("who paused BTC last Tuesday?"). The model lives in `ports/operator.py` because both `cli/operator` (writer) and `cli/live` (reader) consume it through the OperatorPort.

### 7. `ConversationTurn` is also frozen and persistable

**Decision:** `ConversationTurn`:

- `id: UUID`.
- `channel_id: str`, `user_id: str`.
- `role: Literal['operator', 'assistant']`.
- `content: str` — the raw text exchanged.
- `intent: OperatorIntent | None` — present for operator turns once parsed; `None` for assistant turns.
- `timestamp: Timestamp`.

**Reason:** Stage 5.6 persists turns to `conversation_turns` table; the schema is mechanical mapping from this type. Storing `intent` alongside `content` lets future analysis answer "what fraction of operator messages parsed as commands vs queries vs chat?" without re-running the LLM.

### 8. `ConversationContext` is the assembled prompt input

**Decision:** `ConversationContext`:

- `current_message: str` — what the operator just sent.
- `channel_id: str`, `user_id: str`.
- `recent_turns: tuple[ConversationTurn, ...]` — frozen ordered sequence; default cap 10 turns.
- `engine_state_snapshot: EngineStateSnapshot` — structured state the assistant uses to ground its replies.

`EngineStateSnapshot` is its own frozen type — per-symbol active/paused, current balances, session PnL, open-order count, last-advisor-suggestion summary. Composed by Stage 5.6's `cli/operator` before each `parse_intent` call. **Note:** Stage 5.1 declares this type's shape but does NOT compose it from real state — that's Stage 5.6's job. Stage 5.1's tests construct stub snapshots.

**Reason:** the assistant needs to ground its replies in current reality. "Pause BTC" should fail loudly if BTC isn't an active symbol. The snapshot is the assistant's read-only view of engine state; multi-turn pronoun resolution falls out of `recent_turns + engine_state_snapshot + current_message` being in the prompt together.

### 9. Error semantics follow the project convention

- Domain miss (e.g. operator queries a symbol the engine isn't trading) → `*Result` with a structured "not found" field, NOT an exception.
- Protocol failure (transport error, malformed LLM output, DB unavailable) → port-typed exception (`OperatorError` for engine-side, `AssistantError` for LLM-side).
- Both inherit from `WobbleBotPortError`.

**Reason:** matches the existing convention from `ExchangePort` / `AdvisorPort` / `HarvesterPort`. Domain misses are answers, not failures; transport failures are failures.

### 10. SQLite tables are NOT introduced in Stage 5.1

**Decision:** No `StoragePort` method additions, no `SQLiteStorageAdapter` changes, no schema migration. The three new tables (`pending_commands`, `notifications`, `conversation_turns`) land in the stages that own the code reading/writing them:

- `pending_commands` — Stage 5.4 (engine integration; both `cli/operator` and `cli/live` touch it).
- `notifications` — Stage 5.5 (outbound notifications).
- `conversation_turns` — Stage 5.6 (`cli/operator` daemon).

**Reason:** keeps Stage 5.1 pure. A schema is only useful when there's code that uses it; landing schemas ahead of the code creates dead tables and risks the schema drifting from real usage. The domain types in Stage 5.1 are designed so the eventual table columns map directly.

## Test plan

| File | Coverage target |
|------|-----------------|
| `tests/ports/test_operator_intent.py` | OperatorIntent discriminator resolution, JSON round-trip, validation errors for malformed payloads, frozenness. |
| `tests/ports/test_operator_commands.py` | Each concrete command's typed args validation (Symbol parsing, optional fields), frozenness, equality. |
| `tests/ports/test_operator_queries.py` | Each concrete query's typed args validation, default values for optional knobs (lookback_hours, limit). |
| `tests/ports/test_operator_results.py` | Each `*Result` type's construction; the `QueryResult` discriminated union; CommandResult success/failure shapes. |
| `tests/ports/test_pending_command.py` | Status transitions captured as construction tests (no behavior — just type shape), required fields, TTL semantics as data. |
| `tests/ports/test_assistant_types.py` | ConversationTurn, ConversationContext, EngineStateSnapshot construction + frozenness; recent_turns immutability. |

**Target: ~50 new unit tests, all marked `unit`, all pure-function (zero I/O).**

## What's NOT in scope for Stage 5.1

The following are explicitly **deferred** to later stages — listed here so the slicing stays disciplined:

- Any Discord-specific code (`discord.py` import, gateway client, embed builders) — Stage 5.2.
- Any LLM call (Ollama, anthropic, openai, google) — Stage 5.3.
- Any engine-side dispatch handler (`OperatorService`, engine `pause_symbol`/`resume_symbol`/`cancel_all` methods) — Stage 5.4.
- Any SQLite table or `StoragePort` method addition — Stage 5.4 / 5.5 / 5.6 as listed above.
- Any `cli/operator` code, any new config schema (`OperatorConfig`) — Stage 5.6.
- Any cloud LLM provider adapter — Phase 6.
- `cli/live` changes — Stage 5.4 (`OperatorService` integration) and Stage 5.5 (`NotifierPort` injection).
- `cli/harvest` changes — Stage 5.5 (`NotifierPort` injection for transfer events).

## Stage close criteria

- All three slices landed with passing unit tests.
- `pytest -m unit` green (target ~942 total unit tests at stage close, +50 from current 892).
- `pylint src/` reports 10.00/10 (no regressions).
- `mypy src/` clean.
- `black --check src/ tests/` and `isort --check-only src/ tests/` clean.
- `docs/planning/roadmap.md` Stage 5.1 row carries a ✅ date.
- `CLAUDE.md` Project Status reflects Stage 5.1 close.
- `CHANGELOG.md` entry added.
- No imports from `adapters/`, `services/`, `cli/`, or `config/` appear in `src/wobblebot/ports/operator.py` or `src/wobblebot/ports/assistant.py` (manual grep + load-bearing).
- Stage close commit message references this design doc.
