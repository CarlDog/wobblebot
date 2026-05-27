# Phase 4 — Closing Summary

**Status: ✅ Complete (2026-05-15).** Five Phase 4 stages closed in
one focused session (4.1 / 4.2 / 4.3 / 4.4 / 4.5). The Harvester
surface ships end-to-end: domain decision logic → read-only balance
monitor → proposal persistence + inspection → operator-approved
withdrawal execution → integration audit + phase summary.

**Phase 4 added zero real-money cost.** Per ADR-003 + ADR-004 +
ADR-012 (extended), withdrawals only happen via `cli/harvest
--execute <proposal-id>` — an explicit operator action that
passes seven defense layers before `KrakenAdapter.withdraw()`
fires. No real withdrawal happened during slice work; every test
uses a stub. Running project real-money cost still **$0.08**
(unchanged from Phase 2 close).

This document is the Stage 4.5 deliverable per the roadmap's
"demonstrate scenarios" charter. Consolidates per-stage receipts,
the bug-find-and-fix from the integration audit, and the entry
conditions for Phase 5.

## Per-stage outcomes

| Stage | Closed | Slices | Verification |
|---|---|---|---|
| 4.1 Harvester Domain + decision logic | 2026-05-15 | 1 (pure-domain) | 24 unit tests across every band (deficit / topup / hold / surplus) + day-cap arithmetic. No I/O. |
| 4.2 `cli/harvest` read-only balance monitor | 2026-05-15 | 1 | Verified live against the operator's account: read $99.92 USD via the read-only Kraken key, correctly classified as deficit, logged "no proposal." `_StubExchange.withdraw()` raises in tests as a defense-in-depth assertion: 4.2 must never call withdraw. |
| 4.3 Transfer proposal persistence + `tools/show_proposals.py` | 2026-05-15 | 1 | `TransferProposal.created_at` field, new `transfer_proposals` SQLite table with `UNIQUE(proposal_id)` + indexed by direction. Persistence independent of `enabled` flag (that gates execution, not the forensic record). |
| 4.4 Active-mode (Guarded Withdrawals) | 2026-05-15 | 4 (a/b/c/d) | Implemented `KrakenAdapter.withdraw()` + Harvester key wiring + `TransferResult` storage + day-cap from rolling-24h history + `cli/harvest --execute` operator-approval gate (six defense layers; mirror of `cli/apply --commit`) + `tools/show_transfers.py` inspector. **No real withdrawal in slice tests** — stubbed throughout. |
| 4.5 Phase 4 Integration Check | 2026-05-15 | 1 (this doc) | Integration audit caught one real bug (see "Integration audit findings" below) and produced this summary. |

## Integration audit findings

The Stage 4.5 audit walked the full Phase 4 path with the question
"could anything move money in a way the operator didn't intend?"

### One real defect found and fixed (Stage 4.5)

**Bug**: `cli/harvest --execute <proposal-id>` would happily call
`adapter.withdraw()` on a `bank_to_exchange` proposal. Kraken's
`/0/private/Withdraw` is **exchange→bank only** — calling it with a
deposit-direction proposal would have moved money in the opposite
direction from what the operator intended (or, more likely, Kraken
would have rejected the request with a confusing error). The
`withdraw()` adapter method only takes `(asset, amount, destination)`
— no direction parameter — so the original implementation just
trusted the caller.

**Root cause**: Stage 4.4c's gate had per-direction logic on the
balance check (only fires for `exchange_to_bank`) and the day-cap
(same), but never explicitly refused a `bank_to_exchange` proposal
from reaching `withdraw()`. The implicit assumption was "the only
direction we'd reach this code path with is `exchange_to_bank`" —
but nothing enforced that.

**Fix**: Inserted a new defense layer between step 2 (proposal
lookup) and step 3 (staleness check):

> 3. Proposal direction must be `exchange_to_bank`. Deposits cannot
>    be executed via Kraken's API — they're operator-pushed from
>    the bank side using deposit instructions visible in Kraken Pro
>    → Funding → Deposit.

The error message tells the operator exactly what to do
(push from the bank using the deposit instructions) rather than just
refusing. Test added in `tests/cli/test_harvest.py::TestExecuteGuardrails::test_bank_to_exchange_refused_no_api_call`
asserts `adapter.withdraw_calls == []` after the refusal.

