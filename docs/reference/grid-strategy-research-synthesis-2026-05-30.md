# Grid-strategy research synthesis (2026-05-29 → 2026-05-30)

**Purpose.** ONE coherent account of everything the 2026-05-29/30 research established about
whether WobbleBot's grid strategy works — so the scattered findings docs don't have to be
re-derived or re-litigated. This is the consolidation the operator asked for. The detailed
findings docs (cited below) remain the primary sources; this is the narrative + the verdict +
the parked research, in one place.

**TL;DR.** A static grid loses to buy-and-hold over realistic conditions; its edge is confined
to rare chop and is ~break-even there. A dynamic *regime-switching* advisor — re-picking a
strategy every 4h — has a genuine **+164.6% oracle ceiling** (the first thing in the whole
program to beat buy-and-hold), but **no realistic, heuristic detector + policy can reach it**;
the best realistic version just reproduces a static grid, which loses to hold. The capital math
is independent of all of it: returns are a *percentage*, so on $100 the dollar payoff is
pennies regardless. **WobbleBot is a learning/discipline project, not an income engine** — the
research made that concrete; it did not change the original intent. The regime/Oracle idea is
**PARKED with its findings** (not abandoned), revisit conditions below.

---

## 1. The question and how it was answered

Stage 8.5 shipped a vol→spacing cascade advisor. The question the synthetic fixtures couldn't
answer: *does adapting the grid to the market actually make money vs. just holding?* We built a
backtester over the Kraken 1m dump (2013→2026 Q1) and ran it honestly — rolling, un-cherry-
picked windows; realistic slippage; an adversarial red-team; and out-of-sample 2026 Q1 data
that postdated the whole study. Five reference docs hold the detail:

- `grid-backtest-findings-2026-05-29.md` — the static-grid verdict + §10 the two-run blind
  adversarial flip-the-script pass.
- `regime-switch-findings-2026-05-30.md` — the 4h strategy-selection experiment (oracle vs
  realistic).
- `regime-detection-sweep-2026-05-30.md` — sweeping detector quality.
- `regime-policy-sweep-2026-05-30.md` — sweeping the policy map (the de-confounding control).
- (tooling: `tools/grid_backtest.py`, `heuristic_backtest.py`, `regime_switch_backtest.py`,
  `regime_detection_sweep.py`, `regime_policy_sweep.py` — all offline, $0, reproduce blocks in
  each findings doc.)

## 2. What was established (the chain of conclusions)

**(a) The static grid underperforms buy-and-hold.** Over un-cherry-picked 180d rolling windows
it beats a passive 50/50 hold in only ~22–44% of windows, never >50%, across BTC/ETH/SOL/XRP/
DOGE. Its genuine edge is confined to *chop* — which, once round-trips are excluded with a
path-aware definition, is rare and only ~break-even. Trend, not volatility, decides win-vs-lose;
the long-biased grid bleeds in sustained moves. **Survived a two-run blind adversarial red-team**
(`verdict-mostly-holds-with-revisions`; 5 of 6 skeptic biases ran *against* the grid, so the
case is if anything understated) and **reproduced out-of-sample on 2026 Q1** (a broad −21…−33%
downturn: "wider beats tighter" held on all 5 coins; grid < hold).

**(b) The Stage 8.5 vol→spacing advisor is mis-calibrated AND mis-oriented.** Real BTC per-tick
vol sits below the shipped curve's floor, so it flat-clamps to 0.65% and recommends TIGHTEN — the
single worst setting (verified: on a 3% grid it said "tighten to 0.65%" on 2898/2912 windows).
Volatility is the wrong signal for a live tuner; the advisor is also *blind to trend*
(`PerformanceSummary` carries no directional signal). Cascade architecture + the LLM are NOT
rejected — only the vol-curve-as-P&L-driver.

**(c) Spacing: wider beats tighter; 1% (live) is far too tight; ~3% is the least-bad.** Per-cycle
net is ~$0.05 @1% / ~$0.28 @2% / ~$0.64 @3%, but cycle *count* falls proportionally, so total
profit is ~flat across spacings in chop. Wider spacing's real benefit is **downtrend survival**
(2026 Q1: 1% −20% vs 3% −12%), not higher profit. Re-anchor policy is immaterial at 3% (the live
ADR-006 park-when-offside default is fine).

**(d) Regime-switching has a real ceiling but is heuristically unreachable.** Three experiments:
- *4h selection:* a greedy-perfect **oracle returns +164.6%, BEATING buy-and-hold +135.6%** and
  crushing the static grid (+21.3%) — the first time anything beat hold. A naive realistic
  detector returns **−88.1%** (worse than useless; 2435 whipsaws). The entire edge lives in
  detection quality.
- *Detection sweep:* the best of 30 heuristic detectors (longer trail + hysteresis) climbs to
  **−68%** — better, but still a ~136pt chasm to hold. (This run was confounded — it held policy
  fixed.)
