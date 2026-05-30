# Experiment — 4h regime-driven strategy selection (does switching beat static + hold?)

**Status:** SCOPE / design — 2026-05-30. Approach-first; no code until ratified.
This is an **experiment**, not a stage. It answers one question with data before we
invest in building out the MoE strategy-selection layer (Stage 8.6+).

## The question (operator's hypothesis, made precise + falsifiable)

The advisor runs every **4 hours** and picks the best strategy *to get to the next
confab* — a bounded short-horizon bet, not a forecast. The claim to test:

> **A policy that re-selects its strategy each 4h window from a menu — driven by a
> realistic (lagging, imperfect) regime read — beats BOTH (a) the best static grid
> AND (b) buy-and-hold, over un-cherry-picked rolling history, AFTER the whipsaw cost
> of wrong switches.**

This is genuinely UNTESTED. Yesterday's backtest tested a *static* grid and a
*mechanical de-risk trigger*; it did NOT test per-window strategy *switching*. So this
is new ground, not a relitigation of the verdict.

## Why 4h is the right (more tractable) horizon

Short-horizon regime *persistence* is real — vol clusters; the red-team measured mild
positive momentum at short lags. "Will the next 4h resemble the last 4h?" is a far more
winnable bet than "where's the cycle going." The 4h re-evaluation also *bounds* how long
a wrong read bleeds (the next confab can switch us out) — it caps the downside of being
wrong without needing to be right about the whole day. That bound is the experiment's
core source of any edge.

## The decisive trap this must respect

"Net a cycle every 4h" is a **chop-regime objective**, not a universal one. A cycle needs
a round trip (dip-to-buy AND recover-to-sell); in a one-directional trend NO spacing
produces a clean cycle — you fill one side and hold underwater inventory. Forcing a cycle
in a downtrend is *exactly* how the static grid bled −20% in 2026 Q1. So the policy's
objective is **conditional**:
- detected **chop** → run a grid sized to harvest ~1 cycle/window;
- detected **trend/down** → do NOT force a cycle (go wide, flat, or to cash).

That conditionality IS the regime-switch. The experiment measures whether a realistic
classifier can deliver it net-positive after mistakes.

## Method (reuses the existing, verdict-tested harness)

Build on `tools/grid_backtest.py` + the Stage 8.6 `compute_regime` classifier (the
classifier is the input this experiment needs — they share the build).

1. **Walk history in 4h windows** (production cadence = `advise: 4h`). At each boundary:
   - classify the regime from data available *up to that point only* (no lookahead —
     this is the load-bearing honesty constraint; the classifier sees only the trailing
     window the live advisor would see);
   - select next-window strategy from a **menu**: `tight-grid | wide-grid | flat-hold |
     cash` (start small; expand only if the menu is the binding constraint);
   - simulate that strategy over the next 4h window; carry inventory/cash forward.
