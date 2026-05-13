# Future Ideas

A scratchpad for ideas that are not committed to any phase of
`roadmap.md` but are worth remembering. Each entry should capture:
**what**, **why interesting**, **what it touches**, **open questions**.
Move entries into `roadmap.md` (with a Stage) or `decisions.md` (with
an ADR) when they graduate from "idea" to "planned."

---

## MoE (Mixture of Experts) Strategy Advisor

**What:** Run three different local LLMs in parallel against the same
performance summary, then combine their JSON recommendations into a
single advisory output. Could be a majority vote, a confidence-weighted
average over numeric parameters, or "tabled — humans review" when the
three disagree beyond a threshold.

**Why interesting:**
- A single LLM hallucinating a bad grid parameter is a single point of
  failure. Three independent models is one cheap way to dampen that.
- Different model families have different blind spots; an MoE makes
  the Advisor more robust without raising the safety surface (still
  advisory-only per ADR-002).
- Disagreement itself is a useful signal — "all three agree" vs "three
  way split" tells the operator how confident to be.

**What it touches:**
- Phase 3 (Stage 3.2 Advisor Port & LLM Integration). The `AdvisorPort`
  contract probably stays the same; the change is on the adapter side
  — instead of one LLM call, the adapter fans out to three and
  reduces.
- ADR-002 (LLM is advisory-only) — still holds; MoE is just a different
  way of producing the same shape of advisory JSON.
- Possibly a new ADR if the consensus algorithm has non-obvious
  trade-offs worth documenting.

**Open questions:**
- Three different models, or three instances of the same model with
  different prompts/seeds (cheaper, less independent)? Probably the
  former since the cost of running three on a NAS is low and the
  diversity is the point.
- Reduction algorithm: majority vote on categorical decisions
  ("widen grid" vs "tighten grid"); confidence-weighted mean on
  numeric parameters; veto-on-disagreement for any safety-relevant
  bound. Needs design.
- Latency: three sequential calls vs `asyncio.gather`. Probably the
  latter, but token throughput on a Synology may be the bottleneck.
- Storage: do we record all three raw outputs alongside the consensus,
  for retrospective analysis? Probably yes — cheap to store, useful
  for tuning.
- Cost: in a self-hosted Ollama setup the marginal cost is energy
  and time, not dollars. Different math if Phase 3+ ever uses hosted
  models.

**Status:** Idea only. Not on the current roadmap.
