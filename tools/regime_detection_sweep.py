"""Experiment — detection-quality sweep for the 4h regime strategy-selection idea.

The 4h experiment (tools/regime_switch_backtest.py, findings 2026-05-30) found a
PARTIAL result: a greedy-perfect ORACLE returns +164.6% on BTC 2021->2026Q1 (beats
buy-and-hold +135.6%), but a NAIVE realistic detector returns -88.1% (worse than
useless; 2435 whipsaws). The entire edge lives in detection quality. This sweep asks
the decisive follow-up:

    Between the -88% naive floor and the +164% oracle ceiling, where do
    PROGRESSIVELY-SMARTER realistic (no-lookahead) detectors land?

The shape of that curve decides whether "good enough" 4h regime detection is reachable
with heuristics, needs frontier-LLM judgment, or isn't reachable at all — i.e. whether
the Oracle/MoE build is worth pursuing.

Levers swept (each independently moves naive -> smarter):
1. TRAILING-WINDOW length — how much history the read sees (lag vs noise tradeoff).
2. HYSTERESIS / confirmation — require N consecutive windows to AGREE before flipping
   strategy. Directly attacks the 2435-whipsaw leak (the suspected dominant cost).
3. CHOP-THRESHOLD width — the drift band that separates "grid it" from "defend".
4. MULTI-SIGNAL detector — drift-only (baseline) vs drift + linear-fit R² (trend-
   STRENGTH). A choppy round-trip has low R² even if its endpoints drifted; a clean
   trend has high R². Gating trend-calls on R² fixes the "round-trip mislabeled as
   trend" failure the naive detector suffers.

REUSES the verdict-tested machinery: the same continuous carry-forward portfolio
primitives (`_seed_sim`, `_apply_strategy`, `_run_window`, `_POLICY`) from
`regime_switch_backtest`, and `_load_ohlc` / `run_sim` from `grid_backtest`. The ONLY
new logic here is the parameterized detector + the hysteresis state machine. No live
money (offline backtest, $0).

Usage:
    python -m tools.regime_detection_sweep --csv data/kraken-history/XBTUSD_1.csv \
        --append data/kraken-history/2026Q1/XBTUSD_1.csv --start 2021-01-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# Sibling-tool reuse (pylint can't resolve `tools` as a package — false positive; the
# imports resolve at runtime via `python -m tools.regime_detection_sweep`).
from tools.grid_backtest import _load_ohlc, run_sim  # pylint: disable=import-error
from tools.regime_switch_backtest import (  # pylint: disable=import-error
    _POLICY,
    _apply_strategy,
    _run_window,
    _seed_sim,
)

_Bar = tuple[Decimal, Decimal, Decimal, Decimal]  # (open, high, low, close)


@dataclass(frozen=True)
class Detector:
    """One detector configuration point in the sweep."""

    trail_hours: float
    chop_pct: Decimal  # |drift| below this (fraction) = chop
    confirm_n: int  # consecutive agreeing windows required to flip (1 = no hysteresis)
    multi: bool  # True = drift + R² trend-strength gate; False = drift only
    r2_min: float = 0.30  # min linear-fit R² to call a trend (multi only)


@dataclass
class SweepRow:
    detector: Detector
    ret_pct: float
    switches: int


def _drift(trailing: list[Decimal]) -> float:
    if len(trailing) < 2 or trailing[0] <= 0:
        return 0.0
    return float((trailing[-1] - trailing[0]) / trailing[0])


def _r_squared(ys: list[Decimal]) -> float:
    """R² of a least-squares line fit to ``ys`` vs index. High = clean trend;
    low = choppy / round-trip. Subsampled by the caller for speed."""
    n = len(ys)
    if n < 3:
        return 0.0
    fy = [float(y) for y in ys]
    mean_x = (n - 1) / 2.0
    mean_y = sum(fy) / n
    sxx = sum((i - mean_x) ** 2 for i in range(n))
    sxy = sum((i - mean_x) * (fy[i] - mean_y) for i in range(n))
    syy = sum((v - mean_y) ** 2 for v in fy)
    if sxx == 0 or syy == 0:
        return 0.0
    slope = sxy / sxx
    ss_res = sum((fy[i] - (mean_y + slope * (i - mean_x))) ** 2 for i in range(n))
    return max(0.0, 1.0 - ss_res / syy)


def _classify(trailing: list[Decimal], det: Detector) -> str:
    """Raw (pre-hysteresis) regime read from trailing closes only — no lookahead."""
    drift = _drift(trailing)
    chop = float(det.chop_pct)
    if abs(drift) < chop:
        return "chop"
    if det.multi:
        # subsample to <=200 points so the regression stays cheap on long trails
        step = max(1, len(trailing) // 200)
        if _r_squared(trailing[::step]) < det.r2_min:
            return "chop"  # endpoints drifted but the path isn't a clean trend
    return "up" if drift > 0 else "down"


def _simulate(  # pylint: disable=too-many-locals
    bars: list[_Bar], args: argparse.Namespace, det: Detector
) -> SweepRow:
    """Continuous carry-forward portfolio with a hysteresis state machine.

    A new RAW regime must repeat for ``det.confirm_n`` consecutive windows before it is
    COMMITTED (the strategy only changes on a committed flip). confirm_n=1 reproduces the
    no-hysteresis behavior."""
    wb = max(1, int(args.window_hours * 60))
    tb = max(1, int(det.trail_hours * 60))
    closes = [b[3] for b in bars]
    anchor = bars[0][0]
    sim = _seed_sim(anchor, args)
    start_value = sim.portfolio_value(anchor)
    committed = "chop"
    pending: str | None = None
    pending_n = 0
    prev_strategy: str | None = None
    switches = 0
    for w_start in range(0, len(bars) - 1, wb):
        window = bars[w_start : w_start + wb]
        if not window:
            break
        price = bars[w_start][0]
        trailing = closes[max(0, w_start - tb) : w_start] or [price]
        raw = _classify(trailing, det)
        if raw == committed:
            pending, pending_n = None, 0
        elif raw == pending:
            pending_n += 1
            if pending_n >= det.confirm_n:
                committed, pending, pending_n = raw, None, 0
        else:
            pending, pending_n = raw, 1
            if det.confirm_n <= 1:
                committed, pending, pending_n = raw, None, 0
        strategy = _POLICY[committed]
        _apply_strategy(sim, strategy, price, args)
        if prev_strategy is not None and strategy != prev_strategy:
            switches += 1
        prev_strategy = strategy
        _run_window(sim, window)
    end_value = sim.portfolio_value(bars[-1][3])
    ret = float((end_value - start_value) / start_value * 100) if start_value else 0.0
    return SweepRow(detector=det, ret_pct=ret, switches=switches)


def _hold_returns(bars: list[_Bar]) -> tuple[float, float]:
    move = bars[-1][3] / bars[0][0] - Decimal("1")
    return float(move * Decimal("50")), float(move * Decimal("100"))


def _static_best(bars: list[_Bar], args: argparse.Namespace) -> float:
    best = None
    for sp in (args.tight, args.wide):
        r = run_sim(
            bars,
            spacing_pct=sp,
            levels_above=args.levels,
            levels_below=args.levels,
            order_size_usd=args.order_size,
            maker_fee=args.maker_fee,
            reanchor_margin=args.reanchor_margin,
            taker_fee=args.taker_fee,
            slippage_bps=args.slippage_bps,
            derisk_slippage_bps=args.derisk_slippage_bps,
        )
        ret = float(r.pnl / r.start_value * Decimal("100")) if r.start_value else 0.0
        best = ret if best is None else max(best, ret)
    return best or 0.0


def _detector_grid(args: argparse.Namespace) -> list[Detector]:
    """The swept configurations. trail x confirm x {drift, multi} at a fixed chop band."""
    grid: list[Detector] = []
    for multi in (False, True):
        for trail in args.trails:
            for confirm in args.confirms:
                grid.append(
                    Detector(
                        trail_hours=trail,
                        chop_pct=args.chop_pct,
                        confirm_n=confirm,
                        multi=multi,
                    )
                )
    return grid


def _iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _load(args: argparse.Namespace) -> list[_Bar]:
    bars = _load_ohlc(args.csv, _iso(args.start), _iso(args.end))
    if args.append is not None:
        bars = bars + _load_ohlc(args.append, _iso(args.start), _iso(args.end))
    return bars


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--append", type=Path, default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--tight", type=Decimal, default=Decimal("1.0"))
    p.add_argument("--wide", type=Decimal, default=Decimal("3.0"))
    p.add_argument("--levels", type=int, default=3)
    p.add_argument("--order-size", type=Decimal, default=Decimal("10"))
    p.add_argument("--maker-fee", type=Decimal, default=Decimal("0.0026"))
    p.add_argument("--taker-fee", type=Decimal, default=Decimal("0.0040"))
    p.add_argument("--reanchor-margin", type=Decimal, default=Decimal("1.0"))
    p.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    p.add_argument("--derisk-slippage-bps", type=Decimal, default=Decimal("30"))
    p.add_argument("--window-hours", type=float, default=4.0)
    p.add_argument("--chop-pct", type=Decimal, default=Decimal("0.02"), help="chop band (fraction)")
    p.add_argument(
        "--trails",
        type=lambda s: [float(x) for x in s.split(",")],
        default=[6.0, 12.0, 24.0, 48.0, 72.0],
        help="trailing-window lengths (hours), comma list",
    )
    p.add_argument(
        "--confirms",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[1, 2, 3],
        help="hysteresis confirmation counts, comma list (1 = none)",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    bars = _load(args)
    if len(bars) < 100:
        print(f"!! only {len(bars)} bars")
        return 1
    h5050, h100 = _hold_returns(bars)
    static = _static_best(bars, args)
    print("# detection-quality sweep (EXPERIMENT; MODEL — see docstring)")
    print(
        f"# bars: {len(bars)} (1m)  ${float(bars[0][0]):,.0f} -> ${float(bars[-1][3]):,.0f}  "
        f"window={args.window_hours:.0f}h  chop<{float(args.chop_pct)*100:.0f}%"
    )
    print(
        f"# BENCHMARKS:  hold-100% {h100:+.1f}%   hold-50/50 {h5050:+.1f}%   static {static:+.1f}%"
    )
    print("# (naive baseline from the 4h experiment: realistic -88.1%, oracle ceiling +164.6%)")
    print()
    header = f"{'detector':<34} {'return %':>9} {'switches':>9} {'vs hold':>9}"
    print(header)
    print("-" * len(header))
    rows = [_simulate(bars, args, det) for det in _detector_grid(args)]
    rows.sort(key=lambda r: r.ret_pct, reverse=True)
    for r in rows:
        d = r.detector
        kind = "multi" if d.multi else "drift"
        name = f"{kind} trail={d.trail_hours:.0f}h confirm={d.confirm_n}"
        vs = r.ret_pct - h5050
        print(f"{name:<34} {r.ret_pct:>+9.1f} {r.switches:>9} {vs:>+9.1f}")
    print()
    best = rows[0]
    bd = best.detector
    print(
        f"best detector: {'multi' if bd.multi else 'drift'} trail={bd.trail_hours:.0f}h "
        f"confirm={bd.confirm_n}  -> {best.ret_pct:+.1f}%  (50/50-hold {h5050:+.1f}%)"
    )
    if best.ret_pct > h5050:
        print(
            "=> a realistic detector BEATS 50/50-hold. Detection may be reachable with heuristics."
        )
    elif best.ret_pct > static and best.ret_pct > -10:
        print("=> best realistic detector beats static + claws back most of the hole, but < hold.")
    else:
        print("=> no swept detector escapes the hole. 4h heuristic detection looks insufficient.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