**Why the audit caught this**: Phase 4.5's charter is "demonstrate
scenarios" — walking the chain end-to-end. The mental walk-through
("operator runs `cli/harvest --execute` on a `bank_to_exchange`
proposal — what happens?") was the right question to ask. Slice
4.4c's unit tests covered the per-defense-layer failures but
didn't test the cross-product of `(proposal.direction, gate
behavior)` — a 2×6 matrix the original tests only sampled along
the `exchange_to_bank` axis.

The gate now has **seven** defense layers (was six). Step 3 makes
steps 6 and 7 (balance check, day-cap check) unconditional —
they previously had inner `if direction == "exchange_to_bank"`
guards that are now redundant. Code simplified.

### What the audit confirmed working

The other Phase 4 paths verified end-to-end during the audit:

- **`cli/harvest` daemon read-only loop**: live-tested 2026-05-15
  against the operator's real Kraken account. Read $99.92 USD,
  classified as deficit (below $200 floor), no proposal generated.
  `persistence_enabled: true` confirmed in session-start log.
- **`tools/show_proposals.py`**: live-tested. Reports "no transfer
  proposals match the filters" against an empty `transfer_proposals`
  table.
- **`tools/show_transfers.py`**: live-tested. Reports "no transfer
  results match the filters" against an empty `transfer_results`
  table.
- **`cli/harvest --execute` defense layers**: 8 unit tests verify
  every guardrail refuses with `adapter.withdraw_calls == []`
  before reaching the API — including the new direction layer.
  No real withdrawal ever attempted.

### Why no real withdrawal happened in 4.5

Stage 4.5 deliberately does not include a live $1 ACH withdrawal:

1. **The operator's account is in deficit.** $99.92 USD < $200
   floor → `propose_transfer()` returns `None`. No proposal to
   `--execute` against without first depositing or adjusting
   thresholds.
2. **Real withdrawal is operator-paced.** Stage 2.3's precedent
   (the `$0.08` first-trade test) shows the operator decides
   when to take the first real-money action. Phase 4's first
   $1 ACH (planned: to `"360 Performance Savings"`) is a
   separately-tracked event.
3. **The integration audit found a real bug** that would have
   affected the first withdrawal if it had been a deposit-direction
   proposal. Catching the bug pre-execution is the integration
   check earning its place.

The operator's runbook for the first real withdrawal is in
the CLAUDE.md "operator handoff" section; the gist:

```
1. Adjust state to generate a real exchange_to_bank proposal:
   - Either deposit USD to push above surplus_threshold_usd ($500),
   - Or temporarily lower surplus_threshold_usd via a profile to
     classify the current $99.92 as surplus.
