# Stage 6.1 — Shared Cloud-LLM Infrastructure: Design and Slicing

*Drafted 2026-05-17 alongside ADR-014 + ADR-015 at the kickoff of Phase 6, before any 6.1 code was written. Living document — actual slicing may adjust during implementation, but the principles below are load-bearing and should not be relitigated without an ADR.*

## What Stage 6.1 delivers

The shared substrate every Phase 6 cloud-provider adapter consumes: cost accounting, budget enforcement, retry/backoff machinery, per-provider config schemas, and the inspection tool the operator uses to audit LLM spend. No provider adapter yet — those land in 6.2 (Anthropic), 6.3 (OpenAI), 6.4 (Google). The deliverable is foundation code with zero real API calls.

At the end of Stage 6.1:

- A new `llm_calls` SQLite table in `operator.db` records every cloud-LLM call (success or failure) with token counts + USD cost + provider correlation id.
- `services/llm_pricing.py` carries a static price-per-million-tokens table for all in-scope models across all three planned providers, each entry comment-annotated with the pricing-page URL + the date the price was verified.
- `services/llm_cost_gate.py` exposes `check_budget(role, estimated_cost_usd) -> Allow | Deny(reason)` that reads recent `llm_calls` rows and decides allow/deny against `LLMCostConfig`'s daily + session caps.
- `services/llm_retry.py` exposes an `async retry_with_backoff(callable, config)` helper that classifies responses as transient/permanent and retries per ADR-015.
- `LLMCostConfig` + `LLMRetryConfig` Pydantic schemas land in `config/`; `WobbleBotConfig` gains an `llm: LLMConfig | None` block composing both.
- Per-provider auth env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) documented in `.env.example`; schema-drift tests verify alignment.
- `LLMCostCapExceeded` and `LLMRetryExhausted` domain exceptions in `domain/exceptions.py`.
- `tools/show_llm_costs.py` operator inspection mirrors `tools/show_proposals.py` / `tools/show_pending.py` style.
- Unit tests cover construction, gate decisions (under-cap, at-cap, over-cap, multi-day window), retry classification (429 / 5xx / 4xx / network), backoff timing, pricing-table completeness, JSON round-trip for all new types.

The stage closes once mypy + pylint 10.00/10 + black + isort + pytest are all clean and the unit count has grown by ~80–100.

## Critical separation: Stage 6.1 ≠ Stages 6.2–6.5

Stage 6.1 produces **infrastructure types and services only**. Stage 6.2 introduces the first actual cloud-provider adapter (Anthropic). Stage 6.3 adds OpenAI. Stage 6.4 adds Google. Stage 6.5 is the real-API smoke test.

**Do not conflate them.** If a Stage 6.1 PR touches `adapters/` for anthropic / openai / google or calls a real cloud endpoint, something is wrong. The single allowed adapter-layer touch is the existing `OllamaAdapter` if test fixtures need refactoring — but even that should land in its own commit.

## What's already in place

- **`AdvisorPort`** (Phase 3.2) — provider-agnostic interface. Phase 6 adapters implement it without port-shape changes.
- **`AssistantPort`** (Phase 5.1) — provider-agnostic by construction; same property.
- **`OllamaAdapter` + `OllamaAssistantAdapter`** (Phase 3.2 + Phase 5.3) — reference implementations. Phase 6 cloud adapters borrow the prompt-loading, `is_thinking_model`, `extract_last_json_object` helpers (already module-public per the Stage 5.3 audit-driven extraction).
- **`AdvisorConfig.LLMProvider = Literal["ollama", "anthropic", "openai", "google"]`** — the type union is in place since Phase 3; `_build_advisor` currently raises `ValueError("provider=… not implemented yet")` for the three cloud providers. Phase 6 fills these placeholders.
- **`_build_ollama_advisor` factory in `cli/advise.py`** — current shape is provider-checking, will refactor at Stage 6.2 to dispatch by provider.
- **`AssistantLLMConfig.provider: Literal["ollama"]`** in `config/cli.py` — Phase 6 extends to the four-way union.
- **`operator.db`** — Phase 5's operator-state database. The new `llm_calls` table belongs here per ADR-014 decision 5.
- **Schema-drift tests** in `tests/config/test_schema_drift.py` keep `.env.example` ↔ `.env` and `settings.example.yml` ↔ `settings.yml` aligned; Phase 6's new env vars + config block get the same enforcement.
- **`tools/show_*.py`** pattern (Stage 4.4d + 5.6.D) — operator inspection tooling convention.
- **Port-layer exception base `WobbleBotPortError`** — `LLMCostCapExceeded` extends it.

