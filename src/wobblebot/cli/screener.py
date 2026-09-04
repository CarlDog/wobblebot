"""Grid-suitability screener — rank observed symbols (P2 slice 5).

Run as a module::

    python -m wobblebot.cli.screener
    python -m wobblebot.cli.screener --symbols BTC/USD,SOL/USD --lookback-days 60

**Read-only, offline.** v1 needs no Kraken credentials at all: every
metric computes from the observe DB's stored 60m bars (import or
backfill puts them there). Ranks the cohort by grid-suitability —
volatility and ATR% scored as distance from the configured band
centers (too quiet never cycles, too hot trips caps), flatness ranked
descending — and annotates each candidate with its strongest |Pearson|
correlation against the configured live lineup. Correlation is an
annotation, never a rank factor.

The screener RECOMMENDS; it never trades and never edits config
(ADR-002 — the operator gates every symbol addition). Output is a log
table; there is deliberately no DB table and no web page in v1 (per
the P2 blueprint, overriding the older adaptive-grid.md sketch).

v1.5 (recorded, not built): add bid-ask-spread-vs-fee via the existing
``get_ticker`` (needs the READER key) and volume once ``Ticker``
carries it. v2: RSI/ADX/BB refinement.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from datetime import UTC, datetime, timedelta

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    CONFIG_LOAD_ERRORS,
    add_config_args,
    collect_overrides,
    config_load_exit,
    identity,
    load_operator_env,
    missing_section_exit,
    parse_symbol_csv,
)
from wobblebot.config.cli import ScreenerConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import OHLCBar, Symbol
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.screener import (
    ScreenerRanking,
    SymbolMetrics,
    bar_returns,
    compute_symbol_metrics,
    max_abs_correlation,
    rank_candidates,
)

_LOGGER = logging.getLogger("wobblebot.cli.screener")


async def _load_window(
    storage: SQLiteStorageAdapter,
    symbol: Symbol,
    config: ScreenerConfig,
    *,
    now: datetime,
) -> list[OHLCBar]:
    since = now - timedelta(days=config.lookback_days)
    return await storage.get_ohlc_bars(symbol, config.interval_minutes, start_time=since)


async def _screen(  # pylint: disable=too-many-locals
    storage: SQLiteStorageAdapter,
    config: ScreenerConfig,
    *,
    symbols: list[Symbol],
    held: list[Symbol],
    now: datetime | None = None,
) -> tuple[list[ScreenerRanking], list[Symbol]]:
    """Metrics + ranks + correlation annotations for ``symbols``.

    Returns (rankings best-first, skipped-too-thin symbols). ``held``
    is the live lineup the correlation annotation compares against; a
    candidate never correlates against itself.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    metrics: list[SymbolMetrics] = []
    windows: dict[Symbol, list[OHLCBar]] = {}
    skipped: list[Symbol] = []
    for symbol in symbols:
        bars = await _load_window(storage, symbol, config, now=resolved_now)
        computed = compute_symbol_metrics(bars)
        if computed is None:
            skipped.append(symbol)
            continue
        windows[symbol] = bars
        metrics.append(computed)

    rankings = rank_candidates(
        metrics,
        vol_band_center=config.vol_band_center,
        atr_band_center_pct=config.atr_band_center_pct,
    )

    held_returns: dict[Symbol, dict[str, float]] = {}
    for symbol in held:
        held_bars = windows.get(symbol)
        if held_bars is None:
            held_bars = await _load_window(storage, symbol, config, now=resolved_now)
        returns = bar_returns(held_bars)
        if returns:
            held_returns[symbol] = returns

    annotated: list[ScreenerRanking] = []
    for ranking in rankings:
        candidate = ranking.metrics.symbol
        others = {s: r for s, r in held_returns.items() if s != candidate}
        best = max_abs_correlation(bar_returns(windows[candidate]), others)
        if best is not None:
            ranking = dataclasses.replace(ranking, max_correlation=best[0], correlated_with=best[1])
        annotated.append(ranking)
    return annotated, skipped


