# Advisor Seat Register

**One row per LLM seat: who holds it, what evidence put them there, and
whether the config agrees.** Created 2026-08-10 because the answer to
"which model is our risk expert, and why?" required reading an ADR, three
revs of `advisor-llm-models.md`, roadmap prose, and two profile blocks in
`settings.yml` — which then disagreed with each other.

This file is the **index**, not the evidence. Full bake-off records stay
in [`advisor-llm-models.md`](advisor-llm-models.md) (advisor roles) and
[`operator-llm-models.md`](operator-llm-models.md) (the operator
assistant). Seat *architecture* decisions stay in the ADRs.

## The register

| Seat | Holder | Evidence | Battery | Decided | Config status |
|---|---|---|---|---|---|
| **quant** (escalation) | **`xai/grok-4.5`** (Atlas) — **ASSIGNED 2026-08-12** | gen3, 8 rounds: **95% vs 84%**, **0 UNSAFE vs 10** (p=0.001); confirmed on 24 real production inputs (holds 4/24 where gpt-5-mini never held in 216) | `probe_freejudge.py` | assigned 2026-08-12 by the operator | ⚠️ local config only — **not deployed** |
| **arbitrator** | **`claude-haiku-4-5`** — weakly; `gpt-5-mini` defensible | v1+gen2, 75 judgments: haiku **75/75**, gpt-5-mini 73/75 (**p=0.248, NOT significant**); gpt-5-mini's only 2 failures are both `never_emit_a_tighten` | `probe_arbitrator.py` | 2026-08-10, re-run 08-11, gen2 08-12 | ⚠️ **NOT wired** — `moe-advisor` profile still says `phi4:14b-q8_0` |
| **news** | **`claude-haiku-4-5`** — recommended, not yet wired; **HELD after the 16-model matrix + red-team, 2026-08-17** | `news/gen2` 3 rounds: **65/66 (98%)**, tied with sonnet-5 (66/66, p=0.5) at **1/4.5 the cost**; beats gpt-5-mini 56/66, **p=0.0043**. Phase B: 65 is a ceiling-tie with the field's four 66s | `probe_news.py` | 2026-08-11; held 08-17 | ⚠️ **NOT wired** — profile says `deepseek-r1:8b`, never scored |
| **risk** | **`gpt-5-mini`** — recommended on cost, a coin-flip on merit; **HELD after the 16-model matrix + red-team, 2026-08-17** | 3 rounds: **50/54**, `claude-haiku-4-5` 49/54 (**p=0.5**); **0 UNSAFE and 15/15 severe for BOTH**. Phase B best challenger 54/54 lands p=0.059 uncorrected, before a 16-way selection penalty — no honest beat | `probe_risk.py` | 2026-08-12; held 08-17 | ⚠️ **NOT wired** — profile says `qwen3:8b`, never scored |
| **gremlin** | **`qwen2.5:3b-instruct-q4_K_M`** (Ollama, default seat) — **BUILT 2026-08-17 (P4.4c)**, disabled pending operator flip | **un-batteried BY DESIGN** — the outcome ledger is its battery (ADR-035 d.4: directional calls grade against the realized move; P4.4b evaluator live). Seat picked for cheap + leap-prone (free local, temp 1.0, 12.7 tok/s hot on the NAS); discipline rule: do NOT tune or reseat until a ledger sample exists, and never because the scoreboard flatters a challenger on <30 decisive | *(the ledger)* | seat default 2026-08-17; role shipped | `advisor.gremlin.enabled: false` everywhere — operator flips it in the NAS settings.yml + restarts advise |
| **operator assistant** | `qwen2.5:1.5b-instruct-q4_K_M` | 8/8 on the NAS sweep, no cache-warm tax | `probe_assistant.py` / `sweep_assistant_nas.py` | 2026-05-27 | ✅ wired — `cpu-only` profile |

## Full-field matrix (Phase B) + adversarial audit — 2026-08-17: BOTH SEATS HELD

16 models × 3 rounds × (news/gen2 + risk), four price tiers cheapest-first,
511 minutes, $12.79, 1,940 calls on the isolated probe ledger. Every one of
the 96 battery sections completed rc=0. Before acting, the results were
red-teamed by three isolated adversarial reviewers (harness audit /
methodology audit / per-fixture forensics) per the standing flip-the-script
practice — none shown the conclusions. This section is the post-red-team
synthesis; the raw tier logs are the evidence of record.

**Scores (news /66 · risk-OK /54 · risk ERRORs):**

