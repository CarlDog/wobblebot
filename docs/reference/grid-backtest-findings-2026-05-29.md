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

### 6. Multi-coin generalization (2024 up year + 2025 down year)
The findings are NOT BTC-specific. Best-spacing (2.0% — the widest in this sweep)
net P&L across the 5 most-popular coins:

| Coin | 2024 (up) | 2025 (down) | underlying 2024 / 2025 |
|---|---|---|---|
| BTC | +22% | +7% | +121% / −5% |
| ETH | +10% | −35% | +46% / −11% |
| SOL | +45% | −34% | +85% / −34% |
| XRP | +37% | −29% | ~+100% / ~flat→down |
| DOGE | +15% | −80% | ~+300% / −63% |

Wider-beats-tighter and long-bias-bleeds-in-downtrends hold across *every* coin both
years (0.65% catastrophic everywhere). The grid is a **direction-follower scaled by
volatility**: up year → positive on all; down year → negative on all; high-vol alts
swing biggest both ways. BTC was the most robust (only coin positive both years — it
fell least in 2025). Caveat: the grid captured only a fraction of the bull moves and
on most alts lost *more* than buy-and-hold in the crash (exposure isn't
apples-to-apples — the grid is ~half cash — but directionally: limited upside,
full-or-worse downside on *trending* assets).

### 7. Chop windows + wide spacing — per-symbol spacing, and a REVISED vol-curve verdict
Each coin's flattest 90-day window (net drift ~0%, intra-range 44–65%), swept to 5%:

| Coin | chop window | best spacing | best net |
|---|---|---|---|
| BTC | 2020 COVID-V | 5.0% | −1.6% |
| ETH | 2016 sideways | 4.0% | +8.3% |
| SOL | 2025 H2 | 3.0% | +12.4% |
| XRP | 2024–25 | 3.0% | +19.3% |
| DOGE | 2024 | 3.0% | +28.1% |

1. **The grid IS profitable in range-bound conditions** — alts net +8% to +28% at
   the right spacing, and the high-vol alts (DOGE, XRP, SOL) harvest the MOST. The
   "choppy alts shine" hypothesis, rejected on the 2025 crash year (§6), is
   **resurrected for genuine chop** — that rejection was confounded by 2025 being a
   *down* year, not a *flat* one. (BTC's lone loser is its window — a violent COVID
   V, not gentle sideways.)
2. **Optimal spacing diverges by asset/regime and is WIDER than the 2% cap — 3% to
   5%.** Validates **per-symbol spacing** (operator-raised 2026-05-29): the right
   spacing scales with the asset's current volatility. **The live grid (1%) and the
   heuristic's 0.65% floor are both far too tight.**
3. **REVISION to Finding 1 — the vol→spacing relationship is NOT dead.** It was
   *mis-calibrated* (band 0.65–2.70% too tight; real optima 3–5%) and *mis-applied*
   (as a fast per-tick time-series tuner). Reconceived as **per-symbol / per-regime
   base-spacing calibration** ("more volatile asset → wider grid"), the relationship
   holds. The curve was calibrated to the wrong band and used in the wrong role —
   not fundamentally wrong.

### 8. Downtrend defense — de-risk-to-CASH works (where pause didn't)
The §5 pause filter held the bleeding inventory and didn't help. A **sell-to-cash**
defense (on the same confirmed-downtrend signal: cancel all + market-sell inventory
to USD at the taker fee, re-enter on recovery) is a different story. none vs pause
vs cash at 1.5%:

| Regime | none | pause | cash |
|---|---|---|---|
| BTC 2021 bull (+117%) | +31% | +25% | **+3%** |
| BTC 2022 bear (−47%) | −36% | −31% | **−2.6%** |
| BTC 2025 down (−22%) | −14% | −17% | **−1.7%** |
| DOGE 2025 crash (−63%) | −83% | −70% | **−49%** |

**Cash de-risk turns the crash regimes from disasters into near-break-even** (2022
−36%→−2.6%; 2025 −14%→−1.7%) by actually going to cash instead of holding through
the drop. This **revises §6 / the old "downtrend bleed is unfixable" line — it IS
fixable.** The premium: in the 2021 bull it gave up nearly all the upside
(+31%→+3%) — the naive 3-day/−5% signal false-triggered on pullbacks (21.5% of the
time), sold dips, rebought higher. So cash de-risk converts "full downside / partial
upside" into "**capped downside / capped upside**" — classic insurance.

**Implication:** the defense *mechanism* works; the binding constraint is the trend
*signal* (real downtrend vs pullback — inherently probabilistic). So it must be
**operator-confirmed, not auto-fired** (auto-firing surrenders bull upside on every
pullback) — exactly the graduated-auto-apply + projected-loss-banner design — and
the signal should be **per-symbol** (DOGE's extreme vol limited the naive trigger;
its re-entries whipsawed within the crash). Better / per-symbol / LLM-grade trend
detection is the lever that makes cash de-risk strictly better.

### 9. Robustness battery (Tier 2/3) — slippage, rolling generality, signal-robustness, sensitivity
Run to harden (or break) the findings before the flip-the-script pass.

**Slippage (realism).** Per-fill friction + extra slippage on the cash market sell.
Findings SURVIVE + strengthen: tight spacing is punished harder (more fills) so
"wider beats tighter" reinforces; cash de-risk survives its market-sell slippage
(BTC 2022 bear −2.6%→−3.6%).

**Rolling generality — the unbiased OOS test.** Roll 180-day windows (step 30d)
across each coin's full 2021–2025 history (NO window cherry-picking), grid @3% vs a
passive 50/50 hold, with realistic slippage:

