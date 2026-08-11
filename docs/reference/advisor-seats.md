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
| **quant** (escalation) | `gpt-5-mini` — **but a challenger has FILED, see below** | freejudge `hard` OK 102/120 (85%), **UNSAFE 7**; held vs opus-5, sonnet-5, haiku-4-5, gpt-5.4-mini/nano, minimax-m3, deepseek-v4-pro | `probe_freejudge.py` | 2026-06-04 (ADR-022), reconfirmed 2026-07-31 + 2026-08-10 | ✅ wired — `cpu-only` profile |
| **arbitrator** | `gpt-5-mini` | 8/8 vs `voting` 5/8 and `weighted_confidence` 1/8 on identical inputs | `probe_arbitrator.py` | 2026-08-10 | ⚠️ **NOT wired** — `moe-advisor` profile still says `phi4:14b-q8_0` |
| **news** | *undecided* | 4 models scored (haiku-4-5 / sonnet-5 / opus-5 12/12, gpt-5-mini 11/12, qwen3.6:35b-a3b 10/12) but no seat chosen | `probe_news.py` | — | ⚠️ **NOT wired** — profile says `deepseek-r1:8b`, never scored |
| **risk** | *undecided* | battery was blocked on the input mismatch; **unblocked 2026-08-10** | *(none yet)* | — | ⚠️ **NOT wired** — profile says `qwen3:8b`, never scored |
| **gremlin** | *never run* | prompt exists; no battery, no production path | *(none)* | — | not in any profile |
| **operator assistant** | `qwen2.5:1.5b-instruct-q4_K_M` | 8/8 on the NAS sweep, no cache-warm tax | `probe_assistant.py` / `sweep_assistant_nas.py` | 2026-05-27 | ✅ wired — `cpu-only` profile |

## ⚠️ OPEN DECISION — `xai/grok-4.5` has FILED against the quant seat (2026-08-11)

**First challenger in the project's history to clear the review routine's
§5 thresholds with statistical significance.** 8 runs / 120 judgments each
on the `hard` fixture set:

| model | OK | SUB | **UNSAFE** | per-run OK | $/call |
|---|---|---|---|---|---|
| `xai/grok-4.5` | **119/120 (99%)** | 1 | **0** | 15,15,15,14,15,15,15,15 | $0.00781 |
| `gpt-5-mini` (champion) | 102/120 (85%) | 11 | **7** | 13,14,13,13,12,13,12,12 | $0.00206 |

Fisher exact: **OK p=0.000041**, **UNSAFE p=0.014**. The fresh 5-run half
reproduces it alone (p=0.0011), so it is not a pooling artifact. Both §5
criteria met: UNSAFE more than halved (7 → 0), OK gained >10 points. Every
prior challenger died at p=0.24–0.49; this is the first that did not.

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
