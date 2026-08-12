# Trading-Advisor LLM Compatibility Matrix

> **Looking for "which model holds which seat, and why?"** — that index
> is [`advisor-seats.md`](advisor-seats.md). This file is the evidence
> behind it: the bake-off records, per-model scores, and rejected
> challengers, newest rev first.


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

## Rev 2026-08-11e — DECISIVE: `grok-4.5` beats the champion significantly on gen3

8 rounds on `gen3`, **168 judgments each**. This is the run that settles
the direction question the 3-round bake-off left at p=0.076.

| Model | OK/168 | OK% | SUB | **UNSAFE** | tau mean | $/call |
|---|---|---|---|---|---|---|
| **`xai/grok-4.5`** | **160** | **95%** | 8 | **0** | +0.35 | $0.00781 |
| `gpt-5-mini` (champion) | 141 | 84% | 17 | **10** | +0.26 | $0.00206 |

**Fisher exact: OK p=0.001045, UNSAFE p=0.0017.** Power was computed
BEFORE running (resampling the observed per-run scores gave ~100% at +5
rounds, projected p=0.0010) — the run landed within 5% of the
projection, which is itself a check that the model of the process was
right.

**Not a ceiling artifact this time.** Per-run OK for grok:
20,19,21,19,20,21,19,21 — it drops fixtures on most runs, so the test
has real variance to work against. Contrast `hard`, where 119/120 left
almost none and manufactured p=0.000041 from an exhausted battery
(Rev 2026-08-11c).

**Zero unsafe calls in 168 judgments vs the champion's TEN** — one per
17 judgments. On a battery whose forbidden call is the actively
dangerous one, that is a safety property rather than a scoring nicety.

### ⚠️ Correction: the calibration gap CONVERGED

At 3 runs, grok +0.44 vs champion +0.30 was written up as grok's
clearest and most consistent advantage. At 8 runs it is **+0.35 vs
+0.26** — still ahead, but modest. **Direction and safety are the real
separation; calibration is not.** A three-run tau on 21 points was
noisier than the write-up implied.

### The decision is now purely cost

grok is significantly better on both deciding axes at **3.8x** the
champion (~$0.38/day, ~$11/month at ADR-022 full escalation, on a bot
running $10 orders with $60 exposure). No statistical ambiguity remains.

**Flagged for the routine itself:** the ≤3x cost pre-filter would have
excluded grok from the roster. It was probed only because the operator
overrode that gate, and it is the only model that has ever beaten the
incumbent. If the switch happens, that threshold should be revisited —
it nearly cost us the answer.

**NOT SWITCHED — operator's call, live-money config change.**

Cloud spend for this run: **$1.02** (210 judgments).

## Rev 2026-08-11d — constant-baseline audit of EVERY battery

The check that demolished freejudge-v1 had only ever been run on the
quant track. Applied uniformly to all seven fixture sets so each seat's
evidence is judged by one standard. Offline, free, deterministic.

**A battery is sound when no constant strategy scores near the best real
model. A battery a rock can pass measures nothing.**

| battery | best constant | best real model | margin | verdict |
|---|---|---|---|---|
| `freejudge/v1` | **86%** (HOLD) | gpt-5-mini 83% | **−3%** | ROCK BEATS CHAMPION |
| **`quant/heldout`** | **75%** (HOLD) | sonnet-5 71% | **−4%** | **ROCK BEATS CHAMPION** |
| `quant/core` | 33% | gpt-5-mini 83% | +50% | sound |
| `freejudge/hard` | 33% | grok-4.5 99% | +66% | sound |
| `freejudge/gen3` | 33% | grok-4.5 95% | +62% | sound |
| `arbitrator` | 50% (OMIT) | gpt-5-mini 100% | +50% | sound |
| `news` | 58% (echo current) | four models 100% | +42% | sound |

### ⚠️ `quant/heldout` is a SECOND degenerate battery

**Constant-HOLD scores 18/24 = 75%, beating every model ever measured on
it.** `claude-sonnet-5`'s 17/24 was written up on 2026-08-10 as "the best
held-out score ever measured" — a model that answers `hold` to everything
beats it. The set was already known to be off-contract (only 3 of 8
fixtures escalate; the rest are guard-resolved), and it is ALSO
hold-degenerate. Two independent reasons it cannot support a seat
decision.

