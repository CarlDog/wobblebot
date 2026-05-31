# Adaptive grid — making the grid smarter

> **STATUS BANNER (2026-05-30) — partially SUPERSEDED + PARKED by the grid-strategy
> research arc.** The 2026-05-29/30 backtests closed several ideas in this file:
> *vol→spacing adaptive tuning* is demoted (real BTC vol sits below the curve floor;
> trend, not vol, drives win-vs-lose) → it survives only as slow per-symbol base-spacing
> *calibration*, folded into Stage 8.6 hardening. The *dynamic regime-switching* idea
> (regime classifier → strategy/posture) was tested across three experiments and does NOT
> beat buy-and-hold — or even a static grid — with heuristic detection; it is PARKED on the
> Oracle/MoE research track (NOT deleted; the +164.6% oracle ceiling proves the idea has a
> real ceiling that needs LLM-grade detection). Read
> `docs/reference/grid-strategy-research-synthesis-2026-05-30.md` before actioning anything
> below; entries describing a regime classifier / regime-aware grid modes / confidence-driven
> extension are part of that parked track. Per-symbol spacing + the graduated auto-apply gate
> remain valid.*
>
> **Now ratified in ADR-019** (Stage 8.6 close, 2026-05-30): advisor purpose = regime reader +
> guardrail, not a vol-tuner. The vol→spacing curve recalibration + its 20-fixture judgment
> battery rework were *deferred* onto the parked Oracle track rather than shipped — recalibrating
> to "rest at 3%, never tighten" would bake in a false absolute (a tight grid chosen in chop and
> pulled before the trend works — proven live + the +164.6% oracle). The live grid runs a single
> survival-optimized 3% static default meanwhile (Stage 8.6 Slice C).*

*Entries here turn the static-grid engine into a regime-aware system: classify the market, evaluate advisor recommendations against outcomes, extend the grid under operator/advisor agreement, replay historical configs. All preserve ADR-002 (LLM advisory only) and ADR-006 (engine parks honestly when offside).*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Import the local Kraken historical dump into the DB (one-time bulk seed)

**What (operator-raised 2026-05-30):** we already have a large local Kraken
1-minute historical dump on disk — `data/kraken-history/` (BTC/ETH/SOL/XRP/DOGE
+ many other pairs, 2013→2025) plus the `data/kraken-history/2026Q1/` quarterly
subfolder (2026-01-01→03-31, ~99% coverage). It's been used extensively for the
grid-strategy backtests (`tools/grid_backtest.py` et al.) but lives ONLY as loose
CSVs read by those tools — it has never been imported into the project's database
structure (`ohlc_bars` / `price_snapshots` in observe.db). This entry: a one-time
bulk-import path so that already-downloaded data is available to every DB consumer
(advisor metrics, the future Auditor / Historian / regime work) without re-fetching
from Kraken.

**Why valuable:** the `cli/observe --backfill` feature (shipped 2026-05-25) fetches
history from Kraken's `/0/public/OHLC` endpoint — rate-limited (~1 call/sec
free-tier), network-dependent, and bounded by whatever horizon Kraken still retains
at fine intervals. But we *already have* a far deeper, denser local dump than a live
backfill could pull (Kraken's OHLC endpoint won't return 2013-era 1m bars). Importing
the local CSVs is faster, offline, $0, and gives every DB-reading consumer the full
history for free. The substrate already exists: `OHLCBar` domain model, the
`ohlc_bars` table with `UNIQUE(symbol, interval_minutes, opened_at)` INSERT-OR-IGNORE
idempotency, `StoragePort.save_ohlc_bars`, and the dual-write to `price_snapshots`.

**What's missing (small):**
- A `cli/observe --import-csv PATH [--symbols ...] [--interval N]` mode (or a
  `tools/import_kraken_history.py` one-shot) that streams the headerless Kraken CSV
  (`time,open,high,low,close,volume,trades`), maps each row to an `OHLCBar`, and
  batch-writes via the existing `save_ohlc_bars` + `price_snapshots` path. The
  loaders in `tools/grid_backtest.py::_load_ohlc` / `heuristic_backtest.py::_load_csv`
  already parse this exact format — reuse that parsing.
- Symbol mapping: the CSVs use Kraken asset codes (`XBTUSD`→BTC/USD, `XDGUSD`→DOGE/USD);
  the importer must translate to the project's `Symbol` form (the KrakenAdapter altname
  map is the reference).
- Folder convention: import `data/kraken-history/*.csv` for the main history and the
  `2026Q1/` (and future `YYYYQN/`) subfolders for quarterly extensions. The quarters
  seam cleanly onto the main files (zero overlap, verified 2026-05-30), and the
  `ohlc_bars` UNIQUE constraint makes re-import idempotent, so overlapping runs are safe.
- Scale note: the full multi-coin 1m dump is millions of rows per coin; import is a
  one-time minutes-to-low-tens-of-minutes batch job, not a daemon. Disk is cheap
  (~80 bytes/row). Decide whether to import all pairs or just the live/observe set.

**Why deferred:** v1.0 is feature-frozen; this is an importer feature. Also: nothing
in v1.0 *consumes* the deep history yet — the DB consumers that would benefit (Auditor,
Historian, regime detector) are all themselves v1.1+. Import lands naturally alongside
the first of those.

**Trigger:** whenever a v1.1 DB consumer needs deep history (most likely the Auditor or
the parked regime/Oracle track), OR opportunistically as a quiet-afternoon one-shot since
the substrate already exists and the data is already downloaded. Pairs with the
`cli/observe --backfill` ergonomics entry below (same write path) and the OHLC+TA entry
(the imported bars are exactly the TA-indicator input).

### Operator-initiated re-anchor command

**What:** a typed operator command (`re-anchor BTC/USD`) that runs
the existing "SIGINT cli/live → delete grid_state row → restart
cli/live" sequence as one atomic confirm-before-execute flow. Would
route through `pending_commands` like pause/resume/stop, preserving
the ADR-002 firewall. Web UI button + Discord chat command both
plausible surfaces.

**Why deferred:** ADR-006 decision 1 ratified "engine stays parked
when offside" as a safety property — auto re-anchor on offside is
explicitly rejected. But OPERATOR-INITIATED re-anchor (with confirm
gate) is a different policy: the human has decided the price has
genuinely moved to a new band and wants to start a fresh grid
cycle there. The v1.0 boundary leaves this as a manual three-step
procedure (SIGINT + DELETE row + restart); v1.1 could codify it as
a single operator command.