| model | tier | news | risk | ERR |
|---|---|---|---|---|
| `gemini-3.1-pro-preview` | premium | **66** | **54** | 0 |
| `claude-opus-5` | premium | **66** | **54** | 0 |
| `xai/grok-4.5` | mid | **66** | 51 | 0 |
| `claude-sonnet-5` | premium | **66** | 51 | 0 |
| `kimi-k2.6` · `deepseek-v4-pro` | low | 65 | 52 | 0 |
| `doubao-seed-2.0-mini` | **cheap** | 64 | 52 | 0 |
| `deepseek-v4-flash` | **cheap** | 64 | 51 | 0 |
| `kimi-k3` · `gemini-3.6-flash` | prem/mid | 63 | 53 | 0 |
| `minimax-m3` · `glm-5.2` | cheap/low | 63 | 51 | 0 |
| `qwen3.8-max` | mid | 63 | 50 | 3 |
| `gpt-5.4-mini` | mid | 61 | 49 | 0 |
| `gpt-5.4-nano` | low | 53 | 46 | 0 |
| `qwen3.5-flash` | cheap | *(36)* | *(44)* | **39** |

**Both incumbents keep their seats.** News: haiku's 65 is a p=0.5 tie with
the four 66s at the ceiling. Risk: the best challengers' 54/54 vs the
incumbent's 50/54 is **p=0.059 uncorrected — before the winner's-curse
penalty of selecting the max of 16 challengers against one fixed prior
number**. No honest beat exists in this data.

**What the red-team established (full reports in the session record):**

1. **The run was clean and the verdicts honest.** All sections present,
   verdict tokens mechanically correct against the graders' rules,
   summary counts equal line tallies, totals independently recomputed to
   the digit. The problems are inferential, not executional.
2. **Both batteries are CEILINGED at the top — the third time this
   project has hit this instrument failure** (news/v1, arbitrator, now
   gen2 + risk). Four 66s and two 54s mean no ranking within the top ~6
   per seat is supported. Worse, top-of-field separation is essentially
   **one contested rubric label per battery repeated three times**:
   `bullish_rally_coverage` (24/48 news failures matrix-wide — is a $70k
   breakout a volatility event? half the field consistently says yes) and
   `fresh_drawdown_light_exposure` (28/48 risk failures — already flagged
   "watch, not withdraw" on 08-12). A 66-vs-63 gap is one debatable
   judgment call, not three points of quality.
3. **The honest tie band on these instruments is ~5 points, not ±3** —
   misses cluster perfectly by fixture (same fixture, 3/3 rounds), so the
   effective sample is the fixture set, not rounds × fixtures.
4. **The qwen family is UNMEASURABLE here, not bad** — all 42 of the
   matrix's fixture ERRORs belong to it, with empty-response/truncation
   signatures (the PR #64 token-budget trap, re-manifesting through the
   Atlas transport). qwen3.5-flash answered 36 news fixtures and got
   **36 right**; its 36/66 headline sits below the battery's own
   do-nothing floor and measures availability, not judgment. Excluded
   from ranking in either direction; a 45% empty-response rate is
   nonetheless operationally disqualifying for a live seat today.
   (Register rule 4 — "errors are not verdicts" — applied as written.)
5. **Zero UNSAFE in 864 risk judgments across the entire field.** Every
   model that answered is directionally safe under compounding pressure;
   the risk battery's safety axis produced no separation because the
   whole field passed it.
6. **Latent harness defects found, none of which bit this run** (caps
   were 60/8 with $12.79 spent — the cost gate never fired; zero UNSAFE
   means the timid-equals-UNSAFE deadband never triggered): see
   [`probe-battery-punch-list.md`](probe-battery-punch-list.md). Fix
   before the next campaign.

**Cost observation worth keeping** (an observation, not a switch case):
`doubao-seed-2.0-mini` and `deepseek-v4-flash` perform inside the top tie
group at bottom-tier prices — the field's floor for seat-viable judgment
now extends into the cheapest tier. `gpt-5.4-nano` is the exception:
its news 53/66 separates from the top group at p<0.01 (a real, consistent
weakness, all three rounds).

**Consequence for future campaigns:** more candidates cannot produce a
different answer on these instruments. If a unique seat-holder is ever
actually needed (i.e., MoE gets a live path), the prerequisite is
gen3-class batteries whose top is not ceilinged — and the punch-list
fixes, so infrastructure and judgment stop sharing a scoreboard.

## ⚠️ OPEN DECISION — `xai/grok-4.5` beats the quant seat SIGNIFICANTLY; the blocker is cost alone

**DECISIVE RESULT (2026-08-11): 8 rounds on gen3, 168 judgments each.**

| model | OK/168 | OK% | SUB | **UNSAFE** | tau mean |
|---|---|---|---|---|---|
| **`xai/grok-4.5`** | **160** | **95%** | 8 | **0** | +0.35 |
| `gpt-5-mini` (champion) | 141 | 84% | 17 | **10** | +0.26 |

**Fisher exact: OK p=0.001, UNSAFE p=0.0017.** Both clear 0.05 decisively.
Projected p was 0.0010 before the run; it landed at 0.001045.

**This is NOT a ceiling artifact.** Grok's per-run scores are
20,19,21,19,20,21,19,21 — it drops fixtures on most runs, so there is
real variance for the test to work against. That is precisely what was
missing on `hard`, where 119/120 manufactured p=0.000041 out of an
exhausted battery.

