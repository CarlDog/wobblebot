# Stage 8.5 — Advisor Engine: Heuristic + LLM Cascade (pre-soak)

**Status:** design ratified 2026-05-29, build pending. Slots into pre-soak
(before the v1.0 gating soak restart ~2026-06-01) as a value-add so the
month-long soak runs on the real advisor.

This doc is self-contained: it captures the full investigation that
justifies the design, the validated facts, and the build plan, so the
work can resume after a context compaction without re-deriving anything.

---

## 1. Why — the investigation (one session, 2026-05-29)

Question: would an LLM advisor actually help the operator make smarter
grid decisions, or is it theater? We built a probe + battery to settle it
empirically. Arc:

1. **Local CPU models can't reason about it.** On the NAS, every local
   model (qwen2.5:7b/3b, llama3.1:8b, wizard-math, mathstral) mean-reverts
   — emits a near-constant spacing regardless of metrics. Best was
   qwen2.5:7b at 16/36 on the core battery, which a constant also scores.
2. **A "reason-first" prompt reorder helped on the core battery (16→22)
   but was teaching-to-the-test.** A held-out battery of conflicting
   cases exposed it: the lift depended on a decision rule we'd put in the
   prompt that mirrored the fixture answer key. Ablation (temp 0): the
   field-reorder alone did ~nothing (15 vs 16 baseline); the lift needed
   the handed rule.
3. **The real confound was a sparse prompt.** The production prompt never
   *stated* the override considerations, so models couldn't apply what
   they weren't told. With a **complete prompt** that states them, two
   frontier models (gemini-2.5-pro, sonnet-4-6) jumped to 3/3 on the
   clean held-out discriminators they'd all failed.
4. **Newest frontier reasoning models bring genuine judgment.** Held-out
   + complete prompt (temp 1, max_tokens 4000):

   | model | held-out | discriminators (fee-floor / working / drawdown / scalping) |
   |---|---|---|
   | **claude-opus-4-8** | 17/24 | **4/4** ✓✓✓✓ |
   | **openai o3** | 17/24 | **4/4** ✓✓✓✓ |
   | claude-haiku-4-5 | 18/24 | 3/4 (misses drawdown) — ~$2/mo |
   | gemini-3.1-pro-preview | 14/24 | 3/4 |
   | gemini-3.5-flash / sonnet-4-6 / gemini-2.5-pro | 14/24 | 3/4 |
   | gpt-5.5 | 11/24 | 2/4 |
   | every local CPU model | fails | 0/3 clean |

   The drawdown case is a genuine *conflict* (calm market says tighten,
   drawdown says widen); only opus-4-8 and o3 resolved it — real judgment
   a flat rule can't do.

**Conclusion:** local CPU is out; a frontier reasoning model + a complete
prompt is genuinely good. **Operator chose `o3` (4/4, ~$3/mo).** Then
refined the architecture to a heuristic+LLM cascade (this stage).

## 2. What — the cascade design

Both pieces are `AdvisorPort` implementations, composed like
`MoEAdvisorAdapter` composes experts.

### `HeuristicAdvisorAdapter` (new, `src/wobblebot/adapters/heuristic_advisor.py`)
Codifies the decision logic below, deterministically. Returns an
`AdvisorRecommendation` **plus a "clear-match" / confidence signal** the
cascade reads to decide whether to escalate. Zero cost, transparent,
on-brand for "deterministic, safety-first."

The logic (precisely characterized + blind-validated 12/12 + 8/8 this
session; it IS the content of `config/prompts/quant-cot-complete.md`):

- **First-order: ideal spacing tracks volatility.** Compare current
  spacing to the ideal-for-vol band → too tight = WIDEN, too wide =
  TIGHTEN, matched = HOLD. Rough ideal(vol) curve (per-tick σ → spacing %):
  `.0008→0.65, .002→0.90, .003→1.05, .004→1.25, .006→1.60, .008→1.90,
  .012→2.50, .014→2.70`.
