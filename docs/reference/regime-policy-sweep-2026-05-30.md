# Policy-map sweep — findings (2026-05-30) — CLOSES the regime-switch arc

The control the detection sweep left open. The detection sweep
(`regime-detection-sweep-2026-05-30.md`) varied the DETECTOR but held the POLICY MAP
fixed at {chop→tight, up→flat, down→cash}, and its best detector still lost (−68% vs
+68% hold). The oracle, by contrast, grids ~93% of windows — so the fixed policy was a
likely large drag CONFOUNDED with detection quality. This sweep isolates it: **hold the
detector fixed** (the detection-sweep winner: drift, 72h trail, confirm=2) and **sweep
the policy map**. Tool: `tools/regime_policy_sweep.py`.

> **CORRECTION NOTICE (this doc was fixed 2026-05-30 after first commit).** The initially
> committed version of this file carried WRONG numbers (drafted from a 6-week smoke window,
> not the full run), a WRONG causal story ("monotonic in cash exposure"), and a FALSE
> "harness validated" claim. All three are corrected below against the authoritative
> full-span run. The bottom-line verdict was unaffected, but the table + the *why* were
> wrong; this is the corrected record. (Lesson logged: never draft findings numbers from a
> smoke run before the authoritative run lands — `feedback_verify_math_before_propagating`.)

## Result: policy is a big lever, but no realistic policy beats hold or a true static grid

Full sweep, **BTC 2021-01-01 → 2026-Q1** (benchmarks: hold-100% +135.6%, hold-50/50
**+67.8%**, **true-static-best (run_sim) +21.3%**):

| Policy (chop / up / down) | Return | Switches | strategy mix t/w/f/c |
|---|---|---|---|
| **hold-on-trend** (tight / flat / flat — never cash) | **+19.8%** | 978 | 4550/0/6856/0 |
| wide-or-cash (wide / wide / cash) | +17.7% | 560 | 0/8124/0/3282 |
| static-wide *control* (wide / wide / wide) | +13.6% | 0 | 0/11406/0/0 |
| always-grid (tight / wide / wide) | +4.5% | 978 | 4550/6856/0/0 |
| flat-cash *(detection-sweep baseline)* | −68.0% | 1043 | 4550/0/3574/3282 |
| cash-any-trend (tight / cash / cash) | −78.6% | 978 | 4550/0/0/6856 |
| grid-or-cash (tight / tight / cash) | −87.6% | 560 | 8124/0/0/3282 |
| static-tight *control* (tight / tight / tight) | −90.2% | 0 | 11406/0/0/0 |

(The detector splits the era into ~4550 chop / ~3574 up / ~3282 down 4h windows.)

**Three conclusions:**

1. **The policy map was a big lever** — same detector, different regime→strategy map, swings
   the result from −68.0% (flat-cash) to +19.8% (hold-on-trend), an ~88-point spread from
   POLICY alone. So my detection-sweep conclusion ("detection is the bottleneck") was
   confounded; policy mattered at least as much. Owning that.

2. **The dominant axis is SPACING-THROUGH-UPTRENDS, not cash** (this corrects the first
   draft's "cash is destructive / monotonic in cash"). The killers are the **tight-spacing**
   policies: static-tight −90.2%, grid-or-cash −87.6%, cash-any −78.6%, flat-cash −68.0% all
   grid *tight* (1%) through the +135% uptrend — repeatedly selling out and rebuying higher.
   The survivors HOLD or go WIDE through trends: hold-on-trend (+19.8%, flat through trends),
   wide-or-cash (+17.7%), static-wide (+13.6%). Decisive counter-example to the cash story:
   **wide-or-cash (+17.7%, which DOES cash on downtrends) beats always-grid (+4.5%, no cash
   at all)** — so cash-on-down is not the driver; tight-spacing-through-up is. This is the
   "1% is far too tight" core finding (grid-backtest 2026-05-29) showing up again, now in the
   switching context. Cash-on-downtrend is mildly negative on net but secondary.

3. **DECISIVE — no policy beats hold, and none beats a true static grid.** The best policy
   (+19.8%) loses to true-static-best (+21.3% via `run_sim`) and badly to 50/50-hold
   (+67.8%). And "hold-on-trend" is barely a strategy — it grids the chop and just *holds*
   through trends, i.e. a partial buy-and-hold that gives up beta versus simply holding. So
   realistic regime switching adds nothing over a static grid, and the static grid loses to
   hold (the 2026-05-29 verdict, unchanged).

## Harness caveat (the corrected "validation")

The first draft claimed the static-wide control "reproduces the +21.3% static benchmark
exactly." It does NOT — the control returns **+13.6%**, not +21.3%. Cause: the sweep harness
(`_simulate`) calls `_apply_strategy` every 4h window unconditionally, so even an *unchanged*
strategy cancels and re-lays the grid at the current price — i.e. it **re-anchors every 4h**
rather than parking (ADR-006). Over a +135% uptrend that re-anchoring chases price up and
costs ~7.7 points vs a true parked static grid (13.6% vs 21.3%). This artifact is a
**conservative bias** — it penalizes every switching policy with extra re-anchor friction, so
correcting it could only make switching look *better*, and it cannot close the ~48-point gap
to hold. The verdict is therefore robust to it. (Known follow-up if the track is ever
revisited: re-lay only on an actual strategy change; same applies to the detection sweep,
which shares the harness. Not done now — it cannot change the conclusion.)

## What this CLOSES (the regime-switch arc, three experiments)

De-confounded across the 4h selection experiment + detection sweep + this policy sweep:
**with realistic (heuristic) detection, NO detector × policy makes 4h regime-switching beat
buy-and-hold — or even a true static grid.** The best realistic outcome (+19.8%) ≈ "grid the
chop, hold the trends," which underperforms both a parked static grid (+21.3%) and hold
(+67.8%). The +164.6% oracle ceiling is real but needs near-perfect per-window timing —
LLM-grade judgment (the Oracle/MoE build) — a large build for a percentage edge that stays
capital-capped. The decision to pursue it (or not) is now informed, not hopeful.

## Honest caveats (so "closed" isn't overclaimed)

- Single coin (BTC), single ~4.5yr trend-heavy cycle. A sideways era treats the grid more
  kindly — but the rolling-window verdict already showed the grid wins <50% of windows even
  in measured chop, so unlikely to flip.
- Fixed strategy menu (tight/wide/flat/cash) + fixed 4h window + one detector family +
  hard switching (not confidence-proportional sizing). All untested variants live on the
  parked Oracle/MoE track.
- The re-anchor-every-window harness artifact (above) — conservative, verdict-robust.
- The oracle is greedy + cheats with foresight; a ceiling, not a target.

## Reproduce

```bash
CSV=data/kraken-history/XBTUSD_1.csv ; Q1=data/kraken-history/2026Q1/XBTUSD_1.csv
python -m tools.regime_policy_sweep --csv $CSV --append $Q1 --start 2021-01-01
```

## Bottom line

Policy was a big lever (−68% → +19.8%), and the real driver is **spacing through uptrends**,
not cash (tight gridding through the bull craters; holding/going-wide survives — the "1% is
too tight" finding again). But the best realistic policy just reproduces a "grid-chop /
hold-trend" hybrid that loses to both a static grid and to holding. **Realistic heuristic
regime-switching is closed: it does not beat holding.** The oracle's edge needs LLM-grade
detection — a build whose payoff stays capital-capped. Consolidate
([[project_v1_consolidation_pending]]).
