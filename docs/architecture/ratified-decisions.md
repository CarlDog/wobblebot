# Ratified Operational Decisions

This file records design decisions that have been **ratified — do not relitigate
without an ADR** — but that are operational/implementation-level rather than
cross-cutting architectural commitments. The formal architectural decisions live
in [`decisions.md`](decisions.md) (ADR-001 … ADR-031; ADR-020 deferred); the always-loaded summary
of layer rules and conventions lives in the repo-root `CLAUDE.md`.

These were previously inlined in `CLAUDE.md`'s Project Status section; they were
moved here to keep `CLAUDE.md` lean while preserving the record. The two most
load-bearing domain conventions (port error convention; the Pydantic mypy plugin
being load-bearing) are also surfaced in `CLAUDE.md`'s "Project-Specific
Conventions" because they apply to nearly every code change.

## Design decisions ratified during Phase 1 + Stage 2.1

### Domain / safety

- `Balance` is an immutable snapshot (`frozen=True`). Funds "locked for an order"
  come from Kraken's `hold_trade` (live) or are derived from the open-order set (mock).
- `OrderSide` is a `StrEnum` (`OrderSide.BUY`, `OrderSide.SELL`), not a Pydantic
  model. SQL drivers and JSON serialize it as the plain string value.
- **Port error convention:** domain-data miss returns `T | None`; protocol/transport
  failure raises the port's error type (`ExchangeError`, `StorageError`,
  `DataCollectorError`, etc. — all in `wobblebot.ports.exceptions`).
- `StoragePort` callers must serialize per-entity writes themselves (no optimistic
  concurrency control in the adapter).
- `Timestamp` normalizes all tz-aware inputs to UTC so ISO 8601 string ordering
  matches chronological ordering.
- **Pydantic mypy plugin** is enabled in `pyproject.toml` and load-bearing — do not remove.

### Kraken adapter (Stage 2.1)

- **DIY HMAC signing on `httpx`, not `python-kraken-sdk`.** SDK was considered and
  rejected: its only abstraction over httpx is signing + nonce + WebSocket; the REST
  interface is generic `client.request("POST", path)`, same manual parsing burden.
  ~20 lines of crypto, gold-cased against Kraken's published example signature.
- **`/0/private/BalanceEx`, not `/0/private/Balance`.** BalanceEx returns `hold_trade`
  per asset, mapping straight to `Balance.locked`.
- **Asset/symbol aliasing lives in the adapter, not the domain.** Module-level
  `_INTERNAL_TO_KRAKEN_ALTNAME` for colloquial conventions (BTC↔XBT, DOGE↔XDG).
  Legacy X/Z-prefixed response codes (XXBT, ZUSD) resolve via a lazy
  `/0/public/Assets` cache. `Symbol.to_kraken_format()` removed from the domain — it
  violated hex-layer rules and was broken.
- **`pytest -m 'not integration'` is the default** via pyproject `addopts`.
  Integration tests opt in with `pytest -m integration`.
- **`.env` loaded session-wide via `python-dotenv` in `tests/conftest.py`.** Unit
  tests still use `monkeypatch.setenv` for isolation.

## Stage 2.3 design decisions ratified

- **Dry-run = `validate=true`.** `KrakenAdapter(config, dry_run=True)` adds
  `validate=true` to every AddOrder request. Kraken validates auth + pair + precision
  + balance + ordermin + costmin without placing. The adapter synthesizes a
  `DRYRUN-<order.id>` exchange_id so the engine's bookkeeping path still works for
  diagnostic runs.
- **Per-pair precision quantization is mandatory.** AssetPairs cache (`pair_decimals`,
  `lot_decimals`, `ordermin`, `costmin`) populated lazily on first trading call.
  Price/volume rounded DOWN before submission — never up, since rounding up could push
  spending past the engine's intended `order_size_usd` budget.
- **Two separate Kraken keys, not one.** The read-only key (`cli/status`) and the trade
  key (`cli/preflight` / `cli/live`) live side-by-side in `.env`.
  `KrakenConfig.from_env(key_var=..., secret_var=...)` parameterizes which env vars to read.
- **Live taker fee is 0.40%, not the mock's 0.26%.** Discovered during the 2026-05-15
  first-trade test: $0.04 fee on each $9.99 leg of a marketable round-trip = 0.40%. The
  mock uses 0.26% (Kraken maker rate, conservative). The grid engine in normal operation
  places limit orders that sit on the book — those collect MAKER fees, so the mock's
  assumption is right *for the engine's normal mode*; the gap only shows up on marketable
  orders (which the engine doesn't normally place).
- **Cleanup discipline in the loop.** `cli/live`'s shutdown path cancels every open order
  for the symbol in a `finally` block, regardless of why the loop ended (signal, runtime
  cap, loss cap, exception). The session-end log records before/after USD balance, session
  PnL, cancellations succeeded/failed.

## Stage 2.4 design decisions ratified

- **Symbols step in series within a tick.** Per ADR-006 decision 5, the per-symbol
  asyncio.Lock makes parallelization safe — but at measured ~150ms per-symbol latency vs
  the 5s tick budget, even a 30-coin serial sweep finishes in well under one tick.
  Parallelization (asyncio.gather) deferred to Phase 5 hardening if profiling ever shows
  the master-task throughput is a bottleneck.
- **Per-symbol step errors are swallowed at the CLI layer.** One bad coin (network blip,
  Kraken returning EService:Unavailable) cannot kill the tick or the session. The engine
  surfaces the error; `_run_one_tick` logs it with structured fields and continues to the
  next symbol.
- **Caps split: total/daily are global, per-coin is per-symbol.** `max_total_exposure_usd`
  and `max_daily_spend_usd` count across every coin (computed via unfiltered
  `storage.get_open_orders()` / `storage.get_orders(side="buy", created_after=today)`).
  `max_per_coin_exposure_usd` and `max_orders_per_coin` are scoped to one symbol via the
  symbol filter. Same SafetyConfig instance passed to GridEngine; the engine's
  `_check_safety` was already symbol-aware.
- **`--symbols` deduplicates and preserves order.** Comma-separated input. Trailing/leading
  whitespace tolerated. Empty entries from trailing commas silently dropped.