2. Run cli/harvest once; note the proposal_id from the log.
3. Flip harvester.enabled=true in settings.yml.
4. cli/harvest --execute <proposal-id>
5. Inspect with tools/show_transfers.py
6. Confirm in Kraken Pro Funding tab; record the refid.
```

## Hard constraints honored across the phase

- **ADR-003 honored**: the Harvester is the SOLE module with
  withdrawal authority. The trade key (`KRAKEN_TRADER_API_KEY`)
  has Trade scope but not Withdraw; the Harvester key
  (`KRAKEN_HARVESTER_API_KEY`) has Withdraw + Query Funds but
  not Create/Modify/Cancel orders. `KrakenConfig.from_env`'s
  parameterized key var supports this separation at runtime.
- **ADR-004 honored**: no separate banking adapter. All
  exchange↔bank transfers go through `ExchangePort.withdraw()`
  (Kraken's API). No `BankingPort` was added.
- **ADR-012 spirit honored**: operator-in-the-loop for any
  money-moving action. `cli/harvest --execute <proposal-id>`
  is the explicit per-call opt-in; `HarvesterConfig.enabled=True`
  is the durable opt-in. Both required.
- **Defense in depth**: seven layers between operator command
  and `KrakenAdapter.withdraw()`. Any single layer's failure
  aborts the entire execution path.
- **Decimal precision** preserved through the wire. `TransferProposal.amount`
  / `TransferResult.executed_amount` are stored as TEXT in
  SQLite, serialized as strings in the Kraken request body.
- **Pre-registered destinations only**. Kraken's API enforces
  this; `HarvesterConfig.withdrawal_destinations` mirrors the
  operator's Kraken Pro address book and the gate refuses
  any asset without a mapped label.
- **Day-cap enforced from real history** (Stage 4.4b). Pre-4.4b
  was always `Decimal("0")`; now reads the rolling 24h sum from
  `transfer_results` (excluding `failed` status).

## Health snapshot at Phase 4 close

- **Tests:** 889 unit (was 838 at Stage 4.2 close, +51 across
  Phase 4 stages: +24 services/harvester, +14 transfer-result
  storage, +5 transfer-proposal storage, +1 day-cap helper +
  cycle-level integration tests + +7 execute-gate tests).
  21 integration tests opt-in (unchanged).
- **mypy:** clean across 60 source files (was 57 at Phase 3 close).
- **black/isort:** clean.
- **pylint:** **10.00/10** on `src/`.
- **Pre-commit:** gitleaks + PII pattern check + author-identity
  guard via `.githooks/pre-commit` (canonical reference).
- **Real-money cost:** **$0.08** unchanged from Phase 2 close.
  Phase 4 added zero live withdrawals.

## What was deliberately NOT done in Phase 4

- **No automated bank→exchange (deposit) execution.** Kraken's
  API doesn't support it; operator pushes from the bank side
  using Kraken's deposit instructions. Stage 4.5's integration
  audit found this and added the explicit refusal so an
  operator who tries to `--execute` a deposit proposal gets a
  clean error message pointing them to Kraken Pro.
- **No multi-asset support beyond USD.** `HarvesterConfig.withdrawal_destinations`
  is asset-keyed and propose_transfer happily takes any asset,
  but the daemon reads only `get_balance("USD")`. Per-asset
  decision logic (BTC scraping with different thresholds,
  ETH scraping, etc.) is a Phase 5+ scope decision.
- **No daemon-side auto-execution.** Per ADR-012's
  operator-in-the-loop posture extended to money movement,
  `cli/harvest` never executes on its own. Every withdrawal
  is an explicit operator command.
- **No notification surface.** `NotifierPort` is defined in
  `ports/notifier.py` but unimplemented. A "Harvester proposed
  a $X withdrawal; review pending" Discord ping is the
  obvious Phase 5 follow-on.
- **No reconciliation against Kraken Pro Funding history.**
  Pre-existing withdrawals made outside the bot (e.g. before
  Phase 4 shipped) don't appear in `transfer_results` and
  don't count toward the day-cap. Acceptable for a single-user
  hobby bot; would need a reconciliation pass before any
  multi-user scenario.

## Phase 5 entry conditions

Phase 5 — Dashboard, hardening, v1.0 — picks up with these inputs:

1. **A working end-to-end pipeline** through every loop except
   the maintenance loop (Stage 5.3.5): observe → news → advise
   → apply → live → harvest → execute. Ten operator entry
   points ship; four inspection tools ship.
2. **Three persisted audit chains**: `advisor_suggestions` ←
   `applied_suggestions` (advisor side); `transfer_proposals`
   ← `transfer_results` (harvester side); `orders` + `trades`
   + `grid_state` (engine side).
3. **Architectural invariants** all honored: hexagonal layer
   purity, operator-in-the-loop for money moves, defense-in-depth
   gates on every mutation surface, pre-commit secret + PII +
   identity guards on every commit.
4. **Three placeholder slots** to fill in Phase 5 (or before):
   cloud-provider advisor adapters (anthropic/openai/google in
   `_build_advisor`), `NotifierPort` concrete adapter (Discord
   first; ADR-007 named it but Phase 3 didn't ship it),
   Stage 5.3.5 maintenance worker (SQLite VACUUM + log rotation
   + retention pruning).

Begin Phase 5.1 with the web UI structural placement (the roadmap
already documents `src/wobblebot/web/` as the sibling of
`src/wobblebot/cli/`). Phase 5's job is to make Phase 1-4's
already-working internals **observable + presentable** rather
than to add new behavior.

## Cycle-time and operational notes

- **`cli/harvest` daemon cycle**: ~300ms per tick (Kraken balance
  read latency). Default cadence 1h gives ~99.99% idle headroom.
- **`cli/harvest --execute`**: ~500ms (one balance read + one
  withdraw POST + one DB write). Operator-paced; latency
  irrelevant.
- **Day-cap query**: O(log N) via the `(asset, direction,
  timestamp)` index. Even with thousands of historical
  withdrawals, the rolling-24h sum stays sub-millisecond.
- **TransferResult immutability**: Once `status="pending"` lands,
  there's no UPDATE path. A Phase 5+ reconciliation pass would
  need to either INSERT a fresh row with `status="completed"`
  + the same refid (UNIQUE blocks this — needs an extra column
  like `state_version`) OR add a separate "status updates"
  table. Acceptable to defer until a real reconciliation need
  surfaces.

## One operator-runbook step worth highlighting

Before the first real `cli/harvest --execute`, the operator
should verify:

```bash
# 1. Confirm the Harvester key has the right scopes.
#    KRAKEN_HARVESTER_API_KEY/SECRET should be set in .env;
#    cli/harvest already runs against this key for balance reads
#    (Stage 4.4a). If you can run cli/harvest and see a balance
#    read in the log, the Query Funds scope is confirmed working.
#    The Withdraw scope test is the actual --execute call.

# 2. Confirm the destination label exists in Kraken Pro.
#    Kraken Pro → Funding → Withdraw → check that "360 Performance
#    Savings" (or whatever you configured) appears in the address
#    book. If not, the Stage 4.4c destination-label guardrail will
#    refuse before calling Kraken.

# 3. Run with a small amount first.
#    For ACH, Kraken's minimum is typically ~$5 (verify in the
#    Funding tab). Don't go straight to a high-value withdrawal
#    on the first run.

# 4. Inspect after.
#    tools/show_transfers.py shows the persisted result.
#    Kraken Pro Funding history shows the canonical state +
#    settlement time.
```
