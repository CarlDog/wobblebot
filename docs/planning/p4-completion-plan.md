# P4 completion plan — the advisor-feedback cluster after the keystone

**Ratified 2026-08-17** (operator, four decisions below). The keystone
(P4.1 ledger / P4.2 evaluator / P4.3 scoreboard) shipped and deployed
2026-08-17 (`d0d5c95`); this doc slices the remainder of the v1.1
register's P4 cluster (`docs/release/v1.1/README.md`). Register rows
already closed along the way: outcome tracking, the auditor's
rec-scoring half (= the P4.2 evaluator), the `trace_id` migration
(column only — see P4.4a), data retention (ADR-036, early), the
risk-DTO bug (2026-08-10), and the risk/news batteries (seat-matrix
work).

## Ratified decisions (2026-08-17)

1. **Ordering: clocks first.** The items that generate data accrue
   value only from the day they ship (trace wiring, the Gremlin's
   track record); the read-side items (weather_report, Historian,
   cost dashboard) lose nothing by waiting. Ship the clock-starters
   first.
2. **Daily summary: parked.** Operator-demand item; the dashboard +
   Discord `status_report` cover pull-style checking. Stays in the
   register, out of this plan.
3. **weather_report runs on NAS Ollama** via the existing
   `AssistantPort.summarize` implementation. The cloud `summarize`
   implementations stay out of scope unless the prose disappoints.
4. **Canonical scoring runs execute in the NAS tools container** (the
   `cli/apply` pattern): writes land directly in the canonical
   `advise.db`, bars come from the NAS `observe.db` after the dump
   import, no sync step. Desktop scoring remains scratch-copy
   analysis only.

## Slices

### P4.4 — start the clocks

**a. `trace_id` write-side (S).** ✅ **Shipped 2026-08-17.** The P4.1
migration shipped the column; nothing wrote it. Now: an ambient
`ContextVar` scope (`services/llm_trace.py`) set by `cli/advise` once
per symbol-evaluation (`llm_trace(uuid4())` around the advisor call),
read at record-build time in the `services/llm_cloud_call.py`
chokepoint — success AND failure records — so no `AdvisorPort`
signature changed. asyncio task-isolation pinned by test; the
"advise cycle complete" log line carries the trace id for
log↔ledger correlation. Callers outside any scope (cli/operator
today) keep writing NULL, exactly the pre-P4.4a shape.

**b. Directional evaluator dispatch (M).** ✅ **Shipped 2026-08-17.**
`kind=directional_call` scoring: no replay, no arms, NULL granularity
(ADR-035 decision 4). Settled design, now binding at evaluator v1:

- **Kind is ROLE-gated** (`DIRECTIONAL_ROLES = {"gremlin"}`), with
  shape validation on top: `{"direction": up|down|chop,
  "horizon_hours": > 0}` (extra keys tolerated; malformed →
  unscoreable with reason and a ZERO-LENGTH window so it surfaces
  immediately instead of pending forever). The no-version-bump pin
  test holds: a config role emitting direction-shaped keys classifies
  exactly as v1 did, so `EVALUATOR_VERSION` stays 1.
