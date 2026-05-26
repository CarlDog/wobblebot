# Reasoning-model probe-fix sweep — 2026-05-25

Six configurations run by `tools/sweep_reasoning_fixes.py` (plus one
rerun after surfacing the `check_model_suitability` catch-22). Each
row's full stdout is in the sibling `.txt` file by slug.

## Results

| # | Model | Role | Config | Score | Errors | Elapsed | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `phi4-mini-reasoning:3.8b-fp16` | operator | compact + force_json | **0/29** | 29 | — | Model emits topical JSON (treats input as data request, not routing task) |
| 2 | `phi4-mini-reasoning:3.8b-fp16` | advisor | force_json only (std quant.md) | **11/18** | 0 | 52s | Lazy baseline tie — emits `spacing=1.2` for every fixture |
| 3 | `phi4-mini-reasoning:3.8b-fp16` | advisor | compact + force_json | **8/18** | 0 | 56s | WORSE than std — over-widens to `spacing=2.0` (OVERSHOOT band) |
| 4 | `phi4-reasoning:14b-plus-q8_0` | advisor | force_json only | **11/18** | 0 | 131s | TIMEOUT artifact fixed; same lazy baseline as #2 |
| 5 | `deepseek-r1:14b-qwen-distill-q8_0` | operator | force_json | **29/29 parse, 1 routing miss** | 0 | 744s | ~25s/call (vs 44s baseline); routes everything except "what's the weather" |
| 6 | `deepseek-r1:14b-qwen-distill-q8_0` | advisor | force_json | **0/18** | 6 | 35s | Degenerates to `{}` or non-schema dicts on `/api/generate` |

## Key findings

### 1. `force_json` is the right fix for *some* reasoning models on *some* endpoints

- **`/api/chat` (operator role):** `force_json` works for both
  phi4-reasoning families AND deepseek-r1. The legacy "thinking
  models degenerate to `{}`" heuristic was over-broad for this
  endpoint.
- **`/api/generate` (advisor role):** `force_json` works for
  phi4-reasoning (both sizes) but BREAKS deepseek-r1 — the model
  emits `{}` or unrelated JSON dicts. The legacy heuristic was
  RIGHT for deepseek-r1 specifically on this endpoint.

**v1.1 implication:** the `force_json_output` config flag must
be per-model, per-role. Not global. Operator-opts-in per
(model, role) pair.

### 2. Reasoning models on advisor converge to the lazy baseline

`phi4-mini-reasoning` (3.8B) and `phi4-reasoning:14b-plus` (14B+)
both scored exactly **11/18** under `force_json` — and both emitted
`spacing=1.2` for every single fixture. This is the literal
"always slight widen" lazy baseline the methodology caveat in
`advisor-llm-models.md` documents.

The fix unlocks JSON output but **does not unlock differentiated
numerical reasoning** on the advisor task. Reasoning-tuned models
appear no better than the existing 21-model lazy cluster — possibly
worse, since they have no incentive to deviate from the safe-default
emission under format=json constraint.

### 3. `phi4-mini-reasoning` operator role is still broken — but differently

The compact prompt (1364 chars) + `force_json` did NOT fix the
0/14 → 0/29 verdict. **Different failure mode** than the original:

- **Before (full operator.md, 8706 chars):** model invents math
  problems (training default kicks in; doesn't engage with the
  routing task at all).
- **After (compact 1364 chars + force_json):** model emits
  CONTEXTUALLY RELATED JSON for each input — but treats each
  input as a literal data request instead of routing it. Examples:
    - `"what's the weather"` → `{"weather":"sunny","temperature":75}`
    - `"good night"` → `{"symbols":["AAPL","GOOG"]}`
    - `"show me what's available"` → `{"pollution_levels":{"PM":15,"efficiency":87}}`
    - `"pause BTC"` → `{"state":"paused","symbol":"BTC/USD"}` (close — uses our domain vocabulary but skips the `kind:"command"` envelope)

The compact prompt successfully suppresses the math-mode default
but does NOT successfully convey "you are a router, not an
answerer." Needs stronger framing — explicit "DO NOT answer the
user's question; CLASSIFY it" + more concrete I/O examples.

