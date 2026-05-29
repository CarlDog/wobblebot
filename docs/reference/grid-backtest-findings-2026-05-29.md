# Grid backtest findings — does the heuristic advisor stand up? (2026-05-29)

After the Stage 8.5 vol→spacing heuristic advisor shipped, the operator asked the
question the synthetic fixtures couldn't answer: **does adapting grid spacing to
volatility actually make money versus a static grid — and what spacing / re-anchor
/ regime policy actually works on real BTC history?** This is the record of that
investigation. See also [[project_advisor_philosophy]] (memory) for the design
stance that came out of it.

## Tooling + data

- **`tools/heuristic_backtest.py`** (Tier 1) — feeds historical BTC volatility
  through the *production* metric (`services.metrics.compute_volatility`) and the
  *production* `HeuristicAdvisorAdapter`, over a sliding 6h window. Reuses prod code.
- **`tools/grid_backtest.py`** (Tier 2) — simulates a micro-grid over 1m bars,
  **reusing `domain.grid` geometry** (`compute_grid_levels` / `grid_spacing` /
  `next_counter_action`) with a transparent fill / fee / balance loop, a re-anchor
  policy, and an optional stand-down trend filter.
- **Data:** Kraken `XBTUSD_1.csv` (1-minute, 2013-10 → 2025-12-31, 4.64M bars).
  XBT *is* BTC (Kraken's asset code; the adapter maps `BTC→XBT`). Cross-validated
  against `BTCUSD_1.csv`: **all 1,043,739 overlapping 2022–2023 timestamps match
  to $0.0000** — same data, XBTUSD is just the full-history headerless superset.
- **Validation:** Tier 2 reproduces the operator's observed **~$0.048/cycle** on a
  synthetic oscillation ($0.0473 measured vs $0.0477 predicted: 1% gross − 2×0.26%
  maker on a $10 order).
- **Regimes:** 2021 Q1 bull (+117%), 2022 LUNA/FTX bear (−47%), 2023 autumn (+34%),
  2025 Q4 (−22%).

## The calibration anchor (load-bearing)

The heuristic curve is, per the Stage 8.5 design doc, a **"per-tick σ → spacing %"**
curve, where the production tick = `observe_prices: 30s` and the vol lookback =
`advise.metrics_lookback_hours: 6` (= 720 snapshots, which the probe fixtures
hard-code). Kraken's finest candle is **1-minute**, so 1m-sampled vol ≈ √2 × the
per-30s-tick vol the curve expects; the tool divides 1m vol by √2 to put it on the
curve's basis. Random-walk approximation — microstructure makes it inexact — but an
independent annualized cross-check corroborates (curve floor 0.0008/30s ≈ 82%
annualized; real BTC is 24–84% across these regimes).

## Findings

### 1. The shipped curve is dead (Tier 1)
Realized per-30s BTC vol (regime medians 0.0002–0.0008) sits at or below the curve's
floor (0.0008); only **2–52%** of windows even reach the modeled domain
[0.0008, 0.014]. So the curve flat-clamps to its minimum (0.65%) and recommends
TIGHTEN in every regime — its 8-point interpolation never engages. The 36/24
fixtures validated the curve's *logic* on vol magnitudes BTC rarely produces.

| Regime | vol_tick median | % in curve domain | ideal spacing (med→max) | direction |
|---|---|---|---|---|
| 2021 bull | 0.00082 | 52% | 0.65→0.83% | all tighten |
| 2022 bear | 0.00061 | 27% | 0.65→0.85% | all tighten |
| 2023 range | 0.00023 | 2% | 0.65→0.71% | all tighten |
| 2025 recent | 0.00035 | 4% | 0.65→0.73% | all tighten |

### 2. Recalibration engages but churns
Re-anchoring the curve's vol axis to the observed distribution (floor 0.0008→0.00015,
top 0.014→0.0035; spacing values unchanged) makes it track vol: in-domain
2–52% → 68–100%, ideal spacing varies 0.65–2.42% (widen in volatile, tighten in
calm). **But churn jumped 1–14% → 27–41%** — engagement and stability trade off
directly. Candidate at `data/heuristic-recalibrated-candidate.yml` (gitignored, NOT
deployed).

### 3. Spacing sweep — wider wins, long-bias bleeds (Tier 2)

Net P&L by fixed spacing, re-anchor on (margin = 1 spacing):

| Regime (move) | 0.65% | 1.0% | 1.5% | 2.0% | best | recal-heuristic said |
|---|---|---|---|---|---|---|
| 2021 bull (+117%) | **−74%** | −7% | **+31%** | +31% | 1.5% | ~1.36% ✓ |
| 2022 bear (−47%) | −64% | −47% | −36% | **−34%** | 2.0% | ~1.23% ✗ |
| 2023 range (+34%) | +0.5% | +4.8% | +6.7% | **+6.8%** | 2.0% | ~0.65–0.78% ✗✗ |
| 2025 recent (−22%) | −31% | −17% | −14% | **−14%** | 2.0% | ~0.77% ✗✗ |

