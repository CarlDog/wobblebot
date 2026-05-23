# Adaptive grid — making the grid smarter

*Entries here turn the static-grid engine into a regime-aware system: classify the market, evaluate advisor recommendations against outcomes, extend the grid under operator/advisor agreement, replay historical configs. All preserve ADR-002 (LLM advisory only) and ADR-006 (engine parks honestly when offside).*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

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

### Proper OHLC + technical analysis indicators for the advisor

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
- A separate ``cli/observe --backfill`` feature (companion, not
  part of this entry — would be a separate v1.1 item).

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

### Backtester / strategy replay tool

**What:** new ``cli/backtest`` (or ``tools/backtest.py``) that
takes the current ``settings.yml`` config + a date range + a
seed balance, walks the operator's historical ``price_snapshots``
(or Kraken OHLC backfill), and reports: total fills, total fees
paid, gross P&L, net P&L, max drawdown, cycle completion rate.
Pure historical replay — no orders placed, no Kraken calls except
maybe a one-shot OHLC fetch for range fill.

**Why high value:** answers "should I retune?" with data instead
of intuition. Today the operator's retune decision is gut-based;
backtester turns it into "if I'd been running this proposed
config for the last 60 days, my cycle completion rate would have
been 73% vs the current 68%." Closes the loop between the
Historian's narrative ("the last 30 days were
high_volatility_chop") and the operator's actionable choice
("then I should widen spacing per the backtester showing 1.5%
beats 1.0% in that regime").

Distinct from ``cli/shadow``:
- **cli/shadow** = live Kraken prices + synthetic ledger;
  runs forward in real time. Validates a config under *current*
  market conditions.
- **cli/backtest** = historical prices + synthetic ledger;
  runs as fast as possible. Validates a config under *past*
  market conditions.

**Implementation:** reuses ``GridEngine`` + ``MockExchangeAdapter``
unchanged (they don't know the difference between "current
price" coming from a live ticker vs a historical snapshot). New
``BacktestExchangeAdapter`` (subclass of MockExchangeAdapter)
that walks an iterator of (timestamp, price) tuples instead of
honoring real-time changes. New ``cli/backtest`` plumbing reads
the iterator from ``price_snapshots`` (or fetched OHLC bars).
Output: a ``backtest_runs`` table for comparing configs over
time + a Jinja2-rendered report.

**Why deferred:** feature work. Also needs OHLC+TA in place for
the gap-fill story (price_snapshots only go back as far as
cli/observe has run; backfilling pre-soak periods needs the
OHLC endpoint).

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