- **Guard 1 — FEE FLOOR:** never tighten below ~0.52% (2× the 0.26%
  maker fee). At/near the floor in a calm market → HOLD (don't tighten).
- **Guard 2 — DON'T-FIX-WORKING:** high win_rate AND high cycle_count AND
  minimal drawdown → HOLD even if vol/spacing look mismatched.
- **Guard 3 — DEFENSIVE-DRAWDOWN:** sharp recent drawdown → WIDEN even in
  a calm market (capital preservation overrides the calm-tighten instinct).
- **Guard 4 — DIRECTIONAL≠SPACING:** one-sided fills + ~0 cycles (price
  ran away directionally) → HOLD spacing (the fix is re-anchoring).

### `CascadingAdvisorAdapter` (new, `src/wobblebot/adapters/cascading_advisor.py`)
Wraps a heuristic + an LLM advisor; behaviour set by `advisor.engine`:
- **`heuristic`** — deterministic only ($0, no LLM).
- **`llm`** — current always-LLM behaviour (the wrapped cloud advisor).
- **`cascade`** (recommended default) — heuristic runs first; if it
  reports a **clear match**, return it (free); if the heuristic detects a
  **conflict / ambiguity / unrecognized combo**, escalate to the LLM; if
  the LLM call fails or trips the cost cap, **fall back** to the
  heuristic's best guess (resilience). Combines the operator's three
  framings (fallback + operator-default-choice + pass-thru).

### Escalation rule (the design heart — validate against HELDOUT_FIXTURES)
Heuristic escalates to the LLM when:
- two or more guards **conflict** (e.g. Guard 3 drawdown→widen vs.
  first-order calm→tighten — the held-out `drawdown_overrides_calm` case), OR
- the first-order call sits **near a band boundary** (ambiguous), OR
- metrics fall outside any recognized pattern.
It does NOT escalate when exactly one direction is unambiguous and no
guard conflicts (the held-out controls). Acceptance test: the 4 held-out
**controls** resolve via heuristic (no LLM call); the 4 **discriminators**
either resolve correctly via a guard OR escalate to the LLM.

### Config + wiring
- `AdvisorConfig` gains `engine: "heuristic" | "llm" | "cascade"` (default
  `cascade`). When `cascade`/`heuristic`, the heuristic is built; when
  `cascade`/`llm`, the LLM advisor is built via the existing
  `_build_advisor_adapter` cloud path.
- `cli/advise._build_advisor` composes them.
- The cascade's LLM = **o3** (provider openai) with the complete prompt.
- Cost gate (ADR-014): cloud calls already gated. In cascade mode the LLM
  fires only on hard cases → real cost likely << the $3/mo always-LLM.

## 3. Validated artifacts (already in the repo)

- `config/prompts/quant-cot-complete.md` — the complete prompt (states the
  4 overrides). It is BOTH the LLM-advisor prompt AND the spec for the
  heuristic. **Promote → `config/prompts/quant.md` when wiring.**
- `tools/probe_advisor.py` — `FIXTURES` (core 12) + `HELDOUT_FIXTURES`
  (8: 4 controls + 4 discriminators) + scoring. `--fixture-set heldout`
  is the cascade's ready-made test suite. `--provider`/`--temperature`/
  `--max-tokens` for cloud re-tests. (committed 19a5800)
- `tests/tools/test_probe_advisor_scoring.py` — locks the scoring rubric.
- `src/wobblebot/services/llm_pricing.py` — verified 2026-05-29 pricing
  for the newest models (opus-4-8 $5/$25, o3 $2/$8, gpt-5.5 $5/$30,
  gemini-3.1-pro $2/$12, haiku-4-5 $1/$5, …). (committed 19a5800)

## 4. Build plan (ordered)

1. `HeuristicAdvisorAdapter` + the ideal(vol) curve + 4 guards + a
   `clear_match: bool` (or confidence) on the result. Unit tests: run it
   against the core 12 (should match the validated directions) + the
   held-out controls (clear match) + discriminators (conflict → not a
   clear match / escalate signal).
2. `CascadingAdvisorAdapter` + the 3 modes + escalation + LLM-failure
   fallback. Unit tests: controls route to heuristic (no LLM), conflicts
   escalate, LLM-failure falls back to heuristic.
3. `AdvisorConfig.engine` field + validator + `settings.example.yml`
   (cpu-only profile: `engine: cascade`, llm sub-config = openai/o3 +
   `quant.md` + temperature 1.0 + max_tokens 4000 + timeout 120; ensure
   top-level `llm:` block + `operator.operator_db` present for the cost
   ledger). Schema-drift test must stay green.
4. Wire into `cli/advise._build_advisor`.
5. Promote `quant-cot-complete.md` → `quant.md`; delete the throwaway
   ablation prompts (`quant-cot.md`, `quant-cot-norule.md`,
   `quant-cot-recsfirst.md`, `quant-cot-complete.md`) once consolidated.
6. End-to-end: `probe_advisor` (cascade not directly probed, but the
   heuristic + the o3 LLM each are) + a live `o3` smoke via the cloud path.
7. Roadmap + CHANGELOG; ADR if the cascade is deemed architecturally
   load-bearing (it composes ports — arguably ADR-worthy, like MoE).

## 5. Open design decisions to confirm during build
- **Escalation thresholds** — how "near a band boundary" is quantified;
  tune against HELDOUT_FIXTURES so controls don't escalate and
  discriminators do.
- **One source of truth** — the heuristic logic and `quant.md` both encode
  the rule+guards. Keep them in sync (or generate the prompt's rule
  section from the heuristic's constants) so they don't drift into two
  truths. Flag in the heuristic module docstring.
- **Cost-cap sizing for a long-running daemon** — the session cap is an
  in-memory per-process tally; a weeks-long advise daemon accumulates, so
  size `max_spend_per_session_usd` for the run length (the 24h sliding
  `max_spend_per_day_usd` is the real runaway guard). o3 ≈ $0.10/day.

## 6. State at handoff
- Committed: `llm_pricing.py` newest-model pricing + `probe_advisor.py`
  cloud/held-out tooling (19a5800); earlier `quant-cot.md` + harness
  flags (a7bc415, c9d7532, plus the pricing fix).
- NOT done: the plain-o3 production config (superseded by this cascade
  stage); the cascade build (this doc).
- The soak is paused/pre-soak; build the cascade, then the gating soak
  restarts on it (~2026-06-01).

## 7. As-built (2026-05-29) — what actually shipped + deviations

Built and verified (2225 tests pass, mypy clean, pylint 10.00/10):

- **Configurable heuristic spec** (operator's mid-build request — the
  curve + thresholds live in DATA, not code): `config/heuristic.py`
  (`HeuristicSpec` Pydantic schema: `curve` list + `fee_floor` +
  `hold_deadband` + four guard sub-models with per-guard `enabled`
  toggles + `escalation` band; `load_heuristic_spec(path)` loader
  mirroring `config/prompts.py`). Committed default
  `config/heuristic/quant.yml` (operator-editable, bind-mount-friendly,
  like the prompt files). The guard *algorithm* + priority order stay
  in code; only the numbers + toggles are tunable. `curve` is the only
  required field; thresholds default in code.
- **`HeuristicAdvisorAdapter`** (`adapters/heuristic_advisor.py`):
  ideal(vol) piecewise-linear + fee-floor clamp + the 4 guards
  (directional-runaway → defensive-drawdown → dont-fix-working →
  fee-floor-calm → first-order). Exposes `evaluate() -> HeuristicVerdict`
  (recommendation + `clear_match` + `direction` + `reason`); the async
  `get_recommendation` wraps it. Reproduces the SHIPPED spec against
  both batteries: **core 36/36, held-out 24/24** (the test loads
  `config/heuristic/quant.yml`, so editing the curve and breaking a
  fixture fails loudly). Hold-deadband 0.15 absorbs the directional /
  fee-floor near-boundary cases; guards resolve the rest.
- **`CascadingAdvisorAdapter`** (`adapters/cascading_advisor.py`):
  **simplified to no `mode` enum** (deviation from §2). It does one
  thing — heuristic-first, escalate on non-clear-match, fall back to the
  heuristic on `AdvisorError`/`LLMCostCapExceeded`. The three engine
  behaviours are chosen by `cli/advise._build_advisor` returning the
  bare heuristic / bare LLM / cascade-wrapper, which preserves the
  existing `isinstance(advisor, OllamaAdapter/MoEAdvisorAdapter)` tests
  for `engine: llm`.
- **Config:** `AdvisorConfig.engine` (`heuristic|llm|cascade`) +
  `heuristic_file`. **`engine` defaults to `llm`, NOT `cascade`**
  (deviation from §2): defaulting a new composite ON would break every
  existing config + many tests, and the soak gets the cascade anyway
  because the `cpu-only` profile sets `engine: cascade` explicitly. The
  validator gates the type-based LLM checks on `engine in (llm,
  cascade)` and requires `heuristic_file` for `heuristic`/`cascade`.
- **Wiring:** `cli/advise._build_advisor` dispatches on engine;
  `_build_llm_advisor` extracted. `settings.example.yml`: top-level
  `advisor` documents `engine`/`heuristic_file`; the `cpu-only` profile
  now runs `engine: cascade` + heuristic + cloud `o3` (temp 1.0,
  max_tokens 4000, timeout 120) — the local llama3.1:8b advisor was
  retired (no local CPU model reasons well enough).
- **Pre-existing bug fixed (in-scope cleanup):** `cli/advise._run_cycle`
  only caught `AdvisorError`, but the cloud adapters let
  `LLMCostCapExceeded` (a *domain* exception) bubble raw — so today's
  `engine: llm` cloud path would crash the daemon on a cap trip,
  contradicting the ADR-014 "catches and skips" promise. Now caught +
  skips the tick. (The cascade is independently robust — its fallback
  catches both.)

Deferred / open (NOT done — need operator sign-off):

- **Prompt promotion** (`quant-cot-complete.md` → `quant.md` + delete
  ablations `quant-cot*.md`). Deferred: `quant.md` is SHARED by MoE +
  single configs, the complete prompt drops the other-param guidance
  (levels/order_size), and `tools/pull_and_probe_advisors.py` references
  the cot prompts. NOT load-bearing for the cascade — the heuristic
  handles every override case; o3 is only hit on ambiguous first-order
  gaps where the general `quant.md` suffices.
- **Roadmap / CHANGELOG** entry for Stage 8.5 — pending the commit.
- **Operator NAS actions** before the soak runs on the cascade: add the
  top-level `llm:` block + `OPENAI_API_KEY` to the NAS settings (the
  bind-mounted `settings.yml` currently lacks the `llm:` block);
  copy/confirm `config/heuristic/quant.yml`; set `advisor.engine:
  cascade`. Without these the advisor falls back to heuristic-only
  (which is itself a valid $0 mode).
- **Repo-wide black drift** (pre-existing, surfaced this session): the
  venv's black 26.3.1 reformats ~12 files last formatted under an older
  black. Separate cleanup, not bundled here.