### 4. `quant-compact.md` is worse than the original

Dropping the "argue from numbers, not sentiment" constraint let
phi4-mini-reasoning's output run wild — `spacing=2.0` (+100%)
for 4 of 6 fixtures, all in the OVERSHOOT band. Compact-prompt
design needs to preserve magnitude-anchoring constraints.

### 5. `deepseek-r1` operator improves latency by 43% under `force_json`

29/29 successful parses at ~25s/call (vs 44s baseline before
`force_json`). One routing miss ("what's the weather" → unparseable
instead of conversational, same single-fixture miss as the
existing 13/14 baseline). The `format=json` constraint successfully
suppresses the `<think>` block on `/api/chat` for this model
WITHOUT degenerating to `{}` like it does on `/api/generate`.

## Per-model recommendations

### `phi4-mini-reasoning:3.8b-fp16`
- **Operator role:** still incompatible. Neither the standard
  prompt nor the current compact draft works. Needs a more
  router-focused compact prompt + likely few-shot routing
  examples baked into the prompt.
- **Advisor role:** force_json gets it to lazy baseline (11/18).
  Don't use the compact quant draft.

### `phi4-reasoning:14b-plus-q8_0`
- **Operator role:** already 14/14 at 6.2s under the existing
  adapter heuristic (which drops `format=json` for this model).
  Leave alone — `force_json` would also work but isn't necessary.
- **Advisor role:** `force_json` fixes the TIMEOUT artifact and
  yields 11/18 (lazy baseline). Recommendation: viable for the
  advisor role IF the operator accepts lazy-baseline scoring.

### `deepseek-r1:14b-qwen-distill-q8_0`
- **Operator role:** `force_json` improves latency 44s → 25s/call
  with full routing fidelity. Recommendation: enable for this
  model on `/api/chat`.
- **Advisor role:** keep `force_json` OFF; the existing free-text
  extraction path is correct for this model on `/api/generate`.

## v1.1 design implications

1. `force_json_output: bool = False` flag must be **per-model,
   per-role** (not a single global flag). Operator opts in per
   (model, role) combination.
2. The blocklist mechanism (`KNOWN_INCOMPATIBLE_FOR_ASSISTANT`)
   needs a `bypass_suitability_check` escape hatch for diagnostic
   re-evaluation — already shipped in slice 1.
3. Compact prompts are NOT a quick win. The current drafts are
   strictly worse for both operator and quant. A second pass needs
   to: (a) preserve magnitude-anchoring constraints in quant; (b)
   add explicit "router not answerer" framing + few-shot examples
   in operator.
4. The methodology caveat in `advisor-llm-models.md` is validated
   by these results — reasoning models score in the lazy-baseline
   cluster, not above it. The v1.1 auditor remains the right path
   for objective model evaluation.

## Files

- `phi4-mini_operator_compact_json.txt` — 0/29, contextual JSON failure mode
- `phi4-mini_advisor_json_only.txt` — 11/18 lazy baseline
- `phi4-mini_advisor_compact_json.txt` — 8/18 magnitude overshoot
- `phi4-reasoning-14b_advisor_json_only.txt` — 11/18 lazy baseline
- `deepseek-r1-14b_operator_json_only.txt` — 29/29 parse, 1 routing miss
- `deepseek-r1-14b_advisor_json_only.txt` — 0/18, degenerate `{}` failure

---

# v2 compact-prompt follow-up sweep — 2026-05-26

Targeted re-evaluation of `phi4-mini-reasoning:3.8b-fp16` after
redrafting the two compact prompts to address the v1 failure modes
identified above. Per `feedback_compact_prompts_preserve_constraints`,
v2 kept the schema list but **added** what v1 had dropped:

- **`operator-compact.md` v2** (1364 → 4435 chars body): added an
  opening "router not answerer" frame, six anti-pattern examples
  ("what's the weather → unparseable, NOT `{"weather":"sunny"}`"),
  explicit "first key is always `kind`" rule, and an enumerated
  list of the only valid top-level keys.
