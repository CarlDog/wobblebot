# Detection-quality sweep — findings (2026-05-30)

Follow-up to the 4h strategy-selection experiment (`regime-switch-findings-2026-05-30.md`),
which was PARTIAL: oracle +164.6% beats hold, naive realistic detector −88.1%. The entire
edge lives in detection quality, so this sweep asks: **between the −88% naive floor and the
+164% oracle ceiling, where do progressively-smarter realistic (no-lookahead) detectors
land?** Tool: `tools/regime_detection_sweep.py` (reuses the verdict-tested carry-forward
portfolio; sweeps trailing-window length × hysteresis-confirmation × {drift-only,
drift+R²-trend-strength}; chop band fixed at 2%).

## Result: detection bottleneck is SEVERE — no heuristic detector escapes the hole

Full sweep, **BTC 2021-01-01 → 2026-Q1** (30 detectors; benchmarks: hold-100% +135.6%,
hold-50/50 **+67.8%**, static grid +21.3%):

| Detector (best of each tier) | Return | Switches | vs 50/50-hold |
|---|---|---|---|
| **best overall: drift, trail=72h, confirm=2** | **−68.0%** | 1043 | −135.8 |
| drift, trail=72h, confirm=3 | −75.2% | 762 | −143.0 |
| drift, trail=72h, confirm=1 | −77.8% | 1951 | −145.6 |
| naive baseline (drift, 24h, confirm=1) | −88.1% | 2435 | −155.9 |
| worst (drift, 6h, confirm=3) | −92.0% | 119 | −159.8 |

**Two things are true at once:**
1. **The anti-whipsaw machinery WORKS, directionally.** Longer trailing windows + hysteresis
   moved the best detector from the −88% naive floor to **−68%** (a 20-point improvement) and
   roughly halved switches (2435 → 1043). Longer trail dominates; hysteresis adds a few
   points; the R²/multi-signal gate did *not* help (multi tiers scored at or below their
   drift twins).
2. **But −68% vs +67.8% hold is still a ~136-point chasm.** No swept heuristic detector comes
   anywhere near beating hold — or even reaching break-even. Per the pre-registered criteria
   this is the FAIL-leaning end of PARTIAL: the *idea* has a real ceiling (the oracle proved
   that), but **heuristic 4h detection is decisively insufficient to capture it.**

## The load-bearing diagnostic — it's not only detection, it's the POLICY map

The oracle's strategy picks were **tight 8972 / wide 1618 / flat 246 / cash 570** — i.e. it
ran a **GRID ~92% of windows** and went flat/cash only ~7% combined. But the realistic policy
this sweep used maps **up→flat, down→cash** — so on every *detected* trend it stops gridding
and (for "down") liquidates to cash at taker-fee + 30bps slippage. Over a trend-heavy
+135% span that means: frequent cash exits at local lows, re-entry higher (sell-low-buy-high),
and forgone grid-harvest while sitting flat/cash. That is a large part of the −68%.

So the oracle–realistic gap is **detection lag AND a wrong policy map**, entangled. The sweep
varied the *detector* but held the *policy* fixed, so it can't fully separate them. What it
*can* say: the operator's literal "go defensive (flat/cash) when you detect a trend" policy,
driven by any heuristic detector we tried, loses badly — because (a) heuristic 4h trend
detection lags/whipsaws, and (b) even perfectly detected, the optimal move is almost never
cash (the oracle cashed only 5% of windows; 4h drops are small enough that intra-window grid
harvest usually beats sitting out).

## What this means for the direction

- **Heuristic regime detection alone will not make switching beat hold.** If the regime-switch
  is pursued, it needs either frontier-LLM-grade judgment (the Oracle/MoE — the thing this
  whole arc was probing) AND/OR a fundamentally better policy map than "flat/cash on trends."
- **The oracle's +164% remains the one bright spot:** per-window *optimal* strategy choice
  beats hold. But "optimal choice" is exactly what's hard, and a 30-detector heuristic sweep
  got nowhere near it. The honest gap between "provably possible" and "achievable with the
  tools we have" is now measured, and it's wide.
- **Economics unchanged:** even closing the gap is a *percentage* edge — income stays gated on
  capital. This is a $100 learning project; the question is whether chasing LLM-grade 4h
  detection is worth the build, knowing the dollar payoff is capped by capital regardless.

## Open / not-yet-tested (so the FAIL read isn't overclaimed)

- **Policy-map sweep** (the real confound): test "always grid, switch tight↔wide only" and
  "grid-or-cash (no flat)" maps, since the oracle says mostly-grid wins. This is the single
  most important untested follow-up — it may show the bottleneck is the policy, not detection.
- Multi-coin; rolling-distribution (not just full-span); window-size sweep (is 4h even the
  right horizon?); confidence-*proportional* sizing rather than hard switching.
- An LLM-grade detector (the actual Oracle) vs these heuristics — the comparison the MoE build
  would have to justify.

## Reproduce

```bash
CSV=data/kraken-history/XBTUSD_1.csv ; Q1=data/kraken-history/2026Q1/XBTUSD_1.csv
python -m tools.regime_detection_sweep --csv $CSV --append $Q1 --start 2021-01-01
```

## Bottom line

Hysteresis + longer trail help (−88% → −68%) but no heuristic 4h detector escapes the hole;
hold (+68%) is untouchable for them. The +164% oracle ceiling is real but needs per-window
optimal choice that heuristics can't reach — and the realistic policy's "flat/cash on trends"
is itself a large drag (the oracle grids 92% of windows). **Next decision point: sweep the
policy map (cheap, may reframe this) before concluding heuristics are dead; then decide
whether LLM-grade detection is worth building given the capital-capped payoff.** See
[[project_v1_consolidation_pending]] — the owed organize pass is getting closer.