**Trigger:** the soak surfacing BTC moving out of band for
extended periods and the operator wanting a faster, less
error-prone way to re-anchor than the manual sequence.

### Heuristic experts for the other advisor roles (risk / news / arbitrator)

**What:** extend the Stage 8.5 heuristic+LLM cascade beyond the quant
role — deterministic `HeuristicAdvisorAdapter`-style experts for risk
and/or news, usable as MoE experts so a Mixture-of-Experts advisor
could run partly (or fully) without an LLM. The arbitrator already has
deterministic options (the `voting` / `weighted_confidence` aggregators).

**Why deferred (2026-05-29):** no current consumer and weak fits.
- The production advisor is `type: single` + the quant cascade (the
  `cpu-only` profile); the MoE is *not* the production path, so role
  heuristics would feed something nobody runs (YAGNI).
- A **risk** heuristic would overlap the quant heuristic's guards
  (defensive-drawdown, fee-floor *are* risk logic) and the engine's
  hard caps — redundant, and in a MoE it would correlate with quant,
  defeating the diversity the MoE exists for.
- A **news** heuristic fundamentally resists determinism: its value is
  semantic (what a headline *means*), and thresholding `sentiment_score`
  discards that. News is where an LLM earns its keep, or the expert is
  skipped. (ADR-007: news-derived recommendations never auto-apply.)
- Each role would need its own spec schema + guard logic (not a reuse of
  the quant `HeuristicSpec`) — real work for no current payoff.

**Trigger:** the MoE becoming a production path, OR an operator wanting
a fully-offline, zero-cloud deterministic MoE (e.g. an air-gapped or
cost-zero deployment where even the cascade's cloud o3 escalation is
unacceptable). The quant cascade is the template to follow when it lands.

### cli/observe --backfill + auto gap-fill — ✅ shipped in v1.1 (2026-05-25)

**Status:** ✅ Shipped in five commits 2026-05-25 (`d0f2992`,
`43afa79`, `866ce16`, `3d688d8`, `e1b916a`). 75 new tests; pylint
10/10; mypy clean.

**What shipped:**

1. `OHLCBar` domain value object with Kraken-aligned interval
   validation (1m/5m/15m/30m/1h/4h/1d/1w/15d frozenset) +
   tz-aware-UTC normalization (same contract as `Timestamp`).
2. `ExchangePort.get_ohlc(symbol, interval_minutes, since)` —
   implemented on `KrakenAdapter` via `/0/public/OHLC` (defensive
   wire-shape parsing; altname translation reused from Ticker),
   forwarded on `ShadowExchangeAdapter` (live data path), refused
   on `MockExchangeAdapter` with a clear redirect message.
3. `ohlc_bars` table in `observe.db` with
   `UNIQUE(symbol, interval_minutes, opened_at)` for INSERT-OR-IGNORE
   idempotency. New `StoragePort.save_ohlc_bars(bars) -> int` (batch
   write; returns post-dedup rowcount) + `save_price_snapshots`
   (batch executemany — sequential commits would dominate wall-clock
   on 10k+ row backfills) + `get_latest_observed_at(symbol)` (backs
   the auto-gap-fill detection).
4. `services/backfill.backfill_range()` — pure-logic orchestrator
   with Kraken-exclusive-`since` pagination, configurable rate limit
   (default 1.0s for Kraken free-tier), 10,000-iteration safety cap,
   dual-write to `ohlc_bars` (canonical) + `price_snapshots`
   (synthesized as `(opened_at, open)` per bar). On ExchangeError /
   StorageError stops cleanly and populates `BackfillResult.error`
   plus `last_opened_at` as the resume cursor.
5. `python -m wobblebot.cli.observe --backfill --since DATE
   [--until DATE] [--interval N] [--symbols A,B]` — manual mode.
   Date parser accepts bare ISO 8601 (midnight UTC) or full
   timestamps with `Z` / offset suffixes; interval parser accepts
   `1m/5m/15m/30m/1h/4h/1d/1w` or bare minute counts, validated
   against the OHLCBar set.
6. **Auto gap-fill on daemon startup.** New ObserveConfig fields
   `autogapfill_enabled` (default True), `autogapfill_threshold_minutes`
   (default 10.0), `autogapfill_max_hours` (default 24.0). Before the
   poll loop, each symbol's `now - latest_observed_at` gap is computed:
   - no history → skip silently (explicit `--backfill` required)
   - gap < threshold → skip silently (normal restart)
   - threshold..max → run bounded 1m-granularity backfill
   - gap > max → WARN; operator runs `--backfill --since X` manually

**Why it mattered:** unblocks Phase-3 advisor metrics on
recently-added symbols (the 12 observe.symbols added Day 3 only had
~5 days of history pre-backfill), gives the planned Auditor
(below) real data to chew on, and lets short outages self-heal on
restart so the operator doesn't have to remember to backfill after
every bounce.

~~**v1.1 limitation:** `price_snapshots` has no UNIQUE constraint yet,
so re-running an overlapping backfill window duplicates synthesized
snapshots.~~ ✅ **resolved 2026-05-25** in a slice-3 follow-up. The
`_migrate_price_snapshots_unique` migration in `sqlite_storage.py`
dedups any existing rows then adds the UNIQUE index; both
`save_price_snapshot` and `save_price_snapshots` use INSERT OR
IGNORE so concurrent backfill-during-daemon produces no duplicate
rows. Both halves (`ohlc_bars` + `price_snapshots`) are now fully
idempotent.

**Cross-references:** the "Proper OHLC + TA indicators" entry below
now has its data-acquisition substrate in place — implementing
TA-on-bars is the next-step v1.1 candidate that builds on top.

### cli/observe --backfill: v1.1 ergonomics + scenario catalog

**Companion to the shipped backfill feature above.** The substrate
landed 2026-05-25 but the operator-facing UX assumes a savvy
caller who computes ISO dates by hand, copies resume cursors out
of error logs, and accepts silent terminal during multi-minute
fetches. The polish items below close those gaps; the scenarios
below explain what real-world usage looks like so the priority
order makes sense.

#### Scenarios driving demand (sized at free-tier Kraken rate)