This retroactively explains the 2026-07-31 note that `gpt-5.4-nano`'s
"best-in-field heldout 14/24 is the hold-more bias flattering the
maintainer curve" — correct by intuition, never measured. It is measured
now: the bias is worth 75 percentage points to a model that does nothing.

**Neither v1 nor heldout is being "fixed."** Both stay exactly as they
are so every historical score remains comparable — the same reasoning
that kept v1 frozen. They are marked unusable for seat selection, not
repaired.

### The arbitrator and news batteries are STRUCTURALLY SOUND

Constant-OMIT scores 50% on arbitrator and constant-echo 58% on news,
against real models at 100% — margins of +50% and +42%. The concern that
those seats rested on rock-passable evidence was WRONG; both batteries
discriminate.

**Their weakness is sample size, not degeneracy:** 8 and 12 fixtures,
single runs. That is a far cheaper fix than a rebuild — more runs and
more fixtures, not a new instrument. Recorded so the next person does
not mistake "thin" for "broken".

### Why this audit existed at all

The operator asked why the quant advisor had absorbed so much more effort
than the other seats. Roughly a third of the imbalance is justified —
quant is the only seat with a live path (the deployed profile is
`engine: cascade, type: single`). The rest is momentum: the quant track
had the most prior investment, so it had the most surface area for
defects, and each finding generated the next.

The tell is inside the quant track itself: every hard-won standard —
constant baselines, ceiling checks, calibration — was applied to
freejudge while `quant/heldout` sat unaudited beside it, and its scores
were still being cited the same day. Asymmetric rigour, not just
asymmetric attention.

## Rev 2026-08-11c — gen3 bake-off: the `hard` significance was CEILING-INFLATED

Run on `gen3` (21 fixtures, built the same day because grok ceilinged
`hard` at 119/120), 4 models x 3 runs = **252 judgments**. Constant
baseline 33%. Round-interleaved — all four models at r1, then r2, then
r3 — so an early stop leaves a COMPLETE round for every model rather
than full data for whoever ran first.

| Model | OK/63 | OK% | SUB | **UNSAFE** | per-run | **tau_b mean** | $/call |
|---|---|---|---|---|---|---|---|
| **`xai/grok-4.5`** | **60** | **95%** | 3 | **0** | 20,19,21 | **+0.44** | $0.00781 |
| `deepseek-ai/deepseek-v4-pro` | 55 | 87% | 7 | 0 | 17,18,20 | −0.06 | $0.00504 |
| `gpt-5-mini` (champion) | 53 | 84% | 7 | **3** | 18,18,17 | +0.30 | $0.00206 |
| `minimaxai/minimax-m3` | 49 | 78% | 13 | 1 | 17,17,15 | −0.07 | $0.00039 |

Fisher exact vs champion: **grok OK p=0.076**, **UNSAFE (0 vs 3)
p=0.244**, deepseek p=0.80, minimax p=0.50. **Nothing clears 0.05.**

### ⚠️ The methodological finding — this generalizes beyond wobblebot

Rev 2026-08-11b recorded grok at **p=0.000041** on `hard`. That
arithmetic is correct and the data is real, but the result is an
**artifact of grok scoring 119/120 there**: a near-perfect score has
almost no variance for a significance test to work against, so the
p-value collapses toward zero. Give the same model room to be imperfect
— 95% on gen3 — and the identical comparison yields **p=0.076**. Three
orders of magnitude, same models, same prompt, harder fixtures.

**A challenger that ceilings your battery will always look
statistically overwhelming.** The fix is a harder instrument, not more
runs — more runs on a saturated set only sharpen a number that is
measuring the ceiling rather than the model. This is the fourth
instrument defect in this arc and the subtlest: the battery was not
wrong, it was EXHAUSTED, and exhaustion looks like overwhelming
evidence.

### What survived the harder test

**`grok-4.5` leads every axis**, and is the only model with **stable
calibration**: tau_b +0.45 / +0.43 / +0.45, above the "tracks evidence"
threshold on every run. The champion swings +0.40 / +0.18 / +0.31, and
both other challengers average NEGATIVE — more confident as evidence
thins. Calibration is the one axis where the gap is consistent rather
than marginal.

