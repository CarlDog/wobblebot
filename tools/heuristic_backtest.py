"""Tier 1 heuristic backtest — does the ideal(vol) curve track real BTC vol?

Feeds a *historical* BTC price series through the production volatility
math (``services.metrics.compute_volatility``) and the production
``HeuristicAdvisorAdapter``, over a sliding lookback window, and reports
what spacing the curve recommends across the series. This is the
"validate to market" pass the synthetic 36/24 fixture batteries can't
give: the fixtures are hand-built ``PerformanceSummary`` objects (they
test recommendation *quality* against maintainer judgment), whereas this
drives the heuristic with *realized* market volatility.

**The calibration anchor (read this before trusting any number).** The
heuristic curve in ``config/heuristic/quant.yml`` is, per the Stage 8.5
design doc, a "**per-tick σ** → spacing %" curve, where the production
tick is the ``observe_prices: 30s`` poll cadence and the volatility
lookback is ``advise.metrics_lookback_hours: 6`` (6h / 30s = 720
snapshots — the value the probe fixtures hard-code). Kraken's finest
candle is **1-minute**, so a vol computed from 1m closes is sampled at
60s, not 30s. For a random walk σ scales with √Δt, so 1m-sampled vol is
≈ √2 larger than the per-30s-tick vol the curve expects. This tool
divides the 1m vol by ``sqrt(bar_seconds / tick_seconds)`` to put it on
the curve's basis. The rescale assumes i.i.d. returns; real BTC
microstructure (mild mean-reversion + vol clustering at short horizons)
makes √2 approximate — fine for a regime-level signal, not a tick-exact
reproduction.

**Tier 1 scope.** This does NOT simulate trading. It feeds market vol +
market drawdown with ``cycle_count=0`` / ``win_rate=0`` (no simulated
ledger — that's Tier 2's engine sim vs a static baseline). So the
trade-derived guards behave as documented for the "no cycles yet" case;
the report flags which guard fired per window. The load-bearing Tier 1
question is whether realized BTC vol lands inside the curve's modeled
domain and whether the first-order call churns (a flapping
WIDEN/TIGHTEN signal across adjacent windows is a fee-bleed risk).

Data sources:
- default: Kraken ``/0/public/OHLC`` 1m (the ~720 most-recent 1m bars =
  ~12h). A calibration smoke test only — too short for regime study.
- ``--csv PATH``: a Kraken downloadable historical 1m OHLCVT file
  (headerless ``time,open,high,low,close,volume,trades``). The real
  regime run; slice with ``--start`` / ``--end`` (ISO-8601 UTC).

Usage:
    python -m tools.heuristic_backtest                       # live 12h smoke test
    python -m tools.heuristic_backtest --csv data/kraken-history/XBTUSD_1.csv \
        --start 2022-05-01 --end 2022-07-01 --label "2022 LUNA/FTX bear"
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median, quantiles

import httpx

from wobblebot.adapters.heuristic_advisor import HeuristicAdvisorAdapter
from wobblebot.config.heuristic import HeuristicSpec, load_heuristic_spec
from wobblebot.ports.advisor import CurrentGridParams, PerformanceSummary
from wobblebot.services.metrics import (
    compute_flatness,
    compute_max_drawdown,
    compute_volatility,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SPEC = _REPO_ROOT / "config" / "heuristic" / "quant.yml"
_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# (window-end UTC, vol_1m, vol_tick, direction, reason, ideal_target%)
Row = tuple[datetime, float, float, str, str, float | None]


class Bar:
    """One OHLC bar: unix-second open time + close (the price the
    production snapshot tape samples)."""

    __slots__ = ("opened_at", "close")

    def __init__(self, opened_at: int, close: Decimal) -> None:
        self.opened_at = opened_at
        self.close = close


def _load_live(pair: str = "XBTUSD") -> list[Bar]:
    """Fetch the most-recent ~720 1m bars from Kraken's public endpoint."""
    resp = httpx.get(_KRAKEN_OHLC_URL, params={"pair": pair, "interval": "1"}, timeout=30.0)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(f"Kraken OHLC error: {payload['error']}")
    result = payload["result"]
    # One list-valued entry (the bars) plus an int "last" cursor.
    bars_raw = next(v for v in result.values() if isinstance(v, list))
    # Kraken row: [time, open, high, low, close, vwap, volume, count]
    return [Bar(int(row[0]), Decimal(str(row[4]))) for row in bars_raw]


