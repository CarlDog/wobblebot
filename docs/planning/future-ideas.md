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

---

## Discord Integration

**What:** A `NotifierPort` adapter that posts to Discord — trade fills,
significant events, critical alerts. Possibly extended into a
two-way control surface where operator commands (`/pause`, `/status`,
`/cancel-all`) come back through Discord interactions.

**Why interesting:**
- Discord is already running on the operator's phone/desktop — no
  separate dashboard to babysit. Free push notifications for free.
- Severity routing is cheap: one channel for routine info (trades),
  another for warnings/errors. Operator can mute the noisy one.
- Two-way: a slash-command interface in Discord could substitute for
  the Stage 5.2 web Control Surface for a long time. Discord handles
  auth (only people in the guild see commands) and UI rendering.

**What it touches:**
- Phase 5+ initially — Notifier adapters are slated for Stage 5.1
  (Dashboard) / 5.2 (Control Surface) per roadmap.md. A read-only
  Discord notifier could land earlier (any time after Phase 2 once
  there's interesting events to forward).
- ADR-002 (LLM advisory-only) still holds — operator commands from
  Discord become normal service-layer calls, not LLM-driven.
- New ADR likely needed if two-way control lands: spells out which
  commands are exposed, how authentication works (Discord user id
  allowlist?), what's gated.

**Open questions:**
- Webhook-only (write-only, simplest) or a full bot connection
  (read commands, react with embeds)? Probably start webhook-only
  for trade notifications; upgrade to a bot when the control
  surface story matures.
- Channel topology: one channel for everything, or split by
  severity (#wobble-info / #wobble-alerts)? Probably the latter once
  trade volume picks up.
- Rate limits: Discord webhooks cap at 30 messages/minute. A chatty
  Phase 3+ advisor could exhaust that. Batch messages and post on
  an interval, or use a queue with backpressure.
- Secrets: webhook URLs are bearer tokens. Live in `.env`, never
  committed; the pre-commit hook + gitleaks already guards this.
- Embed shape: trade fills want one schema (price, amount, fee,
  P&L delta); advisor recommendations want another (rationale,
  confidence, proposed changes). One `discord_notify(notification)`
  function with format-by-level switching, or per-type emitters?

**Status:** Idea only. Not on the current roadmap.