### What did not

**`minimax-m3` COLLAPSES.** 84% on `hard` → **78% on gen3**, below the
champion, with 13 SUBOPTIMAL. Its cheapness (0.2x) is real; its
judgement does not hold once fixtures require resolving CONFLICTING
rules rather than following a clear one. The "cost-dominant
alternative" recorded in Rev 2026-08-11b is materially weaker than it
looked. `deepseek-v4-pro` rises to 87% but is statistically
indistinguishable from the champion (p=0.80) and mildly anti-calibrated.

### Decision

**NO SWITCH APPLIED — operator's call, a live-money config change.**
grok is the best model measured on every axis by a margin that does NOT
clear the routine's bar at n=63, for 3.8x the champion's cost
(~$0.38/day, ~$11/month at ADR-022 full escalation, on a bot running $10
orders with $60 exposure). Everything else measured is worse than the
incumbent. Options: switch on the consistent direction of the evidence;
stay put because the margin fails its own bar; or run 5+ more gen3
rounds (~$1.60) to settle whether p=0.076 is a real effect needing power.

Cloud spend: **$0.96** (252 judgments). Nothing deployed or
reconfigured.

## Rev 2026-08-11b — head-to-head on `hard`: `grok-4.5` FILES; decision is cost

The first challenger in this project's history to clear the routine's §5
thresholds **with statistical significance**. Run on the `hard` fixture
set (built earlier the same day after v1 was found saturated), 8 runs /
120 judgments each. A constant strategy scores 33% on this set.

| Model | OK | SUB | **UNSAFE** | per-run OK | $/call | vs champ |
|---|---|---|---|---|---|---|
| **`xai/grok-4.5`** | **119/120 (99%)** | 1 | **0** | 15,15,15,14,15,15,15,15 | $0.00781 | **3.8x** |
| `gpt-5-mini` (champion) | 102/120 (85%) | 11 | **7** | 13,14,13,13,12,13,12,12 | $0.00206 | 1x |

**Fisher exact: OK p=0.000041, UNSAFE p=0.014.** The fresh 5-run half
reproduces it on its own (OK p=0.0011), so this is not a pooling artifact.

> **⚠️ SUPERSEDED — these p-values are CEILING-INFLATED.** See Rev
> 2026-08-11c. The arithmetic is right, but grok's 119/120 leaves almost
> no variance for the test; on gen3 (95%) the same comparison gives
> p=0.076. Do not cite 0.000041 as evidence for a switch.
Both §5 criteria met — UNSAFE more than halved (7 → 0) and OK gained 14
points (>+10). For contrast, every previous challenger died in the
p=0.24–0.49 range: `claude-sonnet-5` at p=0.49, `minimax-m3` and
`deepseek-v4-pro` at p=0.24–0.38 on the pre-correction set.

Grok dropped exactly ONE fixture across 120 judgments and produced ZERO
unsafe calls. The champion produced **seven** — one per 17 judgments — on
a battery purpose-built so the actively dangerous call is the thing being
measured.

**NO SWITCH APPLIED. The blocker is cost and the call is the operator's.**
3.8x champion per-call, outside the routine's ≤3x pre-filter — it would
never have been rostered under the routine as written; it ran because the
operator asked for it after being told the gate excluded it. At ADR-022
full escalation: ~$0.38/day, ~$11/month, on a bot running $10 orders with
$60 total exposure.

**Full 4-model field on the corrected `hard` set** (3 runs / 45 judgments,
before the head-to-head extension):

| Model | OK | UNSAFE | $/call | vs champ |
|---|---|---|---|---|
| `xai/grok-4.5` | 45/45 (100%) | 0 | $0.00781 | 3.8x |
| `gpt-5-mini` | 40/45 (89%) | 2 | $0.00206 | 1x |
| `deepseek-ai/deepseek-v4-pro` | 38/45 (84%) | 2 | $0.00504 | 2.4x |
| `minimaxai/minimax-m3` | 38/45 (84%) | **0** | $0.00039 | **0.2x** |

**`minimax-m3` is the cost-dominant answer**: champion-level judgment with
zero unsafe calls at one-fifth the price. If the seat is ever chosen on
$/judgment rather than judgment, it is the pick.

