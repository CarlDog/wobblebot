# Phase 6 — Closing Summary

**Status: ✅ Complete (2026-05-17).** Five Phase 6 stages closed
inside one focused day-long session (6.1 / 6.2 / 6.3 / 6.4 / 6.5).
Cloud LLM integration ships end-to-end: shared cost-tracking
infrastructure → Anthropic adapter → OpenAI adapter (with shared
helper extracted mid-phase) → Google adapter → real-API smoke test
across all three providers under live cost-cap enforcement.

**Phase 6 spent $0.005018 of real money.** Three smoke-test calls
(one per provider) at Stage 6.5.A; receipts persisted to the
`llm_calls` ledger; cost-tracking flow validated end-to-end against
live provider APIs. Running project real-money cost: **$0.08 →
$0.085018**.

This document is the Stage 6.5 deliverable per the roadmap's
"end-to-end check" charter. Consolidates per-stage receipts, the
architecture story (how ADR-014 + ADR-015 commitments held up), the
shared-helper extraction that paid off across providers 2-3, the v2
candidates flagged for future hardening, and entry conditions for
Phase 7 (Web UI / Dashboard).

## Phase 6 kickoff and shape

After Phase 5 closed, Phase 6 needed two architectural decisions
ratified before code, mirroring Phase 5's ADR-013 + design-doc
pattern:

- **ADR-014 — LLM cost caps.** Per-day + per-session USD caps via
  `services/llm_cost_gate.check_budget` against a new `llm_calls`
  SQLite table in `operator.db`. Hard-stop on cap trip (raises
  `LLMCostCapExceeded`). Single-pool across roles in v1; per-role
  split deferred. Pricing table as **code, not config** — entries
  carry `verified_date` + comment-annotated pricing-page URLs;
  `test_pricing_freshness` watchdog fails CI when entries are >180
  days old. `enforce=False` dry-run posture for the first week of
  cloud usage.

- **ADR-015 — Cloud LLM provider failover policy.** Default fail
  loudly + retry on transient errors only (HTTP 429 / 5xx + httpx
  connection / timeout exceptions). Up to 3 retries with exponential
  backoff (1s, 2s, 4s by default). **No cross-provider failover.**
  **No silent cloud-to-Ollama failover** — silent model substitution
  breaks audit provenance. Retries draw from the same ADR-014 cost
  pool (one budget check per logical call, not per attempt).

Five stages followed: shared infrastructure → three per-provider
adapters → integration check.

## Per-stage outcomes

| Stage | Closed     | Sub-slices | Verification                                                  |
| ----- | ---------- | ---------- | ------------------------------------------------------------- |
| 6.1   | 2026-05-17 | A/B/C/D/E  | 1334 unit tests; `tools/show_llm_costs.py` deprived-env walk  |
| 6.2   | 2026-05-17 | A/B/C      | Anthropic adapter (advisor+assistant) via httpx.MockTransport |
| 6.3   | 2026-05-17 | A/B/C      | Shared helper extracted; OpenAI adapter on same orchestrator  |
| 6.4   | 2026-05-17 | A/B        | Google adapter (3rd provider); native additive thinking shape |
| 6.5   | 2026-05-17 | A/B        | Live smoke test all three providers ($0.005 total real spend) |

Each stage commit chain:
- 6.1: `393e6c7` kickoff → `ae771bf` 6.1.A → `df9fcf5` 6.1.B →
  `e747982` 6.1.C → `6cf6d4d` 6.1.D → `fff01cb` 6.1.E close
- 6.2: `c98b070` 6.2.A → `11589b4` 6.2.B → `add2c45` 6.2.C close
- 6.3: `6c5323d` 6.3.A → `eb15a06` 6.3.B → `072da5f` 6.3.C close
- 6.4: `eff74a6` 6.4.A → `23784cf` 6.4.B close
- 6.5: `6ccaef1` 6.5.A → this commit (6.5.B close)