**Zero unsafe calls in 168 judgments against the champion's TEN** — one
per 17 judgments, on a battery built so the actively dangerous call is
the thing measured. That is a safety property, not a scoring nicety, and
it is the finding to weigh most heavily.

**CORRECTION to the earlier write-up:** calibration CONVERGED with more
data — grok +0.35 vs champion +0.26. At 3 runs it looked like grok's
clearest advantage (+0.44 vs +0.30); at 8 runs the gap is modest.
**Direction and safety are the real separation; calibration is not.**

**THE DECISION IS NOW PURELY COST.** grok is significantly better on
both axes at **3.8x** the champion — ~$0.38/day, ~$11/month at ADR-022
full escalation, on a bot running $10 orders with $60 total exposure.
There is no statistical ambiguity left to hold it up: it is better, and
it costs more.

**Worth revisiting if you switch:** the review routine's **≤3x cost
pre-filter** would have excluded grok from the roster entirely. It was
rostered only because the operator overrode that gate — and it turned
out to be the only model that beat the incumbent. The threshold nearly
cost us the answer.

**NOT SWITCHED. Operator's call — a live-money config change.**

### 2026-08-11 UPDATE — confirmed on REAL production inputs

The gen3 result above was measured on fixtures *built* to be ambiguous.
The obvious objection — "production serves easy reads, so the edge buys
nothing there" — was tested rather than argued, by replaying the exact
24 `input_summary` blobs gpt-5-mini answered in production through
grok-4.5 at the deployed escalation params. **The objection was
refuted.**

| | gpt-5-mini (recorded) | grok-4.5 (replay) |
|---|---|---|
| **HOLD** | **0** | **4 / 24 (17%)** |
| ≥ deployed 3.0% | 0 | 0 |
| modal value | 1.00 | 1.20 |
| lowest value | 0.66 | 0.90 |

gpt-5-mini has emitted **zero HOLDs in 216 production calls**; grok
emitted 4 in 24 on the same input class (Fisher **p = 7.9e-05** vs the
full record, **p = 0.055** on the matched 24 — thin sample). All four
grok HOLDs carry `medium` confidence, matching its better gen3
calibration; production gpt-5-mini is 189/216 `high`.

On the 20 non-HOLD cases both models agree on direction every time —
the separation is entirely *whether the model will decline to act*.

**Cost is still the counterweight, and the real numbers are now
measured** (not estimated): production tokens are in 3120 / out 153 /
reasoning 1045, unit cost $0.00318/call. At 4h × 6 symbols that is
**$3.48/mo incumbent vs $14.70/mo grok**. Going direct to xAI does NOT
help — headline rates are identical ($2/$6); only cached input differs
($0.30 vs Atlas's $0.50), worth ~$0.08/mo.

Full evidence: `production-advisor-forensics-2026-08-11.md`.

### Superseded: the 3-round gen3 result

#### gen3 bake-off, 3 rounds (superseded by the 8-round result above)

Run on `gen3`, built the same day because grok CEILINGED `hard` at
119/120. Constant baseline 33%.

| model | OK/63 | OK% | SUB | **UNSAFE** | per-run | **tau_b mean** | $/call |
|---|---|---|---|---|---|---|---|
| **`xai/grok-4.5`** | **60** | **95%** | 3 | **0** | 20,19,21 | **+0.44** | $0.00781 |
| `deepseek-ai/deepseek-v4-pro` | 55 | 87% | 7 | 0 | 17,18,20 | −0.06 | $0.00504 |
| `gpt-5-mini` (champion) | 53 | 84% | 7 | **3** | 18,18,17 | +0.30 | $0.00206 |
| `minimaxai/minimax-m3` | 49 | 78% | 13 | 1 | 17,17,15 | −0.07 | $0.00039 |

**Fisher exact vs the champion: grok OK p=0.076, UNSAFE (0 vs 3)
p=0.244.** Neither clears 0.05. deepseek p=0.80, minimax p=0.50.

**⚠️ RETRACTION — the `hard` significance was CEILING-INFLATED.** The
p=0.000041 recorded below is real arithmetic on real data, but it is an
artifact of grok scoring 119/120 there: a near-perfect score has almost
no variance for a test to work against, so the p-value collapses. Give
the same model room to be imperfect (95% on gen3) and the margin falls
to **p=0.076** — three orders of magnitude, same models, same prompt,
harder fixtures. **General rule: a challenger that ceilings your battery
will always look statistically overwhelming.** The fix is a harder
instrument, not more runs.

**What survived the harder test:** grok leads on every axis, and is the
only model with STABLE calibration — tau_b +0.45/+0.43/+0.45, above the
"tracks evidence" threshold on every run. The champion swings
+0.40/+0.18/+0.31. Both other challengers average NEGATIVE (more
confident as evidence thins).

**What did not:** `minimax-m3` COLLAPSES on the harder set — 84% on
`hard` → 78% on gen3, below the champion. Its cheapness is real; its
judgement does not hold once fixtures require resolving conflicting
rules rather than following a clear one. **The "cost-dominant
alternative" option is materially weaker than it looked.**

**The decision, stated plainly:** grok is the best model measured on
every axis, by a margin that does NOT clear the routine's significance
bar at n=63, for **3.8x** the champion's cost (~$0.38/day, ~$11/month at
ADR-022 full escalation, on a bot running $10 orders with $60 exposure).
Everything else measured is worse than the incumbent on gen3. Three
options: switch on the consistent direction of the evidence; stay put
because the margin fails its own bar; or run 5+ more gen3 rounds
(~$1.60) to settle whether p=0.076 is a real effect needing power.