**`deepseek-v4-pro` was an artifact of the old battery.** It LED v1 at 88%
and sits mid-field here at 84%, with the highest hold rate in the field
(46% vs grok 40%, minimax 42%, champion 24%). v1 accepted `hold` on 12 of
its 14 fixtures, so it structurally rewarded exactly that lean. The
reorder is the corrected instrument doing its job.

**⚠️ Two caveats that must travel with these numbers.**

1. **Grok has CEILINGED `hard`.** 119/120 with one SUBOPTIMAL means the
   battery cannot measure anything better than it, so the next challenger
   cannot be ranked against it here. `hard` fixed saturation at the bottom
   (v1's constant-HOLD scored 86%, beating the champion's 83%); grok has
   now hit it at the top. A third-generation set will be needed before any
   future bake-off means anything.
2. **The fixture corrections made `hard` EASIER.** Every model gained ~5
   points when two defective fixtures were fixed (PR #86 — one labelled a
   working, profitable grid as needing action, contradicting quant.md's
   don't-fix-working clause; the other tested "thin data ⇒ hold", a rule
   the prompt never states). Justification was correctness, not
   difficulty, but the effect is real: absolute scores here are NOT
   comparable to the pre-correction run. The *comparison between models*
   is valid; the *level* is not.

Cloud spend for the head-to-head: **$0.74** (150 fresh judgments), rolling
24h $7.36. Isolated `data/probe_llm_cost.db`. Nothing deployed or
reconfigured.

## Rev 2026-08-11 — Gemini + Atlas sweep: the UNSAFE axis has saturated

Operator-directed, closing the coverage gap named in the 2026-08-10 seat
register (Gemini last measured May 2026; Atlas plumbed but never scored).
15 models on `tools/probe_freejudge.py`. Roster is **capability-first**:
the ≤3x cost gate decides a SWITCH, it does not pick who gets measured —
picking on price first would have sampled only the tier this battery was
already killing.

**Flagship field, 1 run each (14 fixtures).** Champion `gpt-5-mini`
3-run average = 11.7 OK / 0.67 UNSAFE at $0.00221/call.

| Model | OK | SUB | UNSAFE | ERR | $/call | vs champ |
|---|---|---|---|---|---|---|
| `xai/grok-4.5` | 13 | 1 | 0 | 0 | $0.00932 | 4.2x |
| `minimaxai/minimax-m3` | 13 | 1 | 0 | 0 | $0.00047 | **0.2x** |
| `gemini-3.1-pro-preview` | 12 | 2 | 0 | 0 | $0.01971 | 8.9x |
| `moonshotai/kimi-k3` | 12 | 2 | 0 | 0 | $0.02724 | 12.3x |
| `deepseek-ai/deepseek-v4-pro` | 11 | 3 | 0 | 0 | $0.00578 | 2.6x |
| `gemini-3.5-flash` | 11 | 3 | 0 | 0 | $0.01572 | 7.1x |
| `gemini-3.6-flash` | 11 | 3 | 0 | 0 | $0.01249 | 5.6x |
| `zai-org/glm-5.2` (3 runs: 9/10/11) | 10 | 4 | 0 | 0 | $0.00916 | 4.1x |
| `moonshotai/kimi-k2.6` | 10 | 2 | 0 | 2 | $0.01773 | 8.0x |
| `qwen/qwen3.8-max` | 10 | 2 | 0 | 2 | $0.01691 | 7.7x |
| `gemini-2.5-flash` | 8 | 4 | **2** | 0 | $0.00544 | 2.5x |

**⚠️ THE HEADLINE: 10 of 11 scored ZERO UNSAFE.** That axis — the one
the routine's switch thresholds are built on — no longer separates
current models. Only the oldest model in the field registered any. Treat
a clean UNSAFE card as table stakes, not as evidence.

**Confirmation, 3 runs = 42 judgments each** (single runs cannot
separate anything here; `glm-5.2` alone swung 9/10/11 on identical
fixtures):