- **The window is the call's own horizon** (`created_at +
  horizon_hours`), not the 7-day config window; pending semantics
  fall out naturally.
- **Sign definition:** realized move = last-known 60m close at
  horizon end vs at call time — "last known" = the most recent bar
  CLOSED at or before the moment (no lookahead into the forming
  bar), each within 2 intervals or the row stays queued bars-missing.
  Chop band 1% (`DIRECTIONAL_CHOP_BAND`; changing it = version bump).
  up/down: right beyond the band the called way, wrong beyond it the
  other way, TIE inside it (the market didn't rule). chop: right
  inside the band, wrong outside — no tie case, full falsifiability.
- **The grading record** (call, prices, move, band, grading interval)
  lives in `proposed_arm_json`; `inforce_arm_json` stays NULL.
- **The driver's third pass**: `--directional` drains the
  NULL-granularity queue namespace; each pass skips the other kind's
  suggestions without writing, so the namespaces stay independent.

**c. The Chaos Gremlin (M).** ✅ **Shipped 2026-08-17, disabled by
default.** The operator's June-4 prompt draft became the live prompt
with three surgical patches (role `custom`→`gremlin`; the P4.4b
grading contract — ±1% band semantics, horizon 4–72h guidance; the
exact-values constraint clause for small models). Implementation
settled:

- One new `GremlinConfig` (`advisor.gremlin`: enabled false, provider
  ollama, model `qwen2.5:3b-instruct-q4_K_M`, temp 1.0 by default —
  the leap is the charter, `min_interval_minutes` 240 ≈ 6
  calls/day/symbol). The role reuses `_build_advisor_adapter`
  wholesale — no new adapter.
- `gremlin` is in `_BLOCKED_ROLES` (never applies) and in the
  `LLMRole`/`PromptRole` registries; it rides beside the cascade in
  `cli/advise`'s sweep, cooldown-gated per symbol (in-memory,
  restart-resets; marked on SUCCESS only so flaky calls retry next
  tick). NOT MoE-fed, per the ratified firewall.
- Cross-slice contract pinned by test: a gremlin emission persisted
  through the normal cycle path classifies as a SCOREABLE
  directional call under the P4.4b evaluator.
- Discipline note carries over verbatim: "loose intuition loses to
  rigor here" is a finding, not a failure — do not tune the Gremlin
  until its ledger sample is real.
- **Enabling it in production is the operator's flip**:
  `advisor.gremlin.enabled: true` in the NAS settings.yml + an advise
  restart (the model is already resident on the NAS Ollama — 12.7
  tok/s hot, benchmarked 2026-05-27).

### P4.5 — `weather_report` (M–L)

The Oracle seed (operator's reserved name for the eventual
forecast compiler; the catalog name is decided at implementation —
`market_report` / `weather_report` / `outlook`). Deterministic
aggregation first, prose second (the llm-app rule: precompute the
facts, the model narrates and cites):

- Aggregation layer: per-symbol multi-day price trends from
  `ohlc_bars`/TA, news window (3–7d) with sentiment, recent advisor
  suggestions across symbols (the Gremlin's calls included, once
  P4.4c ships — the design doc calls it "a natural single-forecaster
  on-ramp" to exactly this track).
- Prose via `AssistantPort.summarize` on NAS Ollama (decision 3).
- Surface: operator-catalog query beside `status_report`; a web card
  can ride later.

### P4.6 — LLM Historian (XL — own design doc before code)

`cli/historian` synthesizing macro patterns over the full history →
`historian_findings` + `/historian`. Read-only first. Its 90-day data
gate matured 2026-08-16 (production data since 05-18), but it is
sequenced **after** the Q2-dump scoring thread below so its first
synthesis includes a scored ledger. The design doc must settle: model
(likely cloud long-context, cost-gated), cadence, input corpus
(trades / outcomes / news / suggestions / regime), and the privacy
note from the register (a personal scoreboard, not a publishable
benchmark).

### P4.7 — cost-honesty dashboard (M)

Realized PnL beside fees + LLM spend + operator-declared infra
(`cost_assumptions`) → net-vs-cost + annualized projection, plus the
`/cost` "by cycle" toggle riding the trace data P4.4a has been
accumulating by then.

## The standing external thread (interleaves at any point)

The Q2 2026 OHLCVT dump (watched weekly by the `kraken-q2-dump-watch`
scheduled task; overdue vs Kraken's ~3.5–4-week cadence). When it
lands: operator downloads → extract to `data/kraken-history/2026Q2/`
(and the NAS equivalent) → import the six live symbols @1h + BTC @1m
→ **canonical scoring run in the NAS tools container** (decision 4)
at 60m corpus-wide + 1m BTC → the scoreboard's first believable read,
including the 1m-vs-60m fidelity cross-check the ledger design
promised. The Jul 1–8 window gap persists until the Q3 dump
(~late October).

## Parked / adjacent (explicitly not in this plan)

- **Daily summary** — parked (decision 2).
- **`AssistantPort.summarize` cloud impls** — out unless decision 3
  is revisited.
- **MoE enablement** (a CPU-viable `moe-cpu` profile) — a separate,
  legitimate work item (ADR-035 consequence note) that unlocks the
  panel corpus; not P4.
- **Probe-battery harness punch list** (task #29) — dormant until the
  next seat campaign.