**NOT SWITCHED. Operator's call — it is a live-money config change.**

### Superseded: the `hard` result (kept for the record)

8 runs / 120 judgments each on the `hard` fixture set, which grok
ceilinged:

| model | OK | SUB | **UNSAFE** | per-run OK | $/call |
|---|---|---|---|---|---|
| `xai/grok-4.5` | **119/120 (99%)** | 1 | **0** | 15,15,15,14,15,15,15,15 | $0.00781 |
| `gpt-5-mini` (champion) | 102/120 (85%) | 11 | **7** | 13,14,13,13,12,13,12,12 | $0.00206 |

Fisher exact: **OK p=0.000041**, **UNSAFE p=0.014**. The fresh 5-run half
reproduces it alone (p=0.0011), so it is not a pooling artifact. Both §5
criteria met: UNSAFE more than halved (7 → 0), OK gained >10 points. Every
prior challenger died at p=0.24–0.49; this is the first that did not.

> **⚠️ These p-values are ceiling-inflated — see the gen3 section above.**
> The arithmetic is correct, but grok's 119/120 leaves almost no variance
> for the test to work against. On gen3, where it scores 95% instead of
> 99%, the same comparison gives p=0.076. Do not cite the 0.000041 as
> evidence for a switch.

**THE DECISION IS COST, AND IT IS THE OPERATOR'S — NOT SWITCHED.**
`grok-4.5` measures **3.8x** the champion, outside the routine's ≤3x
pre-filter, which would have excluded it from the roster entirely (it was
run because the operator asked for it). At ADR-022 full escalation that is
roughly **$0.38/day, ~$11/month**, against a bot running $10 orders with
$60 total exposure. Perfect judgment is worth something; whether it is
worth 3.8x on a bot this size is a capital decision, not a threshold.

**Two caveats that must travel with these numbers:**