| Model | per-run | OK/42 | UNSAFE | $/call | vs champ |
|---|---|---|---|---|---|
| `minimaxai/minimax-m3` | 14/0/0 · 12/1/**1** · 14/0/0 | **40 (95%)** | 1 | $0.00039 | 0.2x |
| `deepseek-ai/deepseek-v4-pro` | 13/1/0 · 12/2/0 · 12/2/0 | 37 (88%) | **0** | $0.00553 | 2.5x |
| `gpt-5-mini` (champion) | — | 35 (83%) | 2 | $0.00221 | 1x |

Both beat the champion on OK% **and** sit inside the cost gate — the
first challengers all week to clear both. The 3-run rule earned its keep
immediately: `minimax-m3` posted two perfect 14/0/0 runs around one
containing an **UNSAFE**, which its single-run debut had shown as clean.
Its unsafe rate (1/42) is statistically the champion's (2/42).

**Verdict: NO SWITCH, and the blocker is the instrument.** The deciding
axis now separates champion / minimax / deepseek by 2 vs 1 vs 0 at
n=42. Filing on that repeats the `claude-sonnet-5` error (p=0.49) with
cheaper models. Both are **watch items with a real claim** — revisit
when fixtures exist that discriminate above the floor described next.

**Capability floor — a PARSEABILITY cliff, not a judgment cliff.** Four
sub-$0.15/M models, 1 run each, to find where UNSAFE starts
discriminating again:

| Model | OK | SUB | UNSAFE | **ERR** | failure mode |
|---|---|---|---|---|---|
| `qwen/qwen3.5-flash` | 9 | 1 | 0 | **4** | empty response |
| `bytedance/doubao-seed-2.0-mini` | 9 | 2 | 1 | **2** | schema validation |
| `deepseek-ai/deepseek-v4-flash` | 7 | 5 | 1 | 1 | schema validation |
| `xiaomi/mimo-v2.5` | 0 | 0 | 0 | **14** | retries exhausted — total failure |

They failed by **not producing parseable output**, not by judging badly:
21 of 56 fixtures errored, against **0 errors in 84 mid-tier
judgments**. On the fixtures they did answer, calls were mostly
defensible. So the capability separating tiers here is emitting
schema-valid JSON under a long prompt — which means **this battery
measures instruction-following more than market judgment**, and explains
why everything above the floor looks identical on it.

**Next advisor work is a better instrument, not another sweep.**

Cloud spend: **$2.51** flagship sweep + **$0.32** confirmation/floor.
Isolated `data/probe_llm_cost.db`. Nothing deployed or reconfigured.

## Rev 2026-08-10 — Claude-5 roster bake-off: champion holds; haiku verdict closed

Operator-specified roster ("Claude 5 or lower, leave Fable 5 alone"), run on
the **primary instrument** — `tools/probe_freejudge.py`, 14 no-guard fixtures
× 3 runs = 42 judgments per model. Opus 5 dropped by the operator before this
stage (10× champion cost; it had already tied gpt-5-mini on the off-contract
battery). Native provider paths, not the Atlas gateway.

| Model | OK | SUBOPT | **UNSAFE** | ERR | $/call (measured) | vs champion |
|---|---|---|---|---|---|---|
| **gpt-5-mini** (champion) | 83% | 5 | **2 (5%)** | 0 | $0.00221 | 1× |
| claude-sonnet-5 | **88%** | 5 | **0 (0%)** | 0 | $0.01345 | **6.1×** |
| claude-haiku-4-5 | 79% | 5 | **4 (10%)** | 0 | $0.00323 | 1.5× |

**Verdict: no switch — `gpt-5-mini` stays.** `claude-sonnet-5` posts the only
clean UNSAFE card ever recorded here, and it is still not a switch:

1. **Not significant.** Fisher exact two-tailed on 0/42 vs 2/42 gives
   **p = 0.49** — indistinguishable from chance at this n. Its +5 OK points
   also miss the routine's `OK+10` criterion.
2. **Outside the cost class.** At **6.1×** the champion's per-call cost it
   fails the routine's ≤3× pre-filter — the same gate that left
   gemini-3.5/3.6-flash and gpt-5.5 priced-but-unprobed in July. It was probed
   here only because the operator named the roster; that does not exempt it
   from the threshold on the way out.
3. **Scale check.** ~$0.61/day / ~$18/mo at ADR-022's full-escalation rate, on
   a bot running $10 orders and $60 total exposure whose cycles clear in cents.

Watch item on the same footing July gave `gpt-5.4-mini`: revisit if Sonnet 5's
price enters the ≤3× band (its $2/$10 introductory rate expires 2026-08-31 and
moves the WRONG way, to $3/$15) or if a larger sample separates 0 from 2.

**Champion stability across six weeks:** 81%→83% OK, 1→2 UNSAFE of 42 vs the
2026-07-31 stored baseline. One of today's two UNSAFE is
`slightly_tight_but_healthy` — the same fixture that sank gpt-5.4-mini's
challenge. The instrument reproduces.

**`claude-haiku-4-5`: the July "no verdict" is now closed — NOT champion-class.**
July's run died on an exhausted Anthropic credit balance (31/50 calls failed)
and left the note "its valid run 1 showed OK 8/14 with 2 UNSAFE, not obviously
champion-class." Three clean runs confirm it: **4 UNSAFE (10%), double the
champion**, at `calm_well_matched_lowcycle` ×2, `developing_downtrend_mild`,
and `recovering_after_dip` — i.e. tightening a matched grid and tightening into
a developing trend, the exact pathologies the seat is judged on. A **second,
independent instrument agrees**: on the (off-contract) core battery it scored
**8/36**, the worst ever recorded there, tightening on all four
`hold_*_matched` fixtures. Two instruments measuring different things converge
on the same over-tightening bias, so the finding stands on its own.

**⚠️ Method note — the roster's first attempt produced fake zeros.** Sonnet 5
and Opus 5 initially scored 0/36 and 0/8: those were `400 invalid_request_error`
responses (Anthropic deprecated `temperature` for the Claude 5 generation), not
model results. Reported as scores they would have produced a confident and
completely false finding. The battery's separate ERROR count is what exposed
it — a harness that folded errors into "wrong answer" would have laundered an
adapter bug into a model verdict. Fixed in PR #81
(`anthropic.supports_temperature`).

**⚠️ Do not use `probe_advisor.py`'s core/heldout batteries for seat
selection.** TWO independent reasons, the second measured later (Rev
2026-08-11d): they key to the retired vol→spacing curve AND
`heldout` is **hold-degenerate — constant-HOLD scores 18/24 = 75%,
beating every model ever measured on it**, including the sonnet-5 17/24
recorded below as "the best held-out score ever measured". `core` is
clean on that axis (best constant 33%). Only
**3 of 8** heldout / **11 of 12** core fixtures escalate to the LLM at all
(verified deterministically against the shipped `HeuristicAdvisorAdapter`
2026-08-10). Three fixtures failed by all four roster models —
`heldout_fee_floor`, `heldout_drawdown_overrides_calm`, `hold_quiet_matched` —
are guard-handled cases that `quant.md` explicitly tells the model are
"already handled before you." **There is no `quant.md` prompt gap**; a slice
filed to chase one was withdrawn. See the roadmap's 2026-08-10 entry.

Cloud spend: **$0.79** freejudge (126 calls) + $1.71 recorded for the earlier
roster sweep on the off-contract batteries. Isolated
`data/probe_llm_cost.db`. Nothing deployed or reconfigured — recommendation
only.

## Rev 2026-07-31 — Monthly advisor-model-review bake-off #1: champion holds

First challenger bake-off under the monthly advisor-model-review routine
(fleet-kit `fleet/routines/wobblebot-advisor-model-review.md`; standing state
in wobblebot#22, candidate list in wobblebot#23). Primary instrument:
`tools/probe_freejudge.py` — 14 no-guard fixtures × 3 same-day runs per model
(42 judgments), champion re-run the same day as the constant baseline. The
8-fixture heldout battery ran once per model as directional context only (per
the 2026-06-04 methodology note below, most of it never escalates in
production). Candidates pre-filtered by the routine's ≤3× per-call cost gate:
gemini-3.5/3.6-flash (~7-8×) and gpt-5.5 ($5/$30) were priced but not probed.

**Freejudge, 3 runs × 14 fixtures (42 judgments), 2026-07-31:**

| Model | OK | SUBOPT | UNSAFE | ERR | $/call (measured) | mean s/call | heldout /24 (1 run) |
|---|---|---|---|---|---|---|---|
| **gpt-5-mini** (champion) | **81%** | 17% | **2%** (1) | 0 | $0.0023 | 14.2 | 8 (1 WRONG) |
| gpt-5.4-mini | 88% | 5% | 7% (3) | 0 | $0.0016 | **1.5** | 8 (0 WRONG) |
| gpt-5.4-nano | 74% | 10% | 12% (5) | 2 | $0.0005 | 1.7 | **14** (0 WRONG) |
| claude-haiku-4-5 | — | — | — | — | $0.0011 | 2.9 | — |

**Verdict: no switch — gpt-5-mini stays.** Per the routine's §5 thresholds no
challenger files: `gpt-5.4-mini` is +7 OK points, 0.68× cost, and ~10× faster,
but carries **3× the champion's UNSAFE count** (`slightly_tight_but_healthy`
×2, `whipsaw_midspacing` ×1 — tighten-into-risk, the exact failure class the
seat is judged on), failing both the UNSAFE-halved and the OK+10 criteria.
`gpt-5.4-nano` is worse on both axes (its best-in-field heldout 14/24 is the
hold-more bias flattering the maintainer curve, not judgment). Watch item:
5.4-mini's latency/cost profile is attractive — re-test next generation.

**Champion self-drift (files as a finding): gpt-5-mini improved upstream.**
Today's 3-run profile OK 81% / UNSAFE 2% vs the stored 2026-06-04 baseline
OK 62% / UNSAFE 20% (84 judgments) — both axes moved >10 pts, including clean
passes on `moderate_drawdown_below_guard` (2 of 3 runs) and
`whipsaw_midspacing` (all runs), the fixtures behind the June tighten-bias
concern. The 2026-07-31 numbers are the new stored baseline in wobblebot#22.
Heldout context: 8/24 with one WRONG (`heldout_drawdown_overrides_calm`,
tightened into a drawdown) vs June's 19.8/24 4-run mean — single-run,
guard-resolved-in-production battery; tracked, not actioned.

**claude-haiku-4-5: incomplete — Anthropic API credit balance exhausted
mid-bake-off** (HTTP 400 "credit balance too low"; run 1 clean, run 2 partial,
run 3 + heldout all-ERROR; 31/50 calls failed). No verdict. Re-run ~$0.15
after topping up, if wanted — its valid run 1 showed OK 8/14 with 2 UNSAFE,
not obviously champion-class.

Cloud spend, whole bake-off incl. failures: **$0.27** (isolated
`data/probe_llm_cost.db`; artifacts in `data/advisor_probe_results/2026-07/`).
Soak freeze holds — nothing deployed or reconfigured; recommendation only.

## Rev 2026-06-04 — Cloud free-judge escalation model: gpt-5-mini (ADR-022)

When the advisor was reoriented to **guards + LLM free judge** (ADR-022),
the cascade's escalation target became the model that decides every
non-guard tick. This bake-off picked it. Driven by
`tools/probe_advisor.py --provider {openai,anthropic,google}` against the
8-fixture `heldout` battery (real API calls, isolated `data/probe_llm_cost.db`).

**Methodology note (load-bearing).** Run the held-out fixtures through the
*real* heuristic and **all 8 are guard-resolved** — so most of the battery
scores the LLM on cases it never sees in production. The decision-relevant
subset is the **3 fixtures that escalate post-ADR-022** (`heldout_clear_widen`,
`heldout_matched`, `heldout_matched_whipsaw` — a clear widen + two matched
grids that should be left alone). The full-battery score is context; the
escalate subset is the test.

**Full heldout (curve prompt, 4-run mean /24) + measured cost:**

| Model | mean /24 | $/call | $/day @36 | over-tightens matched? |
|---|---|---|---|---|
| **gpt-5-mini** | **19.8** | $0.0028 | ~$0.10 | **no — held both** |
| claude-haiku-4-5 | 16.2 | $0.0026 | ~$0.09 | no, but tightened *into* a drawdown once |
| o3 (incumbent) | 14.8 | $0.0086 | ~$0.31 | **yes — 8/8 runs** |
| gemini-3.5-flash | 14.0 | $0.0158 | ~$0.57 | yes |
| o3-mini / o4-mini | 11 (n=1) | $0.0079 / $0.0060 | ~$0.25 | yes (o4-mini went below the fee floor) |
| gemini-2.5-flash | 8 | $0.0051 | ~$0.18 | yes (worst) |

**Escalate subset (free-judge prompt, 12 calls/model):** gpt-5-mini 6/12 OK
(held the matched grids 6/8, never a wrong-direction call); **o3 0/12 — it
tightened both matched grids in every run**, under both the curve and the
free-judge prompt. That compulsive matched-grid tightening is the exact
pathology ADR-022 fixes.

**Decision: `gpt-5-mini`.** Best judgment on the cases that reach the LLM,
~⅓ o3's cost, prompt-robust. Counter-intuitive findings worth keeping: (a)
o3-mini is only ~5% cheaper than o3 — the weaker model burns more reasoning
tokens, so same-class "minis" don't save money; (b) o4-mini was no better
than o3-mini. **Caveats:** the escalate subset is only 3 fixtures (thin); all
scores are non-deterministic single-to-quad runs; the residual gpt-5-mini
over-tighten is caught by the application-time floor (`8500226`), never
applied. A purpose-built no-guard battery is the gold-standard follow-up —
**now built** (`tools/probe_freejudge.py` + `tests/tools/test_freejudge_battery.py`):
14 ambiguous-middle scenarios, each verified guard-free by the shipped heuristic
(a CI test, no LLM needed), scored against the bot's **risk model** (OK /
SUBOPTIMAL / UNSAFE — `forbidden`=the actively-unsafe call) rather than a curve.
Fixture labels were adversarially reviewed by a 3-lens critic panel (2026-06-04;
two corrected). Run `python tools/probe_freejudge.py --model gpt-5-mini` to grade a
candidate on demand (live API, ~6 min for a reasoning model over 14 calls).

**gpt-5-mini on the no-guard battery (6 runs × 14 = 84 judgments, 2026-06-04):**
OK 52 (62%), SUBOPTIMAL 15, **UNSAFE 17 (20%)**. An initial 2-run sample read a
rosier UNSAFE=1 on run 1 — that was the optimistic outlier; steady state is ~3
UNSAFE/run. Three distinct behaviors: **clear cases rock-solid + correct**
(too-tight→widen, too-wide→tighten 6/6); **ambiguous middle a coin-flip**
(`well_matched_ranging` 3 hold / 3 tighten — the LLM-consistency footgun in the
flesh); and **dangerous cases consistently WRONG** — `moderate_drawdown_below_guard`
tightens 6/6, `developing_downtrend` 5/6, `whipsaw` 4/6, all *tightens into risk*. So
gpt-5-mini is the best model tested but carries a persistent tighten-bias under the
free-judge prompt; it is **not immune** to the pathology the 3-fixture escalate subset
hid.

Why this triggers neither a model change nor (yet) a guard change:
(a) **Inert by construction.** Every one of those tightens is below the configured
spacing → the auto-apply floor (`8500226`) rejects it and the dashboard tags it
"below floor" — tracked, never applied; only widens and holds can land.
(b) **The guard-tune is a POST-soak candidate, not a soak-time one.** Lowering the
`defensive_drawdown` threshold (−0.05 → −0.04 catches the 6/6 case; −0.03 also catches
the 5/6 downtrend — measured 6h-window drawdown frequencies on local BTC history: dd≤−3%
~6.5%, ≤−4% ~2.7%, ≤−5% ~1.3% of windows) *would* make these correct deterministically,
but **during the soak it throws away the highest-value learning signal**: the LLM's
`(situation → recommendation → market outcome)` pairs on exactly the hardest cases —
the dataset for evaluating the free judge and a future learned arbitrator. With
`auto_apply` off the wrong tightens cost nothing, so there is no safety reason to
short-circuit them. The no-guard battery already characterized the failure *offline*
(its job); the soak collects the live pairs. **Revisit the guard-tune when enabling
`auto_apply`, informed by real soak outcomes** — the prototype + frequency data are
filed for that day. (Reference: gpt-4o-mini, non-reasoning, scored OK 10 / SUB 1 / UNSAFE 3.)

## Rev 2026-05-29 — 12-fixture battery + hardened rubric (CURRENT for the local battery)

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