## ADR-014 + ADR-015 commitments — how they held up

**ADR-014 commitments:**

1. **USD caps, not token caps.** ✅ `LLMCostConfig.max_spend_per_day_usd`
   + `max_spend_per_session_usd` both Decimal. Operator reasons about
   dollars; token math hidden in `services/llm_pricing`.

2. **Single shared pool across roles.** ✅ Daily total sums every
   `llm_calls` row in the sliding-24h window regardless of role.
   No starvation observed in the smoke test (only one call per role
   per provider).

3. **Service-layer enforcement, not adapter-layer.** ✅
   `services/llm_cost_gate.check_budget` is one function; called
   inside `execute_cloud_call` once per call. No adapter knows about
   the cap directly.

4. **Hard-stop on trip.** ✅ Verified in unit tests (Stage 6.1.B);
   smoke test never tripped a cap since `$0.005 << $0.50` session
   default.

5. **`llm_calls` SQLite table.** ✅ Lives in operator.db per ADR-013
   precedent. Three indexes (timestamp / provider+model / role) all
   present.

6. **Pricing table is code.** ✅ `services/llm_pricing._PRICING` is
   a module-level dict; eight models priced;
   `test_pricing_freshness` watchdog in place.

7. **Operator inspection.** ✅ `tools/show_llm_costs.py` displays
   per-row, per-provider, per-role rollups.

8. **`enforce=False` dry-run posture.** ✅ Knob exists; smoke test
   used `enforce=True` (real enforcement) because the actual costs
   were trivially under the caps.

**ADR-015 commitments:**

1. **Fail loudly + transient retry.** ✅ `default_classifier` in
   `services/llm_retry`: 429 / 5xx transient; other 4xx permanent;
   httpx Connect/Read/Write/Pool/RemoteProtocol transient; everything
   else permanent.

2. **No cross-provider failover.** ✅ Adapters fail in isolation;
   smoke test ran three separate invocations.

3. **No silent cloud-to-Ollama failover.** ✅ Provider is configured;
   no automatic substitution.

4. **Retries count against caps.** ✅ Single `check_budget` call per
   logical request; retries within `retry_with_backoff` don't get
   fresh checks.

5. **Operator-facing failure notifications.** ✅ `tools/run_cloud_check.py`
   logs error + persists failure record with classified `error_kind`;
   `cli/advise` + `cli/operator` log structured errors. Discord
   surface flows through the existing `cli/operator` notification
   pipe.

6. **Per-provider auth in env.** ✅ `ANTHROPIC_API_KEY` /
   `OPENAI_API_KEY` / `GOOGLE_API_KEY` (plus optional
   `OPENAI_ORGANIZATION`). Adapters fail fast at construction with
   clear errors if a configured-cloud-provider's key is missing.

7. **Retry config knobs default sensibly.** ✅ Defaults (3 retries,
   1.0s initial backoff, 2.0× multiplier) match the ADR. Smoke test
   ran without triggering retries.

8. **No circuit breaker.** ✅ Deferred to Phase 8 if observation
   justifies; not built.

## The shared-helper extraction story

Stage 6.2 (Anthropic) shipped with the full ADR-014/015 flow
internalized in each adapter — ~80 lines of cost-flow boilerplate
per adapter, copy-pasted across advisor + assistant. At Stage 6.3
kickoff, the operator called the right architectural shot:
**extract a shared helper before adding the second provider**.

That extraction landed in Slice 6.3.A as
`services/llm_cloud_call.execute_cloud_call`. By Stage 6.5.A's
close audit a second round of duplication had emerged across the
three providers — pricing-ceiling math, advisor recommendation
parsing, intent dict parsing — and got promoted into shared helpers
the same way.

Net result by Phase 6 close:

| Module                          | Lines | Notes                                             |
| ------------------------------- | ----- | ------------------------------------------------- |
| `services/llm_cloud_call.py`    | ~340  | execute_cloud_call + parse_advisor_recommendation + parse_intent_dict + classify_error |
| `services/llm_cost_gate.py`     | ~170  | SessionCostTracker + check_budget + GateAllow/Deny |
| `services/llm_pricing.py`       | ~225  | 8-model pricing table + cost_for + estimate_cost_ceiling |
| `services/llm_retry.py`         | ~135  | retry_with_backoff + default_classifier + LLMRetryConfig |
| `adapters/anthropic.py`         | ~280  | provider-specific bits only                       |
| `adapters/anthropic_assistant.py` | ~220 | provider-specific bits only                       |
| `adapters/openai.py`            | ~470  | both ports + reasoning-token subtraction          |
| `adapters/google.py`            | ~480  | both ports + role=model role mapping + thinking handling |

Per-provider adapters now only own:
- HTTP wire shape (URL, headers, body format)
- Token-count extraction (provider-specific normalization)
- Response text extraction (envelope traversal)

Everything else — cost gate, retry, persistence, session tracking,
JSON parsing, domain-object construction — lives once in
`services/`. **Adding a fourth provider would cost ~250-500 LOC**
(just the three provider-specific concerns above + the dispatch
branch in `cli/advise._build_advisor_adapter` +
`cli/operator._build_assistant` + the `provider` Literal extension).

## Stage 6.5 live smoke-test receipts

All three providers verified end-to-end against real APIs on
2026-05-17:

| Provider  | Model              | Role     | In   | Out | Reason | Cost USD    | Request ID         |
| --------- | ------------------ | -------- | ---- | --- | ------ | ----------- | ------------------ |
| anthropic | claude-sonnet-4-6  | operator | 1321 |  19 |      0 | 0.004248    | `msg_*` (anthropic id) |
| openai    | gpt-4o-mini        | operator | 1171 |  15 |      0 | 0.000185    | `chatcmpl-*`       |
| google    | gemini-2.5-flash   | operator | 1281 |  20 |     43 | 0.000585    | `*` (responseId)   |

**Smoke-test total: $0.005018.** All three calls succeeded; all three
records persisted with `success=True`; tracker totals matched real
costs; cost gate (default $0.50/session) was never tripped. The
Google call carries `tokens_reasoning=43` — the native-additive
thinking shape Stage 6.4's `extract_google_tokens` was built to
handle.

The full receipt table is queryable any time via
`python tools/show_llm_costs.py --by-provider`.

## Real-money cost ledger

| Event                                | Date       | Cost USD  | Running Total |
| ------------------------------------ | ---------- | --------- | ------------- |
| Phase 2 first-trade smoke (cli/live) | 2026-05-15 | 0.080000  | 0.080000      |
| Phase 6 anthropic smoke              | 2026-05-17 | 0.004248  | 0.084248      |
| Phase 6 openai smoke                 | 2026-05-17 | 0.000185  | 0.084433      |
| Phase 6 google smoke                 | 2026-05-17 | 0.000585  | 0.085018      |

Project running real-money cost at Phase 6 close: **$0.085018**.

Phase 6 added $0.005018 (about half a cent total across three
providers) — the cost-tracking machinery is now battle-tested without
having burned a meaningful fraction of the budget.

## v2 candidates flagged during Phase 6

1. **`--no-call` flag on `tools/run_cloud_check.py`.** Surfaced during
   the Stage 6.5.A deprived-env walkthrough: `--dry-run` only
   disables gate enforcement, not the API call. A separate `--no-call`
   would let operators verify wire-shape construction without
   spending money. Small change; queue for Phase 8 polish.

2. **Per-role budget split.** ADR-014 v1 is single-pool. If the
   operator-assistant role starts starving the trading-advisor (or
   vice versa) in real usage, a follow-on ADR splits the budget.
   The cost table already carries `role` so retroactive analysis is
   possible without schema work.

