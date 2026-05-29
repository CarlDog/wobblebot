"""Tier 2 grid backtest — does adaptive (vol-tracked) spacing beat a static grid?

Simulates a micro-grid over historical 1m BTC bars and reports net P&L per
fixed spacing, so we can answer what Tier 1 could not: does the heuristic's
"wider spacing in higher vol" thesis actually MAKE MONEY versus a fixed grid?
Compare the empirically-best fixed spacing per regime against the spacing the
(recalibrated) heuristic recommends for that regime's vol — if they line up,
the heuristic's mapping is validated; if not, it isn't.

REUSES the production grid geometry — `domain.grid.compute_grid_levels`,
`grid_spacing`, `next_counter_action`, `is_offside` — so the ladder + counter
placement match the engine exactly. The fill / fee / balance loop is a
transparent, auditable model in this tool (NOT the storage-coupled GridEngine),
with every assumption stated below.

ASSUMPTIONS — this is a MODEL; numbers are directional, not exact:
- Fills on touch at the limit price (maker). An order fills the first bar whose
  [low, high] crosses it. Counters placed on a fill are eligible from the NEXT
  bar (no same-bar round-trips). At 1m granularity the intra-bar path barely
  matters (a 1m BTC bar spans ~0.05-0.2%, well under a grid level) — unlike the
  daily-candle case that would dominate.
- Maker fee both legs (grid orders are limit/maker). No taker, no slippage, no
  partial fills, no order-book depth.
- Fund reservation: a BUY reserves USD, a SELL reserves BTC. A SELL cannot be
  placed without backing inventory (mirrors the engine's InsufficientBalance
  refusal) — so the grid is long-biased exactly like production.
- Re-anchor MODELS the operator's manual re-anchor (the engine never
  auto-re-anchors; ADR-006 = park when offside). When price leaves the band by
  >= `reanchor_margin` spacings, cancel all + re-lay at the current price.
  IDENTICAL across every spacing arm — apples-to-apples.
- Seeded two-sided start: begins with USD for the buy side AND enough BTC to
  back the sell side (a running/seeded grid — the steady state we care about,
  not a cold USD-only start that cannot sell).
- Safety caps (max exposure / daily spend) NOT modeled — they'd only reduce
  activity; this measures the raw strategy. Grid SIZE matches the operator's
  config (levels + order size).

Validation: at 1.0% spacing, per-cycle net should land near the operator's
observed ~$0.048 (1% gross - 2x0.26% maker = 0.48% of a $10 order). The harness
prints per-cycle net; run a flat/ranging window to check it (inventory drift ~0
there, so portfolio P&L ~ realized cycle profit).

Usage:
    python -m tools.grid_backtest --csv data/kraken-history/XBTUSD_1.csv \
        --start 2022-05-01 --end 2022-07-01 --label "2022 bear"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from wobblebot.domain.grid import (
    compute_grid_levels,
    grid_spacing,
    next_counter_action,
)
from wobblebot.domain.value_objects import OrderSide

_ZERO = Decimal("0")


@dataclass
class _Order:
    side: OrderSide
    price: Decimal
    amount: Decimal  # base (BTC)
    is_counter: bool  # False = initial-layout order; True = placed in response to a fill


@dataclass
class RunResult:  # pylint: disable=too-many-instance-attributes
    spacing_pct: float
    start_value: Decimal
    end_value: Decimal
    buy_fills: int = 0
    sell_fills: int = 0
    fees_paid: Decimal = _ZERO
    reanchors: int = 0
    end_usd: Decimal = _ZERO
    end_btc: Decimal = _ZERO
    stand_down_frac: float = 0.0  # fraction of bars spent standing down (trend filter)

    @property
    def pnl(self) -> Decimal:
        return self.end_value - self.start_value

    @property
    def cycles(self) -> int:
        # A completed round trip needs one buy AND one sell fill; the lesser
        # side bounds the count. Robust + definition-agnostic.
        return min(self.buy_fills, self.sell_fills)

    @property
    def per_cycle_net(self) -> Decimal:
        return self.pnl / self.cycles if self.cycles else _ZERO


@dataclass
class _Sim:  # pylint: disable=too-many-instance-attributes
    spacing_pct: Decimal
    levels_above: int
    levels_below: int
    order_size_usd: Decimal
    maker_fee: Decimal
    reanchor_margin: Decimal  # re-anchor when price is this many spacings beyond an edge
    free_usd: Decimal = _ZERO
    free_btc: Decimal = _ZERO
    taker_fee: Decimal = _ZERO  # used only by the cash de-risk market sell
    anchor: Decimal = _ZERO
    open_orders: list[_Order] = field(default_factory=list)
    buy_fills: int = 0
    sell_fills: int = 0
    fees_paid: Decimal = _ZERO
    reanchors: int = 0
    # cached on _lay_grid so the per-bar hot loop never rebuilds GridLevels
    cur_spacing: Decimal = _ZERO
    low_edge: Decimal = _ZERO
    high_edge: Decimal = _ZERO

    # --- placement (reuses production geometry for layout/counter direction) ---

    def _place(self, side: OrderSide, price: Decimal, amount: Decimal, is_counter: bool) -> bool:
        if side is OrderSide.BUY:
            cost = price * amount
            if self.free_usd < cost:
                return False
            self.free_usd -= cost
        else:
            if self.free_btc < amount:
                return False
            self.free_btc -= amount
        self.open_orders.append(
            _Order(side=side, price=price, amount=amount, is_counter=is_counter)
        )
        return True

    def _cancel_all(self) -> None:
        for o in self.open_orders:
            if o.side is OrderSide.BUY:
                self.free_usd += o.price * o.amount  # refund USD reservation
            else:
                self.free_btc += o.amount  # refund BTC reservation
        self.open_orders.clear()

    def _derisk_to_cash(self, price: Decimal) -> None:
        """Defensive de-risk: cancel all, then market-SELL all BTC inventory to
        USD at the current price (taker fee). Caps downside but realizes the
        position — the opposite of pause-and-hold."""
        self._cancel_all()  # refunds reservations; all BTC now sits in free_btc
        if self.free_btc > _ZERO:
            proceeds = self.free_btc * price
            fee = proceeds * self.taker_fee
            self.fees_paid += fee
            self.free_usd += proceeds - fee
            self.sell_fills += 1
            self.free_btc = _ZERO

    def _lay_grid(self, anchor: Decimal) -> None:
        self.anchor = anchor
        levels = compute_grid_levels(
            reference_price=anchor,
            spacing_percentage=self.spacing_pct,
            levels_above=self.levels_above,
            levels_below=self.levels_below,
        )
        # Cache scalars the per-bar loop needs (mirrors grid_spacing / is_offside)
        # so the hot path never reconstructs Pydantic GridLevel objects.
        self.cur_spacing = grid_spacing(anchor, self.spacing_pct)
        self.low_edge = levels[0].price
        self.high_edge = levels[-1].price
        for level in levels:
            amount = self.order_size_usd / level.price
            self._place(level.side, level.price, amount, is_counter=False)

    # --- per-bar fill processing ---

    def process_bar(self, high: Decimal, low: Decimal) -> None:
        spacing = self.cur_spacing
        # Snapshot: counters placed this bar are NOT eligible until next bar.
        snapshot = list(self.open_orders)
        for o in snapshot:
            touched = (o.side is OrderSide.BUY and low <= o.price) or (
                o.side is OrderSide.SELL and high >= o.price
            )
            if not touched:
                continue
            self.open_orders.remove(o)
            notional = o.price * o.amount
            fee = notional * self.maker_fee
            self.fees_paid += fee
            if o.side is OrderSide.BUY:
                self.buy_fills += 1
                self.free_btc += o.amount  # USD was reserved at placement; now hold BTC
                self.free_usd -= fee
            else:
                self.sell_fills += 1
                self.free_usd += notional - fee  # BTC was reserved; now hold USD
            counter = next_counter_action(o.side, o.price, spacing)
            self._place(counter.side, counter.price, o.amount, is_counter=True)

    def maybe_reanchor(self, price: Decimal) -> None:
        if not self.open_orders:
            return
        # Inline offside on cached edges (mirrors domain.is_offside) — keeps the
        # per-bar path free of Pydantic GridLevel construction.
        if self.low_edge <= price <= self.high_edge:
            return
        beyond = (self.low_edge - price) if price < self.low_edge else (price - self.high_edge)
        if beyond < self.reanchor_margin * self.cur_spacing:
            return
        self._cancel_all()
        self._lay_grid(price)
        self.reanchors += 1

    def portfolio_value(self, price: Decimal) -> Decimal:
        total_usd = self.free_usd + sum(
            (o.price * o.amount for o in self.open_orders if o.side is OrderSide.BUY), _ZERO
        )
        total_btc = self.free_btc + sum(
            (o.amount for o in self.open_orders if o.side is OrderSide.SELL), _ZERO
        )
        return total_usd + total_btc * price


def _load_ohlc(
    path: Path, start: datetime | None, end: datetime | None
) -> list[tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Stream a Kraken 1m OHLCVT CSV → [(open, high, low, close)] in [start, end)."""
    lo = int(start.timestamp()) if start else None
    hi = int(end.timestamp()) if end else None
    bars: list[tuple[int, Decimal, Decimal, Decimal, Decimal]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            c = line.split(",")
            ts = int(c[0])
            if (lo is not None and ts < lo) or (hi is not None and ts >= hi):
                continue
            bars.append((ts, Decimal(c[1]), Decimal(c[2]), Decimal(c[3]), Decimal(c[4])))
    bars.sort(key=lambda b: b[0])
    return [(o, h, low, cl) for _, o, h, low, cl in bars]


def run_sim(  # pylint: disable=too-many-arguments,too-many-locals
    bars: list[tuple[Decimal, Decimal, Decimal, Decimal]],
    *,
    spacing_pct: Decimal,
    levels_above: int,
    levels_below: int,
    order_size_usd: Decimal,
    maker_fee: Decimal,
    reanchor_margin: Decimal,
    defense: str = "none",
    taker_fee: Decimal = Decimal("0.0040"),
    trend_window_bars: int = 4320,
    trend_down_frac: Decimal = Decimal("0.05"),
    trend_check_bars: int = 360,
) -> RunResult:
    """Run one fixed-spacing grid over the bars. Seed two-sided (USD for buys,
    BTC for sells) with 2x buffer so initial reservations never fail.

    ``defense`` selects the downtrend guardrail (re-checked every
    ``trend_check_bars``; triggers when trailing drift over ``trend_window_bars``
    falls below ``-trend_down_frac``; resumes/re-lays once drift turns positive —
    hysteresis rides out whipsaws):
    - ``none``  — no defense (plain grid).
    - ``pause`` — cancel all + HOLD inventory, no trading (still MtM-bleeds).
    - ``cash``  — cancel all + market-SELL inventory to USD (``taker_fee``),
      capping downside but realizing the position and risking a buy-back-higher
      whipsaw if the signal was a false positive."""
    closes = [b[3] for b in bars]
    anchor = bars[0][0]  # first bar's open
    sim = _Sim(
        spacing_pct=spacing_pct,
        levels_above=levels_above,
        levels_below=levels_below,
        order_size_usd=order_size_usd,
        maker_fee=maker_fee,
        reanchor_margin=reanchor_margin,
        taker_fee=taker_fee,
        free_usd=order_size_usd * Decimal(levels_below) * Decimal("2"),
        free_btc=(order_size_usd * Decimal(levels_above) * Decimal("2")) / anchor,
    )
    start_value = sim.portfolio_value(anchor)
    sim._lay_grid(anchor)  # pylint: disable=protected-access
    defensive = False
    stand_down_bars = 0
    for i, (_open, high, low, close) in enumerate(bars):
        if defense != "none" and i >= trend_window_bars and i % trend_check_bars == 0:
            ref = closes[i - trend_window_bars]
            drift = (close - ref) / ref if ref else _ZERO
            if not defensive and drift < -trend_down_frac:
                if defense == "cash":
                    sim._derisk_to_cash(close)  # pylint: disable=protected-access
                else:
                    sim._cancel_all()  # pylint: disable=protected-access
                defensive = True
            elif defensive and drift > _ZERO:
                defensive = False
                sim._lay_grid(close)  # pylint: disable=protected-access
        if defensive:
            stand_down_bars += 1
            continue
        sim.process_bar(high, low)
        sim.maybe_reanchor(close)
    end_price = bars[-1][3]
    return RunResult(
        spacing_pct=float(spacing_pct),
        start_value=start_value,
        end_value=sim.portfolio_value(end_price),
        buy_fills=sim.buy_fills,
        sell_fills=sim.sell_fills,
        fees_paid=sim.fees_paid,
        reanchors=sim.reanchors,
        end_usd=sim.free_usd,
        end_btc=sim.free_btc,
        stand_down_frac=stand_down_bars / len(bars),
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--start", default=None, help="ISO-8601 UTC window start")
    p.add_argument("--end", default=None, help="ISO-8601 UTC window end")
    p.add_argument("--label", default=None)
    p.add_argument("--spacings", default="0.65,1.0,1.5,2.0", help="comma list of spacing %%")
    p.add_argument("--levels-above", type=int, default=3)
    p.add_argument("--levels-below", type=int, default=3)
    p.add_argument("--order-size", type=Decimal, default=Decimal("10"))
    p.add_argument("--maker-fee", type=Decimal, default=Decimal("0.0026"))
    p.add_argument("--reanchor-margin", type=Decimal, default=Decimal("1.0"))
    p.add_argument(
        "--defense",
        choices=("none", "pause", "cash"),
        default="none",
        help="downtrend defense: none / pause+hold / sell-to-cash",
    )
    p.add_argument("--taker-fee", type=Decimal, default=Decimal("0.0040"), help="cash de-risk fee")
    p.add_argument("--trend-window-hours", type=float, default=72.0, help="trailing drift window")
    p.add_argument(
        "--trend-down-pct", type=Decimal, default=Decimal("5.0"), help="stand-down drift"
    )
    p.add_argument("--trend-check-hours", type=float, default=6.0, help="re-evaluate cadence")
    return p


def _iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def main() -> int:
    args = _build_parser().parse_args()
    bars = _load_ohlc(args.csv, _iso(args.start), _iso(args.end))
    if len(bars) < 2:
        print(f"!! only {len(bars)} bars in window")
        return 1
    spacings = [Decimal(s.strip()) for s in args.spacings.split(",")]

    print("# grid backtest (Tier 2 — fixed-spacing sweep; MODEL, see docstring caveats)")
    if args.label:
        print(f"# regime:      {args.label}")
    print(
        f"# bars:        {len(bars)}  (1m)  "
        f"start ${float(bars[0][0]):,.0f} -> end ${float(bars[-1][3]):,.0f}"
    )
    print(
        f"# grid:        {args.levels_below} buys + {args.levels_above} sells  "
        f"${args.order_size}/order  maker {float(args.maker_fee)*100:.2f}%  "
        f"reanchor>{args.reanchor_margin}xspacing"
    )
    twin = int(args.trend_window_hours * 60)
    tchk = max(1, int(args.trend_check_hours * 60))
    tdown = args.trend_down_pct / Decimal("100")
    if args.defense != "none":
        verb = "SELL TO CASH" if args.defense == "cash" else "PAUSE + HOLD"
        print(
            f"# defense:     {verb} when {args.trend_window_hours:.0f}h drift < "
            f"-{args.trend_down_pct}% (check every {args.trend_check_hours:.0f}h); resume on +drift"
        )
    print()
    header = (
        f"{'spacing':>8} {'net P&L':>10} {'P&L %':>7} {'cycles':>7} "
        f"{'buy/sell':>10} {'fees':>9} {'reanch':>7} {'down%':>6} {'$/cycle':>9}"
    )
    print(header)
    print("-" * len(header))
    results: list[RunResult] = []
    for sp in spacings:
        r = run_sim(
            bars,
            spacing_pct=sp,
            levels_above=args.levels_above,
            levels_below=args.levels_below,
            order_size_usd=args.order_size,
            maker_fee=args.maker_fee,
            reanchor_margin=args.reanchor_margin,
            defense=args.defense,
            taker_fee=args.taker_fee,
            trend_window_bars=twin,
            trend_down_frac=tdown,
            trend_check_bars=tchk,
        )
        results.append(r)
        pnl_pct = (r.pnl / r.start_value * Decimal("100")) if r.start_value else _ZERO
        print(
            f"{r.spacing_pct:>7.2f}% {float(r.pnl):>10.4f} {float(pnl_pct):>6.2f}% "
            f"{r.cycles:>7} {f'{r.buy_fills}/{r.sell_fills}':>10} {float(r.fees_paid):>9.4f} "
            f"{r.reanchors:>7} {100*r.stand_down_frac:>5.1f}% {float(r.per_cycle_net):>9.4f}"
        )
    best = max(results, key=lambda r: r.pnl)
    print()
    print(f"best spacing this regime: {best.spacing_pct:.2f}%  (net ${float(best.pnl):.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
