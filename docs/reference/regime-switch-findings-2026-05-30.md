# 4h regime strategy-selection — findings (2026-05-30)

Tests the operator's hypothesis (`docs/planning/experiment-4h-strategy-selection.md`):
does a policy that re-picks its strategy every 4h window — from a menu of
{tight-grid, wide-grid, flat-hold, cash} driven by a regime read — beat BOTH the best
static grid AND buy-and-hold? Tool: `tools/regime_switch_backtest.py` (reuses the
verdict-tested `grid_backtest._Sim` per window; continuous carry-forward portfolio).

## Result: PARTIAL — the idea is sound, the detector is the bottleneck

Full-span **BTC 2021-01-01 → 2026-Q1** (the dense/trustworthy span; +135.6% underlying,
window 4h, trailing read 24h, chop |drift|<2%, slippage 5bps + 30bps cash, menu policy
chop→tight / up→flat / down→cash):

| Strategy | Return |
|---|---|
| Buy-and-hold 100% | **+135.6%** |
| Buy-and-hold 50/50 (capital-matched bar) | +67.8% |
| Best static grid (@3%) | +21.3% |
| **Switching — realistic** (lagging trailing-drift classifier) | **−88.1%** (2435 switches) |
| **Switching — oracle ceiling** (greedy-perfect 4h foresight) | **+164.6%** (3863 switches) |

Two lines tell the whole story:

1. **The oracle (+164.6%) BEATS buy-and-hold (+135.6%) and crushes the static grid
   (+21.3%).** This is the FIRST time anything in the entire backtest program has beaten
   buy-and-hold. So per-4h strategy switching is **not** a mirage — chosen well, the edge
   is real and large. The operator's core hypothesis is vindicated *at the ceiling*.

2. **The realistic classifier (−88.1%) INVERTS the edge — it's worse than useless.** A
   naive trailing-drift detector was so wrong it lost 88% over a period where simply
   holding made +135% — a ~220-point swing from the same menu of decisions made well vs.
   badly. It whipsawed 2435 times (to cash before bounces, into grids before drops). This
   is the "regime detection is the hard problem" wall, quantified: the entire edge lives
   in detection quality, and a *bad* detector is actively destructive, not merely flat.

**Pre-registered outcome = PARTIAL** (defined before running): *oracle beats hold, realistic
doesn't → the idea is sound but bottlenecked on detection.* The research question is no
longer "does switching work?" (yes, with foresight) but "**how much of the ~250-point
oracle-vs-realistic gap can a real, no-lookahead detector capture?**"

## What this changes

- **Reframes the whole MoE/Oracle build.** The multi-advisor confab + the future
  [[project_oracle_naming]] forecast engine isn't decoration — it IS the detector, and the
  oracle gap is precisely the prize it competes for. Worth building *iff* a detector can
  close a meaningful fraction of the gap.
- **The static-grid verdict is unchanged** (`grid-backtest-findings-2026-05-29.md`): static
  grid still loses to hold (+21% vs +68%). Switching is a *different* strategy, and only
  its *oracle* form wins; its naive-realistic form is the worst option on the board.

## Honest caveats

- The realistic classifier here is DELIBERATELY crude (one trailing-drift threshold). −88%
  proves a *bad* detector is dangerous; it does NOT prove a *good* detector fails. The
  detection-quality sweep (next) measures where smarter detectors land between the −88%
  floor and +164% ceiling.
- The oracle is GREEDY + cheats with perfect 4h foresight — an unreachable ceiling, not a
  target. The real number is whatever a no-lookahead detector achieves.
- Single coin (BTC), single full cycle. Multi-coin + rolling-distribution + window-size
  sensitivity are open (rolling harness exists via `--rolling-days`).
- Economics framing unchanged (2026-05-29): even a winning detector is a *percentage* edge —
  "income engine" stays gated on capital. But beating buy-and-hold is the thing that earns
  the right to scale capital. This experiment found the first credible *path* to that.

## Reproduce

```bash
CSV=data/kraken-history/XBTUSD_1.csv
Q1=data/kraken-history/2026Q1/XBTUSD_1.csv
# full-span realistic + oracle:
python -m tools.regime_switch_backtest --csv $CSV --append $Q1 --start 2021-01-01
# rolling 180d distribution (realistic only):
python -m tools.regime_switch_backtest --csv $CSV --append $Q1 --start 2021-01-01 \
    --rolling-days 180 --rolling-step-days 30
```

## Next

Detection-quality sweep: between the −88% naive floor and +164% oracle ceiling, measure
where progressively-smarter realistic detectors land (longer trailing windows, multi-signal,
confidence-gated switching that only acts when sure, hysteresis to cut whipsaw). The shape
of that curve tells us whether "good enough" detection is plausibly reachable with
heuristics, needs frontier-LLM judgment, or isn't reachable at all — which decides whether
the Oracle/MoE build is worth it. (Then: more regime hypotheses, then the owed v1.1
consolidation pass — see [[project_v1_consolidation_pending]].)