| Coin | grid beats 50/50-hold | in chop | chop share | mechanical cash de-risk (median) |
|---|---|---|---|---|
| BTC | 38% | 82% | 20% | −1.7% |
| ETH | 39% | 50% | 12% | −1.6% |
| SOL | 22% | 25% | 10% | −4.5% |
| XRP | 44% | 100% | 23% | 0.0% |
| DOGE | 32% | 100% | 8% | −7% |

**The grid beats a passive 50/50 hold in only 22–44% of windows — never >50%, on any
coin.** Its edge is real but confined to chop (50–100% there), which is only 8–23% of
the time. Median-grid slightly beats median-hold everywhere — the short-volatility
signature (small frequent edge, fat-tail trend losses, sub-50% win rate). XRP is the
best grid candidate (most chop, 23%); SOL the worst (trended hard).

**Signal-robustness.** No de-risk trigger (3-day/−5% → 21-day/−20%) makes mechanical
cash de-risk beat the no-defense grid over rolling windows: loose triggers bleed via
false positives (median −1.7 to −3.5%), strict ones fire late + rarely (median +5.6%,
still below the +10.1% no-defense). **De-risk cannot be a mechanical auto-trigger** —
it needs judgment (a human, or a far smarter regime detector).

**Sensitivity.** Grid size 5+5 vs 3+3: modestly better (44% vs 38% beats hold, median
+11.8% vs +10.1%) but same direction. Conclusions are robust to grid size + slippage.

## Conclusions + recommendations

**Bottom line (after the full Tier 2/3 battery): the grid underperforms a passive
50/50 hold over realistic, un-cherry-picked conditions** — it beats hold in only
22–44% of rolling windows, never >50%, on all 5 coins. Its genuine edge is confined
to *chop* (~10–23% of the time), and the only mechanical "fix" (cash de-risk) doesn't
beat just-running the grid at any trigger. **The grid is a short-volatility bet whose
viability hinges on detecting/avoiding regimes — which a mechanical signal can't do.**
The recommendations below are conditional on that.

1. **Live config — the grid is far too tight.** Chop-window optima are **3–5%**,
   scaling with each asset's volatility; the live 1% (and the heuristic's 0.65%
   floor) leave the edge on the table and bleed fees/whipsaw. **Widen substantially
   and per-symbol** (high-vol alts wider than BTC). Keep park-when-offside (don't
   auto-re-anchor — already the ADR-006 default).
2. **Advisor — two levers, two roles.** *Volatility* sets the right **spacing**
   (per-symbol / per-regime base-spacing calibration — the vol→spacing relationship
   *correctly applied*, NOT a fast per-tick tuner); *trend/regime* determines
   **win-vs-lose** (the defense question). Pivot the *dynamic* advisor to
   trend/regime → posture (defensive in downtrends, harvest in chop/up),
   operator-confirmed + reasoning shown; use vol→spacing for per-symbol calibration.
   The cascade architecture + the LLM's broader reasoning are NOT rejected.
3. **Auto-apply (graduated gate):** bounded knobs (spacing within
   `max_*_change_percentage`) can auto-apply; high-stakes / ambiguous calls
   (de-risk-to-cash) escalate to the operator. See [[project_advisor_philosophy]].
4. **Downtrend defense is real but cannot be mechanical** (§8–9). De-risk-to-cash
   rescued the cherry-picked 2022 crash (−36%→−2.6%), but over rolling windows NO
   trigger setting beats the no-defense grid (false positives in pullbacks bleed the
   median). So de-risk must be **operator-confirmed / judgment-based** (informed by
   the projected-loss banner), **per-symbol**, NOT auto-fired. The long-bias bleed is
   only fixable by *correctly identifying* a sustained downtrend — the hard,
   probabilistic part, and the advisor's highest-value job.

## Caveats

**SCOPE: hardened across 5 coins (BTC/ETH/SOL/XRP/DOGE), a full 2021–2025 cycle,
37–55 rolling un-cherry-picked windows per coin, realistic slippage,
signal-robustness, and grid-size sensitivity (§9).** Remaining limits: it's still one
~4.5-year cycle (2021–2025 was trend-heavy, which favors hold); the model assumes
maker-fill-on-touch, a seeded two-sided start, and a specific re-anchor mechanic; and
the adversarial **flip-the-script** pass (per
[[feedback_flip_the_script_adversarial_reeval]]) is the next step. Several early
conclusions were **revised** along the way: "choppy alts shine" (rejected on the 2025
crash year → resurrected for genuine chop); "the vol curve is dead" (revised — wrong
band/role, not wrong relationship); "cash de-risk works" (revised — works in
cherry-picked crashes, NOT mechanically over the full window distribution). **No new
trading algorithm has been built** — this is diagnosis. Directional findings are
robust (consistent across coins, windows, slippage, grid size); absolute magnitudes
remain model- and policy-dependent.

## Reproduce

```bash
CSV=data/kraken-history/XBTUSD_1.csv
# Tier 1 (curve calibration vs real vol):
python -m tools.heuristic_backtest --csv $CSV --start 2022-05-01 --end 2022-07-01 --label "2022 bear"
# Tier 2 (spacing sweep):
python -m tools.grid_backtest --csv $CSV --start 2022-05-01 --end 2022-07-01 --label "2022 bear"
# Downtrend defense (pause / cash) + realistic slippage:
python -m tools.grid_backtest --csv $CSV --start 2022-05-01 --end 2022-07-01 --defense cash --slippage-bps 5 --derisk-slippage-bps 30
# Rolling grid-vs-hold generality (ALL windows, no cherry-picking):
python -m tools.grid_backtest --csv $CSV --start 2021-01-01 --end 2025-12-31 --spacings 3.0 --rolling-days 180
```
