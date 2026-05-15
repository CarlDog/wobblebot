# Phase 2 — Closing Summary

**Status: ✅ Complete (2026-05-14).** All five Phase 2 stages closed in a single
focused evening session. Two real-money verifications on the operator's live
Kraken account confirm the entire trading stack works end-to-end. Total
real-money cost across both verifications: **$0.08**, well under the $10
session cap.

This document is the Stage 2.5 deliverable per the roadmap's "demonstrate
a full pipeline" charter. It consolidates the per-stage receipts, the
operator runbook, and the entry conditions for Phase 3.

## Per-stage outcomes

| Stage | Closed | Slices | Verification |
|---|---|---|---|
| 2.1 Kraken Adapter Read-Only + DataCollector v1 | 2026-05-14 | 4 | `cli/status` against live BTC/USD: real auth + signing + envelope parsing for `Ticker` / `BalanceEx` / `Assets` |
| 2.2 Micro-Grid Engine | 2026-05-14 | 5 | 1000-tick e2e oscillation against `MockExchangeAdapter`: 500 cycles, +$25 realized P&L, restart-resume preserves state |
| 2.3 Live Paper / Tiny-Size Mode | 2026-05-14 | 5 | `tools/first_real_trade.py` against live Kraken: zero-fill cancel + marketable round-trip; **−$0.08 actual cost**, 148ms fill latency |
| 2.4 Multi-Asset Support | 2026-05-14 | 3 | `cli/live --symbols BTC/USD,ETH/USD` validated against live Kraken (`cli/preflight` for each pair); 5 new multi-coin engine tests green |
| 2.5 Phase 2 Integration Check | 2026-05-14 | 1 (this doc) | 5-minute live multi-coin run: **$0.00 P&L**, 54 ticks per coin, 6/6 open orders cleanly cancelled on runtime-cap shutdown |

## Real-money receipts

### Receipt 1 — first live trade (2026-05-15 00:51 UTC)

`tools/first_real_trade.py`, two experiments, full forensic JSONL at
`data/first_real_trade_20260515T005140Z.jsonl` (gitignored, local only).

- **Experiment A — zero-fill cancellation cycle.** LIMIT BUY 0.000245 BTC
  at $40,779 (50% of market). Order `O53B3G-IX5Q2-YHHKXQ` placed,
  visible in `OpenOrders`, `QueryOrders` returned correct state, then
  cancelled cleanly. **$0 movement.**
- **Experiment B — marketable round-trip.** LIMIT BUY $10 worth at last+1%,
  filled in **148ms** at the actual ask ($81,558.10). Trade
  `T65YNT-VODM6-3Z3V3T`. Then LIMIT SELL the same 0.00012261 BTC at
  last−1%, filled in **148ms** at the bid ($81,558.00). Trade
  `TDGA5P-2AWYS-QJTST6`. Both legs paid 0.40% taker fee ($0.04 each).
  **−$0.08001 net.**

### Receipt 2 — live multi-coin grid run (2026-05-15 01:25 UTC)

`cli/live --symbols BTC/USD,ETH/USD --max-runtime-minutes 5
--max-session-loss-usd 5`. Forensic JSONL at `data/phase2-grid.log`
(gitignored).

- **Session: 304.6s, 54 ticks per coin, 0 fills.** Prices stayed within
  1% of init reference for both BTC ($81,466.70) and ETH ($2,288.17)
  for the entire 5-minute window — exactly the most-likely outcome at
  1% spacing on stable majors over 5 min.
- **Init landed cleanly.** 3 BUYs placed per coin (USD-funded, $30
  total commitment per coin = $60 across both); 3 SELL refusals per
  coin (operator's account holds zero BTC/ETH so SELL side raised
  `InsufficientBalance`, gracefully caught by the engine and logged
  with structured `asset/required/available` fields — see fix
  `27c7d8a`).
