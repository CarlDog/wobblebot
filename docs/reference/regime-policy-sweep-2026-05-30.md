# Policy-map sweep — findings (2026-05-30) — CLOSES the regime-switch arc

The control the detection sweep left open. The detection sweep
(`regime-detection-sweep-2026-05-30.md`) varied the DETECTOR but held the POLICY MAP
fixed at {chop→tight, up→flat, down→cash}, and noticed the oracle grids ~93% of windows
(cashes only 5%) — making "flat/cash on every detected trend" a likely large drag
CONFOUNDED with detection quality. This sweep isolates it: **hold the detector fixed**
(the detection-sweep winner: drift, 72h trail, confirm=2) and **sweep the policy map**.
Tool: `tools/regime_policy_sweep.py`.

## Result: the policy WAS most of the −68% — but fixing it still doesn't beat hold (or even static)

Full sweep, **BTC 2021-01-01 → 2026-Q1** (benchmarks: hold-100% +135.6%, hold-50/50
**+67.8%**, static-best **+21.3%**):

| Policy (chop / up / down) | Return | Switches | grid% |
|---|---|---|---|
| static-wide (control = static grid) | **+21.3%** | — | 100% |
| tight-chop / wide-trend (**always grid**) | **+18.9%** | 1305 | 94% |
| wide-or-cash | +15.2% | 684 | 87% |
| static-tight (control) | +6.5% | — | 100% |
| hold-on-trend (never cash) | +2.1% | 1305 | 57% |
| grid-or-cash (cash ONLY on down) | −2.6% | 684 | 87% |
| cash-any-trend | −38.3% | 1289 | 57% |
| flat-cash (detection-sweep baseline) | −68.0% | 1043 | 92% |

**Harness validated:** the static-wide control reproduces the +21.3% static benchmark
exactly — the policy sweep is measuring real behavior.

**Three conclusions, the third decisive:**
1. **The policy map was ~most of the −68%.** Same detector, just a better regime→strategy
   map, moves the result from −68.0% (flat-cash) to +18.9% (always-grid) — an ~87-point
   swing from POLICY alone. My detection-sweep conclusion ("detection is the bottleneck")
   was confounded; the policy was the bigger lever. Owning that.
2. **The ranking is monotonic in cash exposure: the more you go to cash, the worse you do.**
   flat-cash −68% → cash-any −38% → grid-or-cash −2.6% → never-cash/always-grid positive.
   With a realistic (imperfect) detector, the "go to cash to defend in a downtrend"
   intuition is **actively destructive** — it sells at local lows and rebuys higher. The
   defense that the oracle uses surgically (5% of windows, perfectly timed) becomes a
   liability the moment timing is imperfect.
3. **BUT no policy beats hold, and none even beats the static grid.** The best switching
   policy (+18.9%) slightly UNDERPERFORMS just running a static wide grid (+21.3%) — i.e.
   realistic regime switching adds NOTHING over a static grid; it mildly degrades it via
   whipsaw. And the static grid itself loses to buy-and-hold (+21% vs +68%) over this
   trend-heavy era (the 2026-05-29 verdict, unchanged).

## What this CLOSES (the regime-switch arc, three experiments)

Across the 4h selection experiment + detection sweep + this policy sweep, the answer is
now clean and de-confounded:

- **With realistic (heuristic) detection, NO combination of detector + policy map makes
  4h regime-switching beat buy-and-hold — or even beat a static wide grid.** The best
  realistic outcome ≈ the static grid, which itself loses to hold.
- **The +164.6% oracle ceiling is real but unreachable with these tools.** It requires
  near-perfect per-window timing of the grid/cash choice; any realistic detector turns the
  cash option from an asset into a liability (conclusion 2). The gap between "provably
  possible" (oracle) and "achievable heuristically" (≈ static grid) is the whole ~143
  points, and heuristics close ~none of it.
- **So the only path to the oracle's edge is near-oracle detection — i.e. LLM-grade
  judgment (the Oracle/MoE build).** That is a large build for a *percentage* edge that
  stays capital-capped (income gated on capital, unchanged). The decision of whether that's
  worth it is now an informed one, not a hopeful one.

## Honest caveats (so "closed" isn't overclaimed)

- Single coin (BTC), single ~4.5yr trend-heavy cycle. A sideways-dominated era would treat
  the grid (and switching) more kindly — but the rolling-window verdict already showed the
  grid wins <50% of windows even in chop, so this is unlikely to flip.
- Fixed strategy menu (tight/wide/flat/cash) + fixed 4h window + one detector family.
  Confidence-PROPORTIONAL sizing (vs hard switching) and other windows are untested — but
  conclusion 2 (cash is poison under imperfect timing) is robust to those.
- The oracle is greedy + cheats with foresight; it's a ceiling, not a target.

## Reproduce

```bash
CSV=data/kraken-history/XBTUSD_1.csv ; Q1=data/kraken-history/2026Q1/XBTUSD_1.csv
python -m tools.regime_policy_sweep --csv $CSV --append $Q1 --start 2021-01-01
```

## Bottom line

The policy map was the bigger confound (−68% → +18.9% from policy alone), and "cash to
defend" is actively harmful under realistic detection. But the de-confounded best case just
reproduces the static grid, which loses to hold. **Realistic heuristic regime-switching is
closed: it does not beat holding.** The oracle's edge exists only with near-perfect timing
that needs LLM-grade detection — a build whose payoff stays capital-capped. Time to stop
testing and CONSOLIDATE everything we've learned ([[project_v1_consolidation_pending]]).
