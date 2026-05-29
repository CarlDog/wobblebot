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
on **2026-05-25** against `config/prompts/quant.md`. Two sweeps
fed the table: the broad new-pulls sweep ran against the memory-
card model store; the 9-model pre-existing-models sweep (gemma4 /
qwq / qwen3.6 / nemotron3 / deepseek-r1 / mistral-nemo / phi4 /
phi4-reasoning / granite4.1) ran against the NVMe-resident store.
Elapsed times are therefore not comparable across rows.

## Rev 2026-05-29 — 12-fixture battery + hardened rubric (CURRENT)

The 6-fixture battery used by the 2026-05-25 sweep below was
**superseded** on 2026-05-29, ahead of the CPU-only NAS advisor
sweep. The originals were gameable — a constant "+10% widen" scored
the documented "11/18 lazy baseline" by accident of fixture
distribution. The current battery (`tools/probe_advisor.py`):

- **12 fixtures, balanced 4 widen / 4 hold / 4 tighten.** Current
  spacing is decoupled from direction — each direction spans the full
  spacing range, with overlap fixtures (widen at high spacing, tighten
  at low) so a model must read volatility *relative to* the current
  grid. Correct direction = `sign(ideal(vol) - current)`; the
  ideal-vs-vol curve lives in the probe's module docstring.
- **No-partial-credit rubric** (max 36): OK=3 (right direction +
  magnitude within ±30% of ideal), OVERSHOOT=2 (right direction, wrong
  size), MISS / OVERTRADE / WRONG / ERROR = 0. Failing to act and
  needless action both score 0, which closes the always-hold loophole
  (now 33% = chance) the old ADJACENT=1 rubric left open.
- **Inherent constant ceiling ~52%** (19/36 — a constant near median
  spacing). Can't be driven lower without penalizing real reasoners or
  reintroducing dead zones, so the SCORE ranks reasoners (~75%+) above
  constants, but the per-fixture **verdict profile** is the real
  discriminator: a reasoner spreads OK across all three directions
  with ~zero WRONG; a constant clusters OK on one direction with WRONG
  on the opposite. **Always inspect the top model's profile, not just
  the headline score.** If no candidate clears ~60%, no NAS-viable
  model reasons well for this task — itself a useful result.

**Ground truth is no longer one maintainer's call.** The 12 fixtures'
expected directions were independently re-derived by two separate
5-agent blind adjudications (anonymized metrics, no answer key, no
ideal curve) — **12/12 unanimous both times**. The caveat below still
applies to the *magnitude* targets (the ideal-vs-vol curve is
judgment), but the *direction* labels are now strongly corroborated.
Two adversarial code-review workflows over the tooling caught and
fixed 6 defects total (one high-severity: a truncated model pull was
being laundered into a fake 0/36 result).

The 2026-05-25 results table below is retained for history; its scores
are **not comparable** to the 12-fixture battery (different fixtures,
different rubric, different max).

## ⚠️ Methodology caveat — read this before interpreting any score

The "expected direction" per fixture is **one maintainer's
informed-but-fallible judgment**, not ground truth. The probe
measures agreement with that single evaluator's reasoning. Three
specific consequences follow:

### The maintainer is the baseline

Every fixture's expected direction (`tighten`/`hold`/`widen`) was
declared by the same person who designed the scenarios, the
scoring weights, and the magnitude bounds. A model that scores
WRONG against my answer key may be reasoning *better* than I am —
especially in ambiguous regimes. The probe is a measure of
**agreement with one biased rubric**, not objective recommendation
quality.

### The 11/18 cluster ties an "always slight widen" baseline

A constant-output strategy that emits `spacing_percentage = 1.1`
(+10% widening) for every scenario scores exactly **11/18**
against the 2026-05-25 fixture set:

| Fixture | Expected | "Always +10% widen" verdict | Pts |
|---|---|---|---|
| quiet_market | tighten | WRONG | 0 |
| healthy_churn | hold | ADJACENT | 1 |
| whipsaw | widen | OK | 3 |
| trending_up | hold | ADJACENT | 1 |
| trending_down | widen | OK | 3 |
| post_cap_trip | widen | OK | 3 |
| **Total** | | | **11/18** |