def _load_csv(path: Path, start: datetime | None, end: datetime | None) -> list[Bar]:
    """Parse a Kraken historical 1m OHLCVT CSV (headerless).

    Columns: ``time,open,high,low,close,volume,trades``. Filters to
    ``[start, end)`` if given. Streams the file so multi-GB dumps don't
    blow memory — only the requested window's bars are retained.
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    lo = int(start.timestamp()) if start else None
    hi = int(end.timestamp()) if end else None
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cols = line.split(",")
            ts = int(cols[0])
            if lo is not None and ts < lo:
                continue
            if hi is not None and ts >= hi:
                continue
            bars.append(Bar(ts, Decimal(cols[4])))
    bars.sort(key=lambda b: b.opened_at)
    return bars


def _evaluate_window(
    *,
    closes: Sequence[Decimal],
    rescale: float,
    baseline_spacing: float,
    adapter: HeuristicAdvisorAdapter,
) -> tuple[float, float, str, str, float | None]:
    """Return (vol_1m, vol_tick, direction, guard/reason, ideal_target)."""
    vol_1m = float(compute_volatility(closes))
    vol_tick = vol_1m * rescale
    drawdown = float(compute_max_drawdown(closes))
    flatness = float(compute_flatness(closes))
    summary = PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=float(closes[-1]),
        snapshot_count=len(closes),
        volatility=vol_tick,
        max_drawdown=drawdown,
        flatness=flatness,
        cycle_count=0,
        win_rate=0.0,
        total_pnl=0.0,
        active_orders=0,
        current_grid=CurrentGridParams(
            spacing_percentage=baseline_spacing,
            levels_above=4,
            levels_below=4,
            order_size_usd=10.0,
        ),
    )
    verdict = adapter.evaluate(summary)
    target = verdict.recommendation.recommendations.get("spacing_percentage")
    return vol_1m, vol_tick, verdict.direction, verdict.reason, target


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, default=None, help="Kraken 1m OHLCVT CSV; omit for live")
    p.add_argument("--pair", default="XBTUSD", help="Kraken altname for the live fetch")
    p.add_argument("--start", default=None, help="ISO-8601 UTC window start (CSV only)")
    p.add_argument("--end", default=None, help="ISO-8601 UTC window end (CSV only)")
    p.add_argument("--label", default=None, help="Regime label for the report header")
    p.add_argument("--window-hours", type=float, default=6.0, help="vol lookback (production: 6)")
    p.add_argument("--step-minutes", type=int, default=30, help="re-evaluation cadence")
    p.add_argument("--spacing", type=float, default=1.0, help="baseline current spacing %%")
    p.add_argument("--bar-minutes", type=int, default=1, help="candle granularity (Kraken min: 1)")
    p.add_argument("--tick-seconds", type=int, default=30, help="production snapshot cadence")
    p.add_argument("--heuristic-file", type=Path, default=_DEFAULT_SPEC)
    p.add_argument("--rows", type=int, default=0, help="print first N per-window rows (0 = all)")
    return p


def _iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _compute_rows(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    bars: list[Bar],
    window_bars: int,
    step_bars: int,
    rescale: float,
    baseline_spacing: float,
    adapter: HeuristicAdvisorAdapter,
) -> list[Row]:
    """Slide the lookback window across the bars, evaluating each."""
    rows: list[Row] = []
    for end_idx in range(window_bars, len(bars) + 1, step_bars):
        window = bars[end_idx - window_bars : end_idx]
        closes = [b.close for b in window]
        vol_1m, vol_tick, direction, reason, target = _evaluate_window(
            closes=closes,
            rescale=rescale,
            baseline_spacing=baseline_spacing,
            adapter=adapter,
        )
        end_dt = datetime.fromtimestamp(window[-1].opened_at, tz=UTC)
        rows.append((end_dt, vol_1m, vol_tick, direction, reason, target))
    return rows


def _print_header(  # pylint: disable=too-many-arguments
    *,
    source: str,
    args: argparse.Namespace,
    spec: HeuristicSpec,
    window_bars: int,
    rescale: float,
    n_bars: int,
) -> None:
    curve_lo, curve_hi = spec.curve[0].vol, spec.curve[-1].vol
    print("# heuristic backtest (Tier 1 — curve response, no trade sim)")
    print(f"# source:        {source}  ({n_bars} bars)")
    if args.label:
        print(f"# regime:        {args.label}")
    print(f"# spec:          {args.heuristic_file.name}  curve vol domain [{curve_lo}, {curve_hi}]")
    print(f"# window:        {args.window_hours}h ({window_bars} bars)  step {args.step_minutes}m")
    print(f"# baseline:      current spacing {args.spacing}%  fee_floor {spec.fee_floor}%")
    print(
        f"# rescale:       1m vol x {rescale:.4f} -> per-{args.tick_seconds}s-tick basis "
        f"(sqrt({args.tick_seconds}/{args.bar_minutes * 60}); random-walk approx)"
    )


def _print_report(*, rows: list[Row], curve_lo: float, curve_hi: float, limit: int) -> None:
    # pylint: disable=too-many-locals
    header = (
        f"{'window end (UTC)':<20} {'vol_1m':>9} {'vol_tick':>9} {'in?':>4} "
        f"{'dir':>8} {'target':>7}  reason"
    )
    print()
    print(header)
    print("-" * len(header))
    for end_dt, vol_1m, vol_tick, direction, reason, target in rows[:limit]:
        in_domain = "yes" if curve_lo <= vol_tick <= curve_hi else "OUT"
        tgt = f"{target:.2f}" if target is not None else "  -"
        print(
            f"{end_dt:%Y-%m-%d %H:%M}     {vol_1m:>9.5f} {vol_tick:>9.5f} {in_domain:>4} "
            f"{direction:>8} {tgt:>7}  {reason}"
        )
    if limit < len(rows):
        print(f"... ({len(rows) - limit} more rows suppressed; --rows 0 for all)")

    vols_tick = [r[2] for r in rows]
    targets = [r[5] for r in rows if r[5] is not None]
    directions = [r[3] for r in rows]
    in_domain_count = sum(1 for v in vols_tick if curve_lo <= v <= curve_hi)
    flips = sum(1 for i in range(1, len(directions)) if directions[i] != directions[i - 1])
    print()
    print(f"windows:         {len(rows)}")
    print(
        f"vol_tick:        min {min(vols_tick):.5f}  median {median(vols_tick):.5f}  "
        f"max {max(vols_tick):.5f}"
    )
    if len(vols_tick) >= 2:
        q = quantiles(vols_tick, n=100, method="inclusive")
        print(
            f"vol_tick pctl:   p10 {q[9]:.5f}  p25 {q[24]:.5f}  p50 {q[49]:.5f}  "
            f"p75 {q[74]:.5f}  p90 {q[89]:.5f}  p99 {q[98]:.5f}"
        )
    print(
        f"in curve domain: {in_domain_count}/{len(rows)} "
        f"({100 * in_domain_count / len(rows):.0f}%)  [domain {curve_lo}-{curve_hi}]"
    )
    if targets:
        print(
            f"ideal spacing:   min {min(targets):.2f}%  median {median(targets):.2f}%  "
            f"max {max(targets):.2f}%"
        )
    counts = {d: directions.count(d) for d in ("widen", "hold", "tighten")}
    print(
        f"directions:      widen {counts['widen']}  hold {counts['hold']}  "
        f"tighten {counts['tighten']}"
    )
    print(
        f"churn:           {flips} direction flips across {len(rows)} windows "
        f"({100 * flips / max(1, len(rows) - 1):.0f}% of transitions)"
    )


def main() -> int:
    args = _build_parser().parse_args()
    spec = load_heuristic_spec(args.heuristic_file)
    adapter = HeuristicAdvisorAdapter(spec=spec)

    if args.csv is not None:
        bars = _load_csv(args.csv, _iso(args.start), _iso(args.end))
        source = f"CSV {args.csv.name}"
    else:
        bars = _load_live(args.pair)
        source = f"live Kraken {args.pair} 1m"

    rescale = math.sqrt(args.tick_seconds / (args.bar_minutes * 60))
    window_bars = int(args.window_hours * 60 / args.bar_minutes)
    step_bars = max(1, int(args.step_minutes / args.bar_minutes))

    _print_header(
        source=source,
        args=args,
        spec=spec,
        window_bars=window_bars,
        rescale=rescale,
        n_bars=len(bars),
    )
    if len(bars) <= window_bars:
        print(f"\n!! only {len(bars)} bars; need > {window_bars} for one window. Widen the range.")
        return 1

    rows = _compute_rows(
        bars=bars,
        window_bars=window_bars,
        step_bars=step_bars,
        rescale=rescale,
        baseline_spacing=args.spacing,
        adapter=adapter,
    )
    limit = args.rows if args.rows > 0 else len(rows)
    _print_report(rows=rows, curve_lo=spec.curve[0].vol, curve_hi=spec.curve[-1].vol, limit=limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