1. **`grok-4.5` has CEILINGED this battery.** 119/120 with a single
   SUBOPTIMAL means the instrument can no longer measure anything better,
   so future challengers cannot be ranked against it here. `hard` fixed
   saturation at the *bottom* (v1's constant-HOLD scored 86%); grok has
   now hit it at the *top*.
2. **The corrected fixtures made the battery EASIER.** Every model gained
   ~5 points when two defective fixtures were fixed (PR #86). The
   justification was correctness — both contradicted `quant.md` — but the
   absolute numbers are softer than the pre-correction set. The
   *comparison* is valid; the *level* is not comparable to earlier revs.

Also measured and worth keeping: **`minimaxai/minimax-m3` at 38/45 (84%)
with 0 UNSAFE for 0.2x champion cost** — the cheapest model that holds
champion-level judgment, and the right pick if cost dominates.
**`deepseek-v4-pro` was an artifact of the old battery**: it led v1 at 88%
and sits at 84% on `hard` with the field's highest hold rate (46%), which
is exactly the bias v1 rewarded.

Full record: `advisor-llm-models.md` Rev 2026-08-11 (head-to-head).

## What "not wired" means here

Only the **quant** seat runs in production. The deployed `cpu-only`
profile is `engine: cascade, type: single` — one LLM, no MoE — so the
arbitrator / news / risk / gremlin seats have no live path today. Their
configured models live in the `moe-advisor` and `cloud-only-moe`
profiles, which nothing currently selects.

That is why the mismatches above are a documentation problem rather than
an outage. They become real the moment anyone flips a profile to
`type: moe`, and at that point the config would run **`phi4:14b-q8_0` in
two seats** — a model that scored **9/36** on the quant battery and, in
the 2026-08-10 MoE run, emitted schema-valid nonsense. `cloud-only-moe`
is worse-dated still (`gpt-4o`, `claude-sonnet-4-6`) under a comment
admitting its tags are "illustrative — refresh them to current models
before use."

**Before enabling MoE, reconcile the profile against this table.**

## Battery health (constant-baseline audit, 2026-08-11)

A battery is sound when no constant strategy scores near the best real
model. Two of the seven fixture sets fail that test and must not be used
for seat selection:

| battery | best constant | best real | margin | usable? |
|---|---|---|---|---|
| `freejudge/gen3` | 33% | 95% | +62% | ✅ current instrument |
| `freejudge/hard` | 33% | 99% | +66% | ✅ (but grok ceilings it) |
| `quant/core` | 33% | 83% | +50% | ⚠️ off-contract only |
| `arbitrator/gen2` | 47% (free `voting`) | 100% | +53% | ⚠️ sound, but CEILINGS both candidates |
| `arbitrator/v1` | **62%** (free `voting`) | 100% | +38% | ❌ floor too high — superseded |
| `news/gen2` | 59% | 98% | +39% | ⚠️ sound floor, but **CEILINGED at the top 2026-08-17** — 4 models at 66/66 in the Phase B matrix |
| `risk/v1` | 50% | 100% | +50% | ⚠️ sound floor, but **CEILINGED at the top 2026-08-17** — 2 models at 54/54; UNSAFE axis saturated (0 in 864) |
| `news/v1` | 58% | 100% | +42% | ❌ CEILINGED — 3 models at 12/12 |
| `freejudge/v1` | **86%** | 83% | **−3%** | ❌ rock beats champion |
| **`quant/heldout`** | **75%** | 71% | **−4%** | ❌ **rock beats champion** |

**arbitrator and news are structurally SOUND** — the concern that those
seats rested on rock-passable evidence was wrong. Their weakness is
SAMPLE SIZE (8 and 12 fixtures, single runs), which is a far cheaper fix
than a rebuild: more runs and more fixtures, not a new instrument.

**News took that fix on 2026-08-11 and it worked** — see the seat result
below. **Arbitrator was re-run the same day and the single-run 8/8 did
NOT reproduce** — see below.

### ⚠️ A SINGLE RUN IS NOT A SCORE (measured 2026-08-11)

Running the same model on the same fixtures twice does not give the same
answer. Observed on `news/gen2`, identical params:

| model | run-to-run scores |
|---|---|
| haiku-4-5 | 21, **22**, 22, **21**, 22 |
| gpt-5-mini | 17, 18, **19, 19, 18** |
| sonnet-5 | 21, 21, **22, 22, 22** |

haiku missed `regulatory_deadline_imminent` on 2 of 5 runs and aced it on
the other 3 — a 1-point swing that, on a single run, would have looked
like a ranking.

**Consequence: every single-run seat decision in this register is weaker
than its number suggests**, the arbitrator's 8/8 most of all (8 fixtures
AND one run). Treat a single run as a smoke test; rank on 3+ rounds.

**`quant/heldout` is degenerate** — constant-HOLD scores 75%, beating
every model measured on it. Combined with being off-contract (3 of 8
fixtures escalate), it has two independent disqualifiers. Frozen, not
fixed, so historical scores stay comparable.

## News seat — RESOLVED 2026-08-11: `claude-haiku-4-5`

`v1` could not rank the models it existed to rank: haiku-4-5, sonnet-5
and opus-5 all scored 12/12. It was never UNSOUND (constant baseline 58%
vs 100% best) — it was EXHAUSTED, which has the cheaper fix the audit
above prescribed. `gen2` adds ten boundary cases to v1's twelve; half the
new hold-cases are seeded with a named trigger word ("regulatory",
"withdrawals", "liquidity") so keyword-matching is punished rather than
rewarded. It broke the tie on the first run.

**3 rounds × 22 fixtures = 66 judgments each.** Constant-HOLD floors at
59%.

| model | score | % | $/call | $/mo @1094 sweeps |
|---|---|---|---|---|
| `claude-sonnet-5` | 66/66 | 100% | $0.01252 | $13.70 |
| **`claude-haiku-4-5`** | **65/66** | **98%** | **$0.00280** | **$3.06** |
| `gpt-5-mini` | 56/66 | 85% | $0.00225 | $2.46 |
| `claude-opus-5` *(2 rounds)* | 42/44 | 95% | $0.02248 | $24.62 |

**Fisher exact:** haiku vs gpt-5-mini **p = 0.0043**; sonnet vs haiku
**p = 0.5** — literally no evidence of a difference. Buying sonnet's one
extra judgment out of 66 costs **$10.63/month**.

opus-5 is excluded on MEASURED PARITY, not on price: two independent
rounds put it at 21/22 — below haiku, tied with sonnet — at 8× haiku's
cost. That is not the ≤3× pre-filter mistake that nearly cost us grok;
there is no evidence here that the expensive model is better.

**gpt-5-mini's failure profile is coherent and disqualifying for THIS
seat.** All 10 of its misses are `OVERTRADE` — widening on a non-event —
recurring on `bullish_rally_coverage` (3/3 rounds), `stale_resolved_incident`,
`distant_macro_event` and `favorable_regulatory_clarity`. It cannot
separate *newsworthy* from *actionable*, and the news role's only lever
is widen, so its errors all push the grid wider on a quiet tape. That is
precisely the over-trading news.md warns against — and it is a different
question from its quant-seat performance, where the same tendency to act
shows up as never once holding.

**A fixture of mine was withdrawn mid-measurement.** `single_denied_rumor`
("unconfirmed report… exchange DENIES it" → labelled hold) was missed by
3 of 4 models. On audit they were defensible and the label was not: a
denied liquidity rumour about a large exchange is live "exchange-outage
chatter", which news.md names as a widen trigger, and markets do move on
denied rumours. The prompt permitted both readings, so the fixture
measured the author's fiat — the same defect that withdrew two `hard`
fixtures. Fixed by making the EVIDENCE dispositive (proof-of-reserves
published, on-chain analysts confirm) rather than by relabelling; it now
passes 9/9 and still tests debunked-vs-live, which the noise-vs-signal
rule does cover.

**NOT WIRED.** The `moe-advisor` profile still says `deepseek-r1:8b`,
which has never been scored. MoE is off, so nothing is live either way.

## Arbitrator seat — RE-VALIDATED 2026-08-11: the 8/8 did not reproduce

The register recorded `gpt-5-mini` at 8/8 from **one run of 8 fixtures**.
Re-run at 3 rounds:

| | round 1 | round 2 | round 3 | total | $/mo @1094 sweeps |
|---|---|---|---|---|---|
| `gpt-5-mini` (incumbent) | **7/8** | 8/8 | 8/8 | **23/24** | $2.46 |
| `claude-haiku-4-5` | 8/8 | 8/8 | 8/8 | **24/24** | $3.06 |
| `voting` (free, deterministic) | 5/8 | 5/8 | 5/8 | 15/24 | $0 |
| `weighted_confidence` (free) | 1/8 | 1/8 | 1/8 | 3/24 | $0 |

**Three things follow.**

**1. The headline number was optimistic.** 8/8 was one draw of a
distribution whose mean is 23/24. Not wrong, not reproducible — which is
the whole point of the variance finding above.

**2. The dropped fixture is the dangerous one.** gpt-5-mini's single
failure was `never_emit_a_tighten`: it emitted 0.9 against a live 3.0 —
a tighten the auto-apply gate discards, on a prompt that forbids them
outright. Note the pattern across seats: on news its 10 failures were all
OVERTRADE, and on quant it has never once held in 216 production calls.
**One disposition — bias toward acting — surfacing as a different defect
in each seat.** That is a model property worth carrying forward, not
three unrelated results.

**3. The LLM arbitrator DOES earn its cost over mechanical aggregation.**
That was this tool's founding question and the 3-round answer is
unambiguous: 23–24/24 against `voting`'s 15/24 and
`weighted_confidence`'s 3/24. The free baselines fail by construction —
they have no concept of expert ROLE, so rules 1 and 2 are unreachable —
but that is exactly the gap an LLM in the seat is being paid to close.

**Still ceilinged, and NOT re-decided.** Both candidates sit at 23–24/24,
so 8 fixtures cannot rank them; `haiku-4-5`'s one-point edge is inside
the run-to-run noise measured above. Choosing between them needs the
gen2 treatment (boundary cases per rule) — built 2026-08-12, results
immediately below.

### gen2 (2026-08-12): the instrument improved, the tie did NOT break

Nine boundary cases added (17 total), each a rule COLLISION rather than a
rule restated, plus the first-ever coverage of `order_size_usd` — which
rule 1 names as a conservative lever and rule 4 names outright, and which
v1 never tested. 3 rounds × 17 = 51 judgments each.

| | round 1 | round 2 | round 3 | gen2 | **v1 + gen2** |
|---|---|---|---|---|---|
| `claude-haiku-4-5` | 17/17 | 17/17 | 17/17 | **51/51** | **75/75** |
| `gpt-5-mini` | 17/17 | 16/17 | 17/17 | 50/51 | 73/75 |
| `voting` (free) | 8/17 | — | — | 47% | (was 62% on v1) |
| `weighted_confidence` (free) | 6/17 | — | — | 35% | (was 12% on v1) |

**Report the failure honestly: gen2 did not do the job it was built for.**
All nine new boundary cases were passed by both models on every round —
including `risk_proposes_a_tighten`, the rule-1-versus-constraint
collision expected to be the sharpest. Both frontier models handle this
role cleanly, and no amount of boundary-case craft separated them.

**What gen2 *did* achieve** is worth keeping: it dropped the free-voting
floor from **62% to 47%**. On v1 the entire gap between a role-blind
aggregator and a competent LLM rested on ~3 fixtures. That is a real
instrument improvement even though it produced no ranking.

**The one discriminator is narrow and not significant.** Across all 75
judgments haiku has never failed a single arbitrator fixture; gpt-5-mini
has failed twice, and **both are the same fixture** —
`never_emit_a_tighten`, where it emitted 0.9 against a live 3.0. Fisher
**p = 0.248**. So: a consistent, specific, safety-adjacent weakness
(it breaches the never-tighten constraint roughly a third of the time on
that case) that the sample size cannot confirm.

**Recommendation moves to `claude-haiku-4-5`, weakly.** It costs $0.60/mo
more ($3.06 vs $2.46) and has never breached the constraint. Reasonable
people could keep the incumbent on p=0.248. What should NOT happen is
another gen3 — the role appears mechanically easy for frontier models,
and more fixtures will likely just re-confirm that.

**This strengthens the case for testing local models here** (see the
pinned local-model pass): if two frontier models are indistinguishable
at ceiling, the open question is not *which cloud model* but *whether
this seat needs a cloud model at all*.

## Risk seat — SCORED 2026-08-12: `gpt-5-mini`, but it is a coin flip

The register had carried this seat as *undecided* with "battery was
blocked… unblocked 2026-08-10" and no recorded run — `probe_risk.py`
shipped but had never been scored. Now it has, at 3 rounds × 18
fixtures = 54 judgments each.

| model | OK | per round | SUBOPT | **UNSAFE** | severe | $/mo @1094 sweeps |
|---|---|---|---|---|---|---|
| **`gpt-5-mini`** | **50/54** | 17, 16, 17 | 4 | **0** | **15/15** | **$2.46** |
| `claude-haiku-4-5` | 49/54 | 15, 18, 16 | 5 | **0** | **15/15** | $3.06 |

**Fisher p = 0.5 — there is no difference.** The recommendation is
`gpt-5-mini` purely because it is cheaper; on merit this is a tie and
either would do.

**The result that matters is the zero.** Neither model made a single
UNSAFE call in 54 judgments, and both scored **15/15 on the severe
fixtures in every round** — the compounding-pressure cases where holding
is the capital-preservation failure the seat exists to prevent. This
battery grades severity precisely because its answer space is binary
(DE-RISK vs HOLD) and so floors at 50% for a constant; the discriminator
was never direction, it was whether a model freezes under pressure.
Neither does.

**A cross-seat note worth carrying.** gpt-5-mini's bias toward acting —
the thing that cost it the news seat (10/10 misses were OVERTRADE), the
arbitrator's `never_emit_a_tighten` fixture, and 216 straight production
quant calls without one HOLD — is **not a liability here**. On the risk
seat, acting *is* de-risking, which is the safe direction. Same
disposition, opposite sign, depending on what the seat's lever does.

**One fixture to watch, not withdraw.** `fresh_drawdown_light_exposure`
was missed by both models in 5 of 6 runs. Unlike the news set's
withdrawn `single_denied_rumor`, this one is already graded
**SUBOPTIMAL rather than UNSAFE** — holding is genuinely defensible
there (a -4.2% drawdown but exposure at only ~25–35% of caps, so one
trigger fires and two don't), and the severity axis is doing exactly its
job by calling it defensible-but-not-ideal. It costs both models the
same point and does not distort the ranking. Left as-is.

## Local-model pass — 2026-08-12: local loses all three seats

Operator-raised and correctly so: three of four seats had been decided on
**cloud-only fields**, and every one of those calls turned on cost — an
axis local inference deletes. This pass tested it instead of assuming.

**Method.** Scored on the desktop (identical weights + quant → identical
output distribution; only latency is hardware-dependent), then measured
real latency on the NAS separately. Scoring on the NAS would have been
~6 hours *per model* — 130s/call for a 7B q4, of which ~112s is prompt
evaluation, not generation.

### Screen — arbitrator gen2, 17 fixtures

| model | score | |
|---|---|---|
| `gemma4:e4b-it-q8_0` | **15/17 (88%)** | only clear survivor |
| `llama3.1:8b-instruct-q4_K_M` | 11/17 (65%) | clears the free floor |
| `qwen2.5:7b-q4` · **`phi4:14b-q4`** · `granite3-dense:8b-q4` · `qwen2.5:3b-q4` · `solar:10.7b-q4` · `falcon3:3b-q8` | **5/17 (29%) each** | — |
| *free* `voting` / `weighted_confidence` | 8/17 / 6/17 | $0 |
| constant-EMPTY / constant-ECHO | 6/17 / 5/17 | does nothing |

**Six of eight landed on the do-nothing floor.** They are not
underperforming the task; they are not attempting it. That includes
**`phi4:14b-q4_K_M` — the tag the `moe-advisor` profile still names for
both quant and arbitrator** — at 5/17, below free `voting`, mostly by
emitting nothing plus one empty-rationale schema error. The configured
default arbitrator is worse than deleting the LLM and keeping the
deterministic aggregator.

### Deep run — 3 rounds, the two survivors

| seat | best cloud | `gemma4` (local) | `llama3.1:8b-q4` | verdict |
|---|---|---|---|---|
| **news** (66) | haiku **65/66** | **61/66 (92%)** | 51/66 | **p = 0.104** — not significantly worse |
| **arbitrator** (51) | haiku **51/51** | 44/51 (86%) | 35/51 | **p = 0.0063** — significantly worse |
| **risk** (54) | gpt-5-mini **50/54, 0 UNSAFE** | 33/54, **10 UNSAFE** | 32/54, 3 UNSAFE | **p = 0.0006** — disqualified |

**Risk: disqualified on the safety axis, not the score.** gemma4's ten
UNSAFE calls are *all* on severe fixtures, and three of them failed in
**all three rounds** — `cap_pressure_plus_fresh_drawdown`,
`deep_drawdown_high_exposure`, `losing_streak_near_caps`. It holds when
coin exposure is 93%, total 79%, daily 82%, against a fresh -4.5%
drawdown on a losing book. That is reproducible failure on precisely the
cases this seat exists to prevent. Both cloud models: 15/15 severe, zero
UNSAFE, every round.

**News: the one genuinely close call — and latency kills it.** gemma4
beats gpt-5-mini (61 vs 56) and trails haiku by a statistically
insignificant margin, at $0 against $3.06/mo. But on the NAS it takes
**228.7s per call** (4.64 tok/s, and it saturated the 600-token cap, so
real calls may run longer) against ~25s for a cloud call. The news seat
fires once per symbol-sweep: 36 calls/day × 228.7s ≈ **2.3 hours/day of
pegged CPU** on a box that also runs eight other containers. Saving
$3.06/month does not buy that.

Note gemma4 is 8B at **q8**, which violates this project's own NAS rule
(7–8B → q4_K_M). A q4 build might roughly halve the latency, but it is
on neither host and quantization changes output quality, so it would
need re-scoring, not extrapolation.

**Conclusion: the cloud recommendations stand, now for measured reasons
rather than absence of evidence.** The seats were never settled by
cost alone — on risk the local option is dangerous, on arbitrator it is
significantly worse, and on news it is competitive but unaffordable in
CPU time. The one thing this did overturn is the *config default*:
`phi4:14b-q4_K_M` should not hold any seat.

## Rules for changing a seat

1. **A seat changes on battery evidence, not on vibes or vendor news.**
   Fluency is not correctness: `granite4.1:30b` wrote the session's most
   impressive risk prose and scored worst of six on quant.
2. **Use the right battery.** `probe_advisor.py`'s core/heldout sets key
   to the vol→spacing curve ADR-022 retired and mostly test cases the
   deterministic guards resolve before the LLM sees them — they are not
   valid for quant-seat selection. `probe_freejudge.py` is.
3. **Respect the cost gate.** The advisor-model-review routine
   pre-filters challengers at **≤3× the champion's measured per-call
   cost**. `claude-sonnet-5` posted the best UNSAFE card ever recorded
   (0 of 42) and still did not file, at 6.1×.
4. **Errors are not verdicts.** A battery run with a non-zero ERROR
   count is a broken request until proven otherwise — the 2026-08-10
   roster scored two Claude models at 0/36 before the cause turned out
   to be an adapter sending a deprecated field.
5. **Record the decision here and in the bake-off doc, in the same
   commit as the config change.**

## Coverage (2026-08-11 — the Gemini + Atlas gap is CLOSED)

Tested provider stacks: **Ollama**, **OpenAI**, **Anthropic**,
**Google/Gemini**, **Atlas Cloud**. The 2026-08-10 gap note below was
closed by a 15-model sweep on `probe_freejudge`; full record in
`advisor-llm-models.md` Rev 2026-08-11.

**The finding that matters is about the instrument, not the models.**
Ten of eleven frontier models scored **zero UNSAFE** — including
`gpt-5-mini`, which averages 0.67 per run. UNSAFE is the axis the
review routine's switch thresholds are built on, and it has
**saturated** among current models: it can no longer separate them.
Only `gemini-2.5-flash`, the oldest model in the field, registered any.

**The capability floor is a PARSEABILITY cliff, not a judgment cliff.**
Four sub-$0.15/M models were run to locate where UNSAFE starts
discriminating again. They failed — but almost none by judging badly:
21 of 56 fixtures produced no parseable output at all
(`xiaomi/mimo-v2.5` errored on all 14; retries exhausted), against
**0 errors in 84 mid-tier judgments**. What separates the tiers on this
battery is reliably emitting schema-valid JSON under a long prompt, not
market judgment. That reframes the flagship result too: this battery
measures instruction-following more than trading sense, which is why
everything above the floor looks identical on it.

**Two genuine challengers exist, and neither files a switch yet.**
Across 42 judgments each: `minimaxai/minimax-m3` **40/42 OK, 1 UNSAFE,
0.2x champion cost**; `deepseek-ai/deepseek-v4-pro` **37/42 OK, 0
UNSAFE, 2.5x**. Both beat the champion's 35/42 and both are in-gate.
But the champion had 2 UNSAFE in 42, so the deciding axis separates
them by 2-vs-1-vs-0 at n=42 — inside noise (`zai-org/glm-5.2` scored
9/10/11 across three runs of identical fixtures). **Filing a switch on
that would repeat the `claude-sonnet-5` error with cheaper models.**

**Next work is a better instrument, not another sweep:** fixtures that
discriminate ABOVE the instruction-following floor. Until those exist,
the battery cannot justify moving a seat.

Atlas's standing value remains the ~61 publicly-priced models
unreachable natively. Models already tested natively should not be
re-run through it. Note Atlas does **not** publish prices for its ~25
Anthropic entries — never infer those from upstream list prices.
