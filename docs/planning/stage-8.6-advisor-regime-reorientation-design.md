# Stage 8.6 — Advisor Hardening + Grid Widen (pre-soak)

**Status:** RESCOPED 2026-05-30. Originally drafted (2026-05-29) as a full "advisor regime
reorientation" with a first-class regime classifier as the centerpiece. The regime-switching
research arc (three experiments, 2026-05-30) then CLOSED with a clear result: **realistic
heuristic regime detection does not make switching beat buy-and-hold or even a static grid**
(full account: `docs/reference/grid-strategy-research-synthesis-2026-05-30.md`). So 8.6 is cut
down to the parts the research still supports — **hardening** — and the regime-classifier-as-
strategy-driver is **PARKED** (not deleted) on the Oracle/MoE research track (synthesis §4).
Slots into pre-soak, before the gating-soak restart (~2026-06-01).

## Why (what survived the research)

The Stage 8.5 vol→spacing advisor is mis-calibrated: real BTC per-tick vol sits below the
shipped curve's floor, so it flat-clamps to 0.65% and recommends TIGHTEN — the single worst
setting (on a 3% grid it said "tighten to 0.65%" on 2898/2912 windows). And the live grid at
1% spacing is far too tight (full-cycle catastrophic; ~3% is the least-bad, and its real value
is downtrend *survival*, not higher profit). Both are calibration defects worth fixing before
the soak so the advisor stops fighting the grid. This is the supported, low-risk core.

## In scope (hardening)

1. **Widen the live BTC grid:** `grid.default.spacing_percentage` 1.0 → ~3.0 (BTC is the only
   live symbol; exposure unchanged at $60 = 3+3 × $10). Sync `settings.example.yml` ↔ the
   deploy-master `settings.yml`; operator cut/pastes to the NAS bind-mount (deployment-split
   rule; Claude does not deploy). Keep park-when-offside (ADR-006).
2. **Recalibrate `config/heuristic/quant.yml`:** move the curve's vol domain to where real BTC
   vol actually lives so its resting recommendation tracks ~3% instead of the 0.65% floor —
   i.e. the advisor stops recommending the worst setting. Validate against
   `tools/heuristic_backtest.py` before committing. This is DATA-only (operator-tunable file).
3. **Fix the lookback coupling — RESOLVED 2026-05-30 (measurement reversed the plan).** The
   original plan was to *widen* `metrics_lookback_hours` so the cycle-based guards see enough
   round-trips. Measurement on 2013–2025 BTC refuted that (see "Slice B finding" below): a 3%
   grid completes only ~0.2–0.4 cycles/day, so `dont_fix_working`'s `cycles_min: 8` is
   unreachable in any window short enough for the vol estimate to stay current — and widening
   the window makes the −5% drawdown guards fire on ordinary daily noise (24h windows dip ≥5%
   ~13% of the time vs ~2% at 6h). Resolution: **keep `metrics_lookback_hours: 6`**, leave
   `dont_fix_working` enabled but documented-dormant at wide spacing (anti-churn comes from the
   recalibrated curve + hold_deadband in Slice A; the guard auto-re-arms for the MoE world's
   fast/tight grids). Net config change is a `quant.yml` comment only.

The advisor stays **advisory-only** (ADR-002); `auto_apply` stays default-off; only bounded
spacing is ever auto-applicable.

## PARKED (moved to the Oracle/MoE research track — synthesis §4)

- The **first-class regime classifier** (`RegimeSignal` + `compute_regime`), `PerformanceSummary.
  regime`, and the **posture output** (harvest/cautious/defensive + projected downside). The
  research showed a heuristic regime signal doesn't drive a winning strategy, so building it as a
  shipped feature isn't justified now. It is NOT abandoned — the +164.6% oracle ceiling proves the
  idea has a real ceiling; revisit conditions are in synthesis §4 (capital growth, or an
  appetite to test an LLM-grade detector). All findings + tooling preserved.

## ADRs

- **ADR-019 — Advisor purpose: regime reader + guardrail, not a vol-tuner.** Still worth writing
  (records the philosophy shift + why vol→spacing was demoted; refines ADR-002/007). The
  posture-advisory-only invariant lands here as the rule even though posture itself is parked.
- **ADR-020 (regime as a first-class metric)** — DEFER with the parked track; only write it if/
  when the Oracle/MoE build is greenlit.

## Slice plan (rescoped)

- **A — curve recalibration** (`quant.yml` DATA edit) + validate vs `heuristic_backtest.py`.
- **B — lookback finding + dormancy doc** ✅ 2026-05-30. Measurement reversed the plan: do NOT
  widen the window; keep `metrics_lookback_hours: 6`; document `dont_fix_working` dormant at 3%
  (comment-only). Numbers in the "Slice B finding" section below.
- **C — grid widen** (`settings*.yml` in sync) + schema-drift green. ✅ 2026-05-30 (`a1b39c4`).
- **D — ADR-019 + roadmap/CHANGELOG/CLAUDE.md receipt + operator NAS deploy instructions.**

## Slice B finding — cycle cadence vs drawdown coupling (2026-05-30)

Measured with a throwaway probe over the local Kraken BTC 1m dump (2013–2025), using the
production grid geometry (`grid_backtest.run_sim`, 3+3 levels, $10/order, 0.26% maker) and the
production drawdown math (`services.metrics.compute_max_drawdown`):

- **Cycle cadence (completed round-trips):** at 3% spacing the grid completes ~0.2–0.4 cycles
  **per day** across six 60-day BTC windows (vs ~1.6–3.1/day at 1%). So `cycles_min: 8` needs
  ~20–40 days of lookback at 3% — unreachable for a vol-current window; even a 48h window
  averages under one cycle.
- **Drawdown vs window length (2024 BTC, fraction of windows whose worst dip ≥5%):** 6h → 1.7%,
  12h → 4.8%, 24h → 12.8%, 48h → 28.2%. Max-drawdown only grows with window length, so widening
  the lookback to chase cycles would make the −5% guards (`directional_runaway`,
  `defensive_drawdown`) fire on routine daily noise.

**Conclusion:** the two guard families want opposite window lengths; no single window serves
both. The cycle guard is a fast-1%-grid artifact that doesn't transfer to a slow 3% grid by
tuning. Resolution = Option A (keep 6h; document `dont_fix_working` dormant; leave it enabled so
it re-arms for the MoE world's tighter grids). The probe was throwaway (gitignored); its numbers
are preserved here so the research isn't lost.

(The original A–F regime-classifier/port/prompt slices are parked with the track.)

## What this is NOT

Not a regime engine (parked). Not auto-de-risk (the research proved mechanical cash-defense is
destructive under imperfect detection). Not a claim the grid becomes profitable — it stays a
learning/discipline project; this hardening just stops the advisor recommending the worst setting
and stops the live grid bleeding at a too-tight 1%. No live money in the design/validation
(offline, $0).
