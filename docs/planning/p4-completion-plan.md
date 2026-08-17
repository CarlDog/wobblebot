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

**b. Directional evaluator dispatch (M).** `kind=directional_call`
scoring: grade a call against realized price direction over its
stated horizon from stored bars — no replay, no counterfactual arm,
`granularity_minutes` NULL (ADR-035 decision 4). **Ordering
constraint: must land before the Gremlin's first emission** —
evaluator v1 classifies unknown-key suggestions as "keys outside the
replayable surface" and would permanently write the Gremlin's calls
off as unscoreable at v1. Slice notes:

- Kind classification (by role vs by recommendation shape) is the
  slice's first design call; whichever is chosen, add a pin test that
  every existing-corpus shape still classifies exactly as v1 did — if
  that invariant holds, no `EVALUATOR_VERSION` bump is needed (the
  new path only touches rows that don't exist yet).
- Sign definition (what counts as "the call was right", the tie band
  for chop calls) gets written down in this doc when settled.

**c. The Chaos Gremlin (M).** Per the ratified design in
`docs/release/v1.1/adaptive-grid.md`: a standalone loose-reasoning
voice reading the same `PerformanceSummary`, emitting a falsifiable
directional/regime call. Load-bearing constraints from that design:

- `gremlin` joins `_BLOCKED_ROLES` — never auto-applies.
- **Standalone observer, never MoE/arbitrator-fed** (the
  `role="aggregated"` laundering hole). It rides beside the cascade;
  it does NOT need MoE enabled — the register's "with MoE-on" note is
  an ideal, not a dependency, and the design's own argument is to
  turn it on early so its track record accumulates.
- Discipline note carries over verbatim: "loose intuition loses to
  rigor here" is a finding, not a failure — do not tune the Gremlin
  until its scoreboard flatters it.
- In-slice decisions: the call schema (direction + horizon +
  conviction, persisted in the suggestion's `recommendations` dict),
  emission cadence, and the seat's model (deliberately
  un-batteriable — the outcome ledger IS its battery; pick something
  cheap and leap-prone, note it in the seat register).

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