- **Cleanup verified end-to-end.** Runtime cap fired at 303.3s elapsed;
  the `finally` block cancelled all 6 open orders successfully (cancel
  IDs in the log), session-end log captured the exit_code=0.
- **Final state: USD $99.92 → USD $99.92.** Zero P&L, zero residual
  inventory, zero leftover open orders.

## What works end-to-end

The hex-architecture layering paid off across every stage. Each layer
was exercised independently first and then composed into the live
pipeline without needing changes to the layers below it.

- **Domain (`src/wobblebot/domain/`).** `Order` / `Trade` / `Balance`
  Pydantic models with Kraken-aligned vocabulary (per ADR-005); pure
  grid math (`compute_grid_levels`, `next_counter_action`, `is_offside`,
  `grid_spacing`); `GridState` and `GridSlot` immutable value objects.
  Zero imports from adapters/services/cli.
- **Ports (`src/wobblebot/ports/`).** `ExchangePort`, `StoragePort`,
  `DataCollectorPort`, `HarvesterPort`. Adapter-neutral by design;
  no Kraken naming leaks into the protocols.
- **Adapters (`src/wobblebot/adapters/`).** `KrakenAdapter` (DIY HMAC
  signing on `httpx`, lazy AssetPairs + Assets caches, dry-run mode
  via `validate=true`); `MockExchangeAdapter` (deterministic in-process
  matching for tests + simulator); `SQLiteStorageAdapter` (Decimal
  precision via TEXT, schema covers orders/trades/balances/grid_state).
- **Services (`src/wobblebot/services/`).** `GridEngine.step(symbol)` —
  per-symbol asyncio.Lock, fill detection via storage/exchange diff,
  safety cap enforcement, offside log signal, InsufficientBalance
  graceful refusal; `DataCollector` wrapping `ExchangePort` for read
  composition; `simulator.run_buy_dip_sell_rebound_cycle` for the Phase
  1 sandbox.
- **CLIs (`src/wobblebot/cli/`).** Five operator-facing entry points:
  `simulate` (Phase 1 mock), `check` (Stage 2.1 read), `validate`
  (Stage 2.3 dry-run), `grid` (Stage 2.3+2.4 live operational, multi-coin),
  plus `tools/first_real_trade.py` for one-shot live diagnostics.

## Operator runbook — from cold start to live grid

1. **Ensure two Kraken keys exist** (per ADR-003-style separation):
   - Read-only key: `Query Funds` + `Query open & closed orders & trades`.
     Stored as `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` in `.env`.
   - Trade key: above scopes + `Create & modify orders` + `Cancel & close
     orders`. **No `Withdraw`.** IP-restricted is recommended. Stored as
     `KRAKEN_TRADE_API_KEY` / `KRAKEN_TRADE_API_SECRET` in `.env`.
2. **Sanity check the read path:** `python -m wobblebot.cli.status`. Should
   print live BTC/USD price + your balances. Read-only — moves nothing.
3. **Validate the grid config:** `python -m wobblebot.cli.preflight
   --symbol BTC/USD`. Runs ONE engine step against live Kraken with the
   adapter in dry-run mode (every AddOrder request adds `validate=true`).
   Exit 0 means Kraken accepts every order in the layout. Repeat per
   symbol you intend to run.
4. **Open Kraken Pro in a browser tab.** Orders + Trade History view.
   The first live run is the highest-risk session — watch it.
5. **Run the live grid:** `python -m wobblebot.cli.live --symbols
   BTC/USD,ETH/USD --max-runtime-minutes 60 --max-session-loss-usd 5`.
   Defaults: 1% spacing, 3 above + 3 below = $60 layout exposure per
   coin. Hit Ctrl+C to stop early; the `finally` block always cancels
   open orders for every configured symbol.
6. **After stopping, sanity-check the receipts:** Kraken UI Orders tab
   should be empty (cleanup cancelled everything). Trade History shows
   any fills that occurred. Total value should reflect the
   `session_pnl_usd` reported in the engine's session-end log.

## Hard constraints honored across the phase

