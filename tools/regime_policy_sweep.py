"""Experiment — POLICY-MAP sweep (the control the detection sweep left open).

The detection sweep (tools/regime_detection_sweep.py, findings 2026-05-30) varied the
DETECTOR but held the POLICY MAP fixed at {chop->tight, up->flat, down->cash} — and its
best detector still lost (-68% vs +68% hold). But the oracle's own picks were ~93% GRID
(tight 79% / wide 14% / flat 2% / cash 5%), i.e. the oracle almost never goes flat/cash.
That makes the fixed "go flat/cash on every detected trend" policy a likely large drag,
CONFOUNDED with detection quality in that sweep.

This tool isolates it: hold the DETECTOR fixed (the best from the detection sweep) and
sweep the POLICY MAP — what strategy each regime maps to. The decisive questions:
- Does "always grid" (never leave the grid; just tight<->wide) reproduce ~static?
- Does "grid-or-cash" (grid in chop AND up; cash ONLY on detected downtrends) beat both
  the static grid AND buy-and-hold — i.e. is selective downtrend defense the real edge,
  with the flat/up handling having been the drag?

REUSES the verdict-tested machinery: `_classify`/`Detector` from `regime_detection_sweep`;
`_apply_strategy`/`_run_window`/`_seed_sim` from `regime_switch_backtest`;
`_load_ohlc`/`run_sim` from `grid_backtest`. The ONLY new thing is parameterizing the
regime->strategy map. No live money (offline, $0).

Usage:
    python -m tools.regime_policy_sweep --csv data/kraken-history/XBTUSD_1.csv \
        --append data/kraken-history/2026Q1/XBTUSD_1.csv --start 2021-01-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# Sibling-tool reuse (pylint can't resolve `tools` as a package — false positive; resolves
# at runtime via `python -m tools.regime_policy_sweep`).
from tools.grid_backtest import _load_ohlc, run_sim  # pylint: disable=import-error
from tools.regime_detection_sweep import Detector, _classify  # pylint: disable=import-error
from tools.regime_switch_backtest import (  # pylint: disable=import-error
    _apply_strategy,
    _run_window,
    _seed_sim,
)

_Bar = tuple[Decimal, Decimal, Decimal, Decimal]

# Candidate regime->strategy maps. Keys: chop / up / down -> tight|wide|flat|cash.
# Named for what they express. "static-*" are degenerate controls (same strategy in
# every regime = a plain static grid, sanity-checks the harness against grid_backtest).
_POLICIES: dict[str, dict[str, str]] = {
    "flat-cash (detection-sweep baseline)": {"chop": "tight", "up": "flat", "down": "cash"},
    "grid-or-cash (cash ONLY on down)": {"chop": "tight", "up": "tight", "down": "cash"},
    "wide-or-cash": {"chop": "wide", "up": "wide", "down": "cash"},
    "tight-chop / wide-trend (always grid)": {"chop": "tight", "up": "wide", "down": "wide"},
    "hold-on-trend (never cash)": {"chop": "tight", "up": "flat", "down": "flat"},
    "cash-any-trend": {"chop": "tight", "up": "cash", "down": "cash"},
    "static-tight (control)": {"chop": "tight", "up": "tight", "down": "tight"},
    "static-wide (control)": {"chop": "wide", "up": "wide", "down": "wide"},
}


@dataclass
class PolicyRow:
    name: str
    ret_pct: float
    switches: int
    strat_counts: dict[str, int]


def _simulate(  # pylint: disable=too-many-locals
    bars: list[_Bar], args: argparse.Namespace, det: Detector, policy: dict[str, str]
) -> tuple[float, int, dict[str, int]]:
    """Carry-forward portfolio: fixed detector + hysteresis, configurable regime->strategy.

    Mirrors regime_detection_sweep._simulate exactly except the strategy comes from
    ``policy[committed]`` instead of the hard-coded _POLICY."""
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
    counts: dict[str, int] = {"tight": 0, "wide": 0, "flat": 0, "cash": 0}
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
        strategy = policy[committed]
        counts[strategy] += 1
        _apply_strategy(sim, strategy, price, args)
        if prev_strategy is not None and strategy != prev_strategy:
            switches += 1
        prev_strategy = strategy
        _run_window(sim, window)
    end_value = sim.portfolio_value(bars[-1][3])
    ret = float((end_value - start_value) / start_value * 100) if start_value else 0.0
    return ret, switches, counts


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
    # Detector fixed at the detection-sweep winner (drift, 72h trail, confirm=2).
    p.add_argument("--trail-hours", type=float, default=72.0)
    p.add_argument("--confirm", type=int, default=2)
    p.add_argument(
        "--multi", action="store_true", help="use the drift+R2 detector instead of drift"
    )
    return p


def main() -> int:  # pylint: disable=too-many-locals
    args = _build_parser().parse_args()
    bars = _load(args)
    if len(bars) < 100:
        print(f"!! only {len(bars)} bars")
        return 1
    det = Detector(
        trail_hours=args.trail_hours,
        chop_pct=args.chop_pct,
        confirm_n=args.confirm,
        multi=args.multi,
    )
    h5050, h100 = _hold_returns(bars)
    static = _static_best(bars, args)
    print("# POLICY-MAP sweep (EXPERIMENT; MODEL — see docstring)")
    print(
        f"# bars: {len(bars)} (1m)  ${float(bars[0][0]):,.0f} -> ${float(bars[-1][3]):,.0f}  "
        f"window={args.window_hours:.0f}h"
    )
    print(
        f"# detector FIXED: {'multi' if det.multi else 'drift'} trail={det.trail_hours:.0f}h "
        f"confirm={det.confirm_n} chop<{float(args.chop_pct)*100:.0f}%   "
        f"(detection-sweep winner)"
    )
    print(f"# BENCHMARKS: hold-100% {h100:+.1f}%  hold-50/50 {h5050:+.1f}%  static {static:+.1f}%")
    print()
    header = f"{'policy (chop/up/down)':<40} {'return %':>9} {'switch':>7} {'t/w/f/c grid%':>16}"
    print(header)
    print("-" * len(header))
    rows: list[PolicyRow] = []
    for name, policy in _POLICIES.items():
        ret, switches, counts = _simulate(bars, args, det, policy)
        rows.append(PolicyRow(name=name, ret_pct=ret, switches=switches, strat_counts=counts))
    rows.sort(key=lambda r: r.ret_pct, reverse=True)
    for r in rows:
        c = r.strat_counts
        total = sum(c.values()) or 1
        gridpct = 100 * (c["tight"] + c["wide"]) / total
        mix = f"{c['tight']}/{c['wide']}/{c['flat']}/{c['cash']} {gridpct:.0f}%g"
        print(f"{r.name:<40} {r.ret_pct:>+9.1f} {r.switches:>7} {mix:>16}")
    print()
    best = rows[0]
    print(f"best policy: {best.name}  -> {best.ret_pct:+.1f}%  (50/50-hold {h5050:+.1f}%)")
    if best.ret_pct > h5050:
        print("=> a realistic policy BEATS 50/50-hold. The policy map WAS the bottleneck.")
    elif best.ret_pct > static:
        print("=> best policy beats the static grid but still < hold. Partial: policy helps.")
    else:
        print("=> no policy beats the static grid. Neither detection nor policy map rescues it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