- **`quant-compact.md` v2** (692 → 1818 chars body): added an
  explicit ±25% magnitude band with direction guidance
  ("tighten = 0.75-0.95, hold = omit or 0.95-1.05, widen =
  1.05-1.25"), counter-examples (`spacing=2.0 is INVALID`), and
  preserved the "argue from numbers" constraint.

## Results

| # | Model | Role | Config | v1 score | **v2 score** | Notes |
|---|---|---|---|---|---|---|
| 1 | `phi4-mini-reasoning:3.8b-fp16` | operator | compact v2 + force_json | 0/29 | **0/29 (29 errors)** | Same failure mode despite stronger framing |
| 2 | `phi4-mini-reasoning:3.8b-fp16` | advisor | compact v2 + force_json | 8/18 | **4/18 (4 errors)** | Regression — longer prompt re-triggers saturation |

## Key findings

### 1. Operator v2: prompt redesign does not reach the model

The v2 prompt added every fix the v1 failure analysis prescribed —
explicit role framing, anti-pattern examples, key-name allowlist.
phi4-mini-reasoning emitted the same off-topic hallucinated JSON
(database joins, geometry queens, pollution levels). The model is
not reading the prompt carefully enough for added framing to land.
**Conclusion: at 3.8B params with reasoning fine-tuning, the
operator-routing task is out of capability range regardless of
prompt design.**

### 2. Advisor v2: magnitude rule worked, but at the cost of JSON validity

v2's ±25% band did successfully suppress the over-widening pattern
— the model emitted `spacing=1.05` on the two fixtures where it
produced valid JSON (vs v1's universal 2.0). But the longer prompt
caused 4 of 6 fixtures to error out entirely (schema validation
failure), netting 4/18 — **worse than v1's 8/18**. The tradeoff is
clear: phi4-mini-reasoning at 3.8B can EITHER produce valid JSON
under a short prompt OR honor magnitude constraints under a longer
prompt, but not both.

### 3. The "diagnose before blocklist" principle held — and confirmed the blocklist

The 2026-05-25 diagnostic correctly identified that v1 compact
prompts were degraded (not the model's full capability). v2 tested
whether a better-designed compact prompt could rescue the model;
it could not. **The original `KNOWN_INCOMPATIBLE_FOR_ASSISTANT`
verdict for phi4-mini-reasoning was correct** — diagnose-before-
blocklist gives confidence in the verdict by testing it twice,
not just inheriting it.

## Per-model recommendation update

### `phi4-mini-reasoning:3.8b-fp16`

- **Operator role:** **incompatible — final.** Both v1 and v2
  compact prompts produce 0/29. No further redesign warranted at
  this model size. Keep on `KNOWN_INCOMPATIBLE_FOR_ASSISTANT`.
- **Advisor role:** force_json + standard `quant.md` (v1 baseline)
  is the best available config — 11/18 lazy baseline, no errors.
  Compact variants both regress.

The earlier per-model matrix's other entries (phi4-reasoning:14b-plus
advisor, deepseek-r1 operator, deepseek-r1 advisor) are unchanged.

## v1.1 design implications (update)

The v1.1 entry "Reasoning-model support: compact prompts +
`format=json` opt-in" (operator-ux.md) needs revision:

- Drop "compact prompts" as a candidate fix for phi4-mini-reasoning.
  Two iterations (v1 + v2) failed to improve on the blocklist
  verdict; further prompt redesign is unlikely to be productive.
- Keep `force_json_output` per-(model, role) as the substantive
  remaining infrastructure work. The fix continues to be valid
  for phi4-reasoning:14b-plus advisor and deepseek-r1 operator.
- The compact-prompt directory (`config/prompts/*-compact.md`)
  can stay as artifacts documenting what was tried; the entry's
  recommendation list narrows to two enable-able models.

## v2 files

- `phi4-mini_operator_compact_v2_json.txt` — 0/29, same failure mode as v1
- `phi4-mini_advisor_compact_v2_json.txt` — 4/18, magnitude band held but JSON validity collapsed
