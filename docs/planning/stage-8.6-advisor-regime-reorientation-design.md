# Stage 8.6 — Advisor Regime Reorientation + Grid Widen (pre-soak)

**Status:** design DRAFT 2026-05-29, awaiting operator ratification. Slots into
pre-soak (before the v1.0 gating-soak restart ~2026-06-01) so the month-long soak
forward-validates **both** the wider grid and the regime-aware advisor together
(operator decision 2026-05-29: "both land before soak").

This doc is self-contained so the work survives a context compaction. It captures
the *why* (the backtest verdict + its adversarial confirmation), the design, the
slice plan, and the open decisions.

---

## 1. Why — what the backtest + flip-the-script proved

The Stage 8.5 cascade shipped a vol→spacing advisor. The grid backtest
(`docs/reference/grid-backtest-findings-2026-05-29.md`) + a two-run blind
adversarial red-team (§10 of that doc) then established, with the verdict surviving
every attack:

1. **The grid underperforms a passive 50/50 hold on raw return** in ~75–90% of
   un-cherry-picked conditions; its edge is confined to genuine (path-aware) chop,
   which is rare and only ~break-even.
2. **Trend/regime — not volatility — determines win-vs-lose.** The grid's edge
   correlates *negatively* with movement of every kind (net −0.97, intra-range
   −0.94, vol −0.75). The dominant risk is the long-biased grid bleeding in
   sustained moves (it sells out in uptrends, catches falling knives in downtrends).
3. **The shipped vol→spacing heuristic is mis-calibrated AND mis-oriented.** Its
   curve domain (`vol ≥ 0.0008`) sits above where real BTC vol lives (median
   0.00024/tick), so 99% of windows flat-clamp to the 0.65% floor → it recommends
   **TIGHTEN** every tick. Empirically verified 2026-05-29: against a 3% BTC grid it
   said *tighten to 0.65%* on 2898/2912 windows. 0.65% is the single worst setting
   (catastrophic −74% at the full-cycle level).
4. **Mechanical auto-de-risk fails** — no trigger setting beat the no-defense grid
   over rolling windows; de-risk must be operator-judged.

**The consequence that drives this stage:** if we widen the live grid (the §1 fix)
without changing the advisor, the advisor will fight the grid — recommending the
exact worst setting. And it *cannot* make the correct call because the variable that
decides the outcome — **trend/regime — is not in the data it sees.** `PerformanceSummary`
carries vol, drawdown (downside-only), flatness, cycles, win-rate, current grid,
news — but **no directional signal.** In an uptrend (also a grid loser) the advisor
sees drawdown ≈ 0 and reads "all clear." This is a *missing input*, not a tuning bug.

## 2. What — the two-lever reorientation

Ratifies the [[project_advisor_philosophy]] two-lever model into code:

- **Lever 1 — Volatility → base spacing (slow CALIBRATION).** Demote vol→spacing
  from a fast per-tick tuner to a per-symbol / per-regime *base* spacing. Recalibrate
  the curve to real BTC vol so its resting recommendation is ~3% (the backtest
  optimum), not 0.65%.
- **Lever 2 — Trend/regime → posture (the DEFENSE / win-vs-lose lever).** Add a
  first-class **regime classifier** as a metric; the advisor reads it and suggests a
  **posture** (harvest / cautious / defensive) with **projected downside** — but
  posture is **advisory-only, never auto-applied** (operator decisions 2026-05-29).

### 2a. Regime classifier (operator decision: first-class, labeled)

New pure-metric in `services/metrics.py` (zero I/O, like its siblings):

```
compute_regime(prices) -> RegimeSignal
```
where `RegimeSignal` (new value object) carries:
- `label: "uptrend" | "chop" | "downtrend"` — the classified regime.
- `confidence: float [0,1]` — strength of the classification.
- `drift_pct: float` — net % move over the regime lookback (signed).
- `slope: float` — normalized regression slope (signed, scale-free).

Method (to validate against the backtest before freezing the thresholds): linear
regression slope over a multi-day window + net drift, classified by configurable
thresholds. Chop = |drift| below a band AND low slope; up/down = signed slope beyond
the band. **The classifier's thresholds are DATA** (a new block in `quant.yml` or a
sibling spec), **the algorithm is CODE** — mirrors the heuristic's data/code split.

Computed once in `metrics.py`, consumed by **both** cascade halves (heuristic guards
+ LLM prompt) so they reason on the same signal.

### 2b. `PerformanceSummary` gets the regime + a symmetric risk measure

