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
| **quant** (escalation) | `gpt-5-mini` | freejudge OK 83% / UNSAFE 2 of 42; held vs opus-5, sonnet-5, haiku-4-5, gpt-5.4-mini/nano | `probe_freejudge.py` | 2026-06-04 (ADR-022), reconfirmed 2026-07-31 + 2026-08-10 | ✅ wired — `cpu-only` profile |
| **arbitrator** | `gpt-5-mini` | 8/8 vs `voting` 5/8 and `weighted_confidence` 1/8 on identical inputs | `probe_arbitrator.py` | 2026-08-10 | ⚠️ **NOT wired** — `moe-advisor` profile still says `phi4:14b-q8_0` |
| **news** | *undecided* | 4 models scored (haiku-4-5 / sonnet-5 / opus-5 12/12, gpt-5-mini 11/12, qwen3.6:35b-a3b 10/12) but no seat chosen | `probe_news.py` | — | ⚠️ **NOT wired** — profile says `deepseek-r1:8b`, never scored |
| **risk** | *undecided* | battery was blocked on the input mismatch; **unblocked 2026-08-10** | *(none yet)* | — | ⚠️ **NOT wired** — profile says `qwen3:8b`, never scored |
| **gremlin** | *never run* | prompt exists; no battery, no production path | *(none)* | — | not in any profile |
| **operator assistant** | `qwen2.5:1.5b-instruct-q4_K_M` | 8/8 on the NAS sweep, no cache-warm tax | `probe_assistant.py` / `sweep_assistant_nas.py` | 2026-05-27 | ✅ wired — `cpu-only` profile |

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

## Coverage gaps (2026-08-10)

Tested provider stacks: **Ollama**, **OpenAI**, **Anthropic**.
Untested: **Google/Gemini** (last measured May 2026, superseded models)
and **Atlas Cloud** (plumbing verified end-to-end, but not one fixture
has been scored through it). Atlas's value is the ~61 publicly-priced
models unreachable natively — DeepSeek, Qwen, Kimi, MiniMax, ByteDance,
Grok — several of which are *cheaper* than the champion and therefore
inside the routine's cost gate. Models already tested natively should
not be re-run through Atlas.
