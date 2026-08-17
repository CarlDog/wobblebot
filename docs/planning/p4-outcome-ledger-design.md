# P4 — Advisor Outcome Ledger: slicing + ratified parameters

**Kickoff 2026-08-17.** The methodology is ADR-035 (counterfactual
two-arm replay via the ADR-028 auditor; rank + hit-rate, never
dollars); this doc pins the implementation parameters ADR-035 left
open and slices the build. Corpus at kickoff: **2,862 suggestions**
(2026-05-27 →), heuristic 2,413 / quant 449 — the escalated branch has
more than doubled since the ADR's 196.

## Ratified parameters (operator, 2026-08-17)

1. **Replay window: 7 days**, recorded on every outcome row
   (`window_start`/`window_end`). Rationale: at production 3% spacing,
   grid cycles take days — a 4h/24h window mostly measures unrealized
   position deltas. Attribution blur across subsequent recs is
   accepted and visible (the window is on the row); window variants
   can coexist later because the row records its own.
2. **Granularity: all symbols @60m, BTC additionally @1m** (per-row
   `granularity_minutes`). Scores the whole 6-symbol corpus at hourly
   fidelity; BTC's 1m arm doubles as a fidelity cross-check of the
   hourly scores (ADR-035's replay-fidelity consequence, handled by
   scoping rather than silence).
3. **In-force arm source: the suggestion's own `input_summary.current_grid`.**
   Every suggestion persists the config in force at emission time, so
   the counterfactual pair is derivable per-row with no config-history
   reconstruction. A suggestion missing `current_grid` is
   **unscoreable** with that reason, never guessed.

## Slices

- **P4.1 — the ledger (one migration, ships with per-cycle tracing).**
  `recommendation_outcomes` in **advise.db** beside its subject +
  `llm_calls.trace_id` (the small per-cycle-tracing column the v1.1
  register bundles into this migration). Domain model, StoragePort
  surface (`save_recommendation_outcome`, `get_recommendation_outcomes`,
  `get_unscored_suggestions`), additive migration per house rules,
  tests. No evaluator yet.
- **P4.2 — the evaluator.** `services/advisor_evaluator.py` +
  `tools/score_recommendations.py` driver. Per suggestion: classify
  (numeric keys inside the replayable surface + `current_grid` present
  + bars available for the window → scoreable; else unscoreable with
  reason — ADR-035 decision 5); build both arms; replay from identical
  seeds over identical bars (auditor internals, `max_daily_spend_usd`
  neutered in BOTH arms per ADR-028 correction 1); write the outcome
  sign. Idempotent by `UNIQUE(suggestion_id, granularity_minutes,
  evaluator_version)`; resumable batch over the corpus.
- **P4.3 — the scoreboard.** Tools log-table report first (screener
  precedent), web card later. Per-role hit-rate + sample size +
  granularity, cascade-vs-counterfactual framing (never a naive
  heuristic-vs-quant ranking — ADR-035 decision 7), plus the paired
  comparison: re-run the free deterministic heuristic offline over the
  449 escalated inputs and compare paired on that subset only.
- **P4.4+** — `weather_report`, daily summary, the (90d) historian —
  per the v1.1 register's ordering; data retention already shipped
  out-of-order (ADR-036).

## `recommendation_outcomes` shape (P4.1)

One row per (suggestion, granularity, evaluator_version):

- `suggestion_id` — `advisor_suggestions.id`, same DB.
- `kind` — `config_rec | directional_call` (ADR-035 decision 4: the
  Gremlin's shape is a first-class citizen from day one; the evaluator
  dispatches on it, and directional calls carry no replay arms).
- `scoreable` 0/1 + `unscoreable_reason` (never silently neutral —
  decision 5).
- `window_start` / `window_end` / `granularity_minutes` (NULL for
  directional calls).
- `proposed_arm_json` / `inforce_arm_json` — replay summaries
  (cycles, fills, end-state deltas; **no dollar figure is ever
  surfaced from these** — decision 3).