Add to `ports/advisor.py`:
- `regime: RegimeSignal | None` — the classified regime (None when the window is too
  thin to classify, like the existing no-grid case).
- Consider a **symmetric excursion** measure alongside `max_drawdown` so the advisor
  can see uptrend exposure, not just downside. (Open: reuse `flatness`/`drift` vs a
  new field — see §6.)

`summary_builder.py` populates it from the price window it already loads. `cli/advise`
needs no new data source — it's derived from the same observe-DB prices.

### 2c. Posture output (operator decision: advisory-only + projected loss)

The advisor's recommendation gains a **posture** dimension that is *informational*:
- `posture: "harvest" | "cautious" | "defensive"` + a `projected_downside` estimate
  (what a continued move of the detected regime would cost the current inventory).
- **Posture NEVER auto-applies.** Only bounded `spacing_percentage` (within the
  existing `auto_apply.max_*` caps) stays auto-applicable. This matches the backtest
  (de-risk must be operator-judged) and generalizes the re-anchor-banner-with-
  projected-loss design (v1.1 operator-ux entry). Mechanism: posture rides in
  `AdvisorRecommendation` (rationale + a posture field) and is surfaced to the
  operator (Discord/web), but the auto-apply gate ignores it — same firewall shape
  as ADR-007's news-never-auto-applies.

### 2d. Lookback coupling fix

At 3% spacing a 6h metrics window completes ~0 cycles, which silently breaks the
cycle-based guards (`dont_fix_working` needs ≥8 cycles; `directional_runaway` fires
on 0 cycles → becomes the default path). Fix: widen `metrics_lookback_hours` (18–24h+)
**and/or** re-size the guard thresholds to a 3% cadence. Validate the new lookback
against the heuristic backtest so the guards engage as intended.

## 3. Grid change (the §1 fix this enables)

- `grid.default.spacing_percentage` 1.0 → **3.0** (BTC is the only live symbol;
  exposure unchanged at $60 = 3+3 × $10). Open: 3% vs 2% — see §6.
- Sync `settings.example.yml` ↔ deploy-master `settings.yml`. Operator cut/pastes to
  the NAS bind-mount (per the deployment-split rule; Claude does not deploy).

## 4. Surfaces touched (full coupling map)

| Layer | File | Change |
|---|---|---|
| domain | `domain/value_objects.py` | new `RegimeSignal` value object |
| services | `services/metrics.py` | new `compute_regime` |
| services | `services/summary_builder.py` | populate `regime` (+ symmetric risk) |
| ports | `ports/advisor.py` | `PerformanceSummary.regime`; posture on recommendation |
| adapters | `adapters/heuristic_advisor.py` | regime-aware guards + posture; demote vol→spacing to base calibration |
| config | `config/heuristic/quant.yml` | recalibrate curve to real BTC vol; regime thresholds |
| config | `config/prompts/quant.md` | rewrite for two-lever reasoning + posture output |
| config | `config/settings*.yml` | grid 1%→3%; metrics_lookback widen; both files in sync |
| tools | `tools/probe_advisor.py` | rebuild fixture battery for regime + posture |
| tools | `tools/heuristic_backtest.py` | (already exists) validate recalibrated curve + lookback |
| tests | `tests/...` | new metric, classifier, adapter, summary, schema-drift, fixtures |
| docs | this doc, roadmap, CHANGELOG, decisions.md (ADR-019/020) | — |

**Layer discipline:** `RegimeSignal` is a domain value object; `compute_regime` is
pure (no adapter imports); the classifier is consumed via the port DTO. No layer
violation. The auto-apply firewall (posture ignored by the gate) preserves ADR-002.

## 5. ADRs this stage needs

- **ADR-019 — Advisor purpose: regime reader + guardrail, not vol-tuner.** Records
  the shift in what the advisor is FOR (the [[project_advisor_philosophy]] stance),
  why (the backtest verdict), and what it supersedes/refines (the Stage 8.5
  vol→spacing framing — not rejected, demoted to base calibration). Names the
  posture-advisory-only invariant as a refinement of ADR-002/ADR-007.
- **ADR-020 — Regime classification as a first-class metric.** Records the
  first-class-labeled-classifier choice (over "raw signal, advisor interprets"), the
  data/code split, and that both cascade halves consume it.

## 5a. Out-of-sample check — 2026 Q1 (added 2026-05-29)

