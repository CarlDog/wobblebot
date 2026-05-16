# Changelog

All notable changes to WobbleBot are documented in this file. Format
is a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [SemVer](https://semver.org/spec/v2.0.0.html).
Pre-v1.0.0, all entries land under `[Unreleased]` until a tagged
release exists; per-stage receipts in
[`docs/planning/roadmap.md`](docs/planning/roadmap.md) carry the
canonical completion dates.

## [Unreleased]

### Phase 5 kickoff — Operator Interaction Engine (ADR-013) (2026-05-16)

After Phase 4 close, the operator surfaced a broader vision than the
roadmap's narrow Stage 5.1.5 (outbound Discord notifier) + Stage 5.2
(structured slash commands): Discord should be a bidirectional
**interaction engine** with multi-turn conversational LLM intent
parsing, ADR-002-preserving confirm-before-execute, and DB-mediated
decoupling between `cli/operator` and `cli/live`.

This becomes the whole of Phase 5. The originally-scoped Phase 5
stages (dashboard, reliability, maintenance, performance, v1.0
release) reorganized into three downstream phases: **Phase 6 Cloud
LLM Integration** (cloud assistant + cloud advisor adapters),
**Phase 7 Web UI / Dashboard**, **Phase 8 Hardening & v1.0 Release**
(reliability + maintenance worker + performance tuning + v1.0 soak).

Kickoff commit landed `ADR-013` (10 architectural commitments
including OperatorPort + AssistantPort split, OperatorIntent strict
typed sum, confirm-before-execute as the ADR-002 firewall, DB-mediated
decoupling, multi-turn conversation state with prompt-context pronoun
resolution, user+channel allowlist auth, pluggable LLM provider with
Ollama in Phase 5 / cloud in Phase 6, `discord.py` as the Gateway
client), `docs/planning/stage-5.1-design.md` (full slicing plan and
implementation-level decisions), and the roadmap rewrite to seven
Phase 5 stages plus the new Phases 6 / 7 / 8.

### Stage 5.3 — Operator Assistant (Ollama) (2026-05-16)

Third Phase 5 slice. `OllamaAssistantAdapter` implementing
`AssistantPort` — the LLM-side intent parser that turns operator
natural-language messages into typed `OperatorIntent` payloads.
Sister adapter to the existing Stage 3.2 `OllamaAdapter` (which
implements `AdvisorPort` for the trading recommendation flow);
different port, different endpoint, different output type, different
prompt.

**Endpoint:** Ollama's `/api/chat`, not `/api/generate`. The chat
endpoint accepts role-tagged messages (`system` / `user` / `assistant`),
giving the LLM a structured multi-turn history instead of a
concatenated prompt — better behavior for context-sensitive intent
parsing where one turn references a prior turn ("now filter to ETH").

**Code reuse (per operator guidance "always reuse what makes
sense"):** the helpers shared with the advisor adapter were
extracted rather than duplicated:

- `is_thinking_model` and `extract_last_json_object` in
  `adapters/ollama.py` promoted from underscore-private to module-public.
- New `OllamaJsonExtractError` raised by the shared extractor — each
  adapter catches and wraps as its port-specific error
  (`AdvisorError` from the advisor side, `AssistantError` from the
  assistant side). Helper stays port-agnostic.
- The ~10 lines of HTTP boilerplate per adapter (init, aclose,
  envelope key extraction) stay duplicated because the envelope
  shapes for `/api/chat` vs `/api/generate` diverge enough that a
  shared wrapper would carry conditional logic for marginal DRY win.

**Prompt:** new `config/prompts/operator.md` with frontmatter
declaring `role=operator` and `response_schema=operator_intent_v1`.
Body documents all four `OperatorIntent` variants with concrete JSON
examples for every command + query in the v1 catalog. Hard
constraint: never invent commands not in the catalog; emit
`unparseable` instead.

**`PromptRole` literal** gained `"operator"`. One-line change in
`config/prompts.py`; test parametrize updated to match.

**Adapter behavior:**
- Constructor refuses prompts whose role != "operator" — fails
  loudly at wiring time rather than silently producing nonsense.
- `parse_intent` builds the role-tagged message list: system prompt
  body + engine state snapshot JSON in the system message; each
  recent `ConversationTurn` becomes a user/assistant message in
  chronological order; current operator message is the last user
  turn.
- Module-level `TypeAdapter[OperatorIntent]` validates the LLM's
  JSON output against the discriminated union (both nesting levels
  — outer `Command`/`Query`/`Conversational`/`Unparseable` and
  inner concrete command/query kind — resolve in one pass).
- Thinking-mode (R1, o1, etc.) + split-response-envelope handling
  matches the advisor pattern.
- Every layer's failure wraps as `AssistantError`. Per ADR-013 the
  conversational LLM is NOT in the money path; an `AssistantError`
  affects only the Discord chat surface — `cli/live` never imports
  this module.

19 new unit tests for the assistant adapter cover constructor
prompt-role validation; happy paths for each `OperatorIntent`
variant (command + query + query-with-args + conversational +
unparseable); multi-turn `ConversationContext` propagation as
role-tagged messages; engine state snapshot embedding in the system
message; thinking-mode drops `format=json` and walks free-text;
split-response envelope (empty `message.content`, JSON in
`thinking`); error paths (HTTP 5xx, malformed envelope, empty
content, invalid JSON, top-level non-object, schema validation
failure, thinking-mode no-JSON); `aclose` lifecycle for owned vs
borrowed clients. 2 existing advisor tests updated to expect the
port-agnostic `OllamaJsonExtractError`. 1 parametrize case added
for the `"operator"` role in `test_prompts.py`. `TestShippedPrompts`
extended to assert `operator.md` loads with
`response_schema=operator_intent_v1`.

Full suite **1088** passes (was 1067 at Stage 5.2 close, +21).
mypy clean across 66 src files. pylint **10.00/10** with no
outstanding warnings. black + isort clean.

Running real-money cost unchanged at $0.08 (Stage 5.3 is an LLM
adapter; tests use `httpx.MockTransport` so no real Ollama call
happened, and the assistant is structurally outside the money path).

### Stage 5.2 — Discord Transport Adapter (2026-05-16)

Second Phase 5 slice. The adapter wraps `discord.py`'s Gateway client.
Inbound Gateway events (messages, reactions) are normalized into typed
`InboundMessage` / `ReactionEvent` value objects, allowlist-filtered
(user + channel both required, empty allowlists deny-by-default, bot's
own user id always rejected), and dispatched to registered handler
callbacks. Outbound surface: `send_message`, `send_embed` (color-coded
by level), `send_confirmation` (amber-bordered embed + ✅ / ❌ reaction
buttons wired for the Stage 5.4 confirm-before-execute gate).

The adapter is concrete (not behind a port). Only `cli/operator`
(Stage 5.6) will consume it; an abstraction would be speculative. Per
ADR-013 decision 9, `cli/live` remains Discord-ignorant — it never
imports this module.

**New runtime dep:** `discord.py>=2.3,<3` (2.7.1 currently). MIT,
actively maintained, the de-facto Python Discord client. Pinned to
major 2 to avoid breaking-change drift. The `message_content` Intent
is enabled (privileged; must also be enabled in the Discord developer
portal for the bot account).

36 new unit tests cover config + value object construction /
frozenness / validation; `is_allowed` allowlist semantics including
bot self-rejection and empty-allowlist deny; handler dispatch +
filtering + per-handler exception swallowing; outbound `send_*`
against a `MagicMock` / `AsyncMock` injected `discord.Client`;
`_resolve_text_channel` fallback path (`get_channel` returns `None`
→ `fetch_channel`); send to non-text channel raises; `start` without
token env var raises; `close` idempotency. 90% module coverage
(uncovered: the Gateway-bound `on_message` / `on_raw_reaction_add`
event shims marked `# pragma: no cover`, and the
`discord.DiscordException` re-raise wrappers that require contrived
mocks). Full suite **1067** passes (was 1031 at Stage 5.1 close,
+36). mypy clean across 65 src files. pylint **10.00/10**.

Running real-money cost unchanged at $0.08 (pure-transport stage; no
real-money operations, no Gateway connection in tests).

### Stage 5.1 — Operator Domain & Ports (2026-05-16)

First Phase 5 slice. Pure-domain — no I/O, no Discord, no LLM call,
no SQLite table. Establishes the type contracts every later stage
consumes. Four sub-slices:

**5.1.A — Operator types + port.** New `ports/operator.py` defines
the full operator-interaction type contract: `OperatorCommand` typed
sum (`PauseCommand` / `ResumeCommand` / `PauseAllCommand` /
`ResumeAllCommand` / `CancelOpenOrdersCommand` / `StopCommand`),
`OperatorQuery` typed sum (nine variants from `StatusQuery` through
`HelpQuery`), `OperatorIntent` outermost union (`IntentCommand` |
`IntentQuery` | `IntentConversational` | `IntentUnparseable`),
per-query `*Result` types with `QueryResult` discriminated union,
`CommandResult`, `PendingCommand` with the six-state lifecycle
(`awaiting_confirmation` → `approved` → `dispatched`, with
`rejected` / `expired` / `failed` terminals), `OperatorPort` ABC
with `dispatch_command` + `answer_query`. New `OperatorError` in
`ports/exceptions.py`. `SymbolInput` / `OptionalSymbolInput`
BeforeValidator helpers accept `"BTC/USD"` strings as well as
`{base, quote}` dicts so the LLM can emit either form. 117 new unit
tests, 100% module coverage on `ports/operator.py`.

**5.1.B — Assistant types + port.** New `ports/assistant.py` defines
the LLM-side contract: `SymbolStateSnapshot` + `EngineStateSnapshot`
(read-only view `cli/operator` composes per inbound message to ground
the assistant's replies), `ConversationTurn` (id / channel_id /
user_id / role / content / `intent: OperatorIntent | None` /
timestamp), `ConversationContext`
(`current_message` + `channel_id` / `user_id` +
`recent_turns: tuple[ConversationTurn, ...]` for the multi-turn
prompt window + `engine_state_snapshot`), `AssistantPort` ABC with
`parse_intent(context) -> OperatorIntent`. New `AssistantError` in
`ports/exceptions.py`. 25 new unit tests, 100% module coverage on
`ports/assistant.py`. Per ADR-013 the conversational LLM is NOT in
the money path — an `AssistantError` affects only the Discord chat
surface; `cli/live` cannot observe it.

**5.1.C — `sqlite_storage.py` split.** Pre-existing pylint flag
(file at 1073 lines, threshold 1000) surfaced during 5.1.A's lint
check. Split out two sibling modules without changing the public
`SQLiteStorageAdapter` interface or its tests:
`adapters/sqlite_storage_schema.py` holds the `SCHEMA` constant
(every `CREATE TABLE` / `CREATE INDEX` the adapter runs at first
connect); `adapters/sqlite_storage_rowmap.py` holds pure row-to-domain
mapping helpers (`row_to_order` / `row_to_trade` / `row_to_price_snapshot`
/ `row_to_news_item` / `row_to_advisor_suggestion` /
`row_to_applied_suggestion` / `row_to_transfer_proposal` /
`row_to_transfer_result` plus the MoE expert-opinion JSON
serialize / deserialize pair). Dropped leading underscores on the
moved names since they cross module boundaries now; updated every
callsite in `sqlite_storage.py` to match. Migration helper
`_migrate_advisor_suggestions_expert_opinions` stays inline (tightly
coupled to `connect()`'s schema bootstrap). Main module:
**1073 → 753 lines**. No behavior change; 1031 tests still pass.

**5.1.D — Stage close.** Roadmap ✅, CHANGELOG entry, CLAUDE.md
Project Status bump, `project_state` memory update.

**Health at Stage 5.1 close:** **1031 unit tests** pass (was 892 at
Phase 4 close, +139 across 5.1.A and 5.1.B); 21 integration tests
opt-in; mypy clean across **64 src files** (was 60; +2 new
`ports/` modules, +2 new `adapters/sqlite_storage_*` modules);
pylint **10.00/10** with **no outstanding warnings** (the
pre-existing `too-many-lines` flag on `sqlite_storage.py` is gone);
black + isort clean.

Running real-money cost unchanged at $0.08 (pure-domain stage; no
real-money operations).

### Stage 4.5 — Phase 4 Integration Check + Phase 4 Close (2026-05-15)

Stage 4.5 audited the full Phase 4 path with the question "could anything move money the operator didn't intend?" and found one real defect. Then wrote `docs/planning/phase-4-summary.md` mirroring `phase-3-summary.md`'s shape.

**Defect found and fixed**: `cli/harvest --execute` would have called `KrakenAdapter.withdraw()` on a `bank_to_exchange` proposal. Kraken's `/0/private/Withdraw` is exchange→bank only — deposits are operator-pushed from the bank side using deposit instructions from Kraken Pro. Calling withdraw with a deposit-direction proposal would have moved money in the wrong direction (or, more likely, Kraken would have refused with a confusing error).

Fix: new defense layer 3 in `_execute_command` refuses any proposal whose direction isn't `exchange_to_bank`, with an operator-facing message pointing them to Kraken Pro's deposit instructions. The gate now has **seven** defense layers (was six). Test added: `tests/cli/test_harvest.py::TestExecuteGuardrails::test_bank_to_exchange_refused_no_api_call` asserts `adapter.withdraw_calls == []` after refusal.

Other Phase 4 paths verified end-to-end during the audit (all read-only against the operator's real account):
- `cli/harvest` read $99.92 USD via the Harvester key + classified as deficit + `persistence_enabled: true` confirmed
- `tools/show_proposals.py` reports "no proposals match" against empty table
- `tools/show_transfers.py` reports "no results match" against empty table
- All 8 (now 9 with the new test) execute-gate guardrails verified by unit tests with `adapter.withdraw_calls == []` assertions

**Phase 4 total real-money cost: $0.00** (no live withdrawal during slice work). The operator's first $1 ACH to "360 Performance Savings" is a separately-tracked event. Project running total still $0.08 unchanged from Phase 2 close.

Phase 4 stages closed: 4.1, 4.2, 4.3, 4.4, 4.5. Phase 5 entry conditions met.

### Stage 4.4 — Active Mode (Guarded Withdrawals) (2026-05-15)

Phase 4's biggest slice. **Money can finally move** — but only when the operator explicitly says so, and only after six defense layers clear. Four sub-slices:

**4.4a — `KrakenAdapter.withdraw()` + Harvester key wiring.**
- Implemented `/0/private/Withdraw` against Kraken's signed API. Returns Kraken's `refid` (withdrawal reference) for forensic linking to Kraken Pro's Funding history.
- `HarvesterConfig` gained `api_key_env_var` / `api_secret_env_var` (configurable for testing; default `KRAKEN_HARVESTER_API_KEY` / `_SECRET`) and `withdrawal_destinations: dict[str, str]` (asset → Kraken Pro destination label; the API only accepts labels from the operator's pre-registered address book).
- `cli/harvest` switched to loading the Harvester key (Withdraw + Query Funds scopes).

**4.4b — TransferResult storage + day-cap from real history.**
- New `transfer_results` SQLite table (UNIQUE on `transaction_id`, CHECK on status + direction).
- `TransferResult` gained denormalized `direction` and `asset` fields so the day-cap query stays single-table.
- `services.harvester.compute_today_total_withdrawn_usd()` — rolling 24h sum of exchange→bank withdrawals (status != failed).
- `cli/harvest._run_cycle` now feeds the real total to `propose_transfer()`. Pre-4.4b was always `Decimal("0")` — the day-cap was effectively never enforced.

**4.4c — `cli/harvest --execute <proposal-id>` operator-approval gate.**
- Mirrors the `cli/apply --commit` pattern: explicit per-call flag, multi-layer validation, persists outcome regardless of success or failure.
- Defense chain (any failure aborts; `adapter.withdraw()` NEVER called):
  1. `HarvesterConfig.enabled=True` required.
  2. Proposal exists in harvest db.
  3. Proposal not stale (≤ `proposal_max_age_hours`, default 24h).
  4. Destination label resolves in `withdrawal_destinations`.
  5. Current balance ≥ proposal amount (exchange→bank only).
  6. Day-cap headroom: `today_total + proposal.amount ≤ max_withdrawal_per_day_usd`.
- After all six clear, calls withdraw. `TransferResult` with `status="pending"` on success (Kraken hasn't settled yet) + Kraken's real refid; `status="failed"` on Kraken refusal with a synthetic `failed-<uuid>` transaction_id.
- The "**WITHDRAWAL SUBMITTED — money moved**" log message is the only place in the codebase that admits real money has moved.

**4.4d — Inspection + close.**
- `tools/show_transfers.py` mirrors `tools/show_proposals.py` shape (`--since-hours` / `--status` / `--direction` / `--asset` / `--limit` / `--log-format`).

**No real withdrawal happened during the slice work** — every test uses a stub `withdraw()`. The first live execution is operator-triggered: $1 ACH against the "360 Performance Savings" destination once balance enters surplus band (currently $99.92 USD, in deficit; would need a deposit or threshold adjustment).

888 unit tests pass (was 853 at Stage 4.3 close, +35 across the four slices). mypy clean (60 src files); pylint 10.00/10. No new runtime deps. Running real-money cost still $0.08 (unchanged from Phase 2 close until the operator's first `--execute`).

### Stage 4.3 — Passive Transfer Proposals (persistence + inspection) (2026-05-15)

Phase 4's third slice. Every non-None proposal from `cli/harvest` now persists to a new `transfer_proposals` SQLite table for operator review. **No transfers** — that's 4.4's job once the operator can approve+execute through an explicit gate. Zero new real-money risk.

- **Domain**: `TransferProposal` gained `created_at: Timestamp`. `services.harvester.propose_transfer()` populates it.
- **Storage**: new `transfer_proposals` table with `UNIQUE(proposal_id)` guard, `CHECK` on direction, indexes on `(created_at)` and `(direction, created_at)`. `StoragePort.save_transfer_proposal` / `get_transfer_proposals` (filter by `since / direction / asset / limit`; DESC by `created_at`).
- **`HarvestConfig.db`**: new field (default `data/wobblebot-harvest.db`) following the per-CLI DB convention (advise.db, news.db, etc.).
- **`cli/harvest`**: persists every non-None proposal on every tick. Storage write failures log + continue (the daemon's main job is observation; one missed audit row is less bad than killing the loop). Session-start log gained `persistence_enabled: true|false`.
- **Persistence ≠ execution**: `HarvesterConfig.enabled` does NOT gate persistence — that flag will gate Stage 4.4 execution. Operators can calibrate thresholds against a real proposal stream before flipping enabled.
- **`tools/show_proposals.py`**: new inspector mirroring `tools/show_suggestions.py` shape (`--since-hours / --direction / --asset / --limit / --log-format`).

**Verified live** against the operator's real Kraken account: daemon read $99.92 USD → deficit band → no proposal → `transfer_proposals` empty → `tools/show_proposals.py` correctly reports "no transfer proposals match the filters". `persistence_enabled: true` confirmed in session-start log.

15 new tests (10 storage round-trip + filters + UNIQUE + CHECK + Decimal precision; 5 cli/harvest persistence happy-path + no-proposal-no-persist + enabled-independence + storage-failure-isolation). 853 total unit tests pass (+15 since Stage 4.2 close); mypy clean (60 src files); pylint 10.00/10. No new runtime deps.

### Stage 4.2 — cli/harvest Read-Only Balance Monitor (2026-05-15)

Phase 4's second slice. Polls Kraken USD balance, runs the Stage 4.1 `propose_transfer()` decision, logs what *would* be proposed. **No transfers, no DB writes** — zero new real-money risk over 4.1. Uses the existing read-only `KRAKEN_API_KEY`; the Harvester key with Withdraw scope isn't needed until Stage 4.4.

- **`HarvestConfig`** (per-CLI section): `log_format` only for now. Future stages may grow more knobs.
- **`schedules.harvest`**: new entry in the unified schedules block; defaults to `1h` in the example yml.
- **`cli/harvest._run_cycle`**: read balance → propose_transfer() → log. Returns `False` on a recoverable balance-read failure so the outer loop continues. Operator-facing band classification (`deficit / topup_band / hold_band / surplus`) included as a structured log field.
- Proposal log lines are tagged `"HYPOTHETICAL proposal (no money moved)"` so a glance at logs can't mistake them for real actions.
- Test stub's `ExchangePort.withdraw()` raises `NotImplementedError` with a `"Stage 4.2 must not call withdraw"` message — surfaces accidental cross-wiring as a hard test failure.

**Verified live** against the operator's real Kraken account: daemon read $99.92 USD (current state), correctly classified as `deficit` (below the $200 `min_exchange_liquidity_usd` threshold), logged "no proposal" with full band context. Below-floor is operator-only territory by design.

14 new tests. 838 total unit tests pass (+14 since Stage 4.1 close); mypy clean (60 src files); pylint 10.00/10. No new runtime deps.

### Stage 4.1 — Harvester Domain + Decision Logic (2026-05-15)

First Phase 4 slice. Pure-domain — no I/O, no Kraken calls, no withdrawals; **zero new real-money risk**.

- **`HarvesterConfig`** (`config/harvester.py`): four operator-tunable USD thresholds (`min_exchange_liquidity_usd / topup_threshold_usd / surplus_threshold_usd / max_withdrawal_per_day_usd`). Model validator enforces the `min < topup < surplus` ordering invariant at config-load. `enabled: bool = False` mirrors the auto-apply gate posture (ADR-012-style): operator opts in for anything that moves money.
- **`services/harvester.propose_transfer()`**: pure function taking `(balance_usd, config, today_total_withdrawn_usd)` and returning `TransferProposal | None` per four bands carved out by the thresholds:
  - **Deficit** (`< min`): no proposal — operator-only territory.
  - **Top-up band** (`min ≤ balance < topup`): propose `bank_to_exchange` to the midpoint of `(topup, surplus)`.
  - **Hold band** (`topup ≤ balance ≤ surplus`): no proposal.
  - **Surplus** (`> surplus`): propose `exchange_to_bank` scrape to the same midpoint.
- **Day-cap interaction**: proposals shrink to the remaining cap when `today_total_withdrawn_usd + desired_amount > max_withdrawal_per_day_usd`; cap exhausted returns `None`. Day-cap doesn't apply to deposits (inflows).
- Existing `HarvesterPort` interface (Phase 1.2) stays unchanged; 4.2+ adapter implementations will consume `propose_transfer()`.
- `settings.example.yml` harvester block reordered to match the new invariant and gained an operator-facing comment explaining the three bands.

24 new tests covering every band, every day-cap branch, config invariants, and proposal shape sanity. 824 total unit tests pass (+24 since Stage 3.6 close); mypy clean (59 src files); pylint 10.00/10. No new runtime deps.

### Stage 3.6 — Operational polish: indefinite runtime + multi-symbol advise (2026-05-15)

Two small slices to remove pre-Phase-4 operational friction.

**Slice 3.6a — indefinite runtime.**
- `LiveConfig.max_runtime_minutes` and `ShadowConfig.max_runtime_minutes` became `Optional[float]`. `None` means "no runtime cap." Pre-3.6a the field was `Field(default=60.0, gt=0)` and operators had to bump it to a sentinel like 525600 for "effectively forever" — `0` was rejected by Pydantic, and even if allowed the loop check `elapsed >= max_runtime_seconds` would have exited on tick 1.
- Loop logic in `cli/live._run_engine_loop` and `cli/shadow._run_loop` resolves `max_runtime_seconds` to `None` when configured and skips the per-tick comparison. SIGINT/SIGTERM, max_session_loss_usd, and the engine's safety caps still apply — this isn't a way to bypass safety.
- `settings.example.yml` comments flag `~null~` as the run-indefinitely value.

**Slice 3.6b — multi-symbol `cli/advise` with per-symbol-isolated LLM calls.**
- `AdviseConfig.symbol: Symbol` → `AdviseConfig.symbols: list[Symbol]`. CLI flag `--symbol` → `--symbols` (comma-separated, matching `cli/live`/`cli/shadow`/`cli/observe`).
- The daemon iterates serial per symbol within each tick: `for symbol in symbols: await _run_cycle(symbol=symbol)`. Each cycle builds a single-symbol `PerformanceSummary` so the LLM never sees more than one coin's context per call. Cross-contamination of opinions prevented by construction.
- Per-symbol cycle errors swallowed at the daemon layer (one bad coin can't kill the sweep) — matches `cli/live`'s Stage 2.4 discipline.
- `cli/apply` updated to filter `advisor_suggestions` by symbol — the multi-symbol advise daemon writes one row per coin per sweep, so a global "newest" pick could land on the wrong coin's row.
- **Verified live** against the operator's real advise.db: one sweep with `--symbols BTC/USD,ETH/USD` produced distinct recommendations per coin — BTC got `spacing 1.1 / order $12` (high confidence), ETH got `spacing 0.7 / order $15` (medium confidence). Different parameters AND different confidence levels prove per-symbol reasoning isolation end-to-end.

800 unit tests pass (was 792 at Phase 3 close, +8 across 3.6a's runtime tests and 3.6b's sweep tests). mypy clean (57 src files); pylint 10.00/10. No new runtime deps.

### Stage 3.5 — Phase 3 Integration Check + Phase 3 Close (2026-05-15)

End-to-end advisor-in-the-loop chain verified against live operator state, then Phase 3 closed.

**Chain verification:**
- **observe → metrics**: 6520 price snapshots accumulated by overnight `cli/observe` soak across BTC/USD + ETH/USD + DOGE/USD.
- **news → summary**: one `cli/news` poll cycle pulled 131 items (CoinDesk 25 + Decrypt 37 + The Block 19 + CryptoCompare 50; matches Stage 3.2.5 closing receipt to the row).
- **advise → suggestion**: one `cli/advise` cycle (39s wall-clock, phi4:14b-q8_0) produced `{spacing 1.1, levels±4}` with 20 news items in the summary's `recent_news`. Notable: same parameter recommendation as the previous cycle but `confidence` dropped from `high` (no news) to `medium` (news context present) — calibration shift even when proposed params hold.
- **apply → operator review**: `cli/apply` (dry-run) correctly rejected every key with reason "auto-apply disabled" — gate default-off posture holds end-to-end.

**Phase 3 close:**
- Closing summary at `docs/planning/phase-3-summary.md` (mirrors Phase 2's at `phase-2-summary.md`). Captures per-stage outcomes, MoE live verification numbers, design decisions ratified across the phase, health snapshot, what was deliberately not done, Phase 4 entry conditions.
- **Phase 3 real-money cost: $0.00** (advisor never executes per ADR-002). Running project total still **$0.08** unchanged from Phase 2 close.
- Phase 3 stages closed: 3.0, 3.1, 3.2, 3.2.5, 3.3, 3.4a, 3.4b, 3.5 (plus the config consolidation audit). Phase 4 entry conditions met.

### Stage 3.4b — Bounded Auto-Tuning Gate (2026-05-15)

Three-slice landing of the operator-in-the-loop apply surface. **Off by default** — `AutoApplyConfig.enabled=False` blanket-rejects every key, matching the conservative posture ADR-007 calls for. When the operator opts in, advisor suggestions can mutate the running grid within configured magnitude bounds. News-role suggestions never apply regardless of bounds.

- **Slice A — Auto-apply gate (pure service).** `services/auto_apply.py::evaluate_auto_apply(suggestion, current_grid, auto_apply_config, *, symbol) -> AutoApplyResult` decides what's eligible. Rules: `enabled=False` blanket-rejects; `role=="news"` blanket-rejects with the ADR-007 reason; whitelist for v1 is `spacing_percentage` + `order_size_usd` (level keys rejected with "no magnitude cap configured" until an operator extends `AutoApplyConfig`); `|delta|/current ≤ max_<key>_change_percentage / 100` with inclusive boundary. AutoApplyResult is a frozen Pydantic model carrying `enabled / role_eligible / symbol / applied_keys / rejected_keys / proposed_grid`. MoE-aggregated suggestions that contain a news opinion in `expert_opinions` still apply for whitelisted keys — the aggregated role IS the metrics-driven synthesis. 29 unit tests.
- **Slice B — `cli/apply` dry-run.** New module reads the latest (or `--recommendation-id`) AdvisorSuggestion from advise.db, runs it through the gate, and logs per-key APPLIED / REJECTED breakdowns with reasons. `--symbol` overrides advise.symbol so an operator with a BTC daemon can also evaluate the same suggestion against ETH's grid. Exit 2 on missing config sections / empty db / recommendation-id not found. 12 unit tests including the news-role safety endpoint.
- **Slice C — `--commit` + AppliedSuggestion audit + stage close.** Adds the `ruamel.yaml` runtime dep, `services/settings_rewriter.apply_grid_overrides()` (atomic .tmp + rename, comment-preserving round-trip, style-preserving integer/float, returns unified diff), `AppliedSuggestion` frozen domain model + `applied_suggestions` SQLite table + StoragePort methods. `cli/apply --commit` rewrites settings.yml AND persists an audit row in one logical operation; if the rewrite fails, no audit row writes. Stdouts the unified diff for operator review. 21 tests across rewriter + storage + cli wiring.

**Verified live**: `python -m wobblebot.cli.apply` against the operator's real `data/wobblebot-advise.db` correctly surfaced the latest BTC suggestion (phi4's `spacing 1.1 / levels±4`) and rejected all keys with reason "auto-apply disabled" — proving the gate's default-off posture holds end-to-end through the CLI.

792 unit tests pass (was 730 at Stage 3.4a close, +62 across the three 3.4b slices). mypy clean (57 src files); pylint 10.00/10. New runtime dep: `ruamel.yaml`.

### Stage 3.4a — Mixture of Experts (MoE) (2026-05-15)

Four-slice landing of the MoE advisor surface per ADR-007. Composes 2+ specialist `AdvisorPort` instances and aggregates their opinions via three strategies. Still advisory-only — Stage 3.4b's auto-apply gate is what eventually consumes these.

- **Slice A — Aggregator pure functions.** `services/aggregators.py` ships `aggregate_voting` (per-key strict majority; ties or no-consensus omit the key) and `aggregate_weighted_confidence` (per-key confidence-weighted average for numerics, weighted mode for categoricals). Confidence weights `high=3 / medium=2 / low=1`. Aggregated `role="aggregated"`. News-role opinions DO contribute to the math (the auto-apply exclusion lives in 3.4b's gate).
- **Slice B — `MoEAdvisorAdapter`.** Fans out to every expert via `asyncio.gather`; one vendor outage gets logged with structured fields and the MoE proceeds with the survivors. All-failed raises `AdvisorError`. Per-expert opinions ride on the aggregated recommendation via a new `AdvisorRecommendation.expert_opinions: list[AdvisorRecommendation]` field (recursive, enabled by `from __future__ import annotations`). The entry's `role` overrides whatever the LLM self-tagged. New `MoEExpertEntry` frozen dataclass wraps `(name, role, advisor)` — `AdvisorPort` stays the only abstraction; OllamaAdapter / future cloud adapters plug in directly.
- **Slice C — Arbitrator aggregator.** `aggregate_arbitrator` async function builds a JSON dump of the experts' opinions and feeds it to a separate arbitrator advisor as `extra_context`. OllamaAdapter gained an `extra_context: str = ""` kwarg (kept off `AdvisorPort` itself — a new `ArbitratorAdvisor` Protocol in `services/aggregators.py` formalizes the structural type). MoEAdvisorAdapter accepts an optional `arbitrator: MoEExpertEntry` required iff `aggregator="arbitrator"`, forbidden otherwise. The arbitrator's name shares the expert namespace (uniqueness enforced). If every expert fails, MoE raises before invoking the arbitrator.
- **Slice D — cli/advise MoE dispatch + audit persistence.** `cli/advise` now dispatches on `advisor.type=single` vs `advisor.type=moe`, building one OllamaAdapter per `ExpertConfig` and the arbitrator entry when configured. `advisor_suggestions.expert_opinions` column added (JSON array of `{role, confidence, recommendations, rationale}`); Stage 3.3 DBs upgrade in-place via a PRAGMA-check + `ALTER TABLE` in `connect()`. `model_name` persisted on the suggestion is a compact `moe[<aggregator>:<role>:<model>/...]` label. `tools/show_suggestions.py` gained an `experts=N[roles]` segment on the one-line summary. Cloud providers (anthropic / openai / google) raise at construction time with "not implemented" — they land later.

**Verified live end-to-end** against the operator's local Ollama lineup (phi4:14b-q8_0 quant, granite4.1:30b-q5_K_M risk, deepseek-r1:14b-qwen-distill-q8_0 news, phi4:14b-q8_0 arbitrator) via the new `tools/run_moe_check.py`:

- `--aggregator weighted_confidence`: 3 experts in 194s parallel dispatch. Quant: `spacing 1.1%, levels±4` (medium); risk: `spacing 1.2%, order_size $8` (high); news: `spacing 1.5%` (high, citing macro headlines). Aggregated: `spacing 1.29%, order_size $8, levels±4` (high confidence; weighted avg = 2.67).
- `--aggregator arbitrator`: 191s total. Same three experts; phi4 arbitrator synthesized `spacing 1.4%, order_size $9` (high) with the rationale: "Risk flagged drawdown approaching cap; quant agreed on tighter spacing. News context noted but not auto-applied per ADR-007." — the arbitrator even reasoned about news's auto-apply restriction.

730 unit tests pass (was 675 at Stage 3.3 close, +55 across the four 3.4a slices: 26 aggregator + 16 MoE adapter + 4 arbitrator-path + 3 storage round-trip/migration + 1 expert-opinions cycle + 5 cli/advise dispatch). mypy clean (54 src files); pylint 10.00/10.

### Stage 3.3 — Passive Advisory Workflow (2026-05-15)

Engine-decoupled advisor loop: `cli/advise` runs as a standalone daemon, periodically asks the configured LLM for a recommendation, and persists the result. **Nothing auto-applies** (ADR-002 + ADR-007). Operator reads with `tools/show_suggestions.py`.

- **Slice A — `AdvisorSuggestion` + storage.** New frozen domain model wraps an `AdvisorRecommendation` with audit context (`input_summary` as a forensic dict, `model_name` for provenance, `created_at`). New `advisor_suggestions` SQLite table; `StoragePort.save_advisor_suggestion` + `get_advisor_suggestions(since, model_name, role, limit)` DESC by created_at.
- **Slice B — `SummaryBuilder`.** Composes Stage 3.1 metrics + Stage 3.2.5 news + supplied grid config into a `PerformanceSummary`. New `NewsItemSummary` (narrowed `NewsItem` view — drops body / external_id / fetched_at) cuts the prompt-token cost of including news context by ~80%. Optional separate `news_storage` parameter lets the builder stitch prices from one DB and news from another.
- **Slice C.0 — Unified `schedules:` config.** Every periodic-task cadence moved to one top-level block in settings.yml. Duration strings (`30s` / `10m` / `4h` / `7d`); bare numbers parse as seconds; `0s` reserved for "disabled". Hard cutover — removed `observe.price_interval_seconds`, `observe.balance_interval_seconds`, `news.poll_interval_minutes`, `advisor.cadence_hours`. cli/observe and cli/news refactored to read from `schedules.*`.
- **Slice C — `cli/advise` daemon.** Long-running, mirrors cli/observe / cli/news shape. Three-DB design (read observe.db + news.db, write its own advise.db) keeps the per-CLI storage separation the project established earlier. Per-cycle fault isolation: advisor errors and storage errors are logged with structured fields and the loop continues. New `AdviseConfig` schema; cadence from `schedules.advise`.
- **Slice D — `tools/show_suggestions.py`.** Read-only operator inspection of recent suggestions. Filters by `--since-hours`, `--model`, `--role`, `--limit`.

**Verified live end-to-end:** `cli/advise` ran a real cycle against the operator's observe + news DBs → phi4:14b-q8_0 emitted a quant recommendation in ~50s (`spacing_percentage: 1.1`, `levels_above: 4`, `levels_below: 4`, confidence high) → persisted to `data/wobblebot-advise.db` → `tools/show_suggestions.py` printed it cleanly.

675 unit tests pass (was 619 at Stage 3.2.5 close, +56 across the four 3.3 slices including +21 for the schedules parser). mypy clean (52 src files); pylint 10.00/10.

Also bundled: Ollama Desktop update mid-stage retagged the local models with explicit quant suffixes (e.g. `phi4:14b` → `phi4:14b-q8_0`). Operator settings.yml updated; example yml already uses an explicit tag for clarity.

### Stage 3.2.5 — News Ingestion (2026-05-15)

Five-slice landing of news polling per ADR-007. **No LLM consumption yet** — Stage 3.4a's news expert is what reads from this. Persists items to a new `news_items` SQLite table with `UNIQUE(source, external_id)` dedup so re-polling across ticks is a no-op.

**Source pivot from ADR-007:** the original plan named CryptoPanic + Whale-alert; both moved to paid-only since the ADR was written (~$2,600/yr + ~$300/yr respectively). v1 pivots to **RSS + CryptoCompare** — all free. `NewsPort` stays abstract so paid sources can plug in later if you ever decide to.

- **Slice A — Domain + storage.** `NewsItem` frozen domain model (source, external_id, published_at, headline, body, sentiment_score, mentioned_coins, fetched_at). `NewsPort` ABC. New `news_items` table with `UNIQUE(source, external_id)`. `save_news_item` (idempotent via INSERT OR IGNORE) + `get_news_items(source, since, until, limit)` returning DESC by published_at.
- **Slice B — `RssNewsAdapter`.** One instance per feed. feedparser-based; httpx fetches the bytes with `follow_redirects=True` (the redirect handling caught CoinDesk during live verification). Mentioned-coin extraction via a whitelist regex over ten popular tickers (BTC/ETH/SOL/DOGE/ADA/XRP/DOT/MATIC/AVAX/LINK).
- **Slice C — `CryptoCompareAdapter`.** Polls `/data/v2/news/`. API key in the `authorization` header (never query string, to avoid upstream-log exposure). `sentiment_score: None` — CryptoCompare's upvotes/downvotes aren't a reliable sentiment signal; the news expert in Stage 3.4a derives tone from the body text. Mentioned coins extracted from the structured `categories` field, filtered to ticker-shaped tokens.
- **Slice D — `cli/news`.** Long-running daemon, same operational shape as `cli/observe`. Per-source fault isolation: one bad feed gets logged with structured fields and the loop continues with the rest. New `NewsConfig` + `RssFeedSpec` + `CryptoCompareSpec` schemas in `config/cli.py`.
- **Slice E — Example yml.** Default `news:` block with four RSS feeds (CoinDesk, Decrypt, The Block enabled; CoinTelegraph disabled as noisy) + CryptoCompare enabled. `CRYPTOCOMPARE_API_KEY` documented in `.env.example` with minimum-scope notes.

**Verified live in one poll across all four sources:** 25 + 37 + 19 + 50 = 131 fresh items into `wobblebot-news.db`. Per-source error isolation tested empirically (CoinDesk redirect failure on first try; rest of the loop continued).

619 unit tests pass (was 525 at Stage 3.2 close, +94); mypy clean (49 src files); pylint 10.00/10. New runtime dep: `feedparser`.

**90-day evaluation queued** (2026-08-13): CryptoCompare's source coverage substantially overlaps with RSS. Re-evaluate whether the additional aggregation earns its place vs. simply running more RSS feeds.

### Stage 3.2 — Advisor Port & Single-LLM Integration (2026-05-15)

Five-slice landing of the first LLM advisor surface. Single-LLM mode only — MoE arrives in Stage 3.4a. No new live-money risk (advisor cannot execute per ADR-002 + ADR-007).

- **Slice A — Schema reconcile.** `AdvisorRecommendation` now matches the wire format the prompt files already declared (`advisor_recommendation_v1`): `config_changes` → `recommendations`, `confidence: float` → `Literal['high','medium','low']`, new `role: str` field. `PerformanceSummary` extended with Phase 3.1 metrics (volatility, max_drawdown, flatness, latest_price, snapshot_count, lookback_hours) plus `CurrentGridParams` so recommendations can be delta-aware.
- **Slice B — OllamaAdapter.** New `adapters/ollama.py` implementing `AdvisorPort`. httpx-based with `MockTransport` test seam; transport, HTTP-status, JSON-parse, and Pydantic-validation failures all wrap as `AdvisorError`. Named `OllamaAdapter` per the `{Vendor}Adapter` convention (matches `KrakenAdapter`).
- **Slice C — Config single-mode.** `AdvisorConfig` gains `provider` / `model` / `prompt_file` / `inference_params` fields required when `type: single`. Example yml flips to `type: single` (Ollama + `quant.md`) as the Stage 3.2 default; the former MoE block moves to a `profiles.moe-advisor` profile alongside the existing `cloud-only-moe`.
- **Slice D — `tools/run_advisor.py`.** Reads observe DB + resolved config → builds PerformanceSummary via `services.metrics` → calls the configured advisor → prints + persists a JSONL receipt. Same pattern as `tools/first_real_trade.py` and `tools/show_metrics.py`.
- **Slice E — Thinking-model support.** R1-family / o1-style / "thinking" / "reasoning" / "thinker" models emit `<think>…</think>` reasoning before the answer; Ollama's `format: "json"` constraint forces the first token to start valid JSON, so they degenerate to `{}`. The adapter now name-detects thinking models, drops the format constraint for them, and walks the response with `json.JSONDecoder.raw_decode` to extract the last balanced `{…}` block. Robust to thinking preambles, code fences, illustrative JSON-shaped strings in the reasoning, and braces inside string literals.

523 unit tests pass (was 458 at Stage 3.1 close, +65); mypy clean (45 src files); pylint 10.00/10. `ports/advisor.py` and `adapters/ollama.py` both at 100% line coverage on the unit-test path.

Verified live against six local Ollama models (phi4:14b, qwq:32b, gemma3:27b, nous-hermes2-mixtral, mistral-nemo:12b, deepseek-r1:14b) on the same BTC/USD 6h window. Five working models converged on `spacing_percentage: 1.2` — striking agreement across genuinely different priors. Confidence calibration was the meaningful differentiator: phi4 / qwq / gemma3 reported `medium` (the honest answer given zero cycle history); mistral-nemo and nous-hermes2 reported `high` overconfidently. **phi4:14b set as the local default** based on this comparison — calibrated, fast (~27s), and the most accurate read of the metrics (correctly characterizing 0.044% per-period stdev as low volatility, where mistral-nemo got the direction wrong).

llama3.3:70b timed out at the default 60s — tunable, not a quality issue. Adding a configurable timeout is queued for whenever a 70B model becomes operationally interesting.

### Stage 3.1 — Data Collector & Metrics v2 (2026-05-15)

Four-slice landing of historical price reads + derived-metric math
on top of the price_snapshots tape that `cli/observe` has been
filling. Lands the read side of Phase 3 without touching the
advisor surface, so no new live-money risk.

- **Slice A — Storage read path.** `StoragePort.get_price_snapshots(symbol, start_time, end_time, limit)` with SQLiteStorageAdapter impl. New `PriceSnapshot` domain model (frozen, stays narrow — distinct from `MarketSnapshot` which is expected to grow). Reads return ASC by `observed_at` so callers can pipe directly into a chronological series.
- **Slice B — Pure-math metrics module.** New `services/metrics.py` exposes `compute_volatility` (sample stdev of simple returns), `compute_max_drawdown` (worst peak-to-trough fraction, ≤ 0), `compute_flatness` (1 − range/mean, clamped to [0, 1]), and `compute_cycle_stats` (FIFO per-symbol buy-then-sell matching → cycle_count / win_count / win_rate / total_pnl / avg_profit_per_cycle). No I/O, no port deps; deterministic golden-input tests.
- **Slice C — DataCollector v2 wiring.** `DataCollector(exchange, storage)` now exposes `get_price_history(symbol, lookback: timedelta)` plus windowed metric methods on `DataCollectorPort` (`get_volatility`, `get_max_drawdown`, `get_flatness`, `get_cycle_stats`). `CycleStats` moved from `services.metrics` to `domain.models` so the port can name it as a return type without closing a ports → services → adapters import cycle. `cli/status` updated to construct a `SQLiteStorageAdapter(":memory:")` to satisfy the now-required storage parameter.
- **Slice D — Inspection tool.** `tools/show_metrics.py` reads any wobblebot DB read-only, auto-discovers symbols from `price_snapshots`, and prints metrics per symbol over a configurable lookback. Safe to run against the live observe DB while `cli/observe` is polling.

458 unit tests pass (was 401 at Phase 2 close); mypy clean (44 src files); pylint 10.00/10. `services/metrics.py` and `services/data_collector.py` both at 100% line coverage.

Verified end-to-end against the live observe DB: 1383 snapshots/symbol over the past ~10h, BTC/USD vol=0.0364%, dd=−2.90%, flat=0.97; DOGE/USD vol=0.0847%, dd=−4.17%; ETH/USD vol=0.0490%, dd=−2.88%. Observer kept polling undisturbed across all four slice commits.

Also: Stage 5.3.5 (Background Maintenance Worker) added to the roadmap — `cli/maintenance --loop` covering periodic SQLite VACUUM, optional retention pruning, `TimedRotatingFileHandler` log output, and local + configurable-remote backups. Implementation deferred to Phase 5; slotted between 5.3 (Reliability) and 5.4 (Performance) before the v1.0 soak test.

### Post-audit infrastructure (2026-05-15)

Follow-up landed in the same window as the config consolidation
audit close. None of these change runtime behavior in a way that
affects live trading; all are operator-experience and project-
hygiene improvements.

- **User-facing docs refresh.** README rewritten to reflect current
  phase status and the full 7-CLI surface (which CLIs touch real
  money, which don't, what each is for); fixed placeholder clone
  URL; updated test commands to match the actual marker setup.
  SECURITY.md replaced GitHub's stock placeholder template with a
  real threat model + private-disclosure flow via GitHub Security
  Advisories. New CONTRIBUTING.md (lightweight; delegates to
  existing docs) and CODE_OF_CONDUCT.md (Contributor Covenant 2.1
  by reference). CHANGELOG moved from
  `docs/implementation/changelog.md` to repo-root `CHANGELOG.md`
  per Keep-a-Changelog convention. LICENSE copyright updated to
  `CarlDog`, year span `2025-2026`. GitHub repo description and
  10 discoverability topics set via the API.
- **Discord on the roadmap (ADR-pending).** Stage 5.1.5 added
  for Discord notifier (`NotifierPort` adapter at
  `src/wobblebot/adapters/discord_notifier.py`, outbound only,
  one-evening scope). Stage 5.2 expanded to cover bidirectional
  Discord control surface (slash commands, new `OperatorPort`).
  Stage 5.1 documents the web UI option's structural placement
  (`src/wobblebot/web/` as sibling of `src/wobblebot/cli/`, both
  presentation layers consuming existing ports).
- **Phase-end audit practice codified.** New global rule at
  `~/.claude/rules/phase-end-audit.md` defines per-phase /
  per-major-feature / quarterly / pre-1.0 audit cadences with
  process discipline (punch list first, fixes in separate commits
  per category, no scope creep into rewrites). Wobblebot's
  `CLAUDE.md` adds a project-specific extension covering all-CLI
  deprived-env walkthrough, schema-drift cleanliness, OC memory
  currency, and Phase 4 Harvester key scope verification when that
  phase lands.
- **Dependabot cleanup.** Removed the speculative
  `github-actions` ecosystem block from `.github/dependabot.yml`
  (no `.github/workflows/` exists yet, so GitHub's Dependency
  Graph was warning "Not all dependency manifest files were
  successfully processed"). Re-add when CI lands. Pip ecosystem
  unaffected — still 16 packages tracked, security alerts on,
  weekly Monday Python update PRs scheduled.
- **GitHub Sponsors + Ko-fi.** New `.github/FUNDING.yml` cloned
  from `openchronicle-mcp`'s setup. Enables the "Sponsor" button
  on the repo page.

### Phase 3 — Strategy Advisor & Analytics (in progress)

- **Stage 3.0 — Observer & Shadow Mode** (2026-05-14, ADR-008). Two
  non-money-touching entry points landed before advisor work begins:
  - `cli/observe` — pure data collection. Polls live Kraken Ticker
    on a configurable interval, persists prices + balance snapshots
    to a `price_snapshots` SQLite table. Read-only API key.
  - `cli/shadow` — shadow trading. Same engine code as `cli/live`
    but with a new `ShadowExchangeAdapter` that uses live Kraken for
    prices and matches orders against a synthetic balance ledger.
    Honest maker/taker fee modeling (default 0.26% / 0.40% — the
    rates Phase 2's first-trade receipt confirmed). Operator-supplied
    initial synthetic balances (no inference from real Kraken — the
    muscle-memory guard from ADR-008).
  - `cli/grid` renamed to `cli/live` to make the live-money
    distinction loud against the new `cli/shadow`.

#### Config consolidation audit (2026-05-14, ADR-009; eight slices, no live-money risk)

Pure infrastructure cleanup before Stage 3.1 to align the
operator-facing config story.

- **Slice 1.** `config/settings.example.yml` redesigned as the
  operator-facing API; ADR-009 ratifies the layering.
- **Slice 2.** Per-CLI Pydantic schemas — `LiveConfig`,
  `ShadowConfig`, `ObserveConfig`, `PreflightConfig`, `StatusConfig`,
  `SandboxConfig` — plus `AdvisorConfig` (with a ≥3-experts
  validator for MoE).
- **Slice 3.** Profile resolver with `deep_merge` semantics: dicts
  recurse, lists override entirely.
- **Slice 4.**
  - 4a — renamed `cli/simulate` → `cli/sandbox`,
    `cli/check` → `cli/status`, `cli/validate` → `cli/preflight` for
    operator clarity.
  - 4b — `wobblebot.config.runtime.load_resolved_config(...)` wired
    into `cli/live` as the YAML-loading pattern (base YAML →
    `--profile` deep-merge → CLI flag overrides).
  - 4c — same pattern wired into the remaining five CLIs. Profiles
    cover both `live` AND `shadow` so the same name (e.g.
    `conservative`, `aggressive`) is meaningful for any operational
    mode.
- **Slice 5.** Prompt-file infrastructure — new runtime dep
  `python-frontmatter`, four committed default prompts at
  `config/prompts/{quant,risk,news,arbitrator}.md`, loader at
  `wobblebot.config.prompts.load_prompt`. Skeletons; Stage 3.4a
  will wire the advisor to consume them.
- **Slice 6.** Schema-drift detection tests for both file pairs
  (`settings.example.yml` ↔ `settings.yml`, `.env.example` ↔
  `.env`). One-way default (operator stale keys fail; missing keys
  warn); `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` promotes warnings to
  hard failures for CI.
- **Slice 7.** `docker/env.example` moved to repo-root `.env.example`
  and refreshed for Phase 2.3 reality (`KRAKEN_TRADE_API_KEY`,
  cloud-LLM keys, harvester key for Phase 4).
- **Slice 8.** Docs + memory close.

#### Verifications (2026-05-14, post-audit)

- **Verification #24 — Deprived-env walkthrough.** Cycled all six
  CLIs through scenarios with no `.env`, no config, partial config,
  bad credentials, bad `--config` paths, bad `--profile` names.
  Surfaced and fixed two real defects:
  - SQLite-using CLIs crashed with raw 18-line traceback when
    `data/` directory didn't exist. Fixed: `SQLiteStorageAdapter.connect`
    now mkdir's the parent directory on demand. `:memory:` and
    empty-string paths pass through unchanged.
  - `load_dotenv()` walked UP from the package source location
    (python-dotenv default with `usecwd=False`), magically picking
    up the dev repo's `.env` from any cwd. Fixed: new
    `wobblebot.cli._common.load_operator_env()` helper composes
    `find_dotenv(usecwd=True)` with `load_dotenv(dotenv_path=...)`
    so discovery walks UP from the operator's cwd. All five
    env-using CLIs use the helper.
- **Verification #25 — PII scanner coverage.** Confirmed
  `.githooks/pre-commit` runs gitleaks + author-identity guard
  + PII pattern scan (Mac/Windows + Linux user-home paths +
  personal-email patterns). gitleaks against full git history (80
  commits): clean. Tracked-files PII sweep: zero hits. Working-tree
  leaks confined to operator's gitignored `.env`. Added missing
  `*.pfx`, `*.p12`, `*.pem` patterns to `.gitignore` per
  security.md spec. Repo is publication-ready from a PII/secret
  standpoint.

### Phase 2 — Core Trading Engine (closed 2026-05-14)

Total real-money cost across two live verifications: **$0.08**.
Closing summary at [`docs/planning/phase-2-summary.md`](docs/planning/phase-2-summary.md).

- **Stage 2.1 — Kraken Adapter (read-only).** DIY HMAC-SHA512
  signing on `httpx` (rejected `python-kraken-sdk`). `BalanceEx` not
  `Balance` (returns `hold_trade` per asset). Asset/symbol aliasing
  in the adapter via module-level `_INTERNAL_TO_KRAKEN_ALTNAME`
  + lazy `/0/public/Assets` cache. `pytest -m 'not integration'` is
  the default; live integration tests opt-in. `.env` loaded
  session-wide via `python-dotenv` in `tests/conftest.py`.
- **Stage 2.2 — Micro-Grid Engine** (ADR-006). Five slices: config
  schemas (`GridConfig`, `SafetyConfig`, YAML loader); pure grid
  math (`compute_grid_levels`, `next_counter_action`, `is_offside`);
  `GridEngine` service with `GridState` persistence; safety cap
  enforcement (per-coin / total exposure + daily-spend); end-to-end
  integration test (1000-tick oscillation, 500 cycles, positive
  realized P&L). Six ratified design decisions in ADR-006. Counter
  orders match filled-order base amounts.
- **Stage 2.3 — Live Paper / Tiny-Size Mode.**
  `KrakenAdapter(dry_run=True)` adds `validate=true` to every
  AddOrder request (auth + pair + precision + balance + ordermin
  + costmin validation without placing). Per-pair quantization
  mandatory; price/volume rounded DOWN before submission. Two
  separate Kraken keys (read-only + trade) live side-by-side in
  `.env`. Live taker fee is 0.40%, not the mock's 0.26% — discovered
  during the first-trade test. `cli/preflight` and `cli/live`
  shipped. Verified live: $0.08 round-trip on the operator's
  account, 148ms fill latency, perfect cleanup.
- **Stage 2.4 — Multi-Asset Support.** `cli/live` takes
  `--symbols` comma-separated. Each tick steps every symbol in
  series. Per-symbol step errors swallowed at the CLI layer (one
  bad coin can't kill the session). Caps split: `total` and `daily`
  are global across symbols; `per-coin` and `max_orders_per_coin`
  scoped per symbol. Five new multi-coin engine tests; engine
  layer required ZERO changes (every per-coin entity already keys
  by symbol).
- **Stage 2.5 — Phase 2 Integration Check.** Live multi-coin grid
  run for 5 minutes against the operator's account; 54 ticks per
  coin, 0 fills (price stayed within 1% of init reference for both
  BTC and ETH the entire window), session PnL $0.0000, all 6 open
  orders cleanly cancelled on runtime-cap shutdown. The
  `InsufficientBalance`-as-refusal fix was load-bearing — pre-fix
  the engine would have crashed at tick 1 because the account holds
  zero base inventory.

### Phase 1 — Foundation & Sandbox (closed 2026-05-13)

- **Stage 1.1 — Repo & Scaffolding.** `pyproject.toml`, dev tooling
  (black/isort/mypy/pytest), VS Code workspace.
- **Stage 1.2 — Hex Core Skeleton.** Domain models (`Order`,
  `Trade`, `Balance`) and value objects (`Symbol`, `Price`, `Amount`,
  `OrderSide`, `Timestamp`); six abstract ports (`ExchangePort`,
  `StoragePort`, `AdvisorPort`, `HarvesterPort`, `NotifierPort`,
  `DataCollectorPort`); ADR-005 alignment with Kraken vocabulary.
- **Stage 1.3 — Storage & Logging Backbone.**
  `SQLiteStorageAdapter` via `aiosqlite` (Decimal-as-TEXT precision,
  transaction rollback on partial-write failure, dual-ID UPSERT on
  `orders`, append-only balance-snapshot history). `configure_logging`
  in `wobblebot.config.logging` — stdlib-only, idempotent,
  plain/JSON switchable via `WOBBLEBOT_LOG_LEVEL` /
  `WOBBLEBOT_LOG_FORMAT`. Pre-commit hook with gitleaks + PII
  pattern check + author-identity guard. Port exception hierarchy
  in `ports/exceptions.py`.
- **Stage 1.4 — Kraken Mock & Simulation Mode.**
  `MockExchangeAdapter` with limit-order matching, configurable fee
  model (default 0.26%), scenario playback, balance tracking with
  locked-funds reservation. 23 unit tests.
- **Stage 1.5 — Phase 1 Integration Check.**
  `wobblebot.services.simulator.run_buy_dip_sell_rebound_cycle`
  wires `ExchangePort` + `StoragePort` to execute a hard-coded
  buy-low / sell-high cycle against a scripted price walk.
  `python -m wobblebot.cli.sandbox` is the operator-facing entry
  point. **Phase 1 complete.**

### Notable cross-cutting changes

- Domain exception signatures take `Decimal` (was `float`),
  preventing precision loss in balance violation reports.
- `Order.mark_closed` replaced by `Order.record_fill(cumulative_amount)`
  — partial fills correctly keep `status='open'` until full fill;
  matches Kraken `vol_exec` semantics.
- `Timestamp` normalizes any tz-aware input to UTC.
- `Balance` is an immutable point-in-time snapshot (`frozen=True`).
- `OrderSide` is a `StrEnum` (was a Pydantic wrapper).
- `ExchangePort.get_balance(asset)` returns `Balance | None` —
  distinguishes never-held from held-but-zero.
- Pydantic mypy plugin enabled in `pyproject.toml` (load-bearing).

## [v1.0.0] — TBD

Per the [roadmap](docs/planning/roadmap.md), v1.0.0 lands at the end
of Phase 5 with: micro-grid trading engine, Kraken adapter (live),
multi-asset support, Strategy Advisor (single-LLM and MoE) with
guarded auto-tuning, Harvester with passive and active withdrawal
modes, centralized Orchestrator, Data Collector v2, observability
layer (structured logging, metrics, dashboard), Docker Compose
deployment, and complete documentation.

### Known limitations planned for v1.0.0

- Restart / reconciliation logic is basic; manual checks required
  after restarts until Phase 5 introduces robust reconciliation.
- Advisor JSON schema is draft; future schema versions may be
  incompatible with earlier ones.
- Automated bank deposits (bank → Kraken) are not supported in
  v1.0.0 — only Kraken → bank withdrawals via the Harvester (per
  ADR-004).
