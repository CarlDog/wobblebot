# Changelog

All notable changes to WobbleBot are documented in this file. Format
is a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [SemVer](https://semver.org/spec/v2.0.0.html).
Pre-v1.0.0, all entries land under `[Unreleased]` until a tagged
release exists; per-stage receipts in
[`docs/planning/roadmap.md`](docs/planning/roadmap.md) carry the
canonical completion dates.

## [Unreleased]

### Stage 6.4 — Google Gemini adapter (2026-05-17)

Third and final cloud provider; closes the per-provider work
ahead of Stage 6.5's integration check. Two sub-slices (down from
three for the previous stages — the shared helper extracted in
Stage 6.3.A has paid off enough that wiring + close fits in one
slice).

**6.4.A — Google advisor + assistant adapters.** New
`adapters/google.py` with both `GoogleAdvisorAdapter` (AdvisorPort)
and `GoogleAssistantAdapter` (AssistantPort) sharing all the
Gemini-specific helpers in one module. API target is Google
Generative AI REST (`generativelanguage.googleapis.com`); Vertex
AI is out of scope (avoids the OAuth + GCP-project ceremony for
a hobby-tier bot).

Provider-specific helpers:
- `extract_google_tokens` — the simplest reasoning-token
  normalization of the three Phase 6 providers. Gemini reports
  `thoughtsTokenCount` separately from `candidatesTokenCount` and
  these are **additive natively** — no subtraction needed (unlike
  OpenAI which had to subtract from completion, unlike Anthropic
  which lumps inside output_tokens). The extractor records both
  as-is.
- `parse_candidate_text` — concatenates `text` parts from
  `candidates[0].content.parts`, filtering non-text parts
  (inlineData / executableCode / etc.).
- `post_generate_content` — POST to
  `/v1beta/models/{model}:generateContent` with `x-goog-api-key`
  header (the v1beta-preferred shape; cleaner than the `?key=`
  query-string fallback). Model id is embedded in the URL path,
  not in the body.
- `_build_generate_body` — composes the Gemini-shaped body:
  `systemInstruction.parts` (separate top-level field, NOT a
  message in `contents`), `contents` array of role+parts dicts,
  `generationConfig` for temperature + maxOutputTokens.
- `_user_part` + `_model_part` — note that Gemini uses role=`model`
  (NOT `assistant`) for assistant turns. The assistant adapter
  maps operator→user / assistant→model on the wire.

24 new unit tests focused on the Google-specific bits:
- Pure helpers: cost ceiling math vs gemini-2.5-pro pricing;
  token extraction across no-thinking / additive-thinking /
  zero-thinking / empty-usage / missing-responseId;
  parse_candidate_text basic + multiple-parts + non-text-parts
  filter + empty.
- Wire shape: x-goog-api-key header + URL endpoint with model
  embedded; systemInstruction separate from contents; user-vs-model
  role mapping verified explicitly.
- Advisor happy path: round-trip records cost (gemini-2.5-pro);
  additive thinking tokens (100 visible + 300 thoughts both
  recorded; cost uses the gemini-2.5-flash explicit thinking-rate
  override from llm_pricing — $3.50/1M for thoughts vs $2.50/1M
  for regular output); prose-wrapping JSON.
- Advisor failures: 403 wraps as AdvisorError with http_403;
  empty candidates raises.
- Assistant: command + query intents round-trip; non-operator
  prompt rejected; empty api_key rejected; cost-cap trips before
  call.
- Construction guards.

**6.4.B — CLI dispatch wiring + Stage 6.4 close.**
`cli/advise._build_advisor_adapter` adds the `google` branch with
`GOOGLE_API_KEY` env-var validation; `cli/operator._build_assistant`
does the same. `AssistantLLMConfig.provider` Literal closes with
all four providers (`ollama`, `anthropic`, `openai`, `google`).
`_UNIMPLEMENTED_PROVIDERS` is now empty — the only error path
left in the dispatcher is "missing `llm:` block" for cloud
providers. Test refactor:
`test_unimplemented_cloud_provider_rejected` becomes
`test_google_without_cloud_wiring_rejected` since the
"not implemented" surface no longer exists.