| Scenario | Driven by | Typical command | Bars | Kraken calls | Wall-clock |
|---|---|---|---|---|---|
| **New-symbol catch-up** (operator adds a coin to `observe.symbols`) | Phase 3 advisor metrics; future auditor | `--since <90d-ago> --symbols NEW` | ~130k | ~180 | ~3 min |
| **Auditor pre-flight** | v1.1 auditor replay | `--since <30-90d-ago>` (all symbols) | ~50-130k each | ~70-180 each | ~2-3 min/symbol |
| **Historian boot-up** | v1.1 Historian (months-to-years pattern recognition) | `--since <1y-ago> --interval 1h` (all symbols) | ~8.7k × 12 | ~145 total | ~3 min |
| **Regime detector calibration** | v1.1 regime classifier | `--since <6mo-ago> --interval 4h` | ~1.1k × 12 | ~24 total | ~30s |
| **Outage recovery (medium)** | Manual fall-back when auto-gap-fill's 24h cap is exceeded | `--since <when-observe-died>` | varies | varies | varies |
| **Pre-1.1 bulk seed** (one-time before any v1.1 consumer ships) | All the above | `--since <90-180d-ago>` × 12 symbols at 1m + 1h | ~1.5M | ~2200 | ~37 min |

**Why this matters:** Scenario 6 is the largest single operator event
the feature will ever absorb. At ~37 minutes of silent terminal +
hand-computed dates + no resume on transient failure, the current
UX makes that event painful. Each of the polish items below is
sized against this scenario.

#### Polish items (priority order)

| # | Item | Operator pain it removes | Implementation |
|---|---|---|---|
| 1 | `--days N` shorthand | Computing ISO date for `--since` every invocation | argparse alias resolving to `now - timedelta(days=N)`. Single helper + arg-group exclusion with `--since`. |
| 2 | `--catchup` / `--since=auto` | Operator manually queries DB for `MAX(observed_at)` per symbol then pastes it into `--since` | `_backfill_main` calls `storage.get_latest_observed_at(symbol)` per symbol to resolve `since`; same code path the slice-5 auto-gap-fill already uses. |
| 3 | `--rate-limit-seconds` | Paid Kraken users stuck at conservative free-tier 1/sec | Already a service kwarg; just plumb the CLI flag. Validate non-negative. |
| 4 | Per-cycle progress on terminal | 37-min seeds look hung; `progress_callback` exists but CLI never wires it | One INFO log per chunk: `"backfill BTC/USD: %d bars so far, cursor at %s, %.1fs elapsed"`. Render every N chunks to keep the log volume sane. |
| 5 | `--resume` (auto-pickup after error) | `--since <cursor>` must be hand-copied from prior error log | Treat `--resume --symbols A,B` as `--catchup` semantically: each symbol resolves to its own latest `opened_at` in `ohlc_bars` (not `price_snapshots` — which could be from daemon polls and lie about backfill progress). |
| 6 | `--intervals 1m,1h` | Auditor wants 1m + historian wants 1h → two separate invocations of the same command | Inner loop iterates the intervals list; per-symbol stats reported per-interval. |
| 7 | Kraken historical-horizon probe | A naive `--since 2020-01-01` silently succeeds with whatever Kraken retains, which can be much shorter than requested for fine intervals | One-line WARN if returned bar count is materially less than `(until - since) / interval`. |

#### Implementation order

**Ship 1 + 2 together as the first follow-up:** they cover scenarios
1, 5, and the most common "I want to top up" pattern at minimal
cost. Item 4 (progress logging) pairs naturally with the bulk-seed
scenario when v1.1 consumers actually need that seed run.

Items 3, 6, 7 are smaller follow-ons that can land independently as
operators request them.

#### What's NOT a gap (worth saying out loud)

- **Parallel fetch across symbols is NOT a gap.** Kraken's free-tier
  1/sec rate limit is per IP — parallelizing 12 symbols would burn
  that and produce 429s. The serial-per-symbol shape is correct
  under free-tier; only paid-tier operators benefit from parallel +
  tighter `--rate-limit`, and they're a separate audience.
- **Disk space is NOT a constraint.** 1.5M rows of OHLC at ~80
  bytes/row = ~120 MB total. Negligible vs the GB-scale storage
  operators provision.
- **The dual-write to ohlc_bars + price_snapshots is NOT redundant.**
  ohlc_bars is the canonical TA-vocabulary record; price_snapshots
  is the metrics-layer view consumers like the existing
  DataCollector already read. The synthesis means scenarios 1 and
  2 work *without* requiring the TA-indicator service to land
  first.

**Why deferred:** UX polish on a feature whose substrate ships
solid. Each item is independently small and ships when an operator
hits the pain it removes.

**Trigger:** earliest individually justified once a v1.1 consumer
(auditor / historian / regime detector) begins requiring a
bulk-seed event. Items 1 + 2 are operator-comfort and could ship
on any quiet afternoon.

### Proper OHLC + technical analysis indicators for the advisor

**Status update (2026-05-25):** the data-acquisition substrate
(OHLC bar fetch + persistence) is now in place — see the
backfill section above. This entry is now reduced to the metrics
computation layer; the adapter / storage / orchestrator wiring is
already done.

**What:** ingest Kraken's `/0/public/OHLC` candle data (intervals
1m through 21d) into a new SQLite table; extend
`services/metrics_service.py` to compute standard technical
indicators on the OHLC bars (RSI, MACD, Bollinger Bands, moving
averages of various periods, ATR, ADX, Stochastic, etc.); feed
those into `PerformanceSummary` so the advisor sees standard TA
vocabulary instead of raw tick-aggregated metrics.