3. **Cross-provider failover.** ADR-015 v1 is fail-loudly. Multi-hour
   provider outages would justify automatic fallback (with explicit
   operator opt-in + audit-trail provenance). Phase 8 reliability
   stage candidate.

4. **Circuit breaker.** ADR-015 decision 8 deferred. Phase 8
   reliability if observation shows recurring transient failures.

5. **Honor `Retry-After` header on 429 responses.** Current backoff
   is fixed-exponential. Reading `Retry-After` from the response is
   a single-line enhancement once a real adapter shows rate-limit
   behavior.

6. **Pricing-table verified-date refresh.** Anthropic / OpenAI /
   Google entries all carry `verified_date=2026-01-15`. Test fails
   when entries exceed 180 days old. Quarterly audit item; first
   refresh due ~2026-07-15.

## Test + code health at Phase 6 close

| Metric                | Phase 5 close | Phase 6 close | Delta |
| --------------------- | ------------- | ------------- | ----- |
| Unit tests            | 1214          | 1460          | +246  |
| Integration tests     | 26            | 29            | +3    |
| Src files (mypy)      | 69            | 79            | +10   |
| pylint score          | 10.00/10      | 10.00/10      | =     |
| Runtime dependencies  | (Phase 5 set) | (Phase 5 set) | 0 new |

**Zero new runtime dependencies in Phase 6.** Every cloud adapter is
pure httpx + pydantic on the dependencies already pulled in by Phases
3-5. The shared orchestrator pattern + the pricing-as-code decision
meant no vendor SDK lock-in.

## Entry conditions for Phase 7 — Web UI / Dashboard

Phase 7 was carved off Phase 5's reframe (per ADR-013 / Phase 5
kickoff). Goal: FastAPI app at `src/wobblebot/web/`, sibling to
`cli/`; both presentation layers consume the existing ports without
new business logic.

Phase 6 leaves the engine in good shape for Phase 7:

- Cost ledger is queryable via `StoragePort.get_llm_calls` — a
  cost-dashboard endpoint is a thin GET wrapper.
- All four LLM provider adapters land on the same shape — the
  operator picks one via `settings.yml`; the dashboard reports
  which is active without per-provider conditionals.
- ADR-014 caps are visible in config (`config.llm.cost`); the
  dashboard can show "today's budget used: $X / $Y" trivially.

Per the Phase 5 kickoff reorg, Phase 8 (Hardening & v1.0 Release)
follows Phase 7 — Stage 8.0 carries forward the deferred Phase 5
audit refactors (R5 ports/operator.py split, R3 storage-fallback
helper, R2 generic poll-loop helper).

CryptoCompare 90-day evaluation remains due **2026-08-13** per
ADR-010. Not closed in Phase 6 since the proper review window
hasn't elapsed yet — deferred to its scheduled date.

## Closing notes

Phase 6's shape proved out the cloud-LLM architecture cleanly:

- **The ADR-first pattern works.** Both ADR-014 and ADR-015 held
  through implementation without revision. Every commitment in
  the ADR text is a verifiable property in the running code.

- **The shared-helper extraction was the right call.** Stage 6.3.A's
  mid-phase refactor + Stage 6.5.A's audit pass collapsed
  ~270 LOC of mechanical duplication while preserving each provider's
  idiomatic surface (Anthropic's messages API ≠ OpenAI's chat
  completions ≠ Google's generateContent — and that's fine).

- **Cost machinery is forensically complete.** Every cloud call —
  success or failure — leaves a persisted record. Operators can
  inspect via `tools/show_llm_costs.py` or via direct SQL against
  `operator.db`'s `llm_calls` table.

- **$0.005 of real money proved the wiring.** The cost-tracking flow
  validates against three independent provider APIs with three
  different reasoning-token shapes (Anthropic lumps, OpenAI
  subtracts, Google additive) — all reconciled to the same
  `tokens_out + tokens_reasoning` additive invariant.

Phase 7 (Web UI / Dashboard) opens with Phase 6's cost-tracking
visible, configurable, and verified.