**1455 unit tests** pass (up from 1431 at Stage 6.3 close; +24 across
Stage 6.4's two sub-slices). mypy clean (79 src files). pylint
10.00/10. black + isort clean. **No new runtime dependencies** —
Google adapter is pure httpx + pydantic. Phase 6 real-money cost
still **$0.00** (Stage 6.5 is the first real API call); running
project total **$0.08** unchanged.

All three Phase 6 cloud providers now ship. Each adapter file
lands at ~530-580 lines including both Advisor + Assistant
implementations + provider helpers — the shared
`execute_cloud_call` orchestrator carries the cost-flow weight.
Stage 6.5 (Phase 6 integration check + first real API calls)
remains.

### Stage 6.3 — OpenAI adapter + shared cloud-call helper (2026-05-17)

Second cloud provider lands plus an extracted shared orchestrator
so Stages 6.4 (Google) and any future cloud provider reuse the
ADR-014/015 flow instead of re-implementing it. Three sub-slices:

**6.3.A — Shared cloud-call helper + refactor Anthropic.** New
`services/llm_cloud_call.py`:
- `CloudCallContext` frozen dataclass bundles storage +
  session_tracker + cost_config + retry_config + role + provider +
  model (the per-adapter identity).
- `classify_error(exc) -> str` pure function promoted out of the
  Anthropic adapters where it was duplicated.
- `execute_cloud_call(ctx, estimated_cost_usd, call_fn,
  extract_tokens)` runs the full ADR-014/015 sequence: check_budget
  → retry_with_backoff(call_fn) → on success build+persist
  LLMCallRecord from extracted tokens + update tracker → on failure
  build+persist failure record with classified error_kind + re-raise.
  Provider-specific shape lives in two closures: `call_fn`
  (zero-arg async returning the parsed envelope) and
  `extract_tokens` (envelope → (in, out, reasoning, request_id)
  tuple).

Anthropic adapters refactored to use the helper — each
`get_recommendation` / `parse_intent` shrinks ~80 lines of
cost-flow boilerplate to ~30 lines of provider-specific body
building + a single `execute_cloud_call` call. New module-level
`extract_anthropic_tokens` carries the Anthropic-specific
normalization (tokens_reasoning=None because the API lumps thinking
with output). Zero behavior change — all 39 Anthropic tests stay
green.

21 new helper tests covering: classify_error matrix (parametrized
5xx + 4xx codes + every transient httpx type + ValueError fallback),
happy path (record persisted with real tokens + cost + tracker
updated; reasoning tokens flow through the extractor), cost gate
(daily + session trips before the call), failure path (permanent
4xx + retry exhaustion + connect error all record failure with
classified error_kind + re-raise).

**6.3.B — OpenAI advisor + assistant adapters.** New
`adapters/openai.py` with both `OpenAIAdvisorAdapter` (AdvisorPort)
and `OpenAIAssistantAdapter` (AssistantPort). Provider-specific
helpers:
- `is_reasoning_model` — name-pattern detection (`o1`, `o3` prefixes).
  Drops `temperature` from the request body for reasoning models;
  always uses `max_completion_tokens` for forward-compat.
- `extract_openai_tokens` — the meaningful provider-specific
  normalization. OpenAI's o-series returns `completion_tokens` that
  INCLUDES reasoning, with `completion_tokens_details.reasoning_tokens`
  reporting the subset. To satisfy the
  `tokens_reasoning is additive to tokens_out` convention, the
  extractor subtracts reasoning from completion. Cost math via
  `cost_for()` applies output rate to both — matching how OpenAI
  bills o-series.
- `parse_message_content` — pulls assistant text from
  `choices[0].message.content`, handling both the string shape and
  the multimodal list-of-parts shape.
- `post_chat_completion` — `Authorization: Bearer <key>` (not
  Anthropic's `x-api-key`) plus optional `OpenAI-Organization`
  header.

Both adapters ~530 lines total. 31 new unit tests covering pure
helpers + wire shape + advisor happy path + reasoning-token
recording + parse failures + assistant intent variants + multi-turn
ordering + construction guards + cost-cap trip.

**6.3.C — CLI dispatch wiring + stage close.**
`cli/advise._build_advisor_adapter` adds `openai` branch with
`OPENAI_API_KEY` + optional `OPENAI_ORGANIZATION` env-var reads.
`cli/operator._build_assistant` does the same.
`AssistantLLMConfig.provider` Literal extends from
`["ollama", "anthropic"]` to
`["ollama", "anthropic", "openai"]`. `_UNIMPLEMENTED_PROVIDERS`
shrinks to `("google",)`. `.env.example` documents the optional
`OPENAI_ORGANIZATION` env var. Test refactor:
`test_unimplemented_cloud_provider_rejected` switched from `openai`
(now implemented) to `google`.

**1431 unit tests** pass (up from 1379 at Stage 6.2 close; +52 across
Stage 6.3's three sub-slices — 21 helper + 31 OpenAI). mypy clean
(78 src files). pylint 10.00/10. black + isort clean. **No new
runtime dependencies** — OpenAI adapter is pure httpx + pydantic
on existing dependencies. Phase 6 real-money cost still **$0.00**
(Stage 6.5 is the first real API call); running project total
**$0.08** unchanged from Phase 2 close.

### Stage 6.2 — Anthropic adapter (2026-05-17)

First real cloud-provider adapter under Phase 6. Both
`AnthropicAdvisorAdapter` (AdvisorPort) and `AnthropicAssistantAdapter`
(AssistantPort) ship with the full ADR-014 cost-tracking flow
internalized: estimate → `check_budget` → `retry_with_backoff` (per
ADR-015) → persist `LLMCallRecord` → update `SessionCostTracker`.
No real API call yet — Stage 6.5 is the first.

Three sub-slices, each landed in its own commit:

**6.2.A — Anthropic shared client + AdvisorAdapter.** New
`adapters/anthropic.py` carrying the shared Messages-API helpers
(`estimate_cost_ceiling`, `parse_text_blocks`, `build_call_record`,
`post_messages`) plus `AnthropicAdvisorAdapter`. Constructor takes
storage + session_tracker + cost_config + retry_config alongside
the usual model/prompt/role; `get_recommendation` runs the full
flow inline. Anthropic thinking tokens recorded as
`tokens_reasoning=None` (the API lumps them with `output_tokens` +
bills at output rate; cost is correct via the pricing fallback).
Reuses `extract_last_json_object` from `adapters/ollama`
(module-public since Stage 5.3). New `SessionCostTracker` mutable
class in `services/llm_cost_gate.py` — one per CLI process
lifetime, shared across every adapter the CLI builds. 32 new
unit tests covering pure helpers + happy paths + cost gate
(daily + session caps, dry-run posture) + retry/backoff (5xx +
429 transient, 4xx permanent, exhaustion propagates
`LLMRetryExhausted`) + parse failures + construction guards.

**6.2.B — AnthropicAssistantAdapter.** New
`adapters/anthropic_assistant.py` implementing `AssistantPort`.
System prompt = operator prompt body + engine state snapshot;
recent turns mapped operator→user / assistant→assistant; current
operator message as final user turn. Same cost-tracking flow as
the advisor adapter, role=operator on every LLMCallRecord.
Module-level `TypeAdapter[OperatorIntent]` for the two-level
discriminator resolution. Constructor refuses non-operator-role
prompts + empty api_key. 17 new unit tests covering every
OperatorIntent variant + wire-shape verification + cost-tracking
+ retry + parse failures.

**6.2.C — CLI dispatch wiring + stage close.**
`cli/advise._build_ollama_advisor` → `_build_advisor_adapter`
with provider dispatch (`ollama` / `anthropic`; `openai` and
`google` still raise "not implemented"). New `_CloudWiring`
frozen dataclass bundles storage + tracker + LLMConfig and
threads through `_build_advisor` + `_build_expert_entry` +
`_build_arbitrator_entry`. `_main_async` opens an extra
operator.db storage when `config.llm` is set; errors at startup
if `config.llm` is set without `config.operator`. `cli/operator`
gains `_build_assistant` helper dispatching on
`OperatorConfig.assistant.provider`. `AssistantLLMConfig.provider`
Literal extends from `["ollama"]` to `["ollama", "anthropic"]`.
Test refactor: `test_unimplemented_cloud_provider_rejected`
switched from `anthropic` (now implemented) to `openai`; new
sibling test `test_anthropic_without_cloud_wiring_rejected`
verifies the clear error message when an `llm:` block is missing.

**1379 unit tests** pass (up from 1334 at Stage 6.1 close; +45 across
Stages 6.2's three sub-slices — 32 advisor + 17 assistant + 5
SessionCostTracker, with -9 from refactor/dedup). mypy clean (76
src files). pylint 10.00/10. black + isort clean. **No new
runtime dependencies** — Anthropic adapter is pure httpx +
pydantic. Phase 6 real-money cost still **$0.00** (Stage 6.5 is
the first real API call); running project total **$0.08**
unchanged from Phase 2 close.

### Stage 6.1 — Shared cloud-LLM infrastructure (2026-05-17)

First Phase 6 implementation stage; pure foundation with **zero real
API calls**. Lays down the substrate every cloud-provider adapter
(Stages 6.2-6.4) will consume: cost accounting, budget enforcement,
retry/backoff, per-provider config schemas, and operator inspection.
Five sub-slices, each landed in its own commit:

**6.1.A — Cost-tracking domain + storage.** `LLMCallRecord` frozen
Pydantic value object (caller-minted UUID id + timestamp + 7-way role
Literal + 3-way provider Literal + tokens triple [in/out/reasoning] +
Decimal cost_usd + request_id + success + error_kind). `llm_calls`
SQLite table + three indexes (timestamp / provider+model / role).
`StoragePort.save_llm_call` + `get_llm_calls(since, role, provider,
limit)` with newest-first ordering. `LLMCostCapExceeded` domain
exception carrying budget state for self-explanatory operator
notifications. 33 new unit tests. Drive-by: fixed pre-existing
`implicit-str-concat` in `sqlite_storage.get_conversation_turns`;
file-level `# pylint: disable=too-many-lines` on sqlite_storage.py
(now 1037 lines; adapter is naturally many-methods).

**6.1.B — Pricing table + cost gate.** `services/llm_pricing.py`
with the 8 in-scope models (Claude Sonnet 4.6 + Opus 4.7, gpt-4o +
gpt-4o-mini + o1 + o3-mini, gemini-2.5-pro + gemini-2.5-flash), each
entry comment-annotated with the provider's pricing-page URL and a
`verified_date`. `cost_for()` applies (input + output + reasoning)
rates with reasoning falling back to output rate unless overridden
(Gemini-flash thinking carries an explicit higher rate). Unknown
(provider, model) raises `PricingLookupError` — silent zero would
defeat ADR-014. `services/llm_cost_gate.py` with `LLMCostConfig`
(defaults $1.00/day + $0.50/session + `enforce=True`) and
`check_budget(storage, role, estimated_cost_usd, session_spent_usd,
config)` returning `GateAllow | GateDeny`. Session cap checked first
(in-memory, no DB round-trip); daily cap uses sliding 24h window
via `storage.get_llm_calls(since=now-24h)`. `enforce=False`
short-circuits to allow (ADR-014 decision 8 dry-run posture).
`test_pricing_freshness.py` watchdog fails CI when any entry's
`verified_date` is >180 days behind today. 38 new unit tests.

**6.1.C — Retry/backoff helper.** `services/llm_retry.py` with
`LLMRetryConfig` (max_retries=3, initial_backoff_seconds=1.0,
backoff_multiplier=2.0, all frozen + validated). `default_classifier`
per ADR-015: httpx Connect/Read/Write/Pool/RemoteProtocol → transient;
HTTPStatusError 429+5xx → transient, other 4xx → permanent; everything
else permanent (don't retry bugs). `retry_with_backoff(fn, config, *,
classifier, sleep_fn)` runs `fn` up to 1+max_retries times, sleeps
between attempts with `initial * multiplier ** attempt`, re-raises
permanent immediately, raises `LLMRetryExhausted` chaining `__cause__`
when transient retries exhaust. `sleep_fn` injection keeps tests
millisecond-fast. 36 new unit tests.

**6.1.D — Config schemas + env wiring.** `config/llm.py` with
`LLMConfig` composing `cost: LLMCostConfig` + `retry: LLMRetryConfig`
(both children carry their own defaults). `WobbleBotConfig.llm:
LLMConfig | None = None` (None = pure-Ollama deployment, gate
inactive — opt-in posture matching ADR-012's `auto_apply.enabled`
default). `.env.example` cloud-LLM-keys comment block refreshed for
Phase 6 + ADR-014/015 framing alongside the existing Phase 3 MoE
framing. `config/settings.example.yml` gains a documented `llm:`
block between `operator:` and `profiles:` with comments explaining
the dry-run posture and retry-defaults formula. Existing
schema-drift tests guard example/operator alignment automatically.
13 new unit tests.

**6.1.E — Inspection tool + stage close.** `tools/show_llm_costs.py`
operator inspection (`--db-path`, `--since-hours`, `--provider`,
`--role`, `--limit`, mutex `--by-provider | --by-role`,
`--log-format`). Default mode: per-row print + grand-total footer.
Rollup modes sort desc by cost. Deprived-env walkthrough green:
missing DB → exit 2; empty table → exit 0 + "no rows match"; seeded
rows → properly formatted output; mutex flags enforced by argparse.
Roadmap / CLAUDE.md / project_state memory updated.

**1334 unit tests** pass (up from 1214 at Phase 5 close; +120 across
Stage 6.1's five sub-slices). mypy clean (74 src files). pylint
10.00/10. black + isort clean. No new runtime dependencies — pricing
table is data, everything else is pure Python on existing httpx +
pydantic. Real-money cost still **$0.00 for Phase 6** (Stages 6.2-6.5
are the first to make actual API calls); running project total
**$0.08** unchanged from Phase 2 close.

### Phase 6 kickoff — Cloud LLM Integration (ADR-014 + ADR-015) (2026-05-17)

After Phase 5 close + the Phase 8.0 refactor slot decision, Phase 6
(Cloud LLM Integration) needed two architectural decisions ratified
before code, mirroring the Phase 5 kickoff pattern (ADR-013 +
`stage-5.1-design.md`).

**ADR-014 — LLM cost caps.** Per-day + per-session USD caps via
`services/llm_cost_gate.check_budget` against a new `llm_calls`
SQLite table in `operator.db`. Hard-stop on cap trip (raises
`LLMCostCapExceeded`). Single-pool across roles in v1; per-role
split deferred. Pricing table is **code, not config** — entries
carry `verified_date` + comment-annotated pricing-page URLs; a
`test_pricing_freshness` watchdog fails CI when entries are >180
days old. `enforce=False` dry-run posture for the first week of
cloud usage.

**ADR-015 — Cloud LLM provider failover policy.** Default policy:
fail loudly + retry on transient errors only. Transient = HTTP 429 /
5xx + httpx connection/timeout exceptions. Permanent = HTTP 4xx
(non-429) + every other exception class. Up to 3 retries with
exponential backoff (1s, 2s, 4s by default formula
`initial * multiplier ** attempt`). **No cross-provider failover.**
**No silent cloud-to-Ollama failover** — silent model substitution
breaks audit provenance. Retries draw from the same ADR-014 cost
pool (one budget check per logical call, not per attempt).
Per-provider auth lives in env (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
/ `GOOGLE_API_KEY`). Single shared `LLMRetryConfig` across providers
in v1.

**Stage 6.1 design + roadmap rewrite.**
`docs/planning/stage-6.1-design.md` slices Stage 6.1 into five
sub-slices. Roadmap drops the "provisional" tag from Phase 6 and
expands the five stages (6.1 infrastructure → 6.2 Anthropic → 6.3
OpenAI → 6.4 Google → 6.5 integration check). CLAUDE.md Project
Status moves Phase 6 from "Next:" to "in progress 2026-05-17."

No code in the kickoff commit. Stage 6.1 sub-slice work follows.

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

### Stage 5.7 — Phase 5 Integration Check + Phase 5 Close (2026-05-16)

Seventh and final Phase 5 slice. Closes Phase 5 with TTL expirer +
end-to-end integration test + the per-precedent phase summary
document. Three sub-slices:

**5.7.A+B (bundled — small enough to land together).**

  **TTL expirer for pending_commands.** cli/operator gains a third
  background asyncio.Task (alongside the notification forwarder and
  Gateway client). The expirer scans pending_commands WHERE
  status='awaiting_confirmation' AND ttl_expires_at < now every
  ttl_expirer_poll_seconds (default 30s) and transitions matches to
  'expired'. Per ADR-013 decision 3 the operator's ✅/❌ reaction is
  the ONLY way out of awaiting_confirmation, so without TTL expiry
  abandoned commands accumulate forever. OperatorConfig gains
  ttl_expirer_poll_seconds: float = 30.0 (positive).

  **End-to-end integration test suite.**
  tests/integration/test_phase5_operator_e2e.py exercises the full
  operator-interaction round-trip without a real Discord Gateway,
  Ollama LLM, or Kraken exchange — the test stubs the LLM and the
  Discord transport but uses real SQLite + real GridEngine + real
  OperatorService + real cli/operator handler functions + real
  cli/live poll helper.

  Five scenarios covered:
  - test_full_pause_round_trip: "pause BTC" → confirm embed → ✅ →
    cli/live picks up approved command → engine actually pauses →
    row marked dispatched with success.
  - test_reject_flow_does_not_dispatch: ❌ reaction → marked
    rejected → cli/live's poll skips it → engine never pauses.
  - test_multi_turn_conversation_records_history: two operator
    messages → 4 conversation_turns; second invocation's context
    sees the first turn pair.
  - test_notification_persisted_and_forwarded: SqliteNotifierAdapter
    writes → forwarder reads + posts embed + marks forwarded.
  - test_ttl_expiry_skipped_by_dispatch: expired commands never
    dispatch even when cli/live polls.

  5 + 5 new tests (5 unit for ttl_expirer + 5 integration for the
  e2e suite). cli/operator module docstring trimmed to keep the
  file under pylint's 1000-line cap (was 1006 after the expirer
  addition; now 990).

**5.7.C — Phase 5 close.** New
docs/planning/phase-5-summary.md (~200 lines) consolidates:
- Per-stage outcomes table for all seven stages + kickoff.
- The Phase 5 reframe story (originally seven small stages →
  one cohesive interaction-engine phase mid-kickoff).
- ADR-013 commitments × shipped reality (all 10 ratified
  commitments held intact).
- v1 limitation flagged for the future: cli/operator's stub
  engine can't see cli/live's in-memory pause state, so
  StatusQuery reports all symbols as 'active'. Fix path
  documented (~50-line slice persisting pause state to a shared
  SQLite table). Probably Phase 8 hardening.
- Test + code health delta (792 → 1214 unit tests, +422; 60 → 69
  src modules; pylint 10.00/10 cleared the pre-existing
  too-many-lines flag on sqlite_storage.py mid-stage).
- Real-money cost ledger: Phase 5 added $0.00; running total
  unchanged at $0.08.
- Entry conditions for Phase 6 (Cloud LLM Integration): the
  AssistantPort is provider-neutral by construction;
  AssistantLLMConfig.provider extends from Literal["ollama"] to
  include the cloud providers; no new SQLite tables needed for the
  cloud adapters themselves, though Phase 6 likely adds an llm_calls
  cost-tracking table.

**Health at Phase 5 close:** 1214 unit tests pass (was 792 at
Phase 4 close, +422 across Phase 5's seven stages); 26 integration
tests opt-in (was 21, +5 from the e2e suite); mypy clean across 69
src files (was 60, +9 new modules); pylint **10.00/10** with no
outstanding warnings; black + isort clean. New runtime dep:
`discord.py>=2.3,<3` (gated under operator interaction).

**Phase 5 closing summary at `docs/planning/phase-5-summary.md`.**
Mirrors phase-2/3/4 precedent.

**Phase 5 total real-money cost: $0.00.** Every test stubs Discord
/ Ollama / Kraken; the live verification "real operator types in
real Discord" is operator-driven and tracked separately.

Running project real-money cost still **$0.08** unchanged from
Phase 2 close.

Phase 5 stages closed: 5.1 / 5.2 / 5.3 / 5.4 / 5.5 / 5.6 / 5.7.
Phase 6 entry conditions met.

### Stage 5.6 — cli/operator Daemon (2026-05-16)

Sixth Phase 5 slice. The long-running CLI that ties together every
piece Phase 5 has shipped — DiscordTransport (5.2) +
OllamaAssistantAdapter (5.3) + OperatorService (5.4) +
SqliteNotifierAdapter (5.5). Four sub-slices + close:

**5.6.A — conversation_turns table + StoragePort.** Third Phase 5
SQLite table: id PK (UUID), channel_id, user_id, role (CHECK in
operator/assistant), content, intent_json (nullable; populated
for parsed operator turns), timestamp. Two indexes — composite
(channel_id, user_id, timestamp) for the prompt-assembly scope
read and (timestamp) for forensic queries. Three new StoragePort
methods: save_conversation_turn (upsert via ON CONFLICT DO UPDATE
so the typical save-on-receipt + re-save-with-intent flow works
without losing the row), get_conversation_turns(channel_id,
user_id, limit) (returns chronologically; when limit is set the
adapter fetches newest-N via DESC+LIMIT then reverses in Python).
row_to_conversation_turn uses a new module-level
TypeAdapter[OperatorIntent] for discriminator rebuild. 10 new
unit tests covering round-trip (operator with intent / assistant
without), nested IntentCommand preservation, scope isolation,
chronological ordering, limit returning newest-N-chronologically,
CHECK rejects unknown role, upsert replaces content + intent.

**5.6.B — OperatorConfig schema.** Three new Pydantic models in
config/cli.py:
- AssistantLLMConfig — provider (ollama for Phase 5), model,
  prompt_file (default config/prompts/operator.md), base_url,
  temperature (0.3 default per the operator.md hint), max_tokens
  (512), timeout_seconds.
- OperatorAuthConfig — bot_token_env_var (default
  DISCORD_BOT_TOKEN), allowed_user_ids, allowed_channel_ids (both
  frozenset, deny-by-default per ADR-013 decision 6),
  outbound_channel_id (where confirm embeds + forwarded
  notifications go; daemon validates at startup that it's in
  allowed_channel_ids).
- OperatorConfig composing both + operator_db (the daemon's own
  pending_commands/notifications/conversation_turns DB) + optional
  live_db/advise_db/news_db/harvest_db for cross-database queries
  + the ADR-013 knobs (context_window_turns 10 capped 1-50,
  confirm_ttl_seconds 300, forwarder_poll_seconds 2.0).
WobbleBotConfig gains operator: OperatorConfig | None = None. 18
new unit tests across defaults, required fields, bounds (temp 0-2,
context window 1-50, positive TTL + poll), frozenness.

**5.6.C — cli/operator daemon.** New cli/operator entry point with
three concurrent concerns:

  Notification forwarder (background asyncio.Task):
  _forwarder_loop polls notifications WHERE forwarded=0 every
  forwarder_poll_seconds, posts each as a color-coded Discord embed,
  marks forwarded on success. Per-row failures logged + batch
  continues — losing one forward beats stopping the daemon.

  Conversation flow (Discord on_message handler):
  _handle_inbound_message persists the operator turn, composes a
  ConversationContext (current message + recent N turns from
  storage + engine state snapshot from live_storage), calls
  AssistantPort.parse_intent, re-saves the turn with parsed
  intent, routes via match/case:
  - IntentCommand → write PendingCommand (awaiting_confirmation)
    + post confirm embed; record message_id → pending_id in
    in-memory map for the reaction handler
  - IntentQuery → OperatorService.answer_query + post result embed
  - IntentConversational → post reply_text as plain message
  - IntentUnparseable → surface "I couldn't parse that: <reason>"

  Confirmation flow (Discord on_raw_reaction_add handler):
  _handle_reaction looks up the in-memory map; on hit fetches the
  pending row and transitions awaiting_confirmation → approved
  (✅) or rejected (❌) with the confirming user_id + timestamp.
  Already-transitioned rows ignored (idempotency vs duplicate
  reactions). action='remove' ignored (we only care about adds).

Per ADR-013 decision 3 cli/operator NEVER calls
OperatorService.dispatch_command directly — every state mutation
crosses pending_commands so cli/live's WHERE status='approved'
poll (Stage 5.4's ADR-002 firewall) is the only path from intent
to engine.

_main_async wires storage + assistant + stub OperatorService +
DiscordTransport + the forwarder Task + SIGINT/SIGTERM handlers.
discord.py's Client.start() runs the Gateway connection until
transport.close().

v1 limitation documented in code: cli/operator's stub engine
can't see cli/live's in-memory pause state, so StatusQuery
reports all symbols as 'active'. Persisting pause state to
storage is a future-stage enhancement.

14 new unit tests for the testable seams (helper functions called
directly with synthetic InboundMessage/ReactionEvent): summarizer
output, forwarder happy path + empty + per-row failure isolation,
message routing through each IntentVariant, reaction confirm /
reject / unknown-id / double-reaction-no-overwrite /
remove-action-ignored.

**5.6.D — tools/show_pending.py + close.** Operator inspection
script in the show_*.py family pattern. Args: --db-path (default
data/wobblebot-operator.db), --status (filter to one of the six
lifecycle states), --limit (default 20), --log-format. Safe
against the live operator DB while cli/operator is running —
SQLite handles concurrent readers; no write surface.

**Health at Stage 5.6 close:** 1209 unit tests pass (was 1167 at
Stage 5.5 close, +42 across the four sub-slices); 21 integration
tests opt-in; mypy clean across 69 src files; pylint **10.00/10**
with no outstanding warnings; black + isort clean.

Running real-money cost unchanged at $0.08. cli/operator and
cli/live haven't been wired end-to-end against the operator's real
Discord + Kraken yet — that's Stage 5.7's integration check.

### Stage 5.5 — Outbound Notifications (2026-05-16)

Fifth Phase 5 slice. Lands the persistence + wiring for outbound
notifications from cli/live and cli/harvest. cli/operator (Stage 5.6)
will forward these rows to Discord; until then they accumulate in
SQLite for operator inspection. Two sub-slices + close:

**5.5.A — notifications table + SqliteNotifierAdapter.** New
`notifications` SQLite table (id PK, level CHECK against the
NotifierPort vocabulary, title + message + timestamp + context_json,
forwarded flag + forwarded_at + created_at; two indexes — forwarded
+ created_at for cli/operator's poll, timestamp for forensic
queries). New `PersistedNotification` value object in
`ports/notifier.py` wraps a raw `Notification` with row-level
fields. Three new `StoragePort` methods: `save_notification` (returns
the assigned row id), `get_notifications(forwarded=..., limit=...)`
(ordered by created_at ASC so cli/operator forwards the oldest
unforwarded event first), and `mark_notification_forwarded`
(idempotent UPDATE; raises StorageError if row not found).
`adapters/sqlite_notifier.py` — thin SqliteNotifierAdapter wrapping
any StoragePort. `send_notification` calls
`storage.save_notification` and wraps StorageError as NotifierError.
`send_error_alert` synthesizes a critical Notification from the
exception (type name as title, str(exc) or repr(exc) as message,
operator-supplied context dict). 14 new unit tests.

**5.5.B — cli/live + cli/harvest notification wiring.** Both CLIs
gain an `operator_db: str | None = None` config field; when set
they open a second SQLiteStorageAdapter and wrap it with
SqliteNotifierAdapter. Both CLIs gain a local `_notify(notifier, ...)`
helper that swallows NotifierError / WobbleBotPortError so a broken
notifier can NEVER break the engine loop — Phase 5 treats
notifications as forensic ledger entries; losing one beats stopping
trading.

  cli/live emit points:
  - **session start** (info): symbols / tick_seconds / caps / starting_usd
  - **per-tick fills** (info): when `StepResult.fills > 0`, one
    notification per (symbol, tick) pair with fills + counters_placed
    counts
  - **cap trip** (error): right before _run_one_tick returns True
    on session-loss-cap path
  - **session end** (info or error depending on exit_code): ticks /
    duration / starting+ending USD / PnL / cancellation counts

  cli/harvest emit points:
  - **proposal generated** (info): every non-None TransferProposal
    in _run_cycle includes proposal_id / direction / asset / amount /
    rationale; message text hints "Run cli/harvest --execute <id>
    to act"
  - **withdrawal failed** (error): when Kraken /Withdraw rejects,
    paired with the failed TransferResult audit row
  - **withdrawal executed** (warning, not info — money moved, the
    operator wants it surfaced loudly): refid + destination +
    pending status

8 new unit tests (3 cli/live + 5 cli/harvest) covering the helper's
no-op-on-None behavior, persistence via SqliteNotifierAdapter, error
swallowing when the notifier raises, _run_cycle emitting on proposal
generation, and _run_cycle staying silent in the hold band.

Full suite **1167** passes (was 1145 at Stage 5.4 close, +22). mypy
clean across 68 src files. pylint **10.00/10** with no outstanding
warnings. black + isort clean.

Per ADR-013 decision 9 neither cli/live nor cli/harvest imports
discord.py — they write to the notifications SQLite table only.
cli/operator (Stage 5.6) is the only module that will ever read
those rows and post them to Discord. Running real-money cost
unchanged at $0.08 (notifications are forensic only; no
state-mutating side effects).

### Stage 5.4 — Engine Integration (2026-05-16)

Fourth Phase 5 slice. The first stage where Phase 5 code actually
touches money-adjacent state. Four sub-slices land the four pieces
the operator interaction layer needs to reach the engine:

**5.4.A — GridEngine operator-control methods.** Engine gains
`pause_symbol(symbol)` / `resume_symbol(symbol)` / `is_paused` /
`paused_symbols`, `request_stop()` / `is_stop_requested`, and
`cancel_open_orders(symbol | None) -> (cancelled, failed)`. New
`StepAction` value `"skipped_paused"` — paused symbols return without
touching exchange or storage. Pause state is per-session in-memory
(rebuild on restart) by design. Cancel reads the open-order set from
the exchange (authoritative per ADR-006 decision 3); per-order
failures are logged and counted without aborting the batch.

**5.4.B — `pending_commands` SQLite table + StoragePort.** New table
in `sqlite_storage_schema.SCHEMA` with id PK, command_kind
denormalized for filtering, command_json + result_json for
schema-evolution headroom, the full six-state CHECK constraint on
status (`awaiting_confirmation` → `approved` → `dispatched` with
`rejected` / `expired` / `failed` terminals), three indexes (status
poll, created_at, TTL cleanup). `StoragePort` gains
`save_pending_command` (upsert via `ON CONFLICT DO UPDATE`),
`get_pending_command(id)`, `get_pending_commands(status, limit)` —
ordered by `created_at` ASC so the polling cli/live picks up the
longest-waiting approval first. `row_to_pending_command` in
`sqlite_storage_rowmap.py` uses a module-level
`TypeAdapter[OperatorCommand]` to resolve the discriminated union on
read.

**5.4.C — OperatorService.** `services/operator_service.py`
implements `OperatorPort` via match/case dispatch. Six commands
(`PauseCommand` / `ResumeCommand` / `PauseAllCommand` /
`ResumeAllCommand` / `CancelOpenOrdersCommand` / `StopCommand`) call
through to the engine and return `CommandResult` with `success` /
`side_effects` reflecting state changes. Nine queries
(`StatusQuery` / `OpenOrdersQuery` / `RecentFillsQuery` /
`RecentSuggestionsQuery` / `RecentNewsQuery` /
`HarvesterStatusQuery` / `RecentProposalsQuery` / `GridConfigQuery` /
`HelpQuery`) compose typed `*Result`s from storage + engine state.
Cross-database queries (advisor suggestions, news, harvester
proposals) take **optional** `advise_storage` / `news_storage` /
`harvest_storage` constructor params; when unwired the corresponding
queries return empty result lists rather than raising. Domain misses
encode as structured `success=False` or empty-list results; protocol
failures wrap as `OperatorError`. `HelpResult` static catalog of 15
entries matches the operator prompt's command + query catalog.

**5.4.D — cli/live poll integration.** `LiveConfig` gains optional
`operator_db: str | None = None`. When set, `cli/live` opens a
second `SQLiteStorageAdapter` (kept independent from live.db per the
per-CLI DB pattern), constructs `OperatorService` with the engine +
live storage + active symbols + grid config + session-start
timestamp, and drains approved pending commands via the new
`_process_pending_commands` helper. **The `WHERE status='approved'`
filter on the SELECT is the literal confirm-before-execute gate** —
the ADR-002 firewall that ADR-013 documents. Per-row dispatch
failures wrap as `failed` `CommandResult`s without aborting the
loop. `engine.is_stop_requested` is checked after the poll so a
`StopCommand` processed this tick exits the loop cleanly without
one more engine step. When `operator_db` is None, cli/live behaves
exactly as before — Discord-ignorant, no operator integration.

**5.4.E — Stage close.** Roadmap ✅, CHANGELOG, CLAUDE.md Project
Status bump, project_state memory update.

57 new unit tests (14 + 10 + 25 + 8 across the four sub-slices).
Full suite **1145** passes (was 1088 at Stage 5.3 close). mypy
clean across 67 src files. pylint **10.00/10** with no outstanding
warnings. black + isort clean.

Running real-money cost unchanged at $0.08 — the new code paths
require an operator-confirmed `pending_commands` row, and no such
row has been written outside test fixtures. Stage 5.6's
`cli/operator` daemon brings the Discord side online; until then
the firewall is entirely operator-pen-and-paper.

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