## Proposed slicing

| Slice | Scope | Estimated size |
|-------|-------|----------------|
| **6.1.A — Cost-tracking domain + storage** | `domain/llm_cost.py`: `LLMCallRecord` Pydantic value object (frozen) with fields per ADR-014 decision 5. SQLite migration adds `llm_calls` table to the `SqliteStorageAdapter` schema (in `adapters/sqlite_storage_schema.py`). `StoragePort.save_llm_call(record) -> UUID` + `StoragePort.get_llm_calls(since, role=None, provider=None, limit=None) -> list[LLMCallRecord]` added to the port. Tests: model construction + JSON round-trip; save/get round-trip; filter combinations; index-backed query latency on a 10k-row table is bounded (smoke). | ~2 hours |
| **6.1.B — Pricing table + cost gate** | `services/llm_pricing.py`: Pydantic `LLMPricePoint` model + module-level `_PRICING: dict[tuple[Provider, str], LLMPricePoint]`. Models in scope: Claude Sonnet 4.6 + Opus 4.7 (Anthropic), gpt-4o + gpt-4o-mini + o1 + o3-mini (OpenAI), gemini-2.5-pro + gemini-2.5-flash (Google). Each entry has `verified_date` + comment-annotated pricing URL. Public function `cost_for(provider, model, tokens_in, tokens_out, tokens_reasoning=0) -> Decimal`. `services/llm_cost_gate.py`: `LLMCostConfig` (Pydantic, frozen): `max_spend_per_day_usd: Decimal`, `max_spend_per_session_usd: Decimal`, `enforce: bool = True`. `check_budget(storage, role, estimated_cost_usd, session_total_usd, config) -> Allow \| Deny(reason: str)`. Tests: pricing table completeness (every provider+model combo Stage 6.2-6.4 will reference); cost computation across token mixes; gate allow/deny across cap states (under, at, over, dry-run with enforce=False, sliding 24h window edge). | ~2 hours |
| **6.1.C — Retry/backoff helper** | `services/llm_retry.py`: `LLMRetryConfig` Pydantic (frozen): `max_retries: int = 3`, `initial_backoff_seconds: float = 1.0`, `backoff_multiplier: float = 2.0`. `async retry_with_backoff(callable, config, classifier=default_classifier) -> result`. Default classifier handles `httpx` responses (429/5xx → transient, other 4xx → permanent) + standard `httpx.ConnectError` / `httpx.ReadTimeout` (transient). Raises `LLMRetryExhausted` after `max_retries`. Tests: classification matrix; backoff timing within tolerance; exhaustion path raises with original-error chain preserved; classifier extension point works. | ~1.5 hours |
| **6.1.D — Config schemas + env wiring** | `config/llm.py` (new module): `LLMConfig` Pydantic (frozen) composing `cost: LLMCostConfig` + `retry: LLMRetryConfig`. `WobbleBotConfig` gains `llm: LLMConfig \| None = None` (None = pure-Ollama deployment, gate inactive). `.env.example` gains commented placeholders for the three provider keys + a callout note about provider precedence. `config/settings.example.yml` gains a top-level `llm:` block with defaults from `LLMCostConfig` / `LLMRetryConfig`. Schema-drift tests verify alignment. Tests: round-trip through `load_resolved_config` with and without `llm:` block; `None` means "no gate"; profiles can override caps. | ~1 hour |
| **6.1.E — Inspection tool + stage close** | `tools/show_llm_costs.py`: read-only operator inspection. `--since 24h` / `--by-provider` / `--by-role` / `--log-format json` flags. Mirrors `tools/show_pending.py` shape. Deprived-env walkthrough (missing operator.db, missing llm_calls table — graceful exit code 2). `docs/planning/roadmap.md` ✅, `CLAUDE.md` Project Status bump, `CHANGELOG.md` entry, `project_state` memory updated, `MEMORY.md` index touched if needed. mypy + pylint 10.00/10 + black/isort/pytest all clean. | ~1 hour |

**Total: ~7–8 hours of focused implementation.** Two-evening stage. The deliberate boundary keeps Stage 6.1 a "no real API call" foundation against which Stages 6.2–6.4 land cleanly.

## Design decisions to ratify