**Why high value:** the advisor's prompts already speak the
language of technical analysis ("consider trend, momentum,
volatility"). But the inputs it receives today come from
`cli/observe`'s tick-by-tick price snapshots, which are not the
canonical TA input. Standard indicators are computed on OHLC
bars; that's what every TA chart in the world uses, including
Kraken's own UI Trade tab. When the LLM thinks "RSI 30 means
oversold," it expects RSI(14) on bars, not a moving-average
proxy on ticks.

Switching the advisor's input vocabulary to match the LLM's
training distribution should meaningfully improve advisor
quality without changing anything else. The grid engine doesn't
care; the advisor does.

**Implementation:** straightforward. Python has mature TA
libraries (`ta`, `pandas-ta`, `TA-Lib`) that compute 100+
indicators from OHLC dataframes. New adapter method
`get_ohlc(symbol, interval, since=...)`, new storage table,
new metrics fields, prompt-file update so the advisor knows
what data it's getting. Could be a single ~1-week slice.

**Why deferred:** the existing tick-based metrics work and
Stage 3 closed against them. Switching is feature work, v1.0
is in freeze.

**Trigger:** post-soak, when the operator wants to invest in
advisor quality. This is probably the single highest-leverage
v1.1 candidate for the advisor pipeline.

### Advisor outcome tracking — close the recommendation feedback loop

**What:** measure post-application outcome for every applied
advisor recommendation, per model + per role. Build a success-rate
ledger that the operator can read (and eventually the arbitrator
can read) when picking which advisor to trust on the next cycle.

**Why high value:** today the advisor side of the project produces
suggestions without any feedback signal on whether they worked. We
log `advisor_suggestions` rows (with model_name + role +
parameters + created_at) and `applied_suggestions` audit rows
(with applied_at, dotted-key overrides), but nothing ties those
forward to what happened next — did the post-apply window see
more fills, healthier PnL, fewer cap trips? Without that loop,
"phi4:14b vs claude-sonnet-4-6 vs gpt-4o-mini" comparisons are
vibes-based. The data to compute it is already on disk.

**What's missing:**

1. **Outcome definition.** "Success" for a grid-tuning
   recommendation needs explicit metrics. Candidates worth
   collecting in v1.1 even if the operator picks differently later:
   - **Fill cadence delta**: trades/hour over the N hours
     pre-apply vs N hours post-apply (with N tuned per cycle
     length — for 4h advise cadence, N=24h is a reasonable
     window).
   - **Realized PnL delta**: mark-to-market portfolio value
     change in the window. Same logic as the
     `_session_portfolio_value_usd` helper from Day-5's cap fix.
   - **Safety-cap trip count**: how often the engine refused
     during the window — high counts mean the recommendation
     pushed the bot into edge conditions.
   - **Operator-regret signal**: did the operator manually
     revert or re-tune within ~1 day of apply? Already
     persistable via `cli/apply` audit rows.

2. **Outcome storage.** New table
   ``recommendation_outcomes(applied_suggestion_id PK,
   window_seconds, fill_delta_pct, pnl_delta_usd,
   cap_trips_count, operator_reverted, classification,
   computed_at)``. Classification is a coarse
   ``success | neutral | regression | undetermined`` label
   derived from the metrics.

3. **Computation service.** New ``services/advisor_evaluator.py``
   that walks ``applied_suggestions`` rows whose
   ``applied_at + window_seconds < now`` and haven't been
   scored yet; computes the four metrics; persists the row.
   Run as a cli/maintenance scheduled task (daily; doesn't need
   to be fast).

4. **Operator-facing surface.** Web UI page at ``/advisor/scoreboard``
   (or extend the existing ``/advisor``) showing per-model rolling
   success rates: "phi4:14b: 14 applied, 9 success, 3 neutral,
   2 regression over last 30d; +0.03% per-cycle PnL avg." Lets
   the operator pick models based on data instead of vibes.

5. **Arbitrator feedback (deeper).** If the MoE arbitrator could
   read the rolling success rates from
   ``recommendation_outcomes`` and bias its synthesis toward
   historically-successful experts, that's a learned arbitrator.
   This is the genuinely-hard ML step and the one where causal
   confounding (market moved, was it the rec or the market?)
   matters most. Recommend deferring until passive observability
   is in place + operator has a few months of data to look at.

**Why deferred:** real ML evaluation infrastructure. Definition
of "success" needs explicit operator decision (not Claude's call
to make). v1.0 is feature-frozen; this is feature work.

**Trigger:** post-v1.0, ideally after a 30-90 day post-tag window
where enough applied recommendations exist to compute
statistically-meaningful outcomes. Earlier than that, the sample
size is too small to distinguish "this model is better" from
"this model got lucky on the few recommendations the operator
auto-applied."

**Privacy note:** the resulting per-model success table is
specific to the operator's market exposure, time period, and
configuration. NOT generalizable; should not be published as
"objective LLM leaderboard." Worth a note in the v1.1 entry's
operator-facing prose so nobody mistakes a personal scoreboard
for a public benchmark.

### LLM Historian — long-horizon pattern recognition over weeks/months/years

**What:** a new advisor role (or standalone daemon — see "open
questions" below) whose job is to read across the project's full
historical dataset and surface *patterns* that the existing
short-horizon advisor roles can't see. The existing MoE roles
(quant / risk / news / arbitrator) operate on rolling N-hour or
N-day windows tuned for "what should we do this cycle"; the
historian operates on rolling N-week or N-month windows tuned for
"what patterns has this account experienced in the macro and how
might they recur."

**Why high value:** the existing advisors are tactical — what
spacing should the grid use, what order_size_usd should we
recommend, is there a news risk worth pausing on. None of them
can answer questions like "is this a regime shift or just noise?",
"have we seen this drawdown pattern before in the operator's
history?", "what fraction of past months had positive realized
PnL vs negative, and what predicted the difference?". Those are
historian-shaped questions. Without them, the advisor stack lacks
a "remember what happened last quarter" muscle.

**What it would read (data already on disk):**

- ``price_snapshots`` from observe.db (tick-level prices going
  back to whenever cli/observe started — currently ~5 days; will
  be months by mid-soak-2 timeframe).
- ``trades`` from live.db (every fill the operator's grid has
  realized).
- ``advisor_suggestions`` + ``applied_suggestions`` from
  advise.db (every recommendation + which got applied).
- ``transfer_proposals`` + ``transfer_results`` from harvest.db
  (treasury movements over time).
- ``news_items`` from news.db (news flow over time).
- ``recommendation_outcomes`` from the evaluator entry above
  (once it ships) — the historian could correlate macro patterns
  with advisor-recommendation success rates.
- ``llm_calls`` from operator.db (LLM usage + cost over time —
  the historian could comment on whether spend trends are healthy).

**What it would output:** a new ``historian_findings`` table (or
similar) with periodic synthesis rows: ``(period_start, period_end,
finding_text, supporting_metrics_json, confidence_score,
model_name)``. Surfaced on a new web UI page (``/historian`` or
``/insights``) for the operator to read. Format would be
narrative — "in the last 90 days the grid saw 47 BUY fills vs 31
SELL fills, suggesting a net-accumulating regime; cycle yield
averaged X bps; the 14-day drawdown of 2026-04-15 had similar
volatility characteristics to 2026-03-02..."

**Open architectural questions** (the ADR's job, not this entry's):

1. **New MoE role vs standalone daemon?** Adding a "historian"
   role to the existing arbitrator-led MoE means it consumes the
   same prompt-assembly pipeline + cost-gate. Standalone daemon
   means independent scheduling + its own settings block. Lean
   toward standalone for v1.1 because the historian's cadence
   (daily or weekly) doesn't match advise's cadence (hourly to
   4-hourly).
2. **Cadence.** Daily synthesis? Weekly? Monthly? Probably weekly
   for the deep look, with optional ad-hoc invocation via a
   ``--lookback 6m`` flag.
3. **Model choice.** Cloud-only? Long-context models (Claude
   Sonnet 4.6's 1M context, or Gemini 2.5 Flash's 2M) genuinely
   help here because the operator wants to feed in a year of
   data. Local Ollama models with smaller contexts (phi4's 16k)
   would force aggressive pre-aggregation that loses signal. So
   the historian is probably mostly cloud, with cost-gate
   discipline (an ADR-014 daily cap of $X just for historian
   calls).
4. **Output write-back vs read-only.** If the historian's
   findings can feed back into shorter-horizon advisor prompts
   ("the historian noted at 2026-08-15 that this grid spacing
   has been consistently too tight in low-volatility regimes; the
   current regime looks like that one"), it becomes part of the
   advisor's working memory. That's powerful but raises the same
   causal-feedback concerns as the recommendation-outcome
   evaluator. Recommend read-only + operator-facing first;
   feedback-into-advisor as a v1.2 follow-up.

**Gap-filling is a separate concern.** The operator floated
"might need to be able to collect data to fill gaps." I'd
recommend keeping that *out* of the historian's scope. Hex
architecture says: cli/observe collects data; cli/historian
synthesizes from data. If cli/observe missed a week, the
gap-fill should be a separate ``cli/observe --backfill --since
2026-04-01`` feature that calls Kraken's OHLC endpoint to
synthesize lost price_snapshots. The historian then reads from
the filled-in dataset. Don't let the historian write directly to
observe.db — that violates the daemon-per-concern boundary.

**What's missing:**
- Cli daemon (``cli/historian``).
- New ``historian_findings`` table in operator.db (or its own
  ``historian.db``).
- Prompt file at ``config/prompts/historian.md`` with the
  long-horizon framing.
- Web UI ``/historian`` page rendering recent findings.
- ~~A separate ``cli/observe --backfill`` feature~~ ✅ **shipped
  2026-05-25** — see the backfill entry at the top of this file.

**Why deferred:** feature work. Also realistically needs
**months of data** to be useful — running a historian on 5 days
of soak data tells you nothing. Sample size matters.

**Trigger:** post-v1.0, ideally after 90+ days of soak/runtime
data has accumulated in the operator's databases. Earlier
launches are training wheels at best.

**Cross-references:** pairs naturally with the OHLC + TA
indicators entry (gives the historian standardized indicator
vocabulary) and the advisor outcome tracking entry (lets the
historian correlate macro patterns with advisor success).

### Market regime detector — explicit classifier for downstream advisors

**What:** a focused LLM role (or deterministic algorithm) whose
single job is to classify the *current* market regime into a
small typed enum: ``bull_trending | bear_trending |
choppy_range | low_volatility_drift | high_volatility_chop |
regime_transition``. Output is a structured field with a
confidence score, persisted to a new ``regime_classifications``
table and surfaced on the web UI.

**Why high value:** every existing MoE role *implicitly* infers
regime from the metrics it consumes ("price moved up over the
window" → quant infers bull-ish; "news risk elevated" → news
infers high-vol). Different roles can land on different implicit
regime assessments, leading to fragmented MoE outputs. A single
upstream classifier makes regime a shared input that all
downstream roles consume identically — quant + risk + news all
read "current regime: choppy_range, confidence 0.78" and reason
from there.

Pairs with the Historian (which *describes* historical regimes
in narrative form) and the future configurable counter-order
target (which picks ``spacing_up`` vs ``top_sell`` depending on
regime). The 90%-cycle-success aspiration **requires** explicit
regime awareness — grids win in choppy regimes, lose in
trending ones; the bot can't aim for 90% without knowing what
regime it's in.

**Implementation:** could be either:
- **LLM-shaped**: new ``services/regime_detector.py`` calling
  whichever provider is configured; prompt at
  ``config/prompts/regime_detector.md``; reads recent OHLC +
  trade patterns + volatility metrics; returns the enum + score.
- **Deterministic-shaped**: pure Python over OHLC bars using
  established regime-detection heuristics (Hidden Markov Models,
  realized volatility tiers, ADX trend strength). No LLM cost.

Lean toward **starting deterministic**: regime classification is
a well-studied problem with rule-based approaches that work
reliably. Reserve LLM augmentation for the narrative around
*why* the regime is what it is.

**Classification is NOT prediction (load-bearing for downstream
consumers).** The detector says "based on the last N candles,
this looks like regime X"; it does NOT say "the next M candles
will be Y." Any consumer that takes action based on the
classification still needs its own conviction layer: hysteresis
(require N consecutive ticks classifying the new regime before
switching shapes) to avoid whipsaw on boundary classifications,
plus per-feature gates (the confidence-driven extension entry's
outcome-evaluator gate, the regime-aware grid modes entry's
proportional spacing scaling). Consumers that treat the
classification as a forward prediction will mis-calibrate and
lose money.

**Shadow-run before any consumer wires in.** Whatever
implementation lands first should run alongside the live engine
for 60-90 days WITHOUT affecting trading. Compare classifications
against the operator's eye-of-the-needle judgments (and ideally
against well-defined post-hoc outcomes from the recommendation-
outcomes table). Only after the classifier has demonstrated
stable agreement + low whipsaw against the operator's calibration
data should it wire into ``cli/live`` decision paths.

**Why deferred:** feature work; not in v1.0 freeze. Also benefits
from OHLC+TA being in place first (regime detection on
tick-aggregated metrics is shaky; on canonical OHLC bars it's
solid). And the consumers themselves (regime-aware grid modes,
confidence-driven extension, per-regime success-rate slicing)
are also deferred — no point shipping the detector without a
validated consumer.

**Trigger:** post-v1.0, ideally paired with the OHLC+TA entry
since the inputs share the same data path. Earliest meaningful
v1.1 candidate that directly serves the 90%-success aspiration.

### Auditor / strategy + recommendation evaluation tool

**Naming note (2026-05-25):** originally called "backtester" in
the v1.1 plan; renamed to **auditor** because the tool's role is
broader than performance replay. It evaluates operator config
choices AND advisor recommendations against historical ground
truth — both are forms of audit, not just backtest. The auditor
provides the objective evaluation that the operator-llm-models
and advisor-llm-models probe data cannot.

**What:** new ``cli/auditor`` that takes the current
``settings.yml`` config + a date range + a seed balance, walks
the operator's historical ``price_snapshots`` (or Kraken OHLC
backfill), and reports: total fills, total fees paid, gross
P&L, net P&L, max drawdown, cycle completion rate. Also audits
each `AdvisorSuggestion` against the realized outcomes in its
post-apply window — closing the "did this model's
recommendations actually improve things?" loop that the
probe-based rankings explicitly cannot answer. Pure historical
replay — no orders placed, no Kraken calls except maybe a
one-shot OHLC fetch for range fill.

**Why high value:** answers "should I retune?" with data instead
of intuition. Today the operator's retune decision is gut-based;
the auditor turns it into "if I'd been running this proposed
config for the last 60 days, my cycle completion rate would have
been 73% vs the current 68%." Closes the loop between the
Historian's narrative ("the last 30 days were
high_volatility_chop") and the operator's actionable choice
("then I should widen spacing per the auditor showing 1.5%
beats 1.0% in that regime").

**The advisor-evaluation use case (added as the rename
motivator):** the probe in `tools/probe_advisor.py` measures
agreement with the maintainer's expected directions, not
objective correctness (see methodology caveat in
`docs/reference/advisor-llm-models.md`). The auditor would
replay each advisor model's actual recommendations against
realized outcomes — `llama3.1:8b`'s 14/18 probe score either
holds up (its recommendations actually improved PnL/cycles in
the audit window) or doesn't (the probe was measuring agreement
with Claude, not quality). This is the ONLY way to make a
defensible "swap phi4 → llama3.1:8b" decision.

Distinct from ``cli/shadow``:
- **cli/shadow** = live Kraken prices + synthetic ledger;
  runs forward in real time. Validates a config under *current*
  market conditions.
- **cli/auditor** = historical prices + synthetic ledger;
  runs as fast as possible. Validates a config under *past*
  market conditions AND evaluates past advisor recommendations
  against realized outcomes.

**Implementation:** reuses ``GridEngine`` + ``MockExchangeAdapter``
unchanged (they don't know the difference between "current
price" coming from a live ticker vs a historical snapshot). New
``AuditorExchangeAdapter`` (subclass of MockExchangeAdapter)
that walks an iterator of (timestamp, price) tuples instead of
honoring real-time changes. New ``cli/auditor`` plumbing reads
the iterator from ``price_snapshots`` (or fetched OHLC bars).
Output: a ``auditor_runs`` table for comparing configs over
time + a Jinja2-rendered report. Advisor-evaluation mode adds
a ``model_audits`` table that scores each advisor model's
historical recommendations against realized post-apply outcomes.

**Why deferred:** feature work. ~~Also needs OHLC+TA in place for
the gap-fill story~~ — the OHLC backfill substrate shipped
2026-05-25, so the auditor can now read either live
`price_snapshots` OR the synthesized snapshots that backfill
populates from OHLC bars. The TA-indicator layer on top of
`ohlc_bars` is still pending (see the entry near the top of this
file), but the auditor doesn't strictly need it — running
against `price_snapshots` (now historically backfillable) is
sufficient for the v1.1 first cut.

**Trigger:** post-v1.0. Operator's first use case will be
validating tweaks to the v1.0 config against the soak period's
historical data. Pairs with the Historian (which narrates) and
the Regime Detector (which classifies); together they answer
"what regime was I in, what config worked then, and what config
should I try now?"

### Regime-aware grid modes — adapt grid shape without timing the market

**Context (2026-05-23 soak day 6):** the operator raised whether
the engine should run a special "downturn strategy" in sustained
bear markets — specifically, drop the multi-SELL grid in favor of
a single concentrated BUY at the estimated bottom + a single
concentrated SELL of all held inventory at the estimated recovery
point. The variant was discussed and **rejected as proposed**
(reasoning captured below); the salvageable kernel — that the
engine's behavior in `bear_trending` regimes leaves alpha on the
table — became this entry.

**Rejected variant (all-in bottom-and-recovery swing trade):**
the proposal as raised was structurally swing trading, not grid
trading. Both legs become high-conviction directional calls:
(1) "estimate the lowest of the low point" and (2) "estimate the
likely recovery point." These are precisely the two predictions
professional traders, quant funds, and bottom-callers fail at —
not because the math is hard, but because non-stationary market
regimes mean training data doesn't generalize. The news-role LLM
running at 30min cadence is not going to nail them. Concentration
also inverts the grid's risk profile: instead of dispersing capital
across many small bets, one trade either makes the call or doesn't.
The rejected variant also has structural similarity to the
margin/futures guardrail (single-point-of-failure exposure) — same
risk shape even though no leverage is involved. **Do not relitigate
without an ADR.**

**What this entry proposes instead:** regime-aware *parameter*
shifts that preserve the grid model. The regime detector
(separately a v1.1 candidate) classifies the current market as
``bull_trending`` / ``bear_trending`` / ``high_volatility_chop`` /
``mean_reverting`` / ``unknown``. Three sub-variants worth
prototyping, all keeping the dispersion-over-conviction discipline:

1. **Asymmetric grid in `bear_trending` regime.** BUY levels at
   1.5× normal spacing below current (accumulates more on the way
   down per level); SELL levels at 0.5× normal spacing above
   (tighter profit-taking on every bounce). Symmetric mode resumes
   when regime exits `bear_trending`. No bottom-picking; just
   "lean accumulative."
2. **Spacing widening in `high_volatility_chop` regime.** Spacing
   widens from 1% to 5-8% with proportionally larger
   ``order_size_usd`` (capital exposure per level scaled up to
   keep total exposure constant). Same grid mechanics, wider net.
   Captures the bigger swings without churning small fills in
   noise.
3. **Suspended-SELL accumulation mode in `bear_trending` regime.**
   Only BUY levels are placed; existing SELL counters from prior
   cycles stay (they're profit-protection on already-bought
   inventory), but no new SELLs are placed below the price the
   inventory was bought at. Builds inventory cheap during a
   downturn; symmetric grid resumes when regime changes. Caps:
   bounded by `max_total_exposure_usd` so accumulation can't
   exceed the operator's risk budget.

**Why high value:** the operator's underlying observation is
correct — the current engine parks in sustained downturns
(ADR-006 decision 5), which is the right default for the
"pure-grid" archetype but does leave alpha on the table when
mean-reversion is reasonable to expect. Existing v1.1 entries
(**operator-initiated re-anchor**, **confidence-driven grid
extension**) cover the manual / advisor-gated paths to deploy
during downturns. This entry covers the **autonomous-but-
parameter-only** path: same grid, regime-aware shape, no
bottom-picking, no all-in concentration.

**Why ADR-002-respecting:** the regime detector outputs a
classification, not a trade. The engine consumes the
classification + reads regime-specific parameters from config
(no parameter-tuning by the LLM). Auto-apply gate is not
involved — these are engine-internal configuration shifts
keyed on a deterministic regime function, not LLM-suggested
parameter changes. The advisor still produces suggestions
about regime-aware parameters, but those flow through
``cli/apply`` like any other suggestion.

**Implementation:**
- ``services/regime_detector.py`` (see the **Market regime
  detector** entry above for its own scope): classifies the
  current regime from price history (deterministic-first per
  that entry; LLM augmentation later). Output:
  ``RegimeClassification`` value object.
- ``config/grid.py`` extended with per-regime overrides:
  ``GridConfig.regime_overrides: dict[RegimeKind,
  GridParameterDelta]`` where each delta carries spacing
  multiplier, level-count delta, order-size multiplier, and
  the boolean ``suspend_sells: bool``.
- ``services/grid_engine.py`` reads the current regime once
  per tick (cached for the tick), looks up the override, and
  passes effective parameters to ``compute_grid_levels``.
- Mode transitions are sticky-with-hysteresis: requires N
  consecutive ticks classifying the new regime before switching
  to avoid whipsaw on a boundary regime classification.

**Why deferred:** depends on the regime detector (separate v1.1
candidate); requires 60-90d of soak with the regime detector
shadow-running before any of the three modes are wired into
``cli/live``; needs a careful UX for the spacing/order-size
proportionality so total exposure stays bounded across mode
transitions. Also: an operator running this autonomously is
trusting the regime detector's classification to deploy more
capital — that trust has to be earned by the detector's
out-of-sample track record before this entry can ship without
explicit per-cycle operator approval.

**Trigger:** post-v1.0; pairs with the regime detector entry
+ the outcome evaluator entry. All three together form the
"adaptive grid" feature group.

### Confidence-driven grid extension — operator-approved buy-the-dip

**What:** when the advisor + regime detector agree confidence is
high that the current downtrend will reverse, propose extending
the grid downward via a new ``PendingCommand`` kind. Operator
approves via the Discord confirm flow. Engine extends the grid
with new BUY levels below the existing lowest level, up to an
operator-set capital cap.

**Why high value:** today the engine *parks* when offside (ADR-006
decision 5) — pure-grid archetype, no opinions about market
direction, no autonomous accumulation. That's the right default,
but it leaves mean-reversion alpha on the table when the operator
has structural conviction the market will rebound. Day-6 surfaced
exactly this scenario: BTC dropped through all 4 BUY levels
overnight, grid parked, 3 SELL counters sit far above market, no
new BUY activity even though "this is exactly when I'd want to
buy lower" was the operator's instinct. A controlled extension
pattern lets the operator buy down without abandoning safety
discipline.

**Architecture (ADR-002-respecting):**

1. **Advisor produces a ``ProposeGridExtension`` suggestion**
   when (a) regime detector says ``bear_trending`` or
   ``high_volatility_chop`` with high confidence, (b) historical
   outcome evaluator shows the advisor's prior "high confidence"
   calls in that regime had >X% success rate, (c) current price
   is offside-low (below the grid's lowest BUY level).
2. **Suggestion contains:** target lowest_level (extend grid to
   $X), N new BUY levels between existing lowest and new lowest,
   total additional USD committed.
3. **Discord embed posts via the existing
   confirm-before-execute flow** (ADR-013). Operator clicks ✅
   or ❌. NO auto-apply path. Per ADR-002 the LLM never directly
   triggers placement.
4. **On ✅, engine writes a ``PendingCommand`` row marked
   ``GridExtension``;** cli/live's normal ``WHERE
   status='approved'`` poll picks it up and runs
   ``engine.extend_grid_downward(new_lowest, n_levels)``.
5. **Hard capital cap.** New
   ``safety.max_extension_budget_usd`` config (operator-set,
   non-LLM-adjustable). Even an operator ✅ can't exceed it; the
   engine refuses + logs. Belt and suspenders — auto-apply gate
   ratifies bounds, this cap is the immovable floor.
6. **Escape valve preserved.** If the advisor was wrong and price
   keeps falling past the extended grid, the engine still parks.
   Same offside discipline, just shifted lower.

**Why NOT autonomous (no auto-apply):** advisor confidence
calibration is uncalibrated today. "High confidence" is a model
output without statistical grounding. Auto-buying on it is
faith, not data. Even after the **advisor outcome evaluator**
ships and we have 60-90 days of outcome data, the autonomy
question deserves its own follow-up ADR — not a soft default.
ADR-002's "LLM is advisory only" stays intact in v1.1.

**Why deferred:** real strategy work + new ADR + depends on the
regime detector + advisor outcome evaluator + auto-apply gate
extension. Multiple dependencies; can't ship until they stack.

**Trigger:** post-v1.0 + after the regime detector, outcome
evaluator, and ~60-90 days of outcome data have all landed. The
ADR that ratifies this should explicitly cite the outcome data
showing the advisor's confidence is statistically meaningful in
the relevant regime(s).

**Companion thought worth holding in mind:** the inverse —
*upside* grid extension when price rallies above the grid — has
the same shape with different math (sell-the-rip instead of
buy-the-dip), same ADR-respecting flow, same capital cap. Both
extensions should land together in one feature or the asymmetric
treatment will bias the operator toward accumulation.

### Configurable counter-order target (advisor-driven strategy regime)

**What:** today the engine places every counter-SELL at
`fill_price × (1 + spacing)` (per ADR-006 decision 2 — keeps
cycles base-amount-balanced). This proposal adds a configurable
counter-order target with at least two modes:

- `counter_target: spacing_up` (current default) — counter sits
  exactly one spacing above the BUY fill. Optimizes for *many
  small cycles* in choppy / range-bound markets.
- `counter_target: top_sell` — every counter-SELL lands at the
  top of the configured grid (`anchor × (1 + spacing × levels_above)`).
  Optimizes for *fewer larger cycles* during deep dips followed by
  full recoveries; effectively turns the grid into a "buy dips, sell
  rips" pattern.

A third `adaptive` mode is the natural endpoint where the *advisor*
picks the target based on observed regime (volatility, trend
direction, recent fill cadence).

**Why high value:** the two modes have genuinely different
risk/reward profiles. `spacing_up` realizes small profits
frequently and keeps USD reserves alive to buy further dips.
`top_sell` realizes much larger profits per cycle (typically 3-7%
spreads vs 1%) but only if the rebound actually reaches the top —
which in range-bound markets it may not, leaving counter-SELLs
parked indefinitely while new lower BUYs keep filling and BTC
inventory accumulates. Operator-surfaced 2026-05-22 during soak
Day 5 — the right answer depends on the market regime, which is
exactly the kind of higher-order decision the MoE advisor is
positioned to make.

**Implementation: three sequenced changes:**

1. **Engine knob.** `GridConfig` gains
   `counter_target: Literal["spacing_up", "top_sell"]` (default
   `spacing_up` — backward-compat). `grid_engine._tick`'s
   counter-placement branch reads it and computes the target
   price accordingly. The amount calculation per ADR-006
   decision 2 stays unchanged (sized to the filled BTC, not
   re-derived in USD) so cycles remain base-amount-balanced.

2. **Advisor schema field.** `AdvisorSuggestion` gains an
   optional `counter_target` field. Advisor prompts get
   guidance on when each regime applies (range-bound + chop →
   `spacing_up`; sustained downtrend with periodic deep dips
   + strong recoveries → `top_sell`).

3. **Auto-apply gate.** `services/auto_apply.py`'s gate
   (ADR-012) treats `counter_target` as a non-numeric knob —
   requires explicit operator approval, doesn't auto-apply.
   Operator sets it manually based on advisor recommendation.

**Why deferred:** real strategy work that deserves an ADR (it
changes the grid bot's behavioral profile, not just a tunable).
v1.0 is in freeze.

**Trigger:** post-soak, packaged with OHLC + TA indicators (the
prior entry) since the advisor needs proper market-regime
signals to pick the right counter target. Both land together
or `top_sell` recommendations would be poorly grounded.

### `cli/auto-tune` daemon

**What:** a long-running variant of `cli/apply --commit` that polls
the latest advisor suggestion and auto-applies it within configured
bounds, without operator approval.

**Why deferred:** ADR-012 explicitly chose operator-in-the-loop
auto-tuning. Stage 3.4b's auto-apply gate enforces the bounds;
removing the operator-trigger is a separate decision.

**Trigger:** operator demonstrates trust in the advisor over the
soak window AND has a documented use case where running checks add
no value (e.g. shadow-mode auto-tuning where there's no real-money
risk).

### Bot learning — discussion stub (full design TBD)

**What (operator-raised 2026-05-23):** "How does our bot LEARN?
Is there a way for it to do that in the future?" Real, substantive
question worth a v1.1 design pass. This entry captures the topic
so future-me can pick it up; it is **not** a fleshed-out design.

**Framing for the future discussion:**

The current advisor stack produces recommendations. The
already-queued **advisor outcome tracking** entry above measures
whether those recommendations worked. Neither closes the loop
into *changing the bot's future behavior based on what worked* —
that's the LEARNING gap.

Several distinct learning shapes worth comparing when the time
comes:

1. **Operator-mediated learning.** Outcomes inform the operator;
   operator manually tunes prompts + config. ADR-002 preserved.
   This is what we have implicitly today.
2. **Arbitrator weighting.** The MoE arbitrator reads outcome
   history and weights individual experts by their historical
   success rate. Closed loop, but ADR-002-compatible because
   each expert's output is still advisory + the arbitrator's
   recommendation still flows through `cli/apply`'s gate.
3. **Prompt evolution.** Outcomes auto-modify the prompts
   (genetic-algorithm-style). Interesting territory but ADR-002
   becomes hard to define when the prompts themselves are
   model-generated.
4. **Custom model fine-tuning.** Train a per-operator model on
   their own trade history. Significant cost + infrastructure.
5. **Reinforcement learning.** Bot directly optimizes a reward
   function. Loud ADR-002 violation; deeply dangerous in a
   trading context where the "reward" is dollars. Not recommended.

**Why deferred:** real design work, multiple ADR-touching paths,
needs the outcome-tracking infrastructure landed first to even
have learning signal. Full v1.1+ design pass.

**Trigger:** after advisor outcome tracking ships AND the operator
has 60-90 days of "did this recommendation work?" data. At that
point, deciding which of the five shapes to invest in becomes
data-informed instead of speculative.

### `cli/screener` — symbol-opportunity scanner (operator: "trufflehunt")

**What (operator-raised 2026-05-23, working name "cli/trufflehunt"):**
a daemon (or one-shot tool) that scans Kraken's tradable pairs and
ranks them by "grid-suitability" — surfaces candidates the operator
might want to add to the live grid lineup. Currently the operator
manually picks symbols (`BTC/USD`, occasionally adding others via
`grid.coins` config); there's no autonomous discovery.

**Why high value:** the bot's behavior is bounded by the symbols
configured. Some pairs are structurally better grid candidates
(stable mean-reversion, healthy volume, fees beaten by spread)
and some are structurally worse (trending alts, illiquid
pairs). Identifying the good ones manually takes hours of OHLC
plotting per symbol; a screener automates it.

**Discussion stub — full design TBD.** Key questions for a
future design pass:

- **Naming.** Operator floated "trufflehunt" as a working name;
  `cli/screener` matches the established finance-industry term
  ("stock screener"). Either fine; `cli/screener` is more
  immediately recognizable. Could also be `cli/scout` or
  `cli/prospector`.
- **Daemon vs one-shot.** Pairs eligible for grid-trading don't
  change minute-to-minute. Weekly cadence (one-shot via cron)
  is probably enough; a daemon adds heartbeat surface for low
  benefit.
- **Suitability metrics.** Candidate set:
  - Realized volatility (high enough for cycles to fire, low
    enough not to trip caps).
  - Bid-ask spread vs maker fee (per ADR-006: spread must
    cover 2× the fee + a margin for the cycle to net positive).
  - Volume / liquidity (so order-size order doesn't move the
    market).
  - Range-bound vs trending classification (overlaps with the
    regime detector entry).
  - Diversification: correlation to currently-held positions.
- **Output shape.** New table `screener_candidates(symbol,
  scored_at, suitability_score, rationale_json)` or a
  Markdown report dropped in `data/screener/`. Web UI page
  rendering the latest ranking + a button to add a candidate
  to `grid.coins` (via the established ADR-013 confirm
  flow).
- **ADR-002 boundary.** The screener recommends; it does NOT
  start trading the recommended symbols. The operator gates
  every addition.

**Why deferred:** real design work, depends on the OHLC + TA
indicators entry above (needs canonical bar data for the
suitability metrics to be meaningful), benefits from the regime
detector landing first.

**Trigger:** post-v1.0, after the OHLC + TA work lands. Earliest
candidate from "the bot already does what I told it to do" to
"the bot proactively expands what I should consider telling it
to do."