- **Wider spacing (1.5–2.0%) is best or least-bad in every regime; 0.65% (the dead
  curve's output) is WORST everywhere.**
- The recalibrated heuristic was right only in 2021 (and half-right in 2022); in the
  low-vol-but-trending 2023/2025 it recommended the *tightest* spacing while the data
  wanted the *widest*. **Volatility is the wrong signal — trend dominates.**
- The long-biased grid **loses at every spacing** in sustained downtrends (2022, 2025).

### 4. Re-anchor sweep — parking beats re-anchoring
At 1.5% spacing, sweeping the re-anchor margin (0.5 / 1 / 2 / 5 spacings / never):
**"never re-anchor" (park when offside — production's ADR-006 default) is best or
tied-best in every regime.** The −74% disaster at 0.65% was a *tight-spacing ×
frequent-re-anchor* interaction (432 re-anchors buying into a parabola), not a
general fragility — at 1.5% the grid is robust to re-anchor policy.

### 5. Trend filter — pausing doesn't defend
A "stand down (cancel + hold) on a confirmed downtrend (3-day drift < −5%)" filter,
at 1.5% spacing:

| Regime | filter OFF | filter ON | down-time | verdict |
|---|---|---|---|---|
| 2021 bull | +$37.6 | +$30.5 | 21.5% | ON worse (false triggers) |
| 2022 bear | −$42.8 | −$37.5 | 38.9% | ON better |
| 2023 uptrend | +$8.0 | +$8.0 | 0% | identical (never fired ✓) |
| 2025 down | −$16.7 | −$20.1 | 29.3% | ON worse |

**Net WORSE.** It correctly never fires in the clean uptrend and helps the steep
2022 bear, but hurts the volatile bull (false positives) and the choppy decline.
Reason: **pausing ≠ defending** — standing down holds inventory (still
mark-to-market bleeds) *and* forgoes the bounce-capture that offsets losses in a
choppy decline. Real defense = **de-risk to cash**, which trades downtrend
protection for false-positive whipsaw risk — the high-stakes, ambiguous call that
belongs to the operator (informed by projected loss), not an auto-trigger.

## Conclusions + recommendations

1. **Live config:** consider widening the grid from 1% toward **~1.5%**; keep
   **park-when-offside** (don't auto-re-anchor — it's already the ADR-006 default).
2. **Advisor:** the **vol→spacing premise is not validated**. Pivot toward
   **trend/regime → suggested posture** (defensive in downtrends, let-it-ride in
   ranges/uptrends), **operator-confirmed, reasoning shown**. The cascade
   *architecture* (cheap-first → escalate → fallback) and the LLM's broader
   reasoning are NOT rejected — only the vol-curve-as-P&L-driver.
3. **Auto-apply (graduated gate):** bounded knobs (spacing within
   `max_*_change_percentage`) can auto-apply; high-stakes / ambiguous calls
   (de-risk-to-cash) escalate to the operator. See [[project_advisor_philosophy]].
4. **The real risk is the long-bias downtrend bleed.** No spacing / re-anchor /
   pause knob fixes it; defending it (de-risk-to-cash) is a careful, human-confirmed
   decision worth its own study.

## Caveats

**SCOPE: BTC/XBT ONLY — generalization is untested and the conclusions may not
hold for other assets.** A grid is fundamentally a *chop / mean-reversion*
strategy, and BTC is comparatively *trendy* — so BTC may be near the worst case
for it. The headline findings here ("trend dominates," "long-bias bleeds in
downtrends," "the vol curve is dead") could be BTC-specific: a range-bound choppy
alt might be where the grid — and even a (re-anchored) vol→spacing heuristic, if
that asset's vol lands in the curve's domain — actually shines; memecoins'
catastrophic crashes could make the bleed worse. **None of this is validated on a
second asset.** The dump has every Kraken pair; a multi-coin sweep is the next
step before treating any of this as a strategy-wide truth or building a new
algorithm. **No new algorithm or heuristic has been built** — this is diagnosis +
one failed prototype (the trend-pause filter), not a replacement.

This is also a **model**, not the production engine — it reuses `domain.grid`
geometry + reproduces the validated cycle economics, but assumes: maker-only fills,
no slippage, no partial fills, a seeded two-sided start, a specific re-anchor
mechanic, and only four regime windows (all BTC). **Directional findings are robust
within BTC** (consistent across both sweeps and all four BTC regimes); **absolute
magnitudes are model- and policy-dependent** (the large tight-spacing losses are
amplified by the mechanical re-anchor rule).

## Reproduce

```bash
CSV=data/kraken-history/XBTUSD_1.csv
# Tier 1 (curve calibration vs real vol):
python -m tools.heuristic_backtest --csv $CSV --start 2022-05-01 --end 2022-07-01 --label "2022 bear"
# Tier 2 (spacing sweep):
python -m tools.grid_backtest --csv $CSV --start 2022-05-01 --end 2022-07-01 --label "2022 bear"
# Tier 2 with the stand-down trend filter:
python -m tools.grid_backtest --csv $CSV --start 2022-05-01 --end 2022-07-01 --trend-filter
```