The 21+ models hitting exactly 11/18 in the results table below
**tie this lazy baseline** and may not represent meaningful
reasoning — they could be emitting the same fixed widen across
all fixtures and happening to score well by accident of the
fixture distribution (3 WIDEN, 2 HOLD, 1 TIGHTEN — biased toward
widen-expected). Distinguishing "real reasoning that lands on
widen" from "lazy widen that lands on the right answer
accidentally" requires the v1.1 redesigned probe (multi-shot at
T=0.5, more balanced fixture distribution, internal-coherence
scoring) or the v1.1 **auditor** (objective evaluation against
historical outcomes).

### What this probe IS good for

- **Schema-validity filtering** (objective): models that score
  0/18 with non-zero ERR counts genuinely can't follow the
  `advisor_recommendation_v1` schema. That part doesn't depend
  on the maintainer's answer key.
- **Strong differentiators** (probably real): scores materially
  above the 11/18 lazy-baseline (12/18+, especially with low
  WRONG counts) DO indicate the model is doing something
  different from the lazy strategy. `llama3.1:8b` at 14/18 with
  0 WRONG and `wizard-math:13b` at 13/18 are the clearest cases
  of real reasoning signal.
- **Wrong-direction outliers**: scores below the lazy baseline
  (5/18 with 3+ WRONG, like nous-hermes2 and openchat) ARE
  recommending the OPPOSITE of the maintainer's calls — that's
  objective behavior worth flagging, even if "opposite" doesn't
  prove "incorrect".

### What this probe is NOT good for

- Benchmark-grade evaluation of advisor quality.
- Claiming "Model X is objectively better at the advisor role
  than Model Y" without auditor corroboration.
- Replacing the operator's currently-deployed advisor (phi4)
  with a sweep winner (llama3.1:8b) based on this data alone.

The v1.1 **auditor** (planned, see `docs/release/v1.1/adaptive-grid.md`)
will provide the objective evaluation. Until it lands, treat
sweep rankings as **directional first-pass signal** — useful for
deciding which models earn the cost of an auditor run.

### Redesign queued for v1.1