def _render(rankings: list[ScreenerRanking], skipped: list[Symbol], config: ScreenerConfig) -> None:
    """Log-table output (the blueprint's v1 surface — no DB, no web)."""
    _LOGGER.info(
        "screener: %d symbol(s) ranked over %dd of %dm bars "
        "(vol center %.3f%%, ATR center %.2f%%)",
        len(rankings),
        config.lookback_days,
        config.interval_minutes,
        config.vol_band_center * 100,
        config.atr_band_center_pct,
    )
    for position, r in enumerate(rankings, start=1):
        if r.max_correlation is not None and r.correlated_with is not None:
            corr = f"corr {r.max_correlation:+.2f} vs {r.correlated_with}"
        else:
            corr = "corr n/a"
        _LOGGER.info(
            "#%-2d %-10s composite %.2f | vol %.3f%% (r%d)  flat %.3f (r%d)  "
            "atr %.2f%% (r%d) | %s | %d bars",
            position,
            str(r.metrics.symbol),
            r.composite,
            r.metrics.volatility * 100,
            r.vol_rank,
            r.metrics.flatness,
            r.flatness_rank,
            r.metrics.atr_pct,
            r.atr_rank,
            corr,
            r.metrics.bar_count,
        )
    for symbol in skipped:
        _LOGGER.info("--  %-10s skipped: fewer than the minimum bars in the window", str(symbol))
    _LOGGER.info(
        "ranking is advisory (ADR-002): the operator gates every symbol addition; "
        "correlation is an annotation, not a rank factor"
    )


async def _run(
    config: ScreenerConfig, *, symbols_override: list[Symbol] | None, held: list[Symbol]
) -> int:
    storage = SQLiteStorageAdapter(config.db)
    try:
        await storage.connect()
    except StorageError as exc:
        _LOGGER.error("cannot open %s: %s", config.db, exc)
        return 2
    try:
        symbols = symbols_override
        if symbols is None:
            symbols = await storage.list_ohlc_symbols(config.interval_minutes)
        if not symbols:
            _LOGGER.error(
                "no %dm bar history in %s -- run tools/import_kraken_history.py or "
                "`cli/observe --backfill` first",
                config.interval_minutes,
                config.db,
            )
            return 1
        rankings, skipped = await _screen(storage, config, symbols=symbols, held=held)
        if not rankings:
            _LOGGER.error(
                "every candidate had fewer than the minimum bars in the last %dd window",
                config.lookback_days,
            )
            return 1
        _render(rankings, skipped, config)
        return 0
    except StorageError as exc:
        _LOGGER.error("storage read failed: %s", exc)
        return 1
    finally:
        await storage.close()


def main() -> int:
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated pairs to rank (default: every symbol with stored bars).",
    )
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    args = parser.parse_args()

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=collect_overrides(
                args,
                "screener",
                {
                    "lookback_days": ("lookback_days", identity),
                    "db": ("db", identity),
                    "log_format": ("log_format", identity),
                },
            ),
        )
    except CONFIG_LOAD_ERRORS as exc:
        return config_load_exit(exc)

    # Configure logging BEFORE the missing-section check, defaulting the
    # format when the section that would carry it is absent — the same
    # order every sibling CLI uses. Until 2026-08-15 this was the one CLI
    # of sixteen that reported a missing section via a raw stderr write
    # instead of the logger, because it read log_format from the section
    # it was about to find missing.
    log_format = config.screener.log_format if config.screener is not None else "plain"
    configure_logging(log_format=log_format)

    if config.screener is None:
        return missing_section_exit(_LOGGER, "screener")
    screener_config = config.screener

    symbols_override: list[Symbol] | None = None
    if args.symbols:
        symbols_override = [Symbol.from_string(s) for s in parse_symbol_csv(args.symbols)]
    held = list(config.live.symbols) if config.live is not None else []
    return asyncio.run(_run(screener_config, symbols_override=symbols_override, held=held))


if __name__ == "__main__":
    sys.exit(main())
