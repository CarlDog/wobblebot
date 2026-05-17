# Phase 5 — Closing Summary

**Status: ✅ Complete (2026-05-16).** Seven Phase 5 stages closed
in one focused day-long session (5.1 / 5.2 / 5.3 / 5.4 / 5.5 / 5.6 /
5.7). The **Operator Interaction Engine** ships end-to-end: pure
domain types → Discord transport → conversational LLM intent parsing
→ engine-side dispatch via the ADR-002 firewall → outbound
notifications via DB-mediated pipe → cli/operator daemon → integration
verification.

**Phase 5 added zero real-money cost.** The architecture is
deliberately operator-mediated: every state mutation crosses the
`pending_commands` table; cli/live's `WHERE status='approved'` poll
filter is the only path from LLM-parsed intent to engine ops. No
real Discord / Kraken / Ollama call happened during slice work; every
test uses a stub. Running project real-money cost still **$0.08**
(unchanged from Phase 2 close).

This document is the Stage 5.7 deliverable per the roadmap's
"end-to-end demo" charter. Consolidates per-stage receipts, the
architecture story (how ADR-013's commitments held up), the v1
limitations worth flagging for the future, and entry conditions for
Phase 6 (Cloud LLM Integration).

## Phase 5 reframe — what changed mid-kickoff

Phase 5 originally read as seven small stages: dashboard + Discord
notifier + structured slash commands + reliability + maintenance +
performance + v1.0 release. Mid-kickoff the operator surfaced a
broader vision: Discord should be an **interaction engine** —
conversational, multi-turn, LLM-mediated — not just a notifier with
slash commands.

That triggered a roadmap restructure (commit `80b22c3`): the
Operator Interaction Engine became the whole of Phase 5, and the
displaced original-Phase-5 work reorganized into:

- **Phase 6** — Cloud LLM Integration (anthropic / openai / google
  adapters for both the trading advisor's `_build_advisor`
  placeholder slot and the new `AssistantPort`)
- **Phase 7** — Web UI / Dashboard
- **Phase 8** — Hardening & v1.0 Release (reliability, maintenance
  worker, performance tuning, soak test)

ADR-013 captures the architectural commitments; this summary
documents how they shipped.

## Per-stage outcomes

| Stage | Closed | Sub-slices | Verification |
|---|---|---|---|
| Kickoff | 2026-05-16 | — (commit `80b22c3`) | ADR-013 + `docs/planning/stage-5.1-design.md` + roadmap rewrite drafted before any code. |
| 5.1 Operator Domain & Ports | 2026-05-16 | 4 (A operator types, B assistant types, C `sqlite_storage.py` refactor inserted mid-stage, D close) | 117 + 25 unit tests for the typed sums; 100% module coverage on `ports/operator.py` + `ports/assistant.py`; sqlite_storage split into `_schema` + `_rowmap` to clear pre-existing `too-many-lines` lint flag. |
| 5.2 Discord Transport Adapter | 2026-05-16 | 1 + close | 36 tests with `MagicMock` + `AsyncMock` for the discord.py Client; 90% module coverage (uncovered: Gateway-bound `# pragma: no cover` shims + DiscordException re-raise wrappers). New runtime dep `discord.py>=2.3,<3`. |
| 5.3 Operator Assistant (Ollama) | 2026-05-16 | 1 + close | 19 + 2 + 1 tests; sister adapter to the Stage 3.2 `OllamaAdapter` (advisor) sharing the promoted-to-public `is_thinking_model` + `extract_last_json_object` helpers per the operator's "always reuse what makes sense" guidance. New prompt `config/prompts/operator.md`. |
| 5.4 Engine Integration | 2026-05-16 | 4 (A engine ops, B `pending_commands` table, C `OperatorService`, D `cli/live` poll firewall) + close | 14 + 10 + 25 + 8 tests. **First Phase 5 SQLite table** (`pending_commands`). The literal ADR-002 confirm-before-execute firewall lives in `cli/live`'s `_process_pending_commands` helper: `WHERE status='approved'` on the SELECT is the only path from LLM intent to engine. |
| 5.5 Outbound Notifications | 2026-05-16 | 2 (A notifications table + SqliteNotifierAdapter, B `cli/live` + `cli/harvest` event wiring) + close | 14 + 8 tests. Both engine CLIs gain optional `operator_db` config; `_notify` helpers swallow `NotifierError` so a broken notifier can NEVER break the engine loop. cli/harvest emits **warning** (not info) on `withdrawal executed` because money moved is the highest-value event the harvester emits. |
| 5.6 cli/operator Daemon | 2026-05-16 | 4 (A `conversation_turns` table, B `OperatorConfig` schema, C daemon, D `tools/show_pending.py`) + close | 10 + 18 + 14 tests. Three concurrent concerns inside the daemon: notification forwarder (background `asyncio.Task`), conversation flow (Discord message handler), confirmation flow (reaction handler). Per ADR-013 decision 3 the daemon NEVER calls `dispatch_command` directly. |
| 5.7 Phase 5 Integration Check | 2026-05-16 | A+B TTL expirer + e2e integration test, C close | 5 + 5 tests. TTL expirer added as a third background task (the safety net for abandoned `awaiting_confirmation` rows). End-to-end integration test exercises the full pause→confirm→approve→dispatch→notify round-trip + reject path + multi-turn conversation + notification forwarding + TTL expiry, all against stubbed Discord transport + stubbed assistant + real storage + real engine. |

**Commit count for Phase 5:** ~20 commits across the seven stages
plus the kickoff. Each sub-slice landed as its own commit with tests
+ lint clean before move-on. The session-level commit cadence
demonstrated the project's "commit at every checkpoint" rule under
sustained work.

## ADR-013 commitments — how they held up

ADR-013 ratified ten architectural commitments before code. All ten
shipped intact:

1. **Two new ports, layered.** ✅ `OperatorPort` (engine-side,
   `ports/operator.py`) + `AssistantPort` (LLM-side,
   `ports/assistant.py`). Constructor DI throughout.

2. **`OperatorIntent` is a strict typed sum.** ✅ Pydantic
   discriminated union with four variants
   (`Command`/`Query`/`Conversational`/`Unparseable`). Concrete
   `OperatorCommand` + `OperatorQuery` are themselves typed sums
   nested inside `Command` / `Query`. `TypeAdapter` validates the LLM's
   JSON output in one pass; rebuild from persisted `intent_json` in
   `conversation_turns` uses the same adapter.

3. **ADR-002 preservation: confirm-before-execute.** ✅ The
   `WHERE status='approved'` filter on cli/live's
   `_process_pending_commands` SELECT is the literal firewall. The
   integration test `test_full_pause_round_trip` exercises the full
   chain; `test_reject_flow_does_not_dispatch` proves rejected rows
   never reach the engine.

4. **DB-mediated decoupling between cli/operator and cli/live.** ✅
   Three tables in operator.db (`pending_commands`, `notifications`,
   `conversation_turns`). cli/live runs Discord-ignorant; only
   `notifications` table writes connect it to the cli/operator side.
   `cli/operator`'s stub-engine constraint (see v1 limitation below)
   shows the decoupling working — neither daemon needs the other to
   start.

5. **Multi-turn conversation state.** ✅ `ConversationContext` carries
   `current_message` + `recent_turns` (default 10, configurable) +
   `engine_state_snapshot`. The integration test
   `test_multi_turn_conversation_records_history` confirms the
   second invocation's context sees the first turn pair.

6. **User + channel allowlist authorization.** ✅
   `DiscordTransport.is_allowed` enforces deny-by-default on both
   axes plus bot-self rejection. `OperatorAuthConfig` validates that
   `outbound_channel_id` is in `allowed_channel_ids` at startup.

7. **Pluggable LLM provider via AssistantPort.** ✅ Phase 5 ships
   `OllamaAssistantAdapter` (provider="ollama" hardcoded in the
   `AssistantLLMConfig.provider` Literal); Phase 6 will extend the
   Literal to include cloud providers and add the corresponding
   adapter classes.

8. **Discord library: `discord.py`.** ✅ Pinned `>=2.3,<3` in
   `pyproject.toml`. The Gateway-bound event handlers are marked
   `# pragma: no cover` since they can only meaningfully run against
   a real connection.

9. **Outbound notifications use the same DB-mediated pipe.** ✅
   `SqliteNotifierAdapter` writes to `notifications`; cli/operator's
   `_forwarder_loop` reads + posts + marks. cli/live never imports
   discord.py.

10. **The conversational LLM is not in the money path.** ✅
    `AssistantError` and `NotifierError` are both swallowed by
    cli/live's `_notify` helper; the engine loop runs even when the
    operator surface is completely broken.

## v1 limitation worth naming

**cli/operator can't see cli/live's in-memory pause state.** The
engine's `_paused_symbols: set[Symbol]` is process-local; cli/operator
runs in a different process and has a stub `GridEngine` it can't
share state with. Concrete consequence: `StatusQuery` answered by
cli/operator reports all symbols as `"active"` even when cli/live has
paused some.

This is annotated in code (see `_main_async` and
`_compose_engine_state_snapshot` in `cli/operator.py`) and the
operator can work around it by checking the dispatched
`pending_commands` audit row (`tools/show_pending.py --status
dispatched`).

**Fix path** (probably Phase 8 hardening): persist pause state to a
new SQLite table that both daemons read. Roughly 20 lines of
storage + a per-tick write from cli/live + replacing the stub engine
in cli/operator with a read-only state view.

Why we didn't fix it in Stage 5.6: the fix is small but requires its
own design decision (when does cli/live write — every tick? on
change?), and Phase 5 was already running long. Better to ship the
documented limitation than rush the fix.

## Test + code health at Phase 5 close

| Metric | Before Phase 5 | After Phase 5 | Delta |
|---|---|---|---|
| Unit tests | 792 | 1214 | **+422** |
| Integration tests | 21 | 26 | +5 (e2e operator round-trip suite) |
| src/ modules (mypy-counted) | 60 | 69 | +9 (operator + assistant ports, sqlite_storage_schema/rowmap split, sqlite_notifier, ollama_assistant, operator_service, discord_transport, cli/operator) |
| pylint score | 10.00/10 | 10.00/10 | unchanged + cleared the pre-existing `too-many-lines` flag on `sqlite_storage.py` mid-stage |
| black / isort | clean | clean | unchanged |
| Runtime deps added | — | `discord.py>=2.3,<3` | one new dep, gated under operator interaction |

## Real-money cost ledger

Phase 5 spent **$0.00**. No real Kraken withdrawal, no real Discord
post (everything stubbed), no real Ollama call from inside Phase 5
test suites (`httpx.MockTransport` throughout).

Running project total: **$0.08** unchanged from Phase 2 close.

## Entry conditions for Phase 6 — Cloud LLM Integration

Phase 6's scope (per roadmap pre-Stage-5.7):

> Operator-selectable cloud LLM providers for both the operator-assistant
> role (Phase 5 added) and the MoE trading-advisor roles (Phase 3
> placeholder slots). Phase 5 ships with Ollama-only assistant; this
> phase fills the long-standing `_build_advisor` placeholders
> (`anthropic`, `openai`, `google`) and extends the same machinery to
> `AssistantPort`.

What Phase 5 leaves Phase 6 to build on:

- `AssistantPort` ABC is provider-neutral by construction. The Phase 5
  `OllamaAssistantAdapter` is one implementation; Phase 6 adds three more.
- `AssistantLLMConfig.provider` is currently `Literal["ollama"]`. Phase 6
  extends to `Literal["ollama", "anthropic", "openai", "google"]` and
  the cli/operator wiring picks the right adapter from a registry.
- Schema-wise, Phase 6 needs no new SQLite tables — the assistant
  adapter is stateless beyond its `httpx.AsyncClient` lifecycle.
- The advisor-side `_build_advisor` cloud placeholders in
  `services/advise.py` get filled in alongside the assistant cloud
  adapters since the request/response shapes are mostly shared.
- Cost-tracking surface (per-call token + dollar accounting) is a
  Phase 6 add since cloud APIs charge per request. Probably a new
  `llm_calls` table.

The architectural firewall (ADR-002 + ADR-013) doesn't move in Phase 6.
Cloud assistants parse intent; they don't execute. The
`pending_commands` confirm-before-execute gate stays load-bearing.

## What else the operator gets

- New CLI: `python -m wobblebot.cli.operator` — long-running daemon
  for Discord interaction. Run alongside `cli/live` and (optionally)
  `cli/harvest`; the three daemons coordinate only through
  operator.db.
- New inspection tool: `python tools/show_pending.py` — query the
  `pending_commands` table by status / limit / format.
- New config block: `operator:` in `settings.yml`. Composes
  `OperatorAuthConfig` (Discord allowlists + bot token env var +
  outbound channel id) + `AssistantLLMConfig` (provider / model /
  prompt file / temperature / max tokens / timeout) + paths to all
  four databases the daemon reads + ADR-013's tunable knobs
  (context_window_turns, confirm_ttl_seconds,
  forwarder_poll_seconds, ttl_expirer_poll_seconds).
- New prompt file: `config/prompts/operator.md`. Operator-edits-freely
  Markdown body + YAML frontmatter declaring `role=operator` and
  `response_schema=operator_intent_v1`.

## Closing notes

Phase 5 was the largest phase by stage count (7), commit count
(~20), and code surface (+9 src modules, +422 tests). It also added
the broadest user-facing change — bots can now be conversed with,
not just configured.

The single biggest design decision was the **DB-mediated decoupling**.
Splitting cli/operator and cli/live across two processes communicating
only via SQLite tables means Discord outages can't kill trading and
trading bugs can't break the operator's chat surface. The cost is
operational complexity (two daemons to run, one more DB to back up)
which Phase 8's maintenance worker will address.

The pause-state v1 limitation is the most concrete piece of debt
shipped in Phase 5. Documented in code, documented here, fixable in a
~50-line slice when Phase 8 or a future operational-need-driven stage
arrives.

Onward to Phase 6.
