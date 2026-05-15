"""One-shot live test of the KrakenAdapter trading methods with real money.

**This script places real orders and moves real funds.** Never run it
unless you mean to. There is no ``--dry-run`` flag — for that, use
``python -m wobblebot.cli.validate``.

Two sub-experiments back-to-back, each with a hard-cap abort path:

**Experiment A: zero-fill BUY + cancel.** Places a LIMIT BUY for $10
worth of BTC at 50% of last price (deeply out of the money — won't
fill on any sane market). Verifies the order appears in OpenOrders /
QueryOrders, then cancels. Expected money movement: $0 (USD locked
then released).

**Experiment B: marketable round-trip.** Places a LIMIT BUY at
last_price * 1.01 (aggressively crossing the spread) for $10 worth.
Should fill at the ask within seconds. Verifies the trade record
appears in TradesHistory. Then places a LIMIT SELL at last_price *
0.99 for the exact BTC amount bought, at the post-BUY price. Should
fill at the bid. Verifies the sell trade. Expected loss: spread + 2x
~0.26% fee = roughly $0.05-$0.10.

**Hard caps:**

- Max single order USD: $15 (configurable via ``--max-order-usd``).
- Max total session loss: $5 (configurable via ``--max-loss-usd``).
  Computed from the post-test USD balance vs the start-of-test
  balance.
- Price jitter abort: if last_price moves more than 5% (configurable
  via ``--max-price-drift-pct``) between BUY and SELL in Experiment B,
  abort before placing the SELL.
- Order fill timeout: if a marketable order doesn't fill in 5s
  (configurable via ``--fill-timeout-seconds``), abort.
- Always-on cleanup: any order still open at script exit is cancelled
  in the ``finally`` block.

Loads credentials from ``KRAKEN_TRADE_API_KEY`` /
``KRAKEN_TRADE_API_SECRET`` (the operator's separate trade key, not
the read-only key).

Logs every step in JSON to stderr AND appends to
``data/first_real_trade_<timestamp>.jsonl`` for permanent record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.logging import configure_logging
from wobblebot.domain.models import Order
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import WobbleBotPortError

BTC_USD = Symbol(base="BTC", quote="USD")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class JsonLineSink(logging.Handler):
    """Append each log record as a JSON line to a file. Lossless forensic record."""

    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.DEBUG)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            for k, v in record.__dict__.items():
                if k.startswith("_"):
                    continue
                if k in {
                    "args",
                    "asctime",
                    "created",
                    "exc_info",
                    "exc_text",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "message",
                    "module",
                    "msecs",
                    "msg",
                    "name",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "stack_info",
                    "thread",
                    "threadName",
                    "taskName",
                }:
                    continue
                payload[k] = v
            self._fh.write(json.dumps(payload, default=str) + "\n")
            self._fh.flush()
        except Exception:  # pylint: disable=broad-except
            self.handleError(record)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


async def _wait_for_fill(
    adapter: KrakenAdapter,
    order: Order,
    logger: logging.Logger,
    timeout_seconds: float,
) -> Order:
    """Poll get_order_status every 500ms until status is terminal or timeout.

    Returns the final order. Raises TimeoutError if it doesn't reach a
    terminal state in time."""
    start = time.monotonic()
    polls = 0
    while time.monotonic() - start < timeout_seconds:
        polls += 1
        refreshed = await adapter.get_order_status(order)
        logger.info(
            "fill poll",
            extra={
                "exchange_id": refreshed.exchange_id,
                "status": refreshed.status,
                "filled_amount": str(refreshed.filled_amount),
                "poll": polls,
                "elapsed_s": round(time.monotonic() - start, 3),
            },
        )
        if refreshed.status in ("closed", "canceled", "expired"):
            return refreshed
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Order {order.exchange_id} did not reach terminal state in {timeout_seconds}s"
    )


async def _experiment_a(
    adapter: KrakenAdapter,
    last_price: Decimal,
    order_size_usd: Decimal,
    logger: logging.Logger,
) -> dict[str, object]:
    """Zero-fill BUY at 50% of market, then cancel. Expected $0 movement."""
    logger.info("=== Experiment A: zero-fill cancellation cycle ===")
    far_from_market = last_price / Decimal("2")  # 50% of last
    volume = order_size_usd / far_from_market

    order = Order(
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=far_from_market, currency="USD"),
        amount=Amount(value=volume, asset="BTC"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    logger.info(
        "A.1 placing far-from-market BUY",
        extra={
            "intended_price": str(far_from_market),
            "intended_volume": str(volume),
            "intended_cost_usd": str(order_size_usd),
        },
    )
    placed = await adapter.place_order(order)
    logger.info(
        "A.2 placed",
        extra={
            "exchange_id": placed.exchange_id,
            "status": placed.status,
            "actual_price": str(placed.price.amount),
            "actual_volume": str(placed.amount.value),
        },
    )

    await asyncio.sleep(1.0)

    open_now = await adapter.get_open_orders(symbol=BTC_USD)
    matching = [o for o in open_now if o.exchange_id == placed.exchange_id]
    logger.info(
        "A.3 OpenOrders snapshot",
        extra={
            "total_open_for_symbol": len(open_now),
            "our_order_visible": len(matching) == 1,
        },
    )
    if not matching:
        raise RuntimeError(
            f"Order {placed.exchange_id} not found in OpenOrders — Kraken may have rejected it"
        )

    queried = await adapter.get_order_status(placed)
    logger.info(
        "A.4 QueryOrders snapshot",
        extra={
            "exchange_id": queried.exchange_id,
            "status": queried.status,
            "filled_amount": str(queried.filled_amount),
        },
    )

    canceled = await adapter.cancel_order(placed)
    logger.info(
        "A.5 cancelled",
        extra={"exchange_id": canceled.exchange_id, "status": canceled.status},
    )

    open_after = await adapter.get_open_orders(symbol=BTC_USD)
    still_there = [o for o in open_after if o.exchange_id == placed.exchange_id]
    logger.info(
        "A.6 OpenOrders post-cancel",
        extra={
            "total_open_for_symbol": len(open_after),
            "our_order_still_present": len(still_there) > 0,
        },
    )
    if still_there:
        raise RuntimeError(f"Order {placed.exchange_id} still in OpenOrders after cancel")

    logger.info("=== Experiment A complete: no money moved ===")
    return {"experiment": "A", "exchange_id": placed.exchange_id, "outcome": "ok"}


async def _experiment_b(
    adapter: KrakenAdapter,
    order_size_usd: Decimal,
    fill_timeout: float,
    max_drift_pct: Decimal,
    logger: logging.Logger,
) -> dict[str, object]:
    """Marketable BUY then marketable SELL. Expected ~$0.05-$0.10 loss."""
    logger.info("=== Experiment B: marketable round-trip ===")

    # ---- BUY leg ----
    pre_buy_price = (await adapter.get_current_price(BTC_USD)).amount
    buy_limit = pre_buy_price * Decimal("1.01")  # 1% above last; aggressively marketable
    buy_volume = order_size_usd / pre_buy_price  # size for the *expected* fill price

    buy_order = Order(
        symbol=BTC_USD,
        side=OrderSide.BUY,
        price=Price(amount=buy_limit, currency="USD"),
        amount=Amount(value=buy_volume, asset="BTC"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    logger.info(
        "B.1 placing marketable BUY",
        extra={
            "pre_buy_last_price": str(pre_buy_price),
            "buy_limit_price": str(buy_limit),
            "intended_volume": str(buy_volume),
            "intended_cost_usd_at_last": str(order_size_usd),
        },
    )
    placed_buy = await adapter.place_order(buy_order)
    logger.info(
        "B.2 BUY placed",
        extra={
            "exchange_id": placed_buy.exchange_id,
            "actual_price_submitted": str(placed_buy.price.amount),
            "actual_volume_submitted": str(placed_buy.amount.value),
        },
    )

    filled_buy = await _wait_for_fill(adapter, placed_buy, logger, fill_timeout)
    if filled_buy.status != "closed":
        raise RuntimeError(
            f"BUY did not fill — terminal status was {filled_buy.status!r}; aborting"
        )

    # Find the trade record for this BUY.
    recent_trades = await adapter.get_trade_history(symbol=BTC_USD, limit=10)
    buy_trades = [t for t in recent_trades if t.order_id == filled_buy.exchange_id]
    if not buy_trades:
        raise RuntimeError(f"No trade record found for BUY {filled_buy.exchange_id}")
    btc_acquired = sum((t.amount.value for t in buy_trades), Decimal("0"))
    usd_spent = sum((t.cost + t.fee for t in buy_trades), Decimal("0"))
    avg_buy_price = (
        sum((t.price.amount * t.amount.value for t in buy_trades), Decimal("0")) / btc_acquired
    )
    logger.info(
        "B.3 BUY filled",
        extra={
            "trade_count": len(buy_trades),
            "btc_acquired": str(btc_acquired),
            "usd_spent_incl_fee": str(usd_spent),
            "avg_buy_price": str(avg_buy_price),
            "trades": [
                {
                    "trade_id": t.id,
                    "price": str(t.price.amount),
                    "volume": str(t.amount.value),
                    "cost": str(t.cost),
                    "fee": str(t.fee),
                }
                for t in buy_trades
            ],
        },
    )

    # ---- Drift check before SELL ----
    pre_sell_price = (await adapter.get_current_price(BTC_USD)).amount
    drift_pct = abs(pre_sell_price - avg_buy_price) / avg_buy_price * Decimal("100")
    logger.info(
        "B.4 pre-SELL drift check",
        extra={
            "avg_buy_price": str(avg_buy_price),
            "current_price": str(pre_sell_price),
            "drift_pct": str(drift_pct),
            "max_drift_pct": str(max_drift_pct),
        },
    )
    if drift_pct > max_drift_pct:
        raise RuntimeError(
            f"Price drifted {drift_pct:.2f}% (>{max_drift_pct}%) since BUY; "
            f"aborting before SELL — manual cleanup needed for {btc_acquired} BTC"
        )

    # ---- SELL leg ----
    sell_limit = pre_sell_price * Decimal("0.99")  # 1% below last; aggressively marketable
    sell_order = Order(
        symbol=BTC_USD,
        side=OrderSide.SELL,
        price=Price(amount=sell_limit, currency="USD"),
        amount=Amount(value=btc_acquired, asset="BTC"),
        created_at=Timestamp(dt=datetime.now(UTC)),
    )
    logger.info(
        "B.5 placing marketable SELL",
        extra={
            "pre_sell_last_price": str(pre_sell_price),
            "sell_limit_price": str(sell_limit),
            "intended_volume": str(btc_acquired),
        },
    )
    placed_sell = await adapter.place_order(sell_order)
    logger.info(
        "B.6 SELL placed",
        extra={
            "exchange_id": placed_sell.exchange_id,
            "actual_volume_submitted": str(placed_sell.amount.value),
        },
    )

    filled_sell = await _wait_for_fill(adapter, placed_sell, logger, fill_timeout)
    if filled_sell.status != "closed":
        raise RuntimeError(
            f"SELL did not fill — terminal status was {filled_sell.status!r}; "
            f"manual cleanup needed for {btc_acquired} BTC"
        )

    recent_trades = await adapter.get_trade_history(symbol=BTC_USD, limit=10)
    sell_trades = [t for t in recent_trades if t.order_id == filled_sell.exchange_id]
    if not sell_trades:
        raise RuntimeError(f"No trade record found for SELL {filled_sell.exchange_id}")
    btc_sold = sum((t.amount.value for t in sell_trades), Decimal("0"))
    usd_received = sum((t.cost - t.fee for t in sell_trades), Decimal("0"))
    avg_sell_price = (
        sum((t.price.amount * t.amount.value for t in sell_trades), Decimal("0")) / btc_sold
    )
    logger.info(
        "B.7 SELL filled",
        extra={
            "trade_count": len(sell_trades),
            "btc_sold": str(btc_sold),
            "usd_received_net_fee": str(usd_received),
            "avg_sell_price": str(avg_sell_price),
            "trades": [
                {
                    "trade_id": t.id,
                    "price": str(t.price.amount),
                    "volume": str(t.amount.value),
                    "cost": str(t.cost),
                    "fee": str(t.fee),
                }
                for t in sell_trades
            ],
        },
    )

    cycle_pnl = usd_received - usd_spent
    logger.info(
        "=== Experiment B complete ===",
        extra={
            "btc_acquired": str(btc_acquired),
            "btc_sold": str(btc_sold),
            "btc_residual": str(btc_acquired - btc_sold),
            "usd_spent_incl_fee": str(usd_spent),
            "usd_received_net_fee": str(usd_received),
            "cycle_pnl_usd": str(cycle_pnl),
            "cycle_pnl_pct": str(cycle_pnl / usd_spent * Decimal("100")),
        },
    )
    return {
        "experiment": "B",
        "buy_order": filled_buy.exchange_id,
        "sell_order": filled_sell.exchange_id,
        "cycle_pnl_usd": str(cycle_pnl),
        "outcome": "ok",
    }


async def _cleanup_open_orders(adapter: KrakenAdapter, logger: logging.Logger) -> None:
    """Cancel any open orders we might have left behind."""
    try:
        opens = await adapter.get_open_orders(symbol=BTC_USD)
    except WobbleBotPortError as exc:
        logger.error("cleanup get_open_orders failed", extra={"error": str(exc)})
        return
    if not opens:
        logger.info("cleanup: no open BTC/USD orders to cancel")
        return
    logger.warning(
        "cleanup: cancelling residual open orders",
        extra={"count": len(opens), "ids": [o.exchange_id for o in opens]},
    )
    for o in opens:
        try:
            await adapter.cancel_order(o)
            logger.info("cleanup cancelled", extra={"exchange_id": o.exchange_id})
        except WobbleBotPortError as exc:
            logger.error(
                "cleanup cancel failed",
                extra={"exchange_id": o.exchange_id, "error": str(exc)},
            )


async def _run(
    order_size_usd: Decimal,
    max_order_usd: Decimal,
    max_loss_usd: Decimal,
    fill_timeout: float,
    max_drift_pct: Decimal,
    skip_a: bool,
    skip_b: bool,
    log_path: Path,
) -> int:
    if order_size_usd > max_order_usd:
        sys.stderr.write(
            f"error: --order-size {order_size_usd} > --max-order-usd {max_order_usd}\n"
        )
        return 2

    configure_logging(log_format="json")
    logger = logging.getLogger("wobblebot.tools.first_real_trade")
    sink = JsonLineSink(log_path)
    logging.getLogger("wobblebot").addHandler(sink)

    logger.info(
        "session start",
        extra={
            "started_at": _now_iso(),
            "order_size_usd": str(order_size_usd),
            "max_order_usd": str(max_order_usd),
            "max_loss_usd": str(max_loss_usd),
            "fill_timeout_seconds": fill_timeout,
            "max_drift_pct": str(max_drift_pct),
            "skip_a": skip_a,
            "skip_b": skip_b,
            "log_path": str(log_path),
        },
    )

    try:
        config = KrakenConfig.from_env(
            key_var="KRAKEN_TRADE_API_KEY",
            secret_var="KRAKEN_TRADE_API_SECRET",
        )
    except ValueError as exc:
        logger.error("missing trade credentials", extra={"error": str(exc)})
        return 2

    adapter = KrakenAdapter(config=config)
    try:
        last_price = (await adapter.get_current_price(BTC_USD)).amount
        usd_before_balance = await adapter.get_balance("USD")
        usd_before = usd_before_balance.total if usd_before_balance else Decimal("0")
        btc_before_balance = await adapter.get_balance("BTC")
        btc_before = btc_before_balance.total if btc_before_balance else Decimal("0")
        logger.info(
            "pre-test state",
            extra={
                "last_price_btc_usd": str(last_price),
                "usd_total": str(usd_before),
                "btc_total": str(btc_before),
            },
        )

        results: list[dict[str, object]] = []

        if not skip_a:
            results.append(await _experiment_a(adapter, last_price, order_size_usd, logger))
        else:
            logger.info("Experiment A skipped via --skip-a")

        if not skip_b:
            results.append(
                await _experiment_b(adapter, order_size_usd, fill_timeout, max_drift_pct, logger)
            )
        else:
            logger.info("Experiment B skipped via --skip-b")

        usd_after_balance = await adapter.get_balance("USD")
        usd_after = usd_after_balance.total if usd_after_balance else Decimal("0")
        btc_after_balance = await adapter.get_balance("BTC")
        btc_after = btc_after_balance.total if btc_after_balance else Decimal("0")
        usd_delta = usd_after - usd_before
        btc_delta = btc_after - btc_before
        logger.info(
            "post-test state",
            extra={
                "usd_total": str(usd_after),
                "btc_total": str(btc_after),
                "usd_delta": str(usd_delta),
                "btc_delta": str(btc_delta),
                "results": results,
            },
        )

        if usd_delta < -max_loss_usd:
            logger.error(
                "session loss exceeded --max-loss-usd",
                extra={"loss": str(-usd_delta), "limit": str(max_loss_usd)},
            )
            return 1

        logger.info(
            "=== ALL EXPERIMENTS COMPLETE ===",
            extra={
                "usd_delta": str(usd_delta),
                "btc_delta": str(btc_delta),
                "verdict": "ok",
            },
        )
        return 0
    except (WobbleBotPortError, RuntimeError, TimeoutError) as exc:
        logger.error(
            "session aborted",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return 1
    finally:
        await _cleanup_open_orders(adapter, logger)
        await adapter.aclose()
        sink.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-size", type=Decimal, default=Decimal("10"))
    parser.add_argument("--max-order-usd", type=Decimal, default=Decimal("15"))
    parser.add_argument("--max-loss-usd", type=Decimal, default=Decimal("5"))
    parser.add_argument("--fill-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-price-drift-pct", type=Decimal, default=Decimal("5"))
    parser.add_argument("--skip-a", action="store_true")
    parser.add_argument("--skip-b", action="store_true")
    args = parser.parse_args()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path("data") / f"first_real_trade_{timestamp}.jsonl"

    return asyncio.run(
        _run(
            order_size_usd=args.order_size,
            max_order_usd=args.max_order_usd,
            max_loss_usd=args.max_loss_usd,
            fill_timeout=args.fill_timeout_seconds,
            max_drift_pct=args.max_price_drift_pct,
            skip_a=args.skip_a,
            skip_b=args.skip_b,
            log_path=log_path,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