The methodology fixes — balanced fixture distribution, multi-shot
at the operator's T=0.5, confidence-calibration scoring,
internal-coherence scoring, magnitude bounds matched to
`auto_apply.*` config — are documented as a follow-up in
`docs/release/v1.1/adaptive-grid.md` (Advisor probe v2). The
current data should be reinterpreted once that redesigned probe
ships.

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
| 3 | `gemma4:e4b-it-q8_0` (NVMe) | 11/18 | 3 | 0 | 2 | 1 | 0 | 51s |
| 3 | `qwq:32b-q8_0` (NVMe) | 11/18 | 3 | 0 | 2 | 1 | 0 | 227s |
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
| 26 | `yi:9b-chat-v1.5-q8_0` | 10/18 | 2 | 0 | 4 | 0 | 0 | 164s |
| 26 | `nemotron3:33b` (NVMe) | 10/18 | 2 | 0 | 4 | 0 | 0 | 56s |
| 26 | `deepseek-r1:14b-qwen-distill-q8_0` (NVMe) | 10/18 | 2 | 1 | 2 | 0 | 1 | 611s |
| 29 | `deepseek-llm:7b-chat-q8_0` | 9/18 | 1 | 2 | 2 | 1 | 0 | 121s |
| 29 | `mistral:7b-instruct-v0.2-q8_0` | 9/18 | 1 | 2 | 2 | 1 | 0 | 141s |
| 29 | `granite3-dense:8b-instruct-q8_0` | 9/18 | 1 | 2 | 2 | 1 | 0 | 153s |
| 29 | `phi4:14b-q8_0` (NVMe; **operator's currently-deployed**) | 9/18 | 1 | 2 | 2 | 1 | 0 | 175s |
| 33 | `yi:6b-chat-q8_0` | 8/18 | 2 | 0 | 2 | 2 | 0 | 129s |
| 33 | `mistral:7b-instruct-v0.3-q8_0` | 8/18 | 0 | 3 | 2 | 1 | 0 | 142s |
| 33 | `llama3:8b-instruct-q8_0` | 8/18 | 2 | 0 | 2 | 2 | 0 | 155s |
| 33 | `llama3.2:3b-instruct-q8_0` | 8/18 | 2 | 0 | 2 | 1 | 0 | 69s |
| 33 | `nemotron-mini:4b-instruct-q8_0` | 8/18 | 2 | 0 | 2 | 1 | 1 | 97s |
| 33 | `qwen3.6:35b-a3b-q8_0` (NVMe) | 8/18 | 0 | 3 | 2 | 1 | 0 | 81s |
| 33 | `mistral-nemo:12b-instruct-2407-q8_0` (NVMe) | 8/18 | 0 | 3 | 2 | 1 | 0 | 128s |
| 33 | `qwen2.5:0.5b-instruct-q8_0` | 7/18 | 2 | 0 | 1 | 0 | 1 | 29s |
| 33 | `dolphin-phi:2.7b` | 7/18 | 2 | 0 | 1 | 0 | 1 | 39s |
| 42 | `openchat:7b` | 5/18 | 1 | 0 | 2 | 3 | 0 | 82s |
| 42 | `nous-hermes2:10.7b` | 5/18 | 1 | 0 | 2 | 3 | 0 | 121s |
| 42 | `granite4.1:30b-q5_K_M` (NVMe) | **5/18** | 1 | 0 | 2 | 3 | 0 | 287s |
| 37 | `phi:2.7b-chat-v2-q8_0` | 4/18 | 1 | 0 | 1 | 1 | 0 | 60s |
| 38 | `stablelm2:1.6b-chat-q8_0` | 3/18 | 1 | 0 | 0 | 1 | 0 | 50s |
| 38 | `llama2:7b-chat-q8_0` | 3/18 | 1 | 0 | 0 | 0 | 1 | 136s |
| 40 | `falcon3:1b-instruct-q8_0` | **1/18** | 0 | 0 | 1 | 0 | 5 | 41s |
| 40 | `phi4-mini-reasoning:3.8b` | **0/18** | 0 | 0 | 0 | 0 | 1 | 1s |
| 40 | `smollm2:360m-instruct-q8_0` | **0/18** | 0 | 0 | 0 | 0 | 1 | 14s |
| 40 | `tinyllama:1.1b-chat-v1-q8_0` | **0/18** | 0 | 0 | 0 | 0 | 1 | 22s |
| 40 | `orca-mini:3b` | **0/18** | 0 | 0 | 0 | 0 | 1 | 41s |
| TIMEOUT | `phi4-reasoning:14b-plus-q8_0` (NVMe) | — | — | — | — | — | — | timed out |

For reference, the **operator's currently-deployed model**
`phi4:14b-q8_0` scored **9/18** in the NVMe pre-existing-models
sweep (1 OK / 2 OVERSHOOT / 2 ADJACENT / 1 WRONG / 0 ERR). Sits
just below the 11/18 lazy-baseline cluster — directionally
correct in 5/6 fixtures but with two magnitude overshoots and
one wrong-direction call. (An earlier same-day smoke test had
suggested 10/18; the multi-shot rerun in the formal sweep
landed slightly lower, likely T=0.5 stochastic variance — see
methodology caveat.)

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
JSON" failure mode predicted in the operator doc).

**2026-05-25 sweep results:**

| Config | Score | Notes |
|---|---|---|
| Baseline (quant.md, no force_json) | 0/18 | Math-mode reasoning, no JSON |
| `--force-json` (quant.md, 1288 chars) | **11/18** | Lazy baseline — emits `spacing=1.2` for every fixture |
| `--force-json --prompt-file quant-compact.md` | 8/18 | WORSE — emits `spacing=2.0` (OVERSHOOT) for 4 of 6 fixtures |

The `--force-json` fix recovers the model to lazy-baseline level.
**The compact `quant-compact.md` draft is strictly worse** —
dropping the "argue from numbers, not sentiment" constraint let
the model over-widen to spacing=2.0 (+100%) which exceeds the
±25% magnitude band → OVERSHOOT verdicts (2 pts each instead of
OK's 3). Magnitude-anchoring constraints must be preserved in
any compact-prompt redesign.

Standard `quant.md` at 1288 chars is short enough for the 3.8B
model's attention budget when `format=json` constrains output;
the "prompt-length saturation" theory applies primarily to the
8706-char `operator.md`, not to advisor prompts.

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

### granite4.1:30b is a wrong-direction outlier (real signal)

`granite4.1:30b-q5_K_M` scored **5/18 with 3 WRONG** — same
disqualifying pattern as `nous-hermes2:10.7b` and `openchat:7b`,
the existing wrong-direction outliers from the broad sweep.
Even under the methodology caveat above, scoring below the
"always slight widen" lazy baseline with 3+ WRONG calls is
**objective behavior**: the model is recommending the OPPOSITE
direction from the maintainer's calls on half the fixtures,
regardless of whether the maintainer's calls are themselves
optimal. **Disqualifying for advisor-role consideration**, same
as the other wrong-direction outliers. (granite4.1 is also
notable as a fresh 2025-era model with strong scores on
general-purpose benchmarks — the advisor role apparently
exercises a different skill profile than those benchmarks
measure.)

### Reasoning-tuned models can't complete the battery quickly (the diagnostic later proved the cause is probe budget, not model)

Two reasoning-tuned models in the NVMe sweep showed atypical
behavior at the probe's default per-call timeout:

| Model | Result | Time |
|---|---|---|
| `phi4-reasoning:14b-plus-q8_0` | **TIMED OUT** | — |
| `deepseek-r1:14b-qwen-distill-q8_0` | 10/18 | **611s** (10× the median 7B time) |

**2026-05-25 sweep results (`tools/sweep_reasoning_fixes.py`):**

| Model | Config | Score | Elapsed |
|---|---|---|---|
| `phi4-reasoning:14b-plus-q8_0` | `--force-json` | **11/18** | 131s |
| `deepseek-r1:14b-qwen-distill-q8_0` | `--force-json` | **0/18 (6 ERR)** | 35s |

**phi4-reasoning:14b-plus:** the "TIMEOUT" was a probe artifact.
Under `--force-json`, the model emits clean JSON in <100 chars
with zero `<think>` preamble. Lands in the 11/18 lazy-baseline
cluster — same caveat as the 21 other models there.

**deepseek-r1:14b-qwen-distill: surprise — `--force-json` BREAKS
this model on the advisor's `/api/generate` endpoint.** All 6
fixtures errored with empty `{}` dicts or fabricated non-schema
JSON (e.g. `{"command":"cancel open orders"...}`). The original
"thinking models degenerate to `{}` under `format=json`" heuristic
in the adapter was CORRECT for this model on this endpoint. The
free-text extraction path is still the right one for deepseek-r1
on advisor.

Asymmetry: the same `--force-json` flag on `/api/chat` (operator
role) works fine for deepseek-r1 (full routing fidelity at 25s/call
vs 44s baseline). `format=json` constraint apparently behaves
differently across Ollama's two endpoints for this model family.

**Two distinct failure modes** identified across reasoning-tuned
candidates, each with a different fix:

| Variant | Failure mode | Fix |
|---|---|---|
| **Small (3.8B-class)** like `phi4-mini-reasoning:3.8b` | Long system prompts saturate the model's attention budget; falls back to training-default output (math-textbook for math-tuned variants) | Compact prompt (<300 chars) + `format=json` |
| **Large (14B+)** like `phi4-reasoning:14b-plus`, `deepseek-r1:14b-qwen-distill` | Unbounded chain-of-thought consumes the probe's `num_predict` budget before JSON emission | `format=json` (suppresses `<think>`) OR raise `num_predict` past 4000 |

**Implication for production use (revised 2026-05-26 after v2
follow-up):** the original "reasoning latency disqualifies these
models" verdict has been re-investigated across two rounds and
returns to "not recommended." With `format=json` the chain-of-
thought is gone and the latency envelope matches non-reasoning
models — but the SCORES sit at the 11/18 lazy-baseline cluster,
indistinguishable from "always slight widen." phi4-mini-reasoning
specifically failed both the 2026-05-25 first-pass compact prompt
(8/18 over-widen) and the 2026-05-26 v2 compact prompt (4/18 with
4 errors) — definitively incompatible at 3.8B params. The 14B+
reasoning models work but don't justify their latency over
non-reasoning peers. **Reasoning-model support is dropped from
v1.1 active work** — see the revised entry in
`docs/release/v1.1/operator-ux.md`. The diagnose-before-blocklist
methodology proved its value (testing the verdict twice gives
confidence in the answer); it did NOT recover the model.

### nemotron3:33b is the only "calibrated" model in the sweep

`nemotron3:33b` (NVMe) scored 10/18 with a **distinctive verdict
profile: 2 OK / 0 OVERSHOOT / 4 ADJACENT / 0 WRONG / 0 ERR**.
Out of all 50+ models tested, only `yi:9b-chat-v1.5-q8_0` and
`nemotron3:33b` share this signature: zero wrong-direction
calls AND zero magnitude overshoots, with multiple ADJACENT
verdicts indicating a `hold`-biased reasoning posture — the
model tends to recommend `hold` when the maintainer expected
`tighten`/`widen` (one step off, not opposite).

This is a *conservative* failure mode rather than a *reactive*
one. For a role that drives real-money grid params, "hesitant
to change anything" is arguably safer than "confidently wrong"
even when the raw score is lower than the 11/18 cluster. Worth
considering for the v1.1 MoE `risk` expert seat (Phase 3.4a),
where the risk-counterpart-to-quant role explicitly rewards
conservatism.

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
- `granite4.1:30b-q5_K_M` — 5/18, 3 wrong-direction calls. Joins
  the wrong-direction outlier tier despite being a substantially
  larger and more recent model than the other two.
- `phi4-mini-reasoning:3.8b` — **incompatible (confirmed
  2026-05-26 v2 follow-up).** Two rounds of investigation:
  the 2026-05-25 diagnostic surfaced prompt-length saturation
  and a first-pass compact prompt produced 8/18 (over-widen
  pattern, OVERSHOOT band). The 2026-05-26 v2 compact prompt
  added the ±25% magnitude rule that v1 dropped — model
  errored on 4/6 fixtures (4/18 total, regression from v1). At
  3.8B params + reasoning fine-tuning the model can EITHER
  emit valid JSON under a short prompt OR honor magnitude
  constraints under a longer prompt, not both. Prompt redesign
  is not a path forward. Stays on the incompatible list.
- `phi4-reasoning:14b-plus-q8_0` — **not recommended.** Under
  `--force-json` it scores 11/18 (ties the lazy-baseline
  cluster) — the TIMEOUT verdict was a probe artifact, but the
  rehabilitated score emits `spacing=1.2` for every fixture
  (the literal "always slight widen" baseline). Adds 131s of
  inference latency vs ~31-90s for non-reasoning advisor
  models in the same cluster. No differentiation justifies
  the latency. Use a non-reasoning advisor model from the
  recommended tier instead.
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
4. **Configurable per-fixture timeout** for reasoning-tuned
   models. The current probe budget caused `phi4-reasoning:14b-plus`
   to TIMEOUT entirely and `deepseek-r1:14b-qwen-distill` to take
   611s (10× the median 7B time). A `--per-fixture-timeout-seconds`
   knob would let the operator separate "model emits invalid
   output" from "model is just slow," producing fairer quality
   data even where latency rules a model out operationally.

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