ADR-014 + ADR-015 ratify the architecture; the items below are *implementation-level* decisions that should land at the start of Slice 6.1.A and stay stable through the stage.

### 1. Cost record uses `tokens_reasoning` as a separate nullable column

**Decision:** The `llm_calls` table has three token columns: `tokens_in`, `tokens_out`, `tokens_reasoning` (the last nullable, only populated for thinking-mode responses on Anthropic Claude / OpenAI o-series / Google Gemini-thinking). Cost computation accepts reasoning tokens as a third parameter, defaulting to zero.

**Why:** Reasoning tokens are billed separately by every provider that exposes them, often at a premium (OpenAI o1 reasoning is the same as output rate; Anthropic thinking is billed as output). Lumping them into `tokens_out` works for cost math but obscures the "this turn ran 50k reasoning tokens" signal that drives operator decisions about which models to keep using.

**Alternative considered:** Single `tokens_out` column with all output (regular + reasoning) summed. Rejected because operators can't see "this $0.30 turn was 80% thinking" from billable totals alone.

### 2. Cost gate's sliding window is 24 wall-clock hours, not "today"

**Decision:** `check_budget` computes the daily total as `sum(cost_usd for calls in last 24 hours)`, not "calls since midnight UTC."

**Why:** The midnight reset has a known failure mode: a burst at 23:55 + a burst at 00:05 reads as two separate days but is functionally one expensive hour. The sliding window matches operator intuition ("don't spend more than $1 in any 24-hour stretch") and the implementation is trivially the same query with a `WHERE timestamp > NOW() - INTERVAL 24 HOUR` predicate.

**Alternative considered:** Calendar-day reset. Rejected for the burst-around-midnight failure mode.

### 3. Session total tracked in-memory by the CLI, not derived from the table

**Decision:** Each CLI tracks its own running session total in an in-memory `Decimal` accumulator passed to `check_budget`. The gate adds the estimated next-call cost to this total before deciding.

**Why:** A "session" is a CLI-process scope. Reading the table to derive it would require either a `session_id` column (and a mechanism to mint one) or a heuristic (calls from the last N minutes). In-memory is simpler, restart-resetting is the expected semantic ("a fresh `cli/advise` invocation is a fresh session"), and the table still has all rows for offline audit.

**Alternative considered:** Add `session_id` UUID column to `llm_calls` with each CLI minting one at startup. Rejected as ceremony with no current consumer; can be added later if a multi-CLI dashboard wants it.

### 4. The cost gate is called BEFORE the API request, with an estimated cost

**Decision:** Gate check happens before sending the request. The estimate uses `len(prompt) / 4` as a tokens-in approximation (the 4 chars/token rule of thumb), plus the model's `max_tokens` ceiling for tokens-out (conservative — assumes worst case). Final actual cost is recorded after the call.

**Why:** Checking after the call defeats the purpose — money is already spent. The estimate's conservatism is the right default: if the budget can't absorb the worst-case call, refuse. Real calls are typically much cheaper than the ceiling.

**Alternative considered:** Two-phase check (rough estimate before, exact after, refund on failure). Rejected as overengineered; the conservative ceiling captures the essential safety property.

### 5. `LLMConfig` is optional on `WobbleBotConfig` (`None` = no cloud usage)

**Decision:** `WobbleBotConfig.llm: LLMConfig | None = None`. When None, the gate is uncallable (CLI layer skips construction); existing Ollama-only deployments require zero config changes.

**Why:** Backwards-compatibility for Phase 5 deployments. Phase 6 isn't a forcing function — an operator running pure Ollama doesn't need to think about cost caps. Setting `llm:` in `settings.yml` is the opt-in signal that cloud usage is now in scope.

**Alternative considered:** Required `LLMConfig` with default values. Rejected — clutters the config surface for operators who'll never use cloud.

### 6. Pricing table verified-date discipline

**Decision:** Every `LLMPricePoint` in `services/llm_pricing.py` has a `verified_date: date` field. A unit test (`test_pricing_freshness.py`) fails when any entry's `verified_date` is more than 180 days behind `date.today()`. The test message points operators at the pricing-page URLs in the source comments to re-verify and bump the date.

**Why:** Pricing tables silently rot. A test that fails forces a periodic refresh decision — re-verify (commit a `verified_date` bump) or accept stale (commit a config to suppress). Either outcome is a documented choice; silence isn't.

**Alternative considered:** Manual quarterly audit item only (no automated test). Rejected — falls off the schedule in practice.