An out-of-sample quarter (`data/kraken-history/2026Q1/`, 2026-01-01 → 03-31, ~99%
coverage on BTC/ETH/SOL/XRP, 88% DOGE; seams cleanly onto the main files with zero
overlap) postdates the entire backtest + both red-teams, so it's a true forward check.
**It was a broad downturn — every coin fell 21–33%** (XBT −22%, ETH −29%, SOL −33%,
XRP −27%, DOGE −21%): exactly the sustained-downtrend regime the verdict says the
long-biased grid loses. The verdict reproduced cleanly on never-seen data:

- **"Wider beats tighter" held on all 5 coins.** Net P&L by spacing (full-quarter):
  | Coin | 1% | 2% | 3% |
  |---|---|---|---|
  | XBT | −20.5% | −13.5% | **−11.6%** |
  | ETH | −34.5% | **−16.7%** | −17.5% |
  | SOL | −53.5% | −26.3% | **−18.9%** |
  | XRP | −44.2% | −25.5% | **−11.5%** |
  | DOGE | −46.7% | −16.8% | **−3.8%** |
  1% (the live setting) was catastrophic everywhere; **3% won or tied on 4 of 5
  coins** (only ETH marginally preferred 2%).
- **The grid lost to a 50/50 hold OOS** (BTC rolling: grid −14% to −17% vs hold −11%),
  re-confirming C1 on fresh data.
- **More fills = more loss in a downtrend.** 2% takes ~120 cycles/quarter vs 3%'s
  ~70; each extra cycle is a falling-knife catch that *adds* to the loss. This
  reframes the "2% gives more soak data" argument — the extra data is loss-amplifying
  churn, not signal.

Implication for Stage 8.6: this *strengthens* the case — the grid bled in Q1
precisely because it had no "we're in a downtrend" signal, which is the regime
classifier this stage adds.

## 6. Open decisions (carry into ratification)

1. **Spacing: 3% or 2%?** Earlier operator lean was 2% (more soak data); the 2026 Q1
   OOS check (§5a) shifts the evidence to **3%** — it won/tied 4 of 5 coins, and 2%'s
   "more fills" advantage is loss-amplifying churn in a downtrend. **Binding call
   deferred to Slice E**, run against the *recalibrated curve + new lookback* on the
   full history + the 2026 Q1 quarter; current lean **3%**.
2. **Symmetric risk measure:** new `PerformanceSummary` field vs. reuse drift/flatness
   the regime signal already carries. Lean: let `RegimeSignal.drift_pct` carry the
   directional exposure and avoid a new field (YAGNI).
3. **Regime lookback window:** the classifier needs a multi-day window; the metrics
   window is 6h. Either lengthen the advise metrics window globally or give the
   classifier its own (longer) lookback. Lean: separate classifier lookback (the
   regime signal is inherently slower than the vol signal).
4. **Posture surfacing:** Discord + web both, or log-only for the soak? Lean: persist
   in `advisor_suggestions` (already happens) + surface in the existing advise log;
   Discord/web posture display can be a v1.1 polish if the soak shows it's useful.

## 7. Slice plan (commit-per-slice, tests+lint green each)

- **A — Regime metric (domain + services).** `RegimeSignal` + `compute_regime` +
  tests. Validate thresholds against `heuristic_backtest.py` on real history. No
  wiring yet. *(ADR-020 lands with this slice.)*
- **B — Port + summary wiring.** `PerformanceSummary.regime`; `summary_builder`
  populates it; schema/DTO tests.
- **C — Heuristic reorientation.** Recalibrate curve; regime-aware guards; posture
  output; demote vol→spacing to base calibration. Rebuild fixture battery; reproduce
  a fresh blind-validated battery. *(ADR-019 lands with this slice.)*
- **D — Prompt rewrite.** `quant.md` for two-lever reasoning + posture; re-validate
  vs the chosen LLM (o3) on the new held-out battery (cost-gated, isolated probe db).
- **E — Grid widen + config sync + lookback fix.** `settings*.yml` 1%→3%,
  metrics_lookback; both files in sync; schema-drift green.
- **F — Roadmap/CHANGELOG/CLAUDE.md + phase-end checks.** Stage 8.6 receipt; operator
  NAS deploy instructions (grid + curve + lookback to the bind-mount).

## 8. What this is NOT

- Not a new trading algorithm — the engine (`domain/grid.py`) is unchanged; the grid
  stays park-when-offside (ADR-006). This is calibration + giving the advisor the
  variable it was missing.
- Not auto-de-risk — posture is advisory; the backtest forbids mechanical auto-fire.
- Not a claim the grid becomes an alpha engine — it stays a ~$100 learning/discipline
  project; the reorientation makes the advisor *honest about regime* and the grid
  *less-bad*, with a month of soak to validate the direction forward.
