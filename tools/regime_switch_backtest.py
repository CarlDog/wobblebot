"""Experiment — 4h regime-driven strategy SELECTION (does switching beat static + hold?).

Tests the operator's hypothesis (docs/planning/experiment-4h-strategy-selection.md):
a policy that re-picks its strategy each 4h window — from a menu of
{tight-grid, wide-grid, flat-hold, cash} driven by a regime read — beats BOTH the
best static grid AND buy-and-hold, over un-cherry-picked history, after whipsaw +
slippage.

REUSES the verdict-tested simulator: ``grid_backtest._Sim`` (same fill-on-touch /
maker-fee / slippage / re-anchor model) and ``grid_backtest.run_sim`` (for the static
baselines). The only new thing here is the CONTINUOUS carry-forward portfolio that
changes strategy at 4h boundaries — inventory + cash roll from one window into the next,
so re-entering a grid from all-cash rebuilds inventory the honest (asymmetric) way.

Two detection modes, the load-bearing comparison:
- ``realistic``: classify the regime from TRAILING data only (no lookahead — exactly what
  the live 4h advisor would see), map regime -> strategy via a fixed policy.
- ``oracle``: greedy-perfect — at each boundary, simulate ALL menu strategies over the
  *actual* next window (on clones of the current portfolio) and commit the best. This is
  the ceiling of per-window selection — "if you could see the next 4h perfectly." If the
  oracle can't beat hold, the idea is dead regardless of classifier quality.

Benchmarks (same span, same friction): best static grid (yesterday's champion),
50/50 buy-and-hold, 100% buy-and-hold. Reports a full-span run + a rolling 180d
sub-window distribution (apples to the grid-backtest verdict's method).

MODEL caveats inherit from grid_backtest's docstring; additionally:
- A grid re-laid from all-cash can only place its BUY side until fills accumulate BTC
  (the sell side needs backing inventory) — realistic, but the grid is asymmetric right
  after a cash exit. Counted as part of the switch cost.
- Oracle is GREEDY (best next window given current state), not globally optimal — a strong,
  computable upper bound that matches the operator's "only needs to be right for 4h" frame.

Usage:
    python -m tools.regime_switch_backtest --csv data/kraken-history/XBTUSD_1.csv \
        --append data/kraken-history/2026Q1/XBTUSD_1.csv --start 2021-01-01
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

# Sibling-tool reuse: resolves at runtime via `python -m tools.regime_switch_backtest`,
# but pylint can't see `tools` as a package (no install), so the import-error is a
# false positive here — same verdict-tested simulator the grid backtest uses.
from tools.grid_backtest import _load_ohlc, _Sim, run_sim  # pylint: disable=import-error

_ZERO = Decimal("0")
_Bar = tuple[Decimal, Decimal, Decimal, Decimal]  # (open, high, low, close)
_STRATEGIES = ("tight", "wide", "flat", "cash")
# Regime -> strategy. chop harvests (tight = more cycles / "a cycle per window");
# uptrend holds (a grid would sell into the rally); downtrend goes to cash (protect).
_POLICY = {"chop": "tight", "up": "flat", "down": "cash"}


@dataclass
class PolicyResult:  # pylint: disable=too-many-instance-attributes
    start_value: Decimal
    end_value: Decimal
    switches: int
    switch_fees: Decimal
    regime_counts: dict[str, int]
    strategy_counts: dict[str, int]
    oracle_agreement: float = 0.0  # fraction of windows realistic chose the oracle pick

    @property
    def ret_pct(self) -> float:
        if not self.start_value:
            return 0.0
        return float((self.end_value - self.start_value) / self.start_value * 100)


def _seed_sim(anchor: Decimal, args: argparse.Namespace) -> _Sim:
    """A fresh continuous portfolio, 50/50 USD/BTC at the anchor (matches the
    50/50-hold benchmark), seeded with the 2x buffer like grid_backtest."""
    return _Sim(
        spacing_pct=args.tight,
        levels_above=args.levels,
        levels_below=args.levels,
        order_size_usd=args.order_size,
        maker_fee=args.maker_fee,
        reanchor_margin=args.reanchor_margin,
        taker_fee=args.taker_fee,
        slippage_bps=args.slippage_bps,
        derisk_slippage_bps=args.derisk_slippage_bps,
        free_usd=args.order_size * Decimal(args.levels) * Decimal("2"),
        free_btc=(args.order_size * Decimal(args.levels) * Decimal("2")) / anchor,
    )


def _apply_strategy(sim: _Sim, strategy: str, price: Decimal, args: argparse.Namespace) -> Decimal:
    """Reconfigure the book for the chosen strategy at a window boundary.

    Returns the fees paid by the switch itself (the cash-out taker fee; grid re-lays
    pay their fees as fills happen, not here)."""
    fees_before = sim.fees_paid
    sim._cancel_all()  # pylint: disable=protected-access
    if strategy == "cash":
        sim._derisk_to_cash(price)  # pylint: disable=protected-access
    elif strategy == "flat":
        pass  # hold whatever inventory we have; place no orders
    else:  # tight | wide -> lay a grid at the current price
        sim.spacing_pct = args.tight if strategy == "tight" else args.wide
        sim._lay_grid(price)  # pylint: disable=protected-access
    return sim.fees_paid - fees_before


def _classify(trailing: list[Decimal], chop_threshold: Decimal) -> str:
    """Lagging regime read from trailing closes only (no lookahead)."""
    if len(trailing) < 2 or trailing[0] <= 0:
        return "chop"
    drift = (trailing[-1] - trailing[0]) / trailing[0]
    if abs(drift) < chop_threshold:
        return "chop"
    return "up" if drift > 0 else "down"


def _run_window(sim: _Sim, window: list[_Bar]) -> None:
    """Step the sim through one window's bars (fills + re-anchor), like run_sim's loop."""
    for _open, high, low, close in window:
        sim.process_bar(high, low)
        sim.maybe_reanchor(close)


def _oracle_pick(sim: _Sim, window: list[_Bar], price: Decimal, args: argparse.Namespace) -> str:
    """Greedy-perfect: clone the portfolio, try every menu strategy over the ACTUAL
    next window, return the one with the best end value. Upper bound on selection."""
    best_strategy = "flat"
    best_value = None
    end_price = window[-1][3]
    for strategy in _STRATEGIES:
        clone = copy.deepcopy(sim)
        _apply_strategy(clone, strategy, price, args)
        _run_window(clone, window)
        value = clone.portfolio_value(end_price)
        if best_value is None or value > best_value:
            best_value = value
            best_strategy = strategy
    return best_strategy


def _simulate_policy(  # pylint: disable=too-many-locals
    bars: list[_Bar], args: argparse.Namespace, *, mode: str
) -> PolicyResult:
    """Continuous carry-forward portfolio that re-selects strategy each ``window_bars``.

    ``mode`` is ``realistic`` (trailing classifier) or ``oracle`` (greedy-perfect)."""
    wb = args.window_bars
    tb = args.trail_bars
    chop = args.chop_threshold
    anchor = bars[0][0]
    sim = _seed_sim(anchor, args)
    start_value = sim.portfolio_value(anchor)
    closes = [b[3] for b in bars]
    switches = 0
    switch_fees = _ZERO
    prev_strategy: str | None = None
    regime_counts = {"chop": 0, "up": 0, "down": 0}
    strategy_counts = dict.fromkeys(_STRATEGIES, 0)
    agree = 0
    decisions = 0
    for w_start in range(0, len(bars) - 1, wb):
        window = bars[w_start : w_start + wb]
        if not window:
            break
        price = bars[w_start][0]
        if mode == "oracle":
            strategy = _oracle_pick(sim, window, price, args)
            regime = "—"
        else:
            trailing = closes[max(0, w_start - tb) : w_start] or [price]
            regime = _classify(trailing, chop)
            strategy = _POLICY[regime]
            regime_counts[regime] += 1
            # shadow the oracle to measure agreement (cheap: only in realistic mode)
            if args.measure_agreement:
                if strategy == _oracle_pick(sim, window, price, args):
                    agree += 1
                decisions += 1
        strategy_counts[strategy] += 1
        switch_fees += _apply_strategy(sim, strategy, price, args)
        if prev_strategy is not None and strategy != prev_strategy:
            switches += 1
        prev_strategy = strategy
        _run_window(sim, window)
    end_value = sim.portfolio_value(bars[-1][3])
    return PolicyResult(
        start_value=start_value,
        end_value=end_value,
        switches=switches,
        switch_fees=switch_fees,
        regime_counts=regime_counts,
        strategy_counts=strategy_counts,
        oracle_agreement=(agree / decisions) if decisions else 0.0,
    )


def _hold_returns(bars: list[_Bar]) -> tuple[float, float]:
    """(50/50-hold %, 100%-hold %) over the span — closed form from the price move."""
    move = bars[-1][3] / bars[0][0] - Decimal("1")
    return float(move * Decimal("50")), float(move * Decimal("100"))


def _static_best(bars: list[_Bar], args: argparse.Namespace) -> tuple[float, float]:
    """Best static grid over the span (tight vs wide), via the verdict-tested run_sim.
    Returns (best_ret_pct, best_spacing)."""
    best_ret = None
    best_sp = float(args.tight)
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
        if best_ret is None or ret > best_ret:
            best_ret = ret
            best_sp = float(sp)
    return (best_ret or 0.0), best_sp


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
    p.add_argument("--append", type=Path, default=None, help="second CSV appended (e.g. 2026Q1)")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--label", default=None)
    p.add_argument("--tight", type=Decimal, default=Decimal("1.0"), help="tight-grid spacing %%")
    p.add_argument("--wide", type=Decimal, default=Decimal("3.0"), help="wide-grid spacing %%")
    p.add_argument("--levels", type=int, default=3)
    p.add_argument("--order-size", type=Decimal, default=Decimal("10"))
    p.add_argument("--maker-fee", type=Decimal, default=Decimal("0.0026"))
    p.add_argument("--taker-fee", type=Decimal, default=Decimal("0.0040"))
    p.add_argument("--reanchor-margin", type=Decimal, default=Decimal("1.0"))
    p.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"), help="per-fill friction")
    p.add_argument("--derisk-slippage-bps", type=Decimal, default=Decimal("30"))
    p.add_argument("--window-hours", type=float, default=4.0, help="advisor cadence (4h prod)")
    p.add_argument("--trail-hours", type=float, default=24.0, help="trailing window for the read")
    p.add_argument(
        "--chop-pct", type=Decimal, default=Decimal("2.0"), help="|trailing drift| below = chop"
    )
    p.add_argument(
        "--rolling-days", type=int, default=0, help=">0: rolling sub-window beats-hold distribution"
    )
    p.add_argument("--rolling-step-days", type=int, default=30)
    p.add_argument("--no-oracle", action="store_true", help="skip the (slower) oracle run")
    p.add_argument(
        "--measure-agreement",
        action="store_true",
        help="in realistic mode, also compute oracle-agreement (doubles realistic cost)",
    )
    return p


def _derive(args: argparse.Namespace) -> None:
    args.window_bars = max(1, int(args.window_hours * 60))
    args.trail_bars = max(1, int(args.trail_hours * 60))
    args.chop_threshold = args.chop_pct / Decimal("100")


def _print_full_span(bars: list[_Bar], args: argparse.Namespace) -> None:
    h5050, h100 = _hold_returns(bars)
    static_ret, static_sp = _static_best(bars, args)
    realistic = _simulate_policy(bars, args, mode="realistic")
    print("# full-span comparison")
    print(f"{'strategy':<26} {'return %':>10}  notes")
    print("-" * 60)
    print(f"{'buy-and-hold 100%':<26} {h100:>+10.1f}")
    print(f"{'buy-and-hold 50/50':<26} {h5050:>+10.1f}  (capital-matched bar)")
    print(f"{'best static grid':<26} {static_ret:>+10.1f}  (@ {static_sp:.1f}%)")
    rc = realistic.regime_counts
    print(
        f"{'SWITCHING (realistic)':<26} {realistic.ret_pct:>+10.1f}  "
        f"switches={realistic.switches} "
        f"regime[chop/up/down]={rc['chop']}/{rc['up']}/{rc['down']}"
    )
    if args.measure_agreement:
        print(f"{'  oracle agreement':<26} {realistic.oracle_agreement*100:>9.0f}%")
    if not args.no_oracle:
        oracle = _simulate_policy(bars, args, mode="oracle")
        sc = oracle.strategy_counts
        print(
            f"{'SWITCHING (oracle ceiling)':<26} {oracle.ret_pct:>+10.1f}  "
            f"switches={oracle.switches} "
            f"pick[t/w/f/c]={sc['tight']}/{sc['wide']}/{sc['flat']}/{sc['cash']}"
        )
    print()
    _verdict(realistic.ret_pct, h5050, static_ret)


def _verdict(realistic: float, hold: float, static: float) -> None:
    beats_hold = realistic > hold
    beats_static = realistic > static
    if beats_hold and beats_static:
        print("=> realistic switching BEATS both 50/50-hold and the best static grid.")
    elif beats_hold:
        print("=> realistic switching beats 50/50-hold but not the best static grid.")
    elif beats_static:
        print("=> realistic switching beats static but NOT 50/50-hold.")
    else:
        print("=> realistic switching beats NEITHER hold nor static.")


def _print_rolling(bars: list[_Bar], args: argparse.Namespace) -> None:
    wbars = args.rolling_days * 1440
    sbars = max(1, args.rolling_step_days * 1440)
    if len(bars) <= wbars:
        print(f"!! rolling window {args.rolling_days}d exceeds data ({len(bars)} bars)")
        return
    beats_hold = beats_static = n = 0
    pol_rets: list[float] = []
    hold_rets: list[float] = []
    for s in range(0, len(bars) - wbars + 1, sbars):
        win = bars[s : s + wbars]
        h5050, _ = _hold_returns(win)
        static_ret, _ = _static_best(win, args)
        realistic = _simulate_policy(win, args, mode="realistic")
        pol_rets.append(realistic.ret_pct)
        hold_rets.append(h5050)
        beats_hold += realistic.ret_pct > h5050
        beats_static += realistic.ret_pct > static_ret
        n += 1
    if not n:
        print("!! no full rolling windows")
        return
    print(f"# ROLLING {args.rolling_days}d (step {args.rolling_step_days}d) — realistic switching")
    print(f"windows:                 {n}")
    print(f"beats 50/50-hold:        {beats_hold}/{n}  ({100*beats_hold/n:.0f}%)")
    print(f"beats best static grid:  {beats_static}/{n}  ({100*beats_static/n:.0f}%)")
    print(f"median switching return: {median(pol_rets):+.1f}%")
    print(f"median 50/50-hold:       {median(hold_rets):+.1f}%")


def main() -> int:
    args = _build_parser().parse_args()
    _derive(args)
    bars = _load(args)
    if len(bars) < args.window_bars * 2:
        print(f"!! only {len(bars)} bars; need >= {args.window_bars*2}")
        return 1
    print("# 4h regime strategy-selection backtest (EXPERIMENT; MODEL — see docstring)")
    if args.label:
        print(f"# regime label: {args.label}")
    print(
        f"# bars: {len(bars)} (1m)  ${float(bars[0][0]):,.0f} -> ${float(bars[-1][3]):,.0f}  "
        f"window={args.window_hours:.0f}h trail={args.trail_hours:.0f}h chop<{args.chop_pct}%"
    )
    print(
        f"# menu: tight {args.tight}% / wide {args.wide}% / flat / cash   "
        f"slippage {args.slippage_bps}bps (+{args.derisk_slippage_bps} cash)   "
        f"policy chop->tight up->flat down->cash"
    )
    print()
    if args.rolling_days > 0:
        _print_rolling(bars, args)
    else:
        _print_full_span(bars, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