### 7. Retry classification has an extension point but ships with one classifier

**Decision:** `retry_with_backoff(..., classifier=default_classifier)` accepts a `classifier: Callable[[Exception | httpx.Response], RetryClass]` parameter. Stage 6.1 ships `default_classifier` only. Provider-specific classifiers (e.g., Anthropic's `overloaded_error` JSON body distinguishing rate-limit from outage) land in the provider adapters in Stages 6.2–6.4 if needed.

**Why:** The pattern is in place but unbuilt — no premature abstraction, just a hook. If Stage 6.2 finds it doesn't need a custom classifier, the default holds.

### 8. The cost-gate domain exception carries the budget state

**Decision:** `LLMCostCapExceeded(reason, daily_spent_usd, session_spent_usd, cap_kind, cap_value_usd)`. Operator-facing notification renders the values verbatim ("daily budget $1.00 reached at $1.03 spend; please bump LLMCostConfig.max_spend_per_day_usd or wait until 14:23 UTC").

**Why:** The error needs to be self-explanatory at the operator-notification layer without re-querying. Bundling the state into the exception keeps the error formatter logic-free.

### 9. No connection pooling or HTTP client sharing across adapters in Stage 6.1

**Decision:** Stage 6.1 doesn't instantiate any HTTP client. Each provider adapter (Stages 6.2–6.4) constructs its own `httpx.AsyncClient` and decides on lifecycle/sharing. The retry helper is HTTP-client-agnostic — it accepts any awaitable.

**Why:** Premature abstraction. Three adapters with three slightly different transport needs (auth header shape, base URL, response-shape parsing) don't benefit from a forced shared client. If shared lifecycle matters in Phase 8, abstract then.

### 10. Per-provider auth keys are read at adapter construction, not at module import

**Decision:** Each cloud adapter's `__init__` reads its auth env var (`os.environ["ANTHROPIC_API_KEY"]` etc.) and stores it. Missing key → adapter constructor raises a clear error; `cli/advise` / `cli/operator` catch and exit code 2 (the existing "missing creds" pattern from `cli/status`).

**Why:** Module-import-time reads (a la `_API_KEY = os.environ["..."]` at top level) break unit tests that don't set the var. Constructor-time matches the Kraken adapter's pattern.

## Test plan

- **Unit, ~80–100 new tests.** Most weight on the gate (cap-state matrix) and the retry helper (classification matrix + backoff timing).
- **Integration: zero net new for Stage 6.1.** No real API calls until Stage 6.2. The existing `tests/config/test_schema_drift.py` extends to cover the new `llm:` block + the three new env vars.
- **Deprived-env walkthrough:** `tools/show_llm_costs.py` against (a) no operator.db, (b) operator.db missing the llm_calls table, (c) empty table. Each should exit 0 or 2 with a clean message, no traceback.

## What's NOT in scope for Stage 6.1

- Any actual provider HTTP call. (Stage 6.2+.)
- Streaming-response support. (Out of Phase 6 scope per the kickoff goal.)
- Provider-native function-calling. (Same — out of phase scope.)
- Cross-provider failover or fallback wiring. (Deferred per ADR-015; post-v1.)
- Per-role budget split. (Deferred per ADR-014; v2.)
- Token-usage-aware prompt truncation. (Useful, but a different scope; queue separately if it ever becomes urgent.)
- Circuit-breaker pattern. (Deferred per ADR-015; Phase 8 reliability if observation justifies.)
- A web-UI cost dashboard. (Phase 7's job.)

## Stage close criteria

1. ADR-014 + ADR-015 committed in `docs/architecture/decisions.md`.
2. `llm_calls` table migration tested against a fresh + an existing operator.db.
3. `services/llm_pricing.py` carries verified entries for every model Stages 6.2–6.4 will use; `test_pricing_freshness.py` passes.
4. `services/llm_cost_gate.py` + `services/llm_retry.py` unit-tested across their state matrices.
5. `config/llm.py` schemas + `.env.example` + `config/settings.example.yml` updates align (schema-drift clean).
6. `tools/show_llm_costs.py` operator inspection covered by deprived-env walkthrough.
7. mypy clean (now 70+ src files), pylint 10.00/10 on `src/`, black + isort clean.
8. Roadmap, CLAUDE.md, CHANGELOG, `project_state` memory, MEMORY.md index all carry the Stage 6.1 ✅ date.
9. Total unit-test count grows by ~80–100; no test deletions.