- *Policy sweep (the de-confounder):* holding the detector fixed and varying the policy map, the
  best policy (hold-on-trend) reaches **+19.8%** — an ~88pt swing from the −68% baseline, so the
  policy was a big lever. The real driver is **spacing through uptrends, not cash**: every
  *tight*-gridding policy craters over the +135% bull (static-tight −90.2%, grid-or-cash −87.6%,
  flat-cash −68.0%) by selling out and rebuying higher, while *hold*/*wide* policies survive
  (hold-on-trend +19.8%, wide-or-cash +17.7%, static-wide +13.6%) — the "1% is far too tight"
  finding again. (Counter-example to a cash story: wide-or-cash, which DOES cash on downtrends,
  *beats* always-grid, which never cashes.) **Decisively, no detector × policy beats hold or even
  a true parked static grid (+21.3%)** — the best switcher (+19.8%) is just "grid the chop, hold
  the trends," a partial buy-and-hold that gives up beta vs. simply holding.

**Net:** with heuristic detection, regime-switching does not beat holding. The oracle's edge
requires near-perfect per-window timing = **LLM-grade judgment** — which is exactly what the
MoE/Oracle build would be, and is a large build for a payoff that stays capital-capped.

**(e) The capital wall (independent of strategy quality).** Returns are a percentage. +2.95% (a
good chop window) is $3 on $100, $295 on $10k — same strategy. On $100, `order_size` maxes ~$10–15
(the sell side needs backing inventory), structurally capping per-cycle profit at pennies–$0.40.
A *good* strategy and an *income engine* are different claims needing different things: research
vs. capital. Even a winning detector wouldn't make $100 an income engine. **Beating buy-and-hold
is the thing that would EARN the right to scale capital** — that's the real prize, and the
research found that only the (unreachable-heuristically) oracle clears it.

## 3. Decisions ratified by this research

- **The advisor is a regime/trend READER + transparent GUARDRAIL, not a predictor** — operator
  owns the call (memory `project_advisor_philosophy`). Two-lever model: vol→spacing = slow
  per-symbol base-spacing *calibration*; trend/regime = the defense lever.
- **Posture (defensive/harvest) is advisory-only, NEVER auto-applied** — mechanical auto-de-risk
  fails (the policy sweep proved cash-on-downtrend is destructive without perfect timing). Only
  bounded spacing stays auto-applicable.
- **Stage 8.6 shrinks to hardening only** (widen grid + recalibrate the curve so the advisor
  stops recommending the worst setting); the regime-classifier-as-strategy-driver is PARKED (§4).
- **WobbleBot is a learning/discipline project** — sizing/expectations reflect that; the grid<hold
  finding informs it, doesn't kill the purpose. (Operator reaffirmed clear-headed: $100 always a
  test case; $10k aspirational and maybe never.)

## 4. PARKED research track — the Oracle/MoE regime engine (NOT abandoned)

Per the operator's standing principle (*we don't ever throw away research — it gets parked with
its findings*): the regime-switching idea is **shelved, not deleted.** Everything needed to
revisit is preserved here + in the five findings docs.

- **What's proven:** per-window optimal strategy choice (the oracle) beats buy-and-hold (+164.6%
  vs +135.6% on BTC 2021→2026Q1). The *idea* has a real, measured ceiling.
- **What's missing:** a no-lookahead detector good enough to capture a meaningful fraction of the
  ~143pt oracle-vs-static gap. No heuristic comes close; the only candidate is LLM-grade 4h
  regime detection — i.e. the full MoE confab feeding the **Oracle** forecast engine
  (`project_oracle_naming`). The cascade architecture is the right host (heuristic disposes clear
  windows free; escalate near regime transitions to the MoE).
- **Revisit conditions (any of):** (1) capital grows enough that a percentage edge is worth a big
  build; (2) an appetite to test whether a frontier LLM can do 4h regime detection at near-oracle
  quality (the one untested high-value experiment — an LLM-grade detector vs these heuristics);
  (3) a fundamentally cheaper detection signal emerges. Until then: do NOT build the MoE/Oracle on
  hope — the research says heuristics don't clear the bar.
- **Untested-but-cheap follow-ups if revisited:** confidence-*proportional* sizing (vs hard
  switching); other window sizes (is 4h the sweet spot?); multi-coin; rolling-distribution of the
  switching policy; the LLM-grade detector comparison.

## 5. The reconciled plan (what we're actually doing)

1. **Stage 8.6 (rescoped → hardening only, pre-soak):** widen the live BTC grid off 1% toward
   ~3%; recalibrate `config/heuristic/quant.yml` so the advisor's resting recommendation tracks
   real BTC vol (~3%) instead of the 0.65% floor; fix the lookback coupling. Advisory-only;
   posture display optional. ADRs: 019 (advisor purpose: regime reader, refines ADR-002/007) +
   020 may slim (regime-as-metric becomes parked-track, not shipped). See the revised
   `docs/planning/stage-8.6-advisor-regime-reorientation-design.md`.
2. **Gating soak (~2026-06-01):** runs the wider grid + recalibrated advisor; forward-validates
   the hardening, not a regime engine.
3. **v1.0 tag** after the soak. Then **Phase 9 (Kraken Securities equities)** as committed.
4. **Parked:** the Oracle/MoE regime track (§4); v1.1 backlog items the research superseded get
   marked (notably `docs/release/v1.1/adaptive-grid.md` vol→spacing entries — demoted/parked).

## 6. Caveats (so the verdict isn't overclaimed)

Single asset class (crypto), mostly one ~4.5yr trend-heavy cycle (a sideways-dominated era would
treat the grid more kindly — but the grid wins <50% of windows even in measured chop, so unlikely
to flip). Fixed strategy menu + 4h window + heuristic detector family; confidence-proportional
sizing + LLM-grade detection are genuinely untested (the parked track). The oracle is greedy +
cheats with foresight (a ceiling, not a target). Directional findings are robust across coins,
windows, slippage, grid size, two blind red-teams, and out-of-sample data; absolute magnitudes
are model-dependent. No new trading algorithm was built — this is diagnosis. No live money was
touched across the entire research arc (offline, $0); running real-money trading cost stays
$0.085018.