- **Financial power fragmentation (CLAUDE.md "Safety Design").** The
  trade key has Trade scope but not Withdraw. The read-only key has
  neither. The future Phase 4 Harvester key will get Withdraw
  exclusively. Bot Core (engine) cannot initiate transfers; Harvester
  (Phase 4) cannot trade.
- **Caps enforced inside Bot Core, not adapters.** Per-coin / global /
  daily-spend caps live in `GridEngine._check_safety`. Adapters only
  surface exchange-side refusals (`InsufficientBalance` from Kraken).
- **Domain has zero adapter imports.** `grep -r "from wobblebot.adapters"
  src/wobblebot/domain/` returns empty.
- **No `print()` calls in production code.** Everything routes through
  the project logger; both `plain` and `json` formats supported.
- **Decimal precision throughout.** Per-pair quantization
  (`pair_decimals` for price, `lot_decimals` for volume) applied
  ROUND_DOWN before submission. SQLite stores Decimals as TEXT.
- **No real network in unit tests.** `httpx.MockTransport` is the test
  seam for `KrakenAdapter`; `:memory:` SQLite is the test seam for
  storage; `MockExchangeAdapter` is the test seam for the engine.

## Health snapshot at Phase 2 close

- **Tests:** 296 unit (was 162 at Stage 2.1 close), 21 integration
  (5 Kraken API drift + 3 live read + 2 simulator + 2 grid e2e + 9
  live trading).
- **mypy:** clean across 33 source files.
- **black/isort:** clean.
- **pylint:** **9.98/10** on `src/`.
- **Pre-commit:** gitleaks + PII pattern check + author-identity guard
  via `.githooks/pre-commit`. No secrets in any commit.
- **OC memory:** five pinned milestone memories (one per stage) plus
  the design-decision corpus.

## What was deliberately NOT done in Phase 2

- **No LLM advisor.** That's Phase 3. The engine takes its parameters
  from `GridConfig` only.
- **No withdrawals.** That's Phase 4. The Phase 2 trade key has no
  `Withdraw` scope; even if Bot Core wanted to call `withdraw()` (it
  doesn't), Kraken would refuse.
- **No automated reconciliation against ghost orders.** ADR-006 decision
  3 covers this design ("DB primary, exchange ultimate") but no slice
  needed it; the existing per-tick storage/exchange diff handles fill
  detection, and operator-injected orders into the same account would
  surface as anomalies in the next tick's reconciliation pass when one
  is added.
- **No cycle-pair detection in the engine.** Per ADR-006 decision 6
  cycles are reported per-fill (not per-matched-pair). Post-hoc
  cycle analysis can be a query against the `trades` table.
- **No parallel per-coin tasks.** Stage 2.4 ticks symbols in series.
  Per ADR-006 decision 5, the per-symbol asyncio.Lock makes
  parallelization safe; deferred to Phase 5 hardening if profiling
  ever shows the master task is the bottleneck (it won't, at observed
  ~150ms per-symbol latency vs the 5s tick budget).

## Phase 3 entry conditions

Phase 3 — Strategy Advisor & Analytics — picks up with these inputs:

1. **A working live trading engine** with safety caps, clean shutdown,
   and demonstrated cycles against mock + real fills against live.
2. **A trades / orders / balance_snapshots history** in SQLite that
   Phase 3's `DataCollector v2` can compute metrics over (volatility,
   cycle counts, win rates, drawdown).
3. **An `AdvisorPort` skeleton** in `src/wobblebot/ports/advisor.py`
   already stubbed during Phase 1.2.
4. **Architectural invariants** that make the advisor's role sharply
   bounded (per ADR-002): JSON output only, no execution authority,
   bounded by configured min/max ranges before any auto-application.

Begin Phase 3.1 with the data collector v2 design — what metrics to
compute, what cadence, where they store. Then 3.2 wires up the local
LLM (Ollama) adapter with a strict JSON schema for recommendations.
The engine doesn't change.
