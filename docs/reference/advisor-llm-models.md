# Trading-Advisor LLM Compatibility Matrix

Empirical comparison of Ollama-served local models against the
**trading-advisor** routing battery. Sister document to
[operator-llm-models.md](operator-llm-models.md), which covers the
**operator-assistant** role; the two roles differ in adapter
(`OllamaAdapter` via `/api/generate` vs `OllamaAssistantAdapter`
via `/api/chat`), prompt (`config/prompts/quant.md` vs
`operator.md`), and measurement (no single "right answer" per
scenario — only direction + magnitude bands).

Driven by `tools/probe_advisor.py` + `tools/pull_and_probe_advisors.py`
on **2026-05-25** against `config/prompts/quant.md`.

## Battery

Six canned `PerformanceSummary` fixtures spanning the realistic
market regimes a grid bot encounters. Each scored against a
baseline `spacing_percentage=1.0` grid:

| Fixture | Vol | Drawdown | Cycles | Expected direction |
|---|---|---|---|---|
| `quiet_market` | 0.0008 | -0.002 | 1 | TIGHTEN (denser grid for small moves) |
| `healthy_churn` | 0.003 | -0.008 | 4 | HOLD (working as intended) |
| `whipsaw` | 0.012 | -0.035 | 8 | WIDEN (oscillation eats fills) |
| `trending_up` | 0.004 | -0.005 | 2 | HOLD (favorable trend; don't chase) |
| `trending_down` | 0.006 | -0.045 | 1 | WIDEN (defensive grid in downturn) |
| `post_cap_trip` | 0.008 | -0.060 | 0 | WIDEN (defensive on restart) |

The advisor's response schema only emits param changes
(spacing/levels/order_size). It has no "pause" recommendation —
that's an operator decision. Fixtures that would warrant pause in
the operator's mind are scored against what the advisor CAN
emit (defensive widening).

## Scoring rubric

Per-scenario verdicts:

| Verdict | Score | Meaning |
|---|---|---|
| **OK** | 3 | Right direction + magnitude within ±25% of current spacing |
| **OVERSHOOT** | 2 | Right direction, magnitude beyond ±25% |
| **ADJACENT** | 1 | `hold` ↔ `tighten`/`widen` mismatch (one step off) |
| **WRONG** | 0 | Opposite direction (e.g. WIDEN when TIGHTEN expected) |
| **ERROR** | 0 | Schema-invalid output (e.g. math-mode prose, no JSON) |

Max score across 6 fixtures: **18**.

## Results

Ranked by score, then by error count, then by elapsed time. Memory-
card storage during the 2026-05-25 sweep dominates the elapsed
numbers; treat as informational, not a model-speed benchmark.

| Rank | Model | Score | OK | OVER | ADJ | WR | ERR | Time |
|---|---|---|---|---|---|---|---|---|
| **1** | `llama3.1:8b-instruct-q8_0` | **14/18** | 4 | 0 | 2 | 0 | 0 | 150s |
| **2** | `wizard-math:13b` | **13/18** | 4 | 0 | 1 | 1 | 0 | 120s |
| **3** | `mathstral:7b` (q4_K_M) | **12/18** | 3 | 0 | 3 | 0 | 0 | 31s |
| **3** | `mathstral:7b-v0.1-q8_0` | **12/18** | 3 | 0 | 3 | 0 | 0 | 141s |
| 3 | `qwen2:0.5b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 28s |
| 3 | `smollm2:1.7b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 31s |
| 3 | `stablelm-zephyr:3b` | 11/18 | 3 | 0 | 2 | 1 | 0 | 35s |
| 3 | `llama3.2:1b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 36s |
| 3 | `qwen2.5:1.5b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 54s |
| 3 | `granite3-dense:2b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 58s |
| 3 | `nous-hermes:7b` | 11/18 | 3 | 0 | 2 | 1 | 0 | 61s |
| 3 | `wizard-math:7b` | 11/18 | 3 | 0 | 2 | 1 | 0 | 186s |
| 3 | `falcon3:3b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 108s |
| 3 | `falcon3:7b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 161s |
| 3 | `falcon3:10b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 189s |
| 3 | `gemma:2b-instruct-q8_0` | 11/18 | 2 | 2 | 1 | 1 | 0 | 76s |
| 3 | `zephyr:7b` | 11/18 | 3 | 0 | 2 | 1 | 0 | 81s |
| 3 | `qwen2.5:3b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 82s |
| 3 | `neural-chat:7b` | 11/18 | 3 | 0 | 2 | 1 | 0 | 83s |
| 3 | `gemma2:2b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 89s |
| 3 | `starling-lm:7b` | 11/18 | 3 | 0 | 2 | 1 | 0 | 96s |
| 3 | `phi3.5:3.8b-mini-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 106s |
| 3 | `qwen2:7b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 157s |
| 3 | `qwen2.5:7b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 160s |
| 3 | `internlm2:7b-chat-v2.5-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 161s |
| 3 | `gemma:7b-instruct-v1.1-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 172s |
| 3 | `llama2:13b-chat-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 238s |
| 3 | `solar:10.7b-instruct-v1-q8_0` | 11/18 | 3 | 0 | 2 | 1 | 0 | 372s |
| 3 | `gemma2:9b-instruct-q8_0` | 11/18 | 3 | 0 | 2 | 0 | 1 | 167s |
| 24 | `yi:9b-chat-v1.5-q8_0` | 10/18 | 2 | 0 | 4 | 0 | 0 | 164s |
| 25 | `deepseek-llm:7b-chat-q8_0` | 9/18 | 1 | 2 | 2 | 1 | 0 | 121s |
| 25 | `mistral:7b-instruct-v0.2-q8_0` | 9/18 | 1 | 2 | 2 | 1 | 0 | 141s |
| 25 | `granite3-dense:8b-instruct-q8_0` | 9/18 | 1 | 2 | 2 | 1 | 0 | 153s |
| 28 | `yi:6b-chat-q8_0` | 8/18 | 2 | 0 | 2 | 2 | 0 | 129s |
| 28 | `mistral:7b-instruct-v0.3-q8_0` | 8/18 | 0 | 3 | 2 | 1 | 0 | 142s |
| 28 | `llama3:8b-instruct-q8_0` | 8/18 | 2 | 0 | 2 | 2 | 0 | 155s |
| 28 | `llama3.2:3b-instruct-q8_0` | 8/18 | 2 | 0 | 2 | 1 | 0 | 69s |
| 28 | `nemotron-mini:4b-instruct-q8_0` | 8/18 | 2 | 0 | 2 | 1 | 1 | 97s |
| 33 | `qwen2.5:0.5b-instruct-q8_0` | 7/18 | 2 | 0 | 1 | 0 | 1 | 29s |
| 33 | `dolphin-phi:2.7b` | 7/18 | 2 | 0 | 1 | 0 | 1 | 39s |
| 35 | `openchat:7b` | 5/18 | 1 | 0 | 2 | 3 | 0 | 82s |
| 35 | `nous-hermes2:10.7b` | 5/18 | 1 | 0 | 2 | 3 | 0 | 121s |
| 37 | `phi:2.7b-chat-v2-q8_0` | 4/18 | 1 | 0 | 1 | 1 | 0 | 60s |
| 38 | `stablelm2:1.6b-chat-q8_0` | 3/18 | 1 | 0 | 0 | 1 | 0 | 50s |
| 38 | `llama2:7b-chat-q8_0` | 3/18 | 1 | 0 | 0 | 0 | 1 | 136s |
| 40 | `falcon3:1b-instruct-q8_0` | **1/18** | 0 | 0 | 1 | 0 | 5 | 41s |
| 40 | `phi4-mini-reasoning:3.8b` | **0/18** | 0 | 0 | 0 | 0 | 1 | 1s |
| 40 | `smollm2:360m-instruct-q8_0` | **0/18** | 0 | 0 | 0 | 0 | 1 | 14s |
| 40 | `tinyllama:1.1b-chat-v1-q8_0` | **0/18** | 0 | 0 | 0 | 0 | 1 | 22s |
| 40 | `orca-mini:3b` | **0/18** | 0 | 0 | 0 | 0 | 1 | 41s |

For reference, the **operator's currently-deployed model**
`phi4:14b-q8_0` (not re-tested in this sweep; smoke-tested
earlier the same day): 10/18 (2 OK / 1 OVERSHOOT / 2 ADJACENT / 1
WRONG / 0 ERR). Sits between the 11/18 cluster and the 9-8/18
tier.

## Findings

### llama3.1:8b is the standalone leader

`llama3.1:8b-instruct-q8_0` is the only model to break the 11/18
cluster ceiling. 4 OK + 2 ADJACENT + 0 WRONG. Notably, it has
**zero wrong-direction calls** — every scenario got at least
the right direction. The 2 ADJACENTs are `hold ↔ tighten/widen`
mismatches, the closest possible misses.

Worth standalone evaluation as the advisor for `cli/advise`.
Operator currently runs `phi4:14b-q8_0` (10/18); switching to
`llama3.1:8b` would be a 40% score improvement and a 4× smaller
model. Caveat: this is one snapshot of one fixture battery; a
second sweep at different fixture parameters would build
confidence.

### wizard-math:13b is the strongest math specialist by score

Added 2026-05-25 follow-up sweep after operator caught the tag
typo (Ollama's library uses `wizard-math`, not `wizardmath`).
The 13B variant scored **13/18** — second only to llama3.1:8b
across the entire sweep. 4 OK + 1 ADJACENT + 1 WRONG.

The wizard-math:13b vs mathstral:7b trade-off is real:

| Metric | wizard-math:13b | mathstral:7b |
|---|---|---|
| Score | 13/18 | 12/18 |
| OK count | 4 | 3 |
| WRONG count | **1** | **0** |
| Disk (q4_K_M) | ~7-8 GB | 4.1 GB |
| Disk (q8_0) | ~14 GB | 7.5 GB |

wizard-math:13b has the higher ceiling (one more OK verdict) but
makes one wrong-direction call. mathstral:7b never goes the
wrong direction across any tested fixture. For a role that
drives real-money grid params, the "never wrong direction"
property may matter more than 1 extra correct verdict —
especially under model temperature where a wrong call once-per-N
cycles compounds into bad params.

`wizard-math:7b` scored 11/18 in the same sweep — the 13B variant
genuinely benefits from scale on this task.

### Mathstral quantization is NOT the limiting factor

The 2026-05-25 follow-up tested both `mathstral:7b` (Ollama's
default plain-tag, which resolves to q4_K_M at 4.1 GB) AND the
explicit `mathstral:7b-v0.1-q8_0` (7.5 GB). **Identical scores:**

| Tag | Score | OK | ADJ | WR |
|---|---|---|---|---|
| `mathstral:7b` (q4_K_M) | 12/18 | 3 | 3 | 0 |
| `mathstral:7b-v0.1-q8_0` | 12/18 | 3 | 3 | 0 |

The 3 ADJACENT verdicts are model-capability gaps on this
prompt, not quant-precision gaps. fp16 (untested) wouldn't help
either by extrapolation — the reasoning ceiling is saturated at
q4 already.

**Practical implication:** operators wanting mathstral can use
the q4_K_M variant at 4.1 GB rather than q8_0 at 7.5 GB without
quality penalty. ~45% disk savings for the same score.

### falcon3:3b ties top-tier at one-third the size (operator-assistant)

Tested in the 2026-05-25 follow-up. In the advisor sweep,
`falcon3:3b-instruct-q8_0` scored 11/18 — same cluster as most
mid-tier candidates. **But in the operator-assistant probe
(separate sweep, see operator-llm-models.md), falcon3:3b scored
13/15** — matching granite3-dense:8b's top-tier score at less
than half the size. Strong scaling story for low-end-hardware
operator-assistant use.

falcon3:7b and falcon3:10b both scored 11/15 with 1 error in
the operator-assistant sweep AND 11/18 with 1 WRONG in the
advisor sweep — no scaling benefit past 3B for either role.
The 1B variant is below the schema-following threshold (5
errors out of 6 advisor scenarios; 2/15 routing on the
operator-assistant probe).

### Math specialists validate the doc's hypothesis

The [operator-llm-models.md](operator-llm-models.md) doc
explicitly flagged math specialists as advisor candidates while
rejecting them for the operator-assistant role:

> *"Scope note: these rejections apply to the OPERATOR-ASSISTANT
> role only. WobbleBot is fundamentally a numerical-reasoning
> application — prices, percentages, ratios, fee accounting,
> volatility, position sizing — so math specialists have several
> plausible high-value homes. Candidate roles for math-specialist
> LLMs: 1. MoE quant-expert (Phase 3.4's `config/prompts/quant.md`
> advisor slot)."*

**`mathstral:7b` scored 12/18, second overall, with zero
wrong-direction calls.** 7B params, 31s probe time (fastest in
the 8B-and-above tier). This validates the hypothesis: the same
schema-following model class that pattern-matched every operator
message to a quadratic equation can produce sensible
grid-tuning recommendations when the input IS numerical analysis.

`phi4-mini-reasoning:3.8b` scored 0/18 (errored on all 6
scenarios — exactly the "always emit math prose, never valid
JSON" failure mode predicted in the operator doc). This was
expected; the model would need a tuned advisor-specific prompt
to elicit schema-conforming output. **Queued as a v1.1
follow-up:** craft a math-specialist-friendly variant of
`quant.md` and re-sweep `phi4-mini-reasoning` + `mathstral`
together to see if the prompt change closes the gap.

`wizardmath:7b` and `wizardmath:13b` are **not in Ollama's
library** under those tags. The pull failed for both with
`pull model manifest: file does not exist`. Treat as
unavailable for now.

### The 11/18 cluster: 21 models converge to the same behavior

21 of 43 successfully-probed models scored **exactly 11/18**.
Almost all share the same verdict pattern: **3 OK / 0 OVERSHOOT /
2 ADJACENT / 1 WRONG**. Across model families (llama / qwen /
mistral / gemma / phi / granite / smollm / nous-hermes /
zephyr / starling / neural-chat / internlm), parameter counts
(0.5B to 13B), and tunings (instruct / chat / general).

This convergence is signal: **the `quant.md` prompt steers most
general-purpose models toward the same baseline recommendation
strategy** ("slight widen across the board"). Reasoning capacity
is not the dominant variable inside this cluster — prompt steering
is.

`llama3.1:8b`'s ability to break the ceiling, and the `chat`-tuned
fall-throughs below (see next finding), suggest that there ARE
models that reason differently against this prompt — they're just
the minority.

### Surprising: chat-tuned models regress badly

Two models that were **top performers in the operator-assistant
sweep** scored at the bottom of the advisor sweep:

| Model | Operator-Assistant (2026-05-24) | Advisor (2026-05-25) |
|---|---|---|
| `nous-hermes2:10.7b` | 12/14 → 13/15 multi-turn (top tier) | **5/18** (3 WRONG) |
| `openchat:7b` | 12/14 (top tier) | **5/18** (3 WRONG) |

Both produced 3 wrong-direction recommendations (out of 6
scenarios) — actively suggesting the OPPOSITE of what the
fixture asked for. Hypothesis: chat-tuned models are
discriminative (good at intent classification) but weak at
numerical reasoning over engineering metrics. Different skill
sets — the operator-assistant role rewards "what bucket does
this fit in?", the advisor role rewards "given this numerical
state, what direction should the params move?".

**Implication for the MoE design (Phase 3.4a):** the three-
expert architecture (quant / risk / news) makes more sense in
light of this finding. A `news` expert doesn't need numerical
reasoning skills; a `quant` expert needs little else. Picking
the right model per role matters more than picking one model
for everything.

### Pull failures + "tag does not exist"

| Tag | Status |
|---|---|
| `wizardmath:7b` | NOT FOUND on Ollama (2026-05-25) |
| `wizardmath:13b` | NOT FOUND on Ollama (2026-05-25) |

The advisor sweep's candidate list keeps these tags listed (with
the failure status) so future contributors don't re-attempt them
without verifying Ollama's library first. Math-specialist
coverage is currently limited to `mathstral:7b` until / unless
the WizardMath family returns to the library OR an alternative
math-specialist appears.

### Schema-error tier (0/18 with 1 ERR)

| Model | Likely failure mode |
|---|---|
| `phi4-mini-reasoning:3.8b` | Math-mode reasoning, no JSON |
| `smollm2:360m-instruct-q8_0` | Below schema-following threshold (360M params) |
| `tinyllama:1.1b-chat-v1-q8_0` | Below schema-following threshold (1.1B) |
| `orca-mini:3b` | Pre-instruct-tuning generation; weak JSON output |

These are not viable for the advisor role. `phi4-mini-reasoning`
is the candidate worth revisiting with a tuned prompt (see
math-specialist section above).

## Recommendations

### Best overall (replace the current default?)

`llama3.1:8b-instruct-q8_0` — 14/18, zero wrong-direction calls.
4× smaller than the current `phi4:14b-q8_0` default. Strong
candidate for a `cli/advise` swap, but worth a re-sweep at
different fixture parameters before committing.

### Best math-reasoning fit

`mathstral:7b` — 12/18, zero wrong-direction calls, fastest probe
in the 7B class at 31s. Specifically validates the math-specialist-
in-advisor-role hypothesis. Would slot naturally into the future
Phase 3.4a MoE `quant` expert seat.

### Avoid at this prompt

- `nous-hermes2:10.7b` — 5/18, 3 wrong-direction calls. Excellent
  at intent classification (operator-assistant), poor at advisor
  numerical reasoning.
- `openchat:7b` — same pattern.
- `phi4-mini-reasoning:3.8b` — math-mode output, no valid JSON
  against the current quant prompt.
- Sub-1B models (`smollm2:360m`, `tinyllama`, `orca-mini`) — below
  the schema-following capacity threshold.

## v1.1 follow-ups surfaced by this sweep

1. **Math-specialist-tuned variant of `quant.md`** to elicit
   schema-conforming output from `phi4-mini-reasoning` and to
   measure mathstral's ceiling with a friendlier prompt. Operator
   explicitly flagged this during the 2026-05-25 sweep design:
   *"we may find that we need a special prompt for the math
   specialists, tuned so that they give proper responses."*
2. **Second sweep at different fixture parameters** to build
   confidence in `llama3.1:8b`'s lead vs the 11/18 cluster.
   Current sweep is one snapshot; the cluster's tightness suggests
   prompt-steering may dominate model differences, which would mean
   shifting fixtures could re-rank substantially.
3. **Bigger fixture battery** — 6 scenarios may be too few to
   discriminate finely. Adding regime variants (e.g. low-vol
   uptrend, high-vol uptrend, choppy + drawdown, etc.) would
   widen the differentiation surface.

## How to add a new model to this list

1. `ollama pull <model>`
2. `python tools/probe_advisor.py --model <model>`
3. Note the score + per-verdict counts from the summary table.
4. Append to the results table above, ranked by score.

## Related

- `tools/probe_advisor.py` — single-model probe (LLM-only).
- `tools/pull_and_probe_advisors.py` — sweep batch driver.
- [operator-llm-models.md](operator-llm-models.md) — sister doc
  for the operator-assistant role.
- `config/prompts/quant.md` — the system prompt every advisor is
  scored against. Changes here invalidate prior compatibility
  data.