- `outcome` — `better | worse | tie` (the sign of the difference;
  NULL while unscored/unscoreable).
- `evaluator_version` + `scored_at` — re-scoring after an evaluator
  change writes new rows at the new version; old rows are audit
  history, never overwritten.

## P4.2 implementation notes (2026-08-17, from the first live probe)

Two behaviors the design left implicit were settled by running the
evaluator against the real corpus, not by argument:

1. **The spacing-vs-fees floor is the WINDOW's, not today's.** Arms
   validate against `2 × maker` at the window-start fee schedule
   (0.5% before the 2026-07-09 doubling, 0.8% after), matching the
   fees the replay charges. `GridConfig`'s own validator hardcodes
   today's constant, so arms build via `model_construct` — the one
   sanctioned bypass, with the floor re-applied at the window rate.
   Without this, the May-era heuristic's 0.65% first-order-curve recs
   (legal in their era) would be censored as "invalid config" —
   1,300+ rows, the corpus's most opinionated stretch. Post-cutover
   0.65% recs ARE floor-blocked, with the floor in the reason: the
   old curve kept emitting them for ten weeks after they became
   unprofitable, which is itself a finding the ledger now records.
2. **Bars-missing leaves the suggestion in the queue.** A written
   unscoreable row is permanent at its evaluator version; bar absence
   is a fact about the scoring machine's imports, not the suggestion.
   The driver tallies `bars-missing` loudly and writes nothing, so
   importing the dump and re-running scores the same rows at the SAME
   version. Permanent facts (empty rec, foreign keys, missing/invalid
   `current_grid`, below-window-floor spacing) still write rows.

Data logistics for the full 60m pass: 1,230 suggestions wait on the
**Q2 2026 OHLCVT dump** (Apr–Jun; local 60m history ends at the Q1
dump, and the live endpoint's ~720-bar retention only reaches back to
~Jul 9). Jul 1–8 windows stay bars-missing until the **Q3 dump**
(published ~October). The canonical ledger lives in the NAS
`wobblebot-advise.db`; where the canonical scoring run executes (NAS
tools container vs desktop against a synced copy) is an open operator
call — every verification run so far used scratch copies only.

## P4.3 implementation notes (2026-08-17)

- `services/outcome_scoreboard.py` (pure aggregation — the future web
  card reuses it so framing can't drift between surfaces) +
  `tools/score_report.py` (the log-table renderer, read-only; refuses
  to connect-and-create a DB at a mistyped ``--db`` path).
- **Hit-rate = better / (better + worse)**, ties reported beside and
  never folded in; the rate is withheld under 30 decisive rows (counts
  still print). Fidelity note rendered with every scored table: 60m
  rows are directional, 1m rows `_Sim`-equivalent.
- **The decision-7 pairing resolved to something simpler than the ADR
  imagined.** On an input the cascade escalated, the guard layer's
  would-have-said is HOLD *by construction* — that is what escalation
  means — and a hold keeps the in-force config, which is exactly the
  counterfactual's in-force arm. So the paired quant-vs-heuristic
  result on the escalated subset is the outcome sign re-labeled; no
  second replay exists to run. The report still VERIFIES the premise
  per row instead of assuming it: the current guard spec re-runs over
  each scored quant row's stored `input_summary`, and rows where a
  guard fires on re-run (spec drift since emission) or the summary no
  longer parses are excluded and counted. First live run against the
  scratch corpus: premise held on **109/109** re-run inputs under the
  shipped `config/heuristic/quant.yml`.
- `--heuristic-file` overrides the settings' spec — the desktop
  reporting on a NAS corpus copy doesn't run the cascade locally
  (found on the first live run, not in design).

## Honest bounds (restated from ADR-035, binding on P4.3's copy)

The first scoreboard measures **the cascade's escalated branch against
its counterfactual** — two roles, one LLM — not an inter-expert panel.
Hit-rates surface only past a stated minimum sample with the sample
beside every figure. Hourly-granularity rows carry more replay
uncertainty than the BTC 1m rows and say so.