2. **Three benchmarks, same windows, same friction (incl. slippage):**
   - the switching policy (the hypothesis);
   - the best *static* grid (yesterday's champion);
   - buy-and-hold (50/50 and 100%, both — the bar the verdict said the grid fails).
3. **Roll across the full 2021→2026Q1 dense history** (per the coverage audit, that's the
   trustworthy span; pre-2017 too sparse). Report per-coin (BTC primary; ETH/SOL/XRP/DOGE
   secondary).
4. **Whipsaw accounting is explicit:** count switches, count *wrong* switches (where the
   chosen strategy underperformed the alternative that window), and the realized cost of
   each switch (fees + slippage to re-lay/liquidate). The edge, if any, is
   `chop-harvest gains − trend-period losses − whipsaw costs`. Show all three terms.
5. **Detection-quality sweep:** because the whole edge lives in detection quality, run the
   policy at several classifier accuracies — including a *perfect-hindsight* oracle (upper
   bound) and a *lagged/realistic* classifier (the real number). The gap between them is
   the value of better detection — and tells us if even a PERFECT detector clears the bar
   (if the oracle doesn't beat hold, the idea is dead regardless of classifier quality;
   if the oracle wins big but the realistic one doesn't, the bottleneck is detection and
   the research target is clear).

## Success criteria (decide BEFORE running — no moving goalposts)

- **PASS:** the realistic (lagged) switching policy beats BOTH the best static grid AND
  buy-and-hold over rolling windows on BTC, after whipsaw + slippage, and the result is
  directionally consistent across the other coins. → the MoE strategy-selection layer is
  worth building; there's a data-backed case that this could scale with capital.
- **PARTIAL:** the oracle beats hold but the realistic classifier doesn't. → the idea is
  sound but bottlenecked on detection; the research target becomes "make the 4h regime
  read good enough," and we scope that explicitly before building live.
- **FAIL:** even the oracle doesn't beat hold over rolling windows. → per-4h switching
  doesn't rescue the grid; the project stays the learning exercise, and we've learned it
  on $0 instead of finding out live.

## Honest framing (carry-over from 2026-05-29)

Even a PASS is a *percentage* edge — "income engine" stays gated on CAPITAL (20%/yr is
$20 on $100). But a strategy that genuinely beats buy-and-hold is the thing that *earns*
the right to scale capital later. Income follows proven edge follows capital — in that
order. This experiment targets the FIRST link (proven edge), which is the correct one to
chase first. It does NOT touch live money (offline backtest, cost $0).

## Relationship to Stage 8.6 + the MoE design

- The `compute_regime` classifier this needs IS Stage 8.6 Slice A — they share the build,
  so this experiment front-loads the "is it worth it?" question onto work we were doing
  anyway.
- The cascade architecture (heuristic-first → escalate-to-LLM/MoE) is RETAINED and
  composes with this: in the live system the heuristic picks the strategy on clear
  windows; the LLM/MoE confab is consulted on ambiguous (near-regime-transition) windows.
  Escalation should be *transition-aware* — escalate MORE readily near regime boundaries
  (that's where "clear match" is most dangerous), not less.
- If this experiment PASSES, it validates building out the full MoE (risk + news advisors
  + arbitrator strategy synthesis), currently deferred to v1.1.

## Naming — "Oracle" (operator, 2026-05-30)

When the market-forecast / "weather report" layer gets built, the engine that COMPILES it
— the component that reads the MoE advisors + news + regime signals and synthesizes the
per-window market forecast the strategy selection keys off — is named **Oracle**. (The
name surfaced from this experiment's greedy-perfect `oracle` detection mode; the operator
liked it for the real forecast compiler.) NOTE the deliberate distinction: the experiment's
`oracle` *mode* is the perfect-hindsight CEILING (cheating, for upper-bound measurement);
the future **Oracle ENGINE** is the real-time forecast compiler (no lookahead). Same name,
opposite epistemics — keep them straight in code/docs (e.g. `oracle_mode` the test flag vs.
the `Oracle` service). Not the unrelated database product.

## Build outline (after ratification)

- `tools/regime_switch_backtest.py` (new) — the 4h-window selection harness; reuses
  `grid_backtest.run_sim` per window + the regime classifier; menu of strategies;
  3 benchmarks; whipsaw accounting; oracle-vs-lagged sweep. Lives in `tools/` (diagnostic,
  outside the mypy gate, like the other backtest tools). Read-only; no live money.
- Findings recorded in `docs/reference/` (sibling to the grid-backtest findings),
  with reproduce commands.

## Open decisions (for ratification)

1. **Strategy menu size:** start with 4 (`tight / wide / flat-hold / cash`) or include a
   per-symbol-calibrated grid? Lean: start with 4; expand only if the menu binds.
2. **Classifier for the experiment:** build the real Stage 8.6 `compute_regime` first
   (cleaner, shared work), or prototype a throwaway classifier in the tool to get a fast
   read, then formalize? Lean: prototype in the tool first for a FAST go/no-go, then
   formalize into `compute_regime` only if it passes — avoids building the Stage 8.6
   classifier on spec before knowing switching even works.
3. **Window size sensitivity:** test 4h (production) only, or sweep 1h/4h/12h/1d to see if
   4h is actually the sweet spot? Lean: 4h primary + a quick sweep, since the horizon is
   itself a hypothesis.
