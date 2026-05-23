# Harvester — treasury and fund movement

*Entries here extend the Harvester (ADR-003: sole module with transfer authority) — adding deposit direction, reconciliation, and operator-facing improvements.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Harvester reconciliation

**What:** a `services/harvester_reconciler.py` parallel to
`services/reconciler.py`. Compares `transfer_proposals` /
`transfer_results` rows against Kraken's recent withdrawals
endpoint to detect:
- Proposals the operator approved that Kraken processed but no
  `TransferResult` row exists (forgotten approvals).
- Results in storage marked `pending` that Kraken has since
  completed.

**Why deferred:** ADR-018 explicitly defers harvester reconciliation
to v1.1. The harvester runs on a slow cadence (default daily); the
engine's reconciliation justification (every-startup-detection of
crash drift) applies less strongly.

**Trigger:** any soak-window evidence of state drift between
storage and Kraken's withdrawals.

### Harvester gate's eighth defense layer: cumulative daily total

**What:** the harvester gate currently has seven defense layers
(Phase 4.5 audit). An eighth would surface the operator-visible
cumulative daily withdrawal total at proposal-generation time so
the operator approves with full context.

**Why deferred:** the existing `day_cap_usd` defense layer already
prevents over-withdrawal. The eighth layer is operator UX, not
safety: cleaner approval messaging.

**Trigger:** operator approves a withdrawal during the soak and
later wishes they'd known the day's running total.

### Harvester top-up deposits — keep a minimum exchange USD balance for trading

**What:** extend the Harvester from its current
exchange-to-bank-only posture (Stage 4.5 added the seventh defense
layer specifically because Kraken's `/0/private/Withdraw` is
exchange→bank only) to also handle **bank→exchange top-ups**.
Operator sets a `minimum_trading_balance_usd` threshold; when
Kraken USD drops below it during normal Harvester polling, the
Harvester generates a top-up proposal that — once operator-approved
— pulls USD from a designated bank account up to a configured
target balance.

**Why high value (operator framing 2026-05-23, soak day 6):** the
2022 walkthrough surfaced exactly this gap. During sustained
accumulation events (Phase 2-3 of the 2022 BTC drop), the bot's
behavior is dominated by `max_daily_spend_usd` capping new BUYs
once the USD balance gets low. The current bot then sits idle for
the duration of the bear market because nothing refills the USD
side. With a top-up capability the cycle would be: BUYs deplete
USD → balance drops below threshold → Harvester proposes top-up
→ operator approves → USD restored → trading continues with the
new lower price band the regime has settled into. Closes the loop
between accumulation strategies (regime-aware modes,
confidence-driven extension) and the funding side.

**Why bounded risk (load-bearing operator constraint):** the
Harvester's bank-side anchor is a **savings account, not
checking**, so the top-up cannot draw against operator's checking
or trigger an overdraft. Worst-case failure mode if the savings
account runs dry is **identical to current behavior**: bot runs
out of allocated USD, stops trading, waits for operator
intervention. No new catastrophic-loss path introduced. This
constraint is non-negotiable for the entry — any implementation
must verify the configured source account is in fact a savings
or otherwise-bounded account, not a checking account that could
overdraft.

**Technical-feasibility flag (must investigate before
implementing):** Kraken's API key permissions DO include a
``Deposit`` scope (operator confirmed 2026-05-23 by inspecting
the key-edit dialog at pro.kraken.com/app/settings/api — scope
sits alongside ``Query`` / ``Withdraw`` / ``Earn`` under "Funds
permissions"). But the public REST endpoints that scope gates
are all **read-only** today:

- ``/0/private/DepositMethods`` — list available deposit methods
  for an asset
- ``/0/private/DepositAddresses`` — generate or retrieve a deposit
  address (crypto)
- ``/0/private/DepositStatus`` — query status of recent deposits

There's no ``/InitiateDeposit`` or ``/PullFromBank`` for fiat ACH
in the public REST surface — that's what the Stage 4.5 audit
caught in the harvester gate's seventh defense layer when refusing
``bank_to_exchange`` direction. The Deposit scope being granular
(distinct from Withdraw, alongside an Earn scope for Kraken's
yield products) is a strong forward-looking signal that Kraken
intends to expose more deposit functionality eventually, but it's
not there yet.

Three potential paths to evaluate:

1. **Kraken adds a fiat deposit-initiation endpoint to the
   public REST.** Most likely future path given the scope already
   exists. Cleanest implementation: ``KrakenAdapter.deposit()``
   mirroring ``KrakenAdapter.withdraw()``. Trigger: Kraken
   announces a deposit-initiation endpoint OR an
   instant-funding endpoint reachable with Deposit scope.
   Watch the API changelog.
2. **Non-public-REST deposit endpoints discovered.** The Kraken
   WebUI clearly can initiate fiat deposits today (Plaid-mediated
   instant deposits, ACH transfers from linked bank accounts).
   Those flows go through endpoints the public REST doesn't
   document. Investigation: capture the network traffic during a
   manual deposit in the WebUI, identify what endpoint is called,
   determine whether an API key with Deposit scope can invoke it.
   If yes — this becomes the v1.1 path. If no (key-scope-only
   access for an OAuth-style session token) — back to Path 1 or
   Path 3.
3. **Scheduled ACH push from bank side + Harvester observation.**
   If the operator's bank supports scheduled recurring ACH pushes
   (most do), the Harvester's role pivots from initiator to
   *observer* — uses ``/0/private/DepositStatus`` (which today's
   Deposit scope already enables) to watch for the expected
   deposit arrival on Kraken, surfaces an alert if it doesn't
   arrive on schedule, and adjusts the next top-up schedule. This
   path requires zero new Kraken API surface — purely consumes
   what's already there. Less elegant than Path 1 or 2 but
   doable today without waiting on Kraken.

Path 3 is the realistic v1.1 default unless investigation surfaces
that Path 1 or 2 is genuinely available — and Path 3 has the
side benefit of forcing us to build the ``/DepositStatus``-watching
infrastructure that Path 1 and 2 would *also* need (the bot still
has to know "did the deposit actually land?" regardless of who
initiated it).

**Architecture (whichever path):** preserves ADR-003 (Harvester
remains the only module with transfer authority) and ADR-004 (no
separate banking adapter — top-up logic lives in the same
``services/harvester.py`` module that owns withdrawals). The
``TransferProposal`` model already has a ``direction`` field with
``bank_to_exchange`` as a valid enum value (which the Stage 4.5
audit refused at the gate); this entry re-enables that direction
under a separate code path with its own defense layers (account-
type check, target-balance ceiling, daily top-up cap mirroring
the existing day_cap_usd).

**Harvester key permission delta:** today's Harvester key has
Withdraw + Query Funds scopes. Top-up via Path 1 or 2 likely needs
an additional scope — ADR-003's "withdraw scope on a separate
key" principle suggests the deposit scope should ALSO be isolated
on a separate key from the Withdraw key, since the two operations
have different blast radii. Worth re-ratifying via ADR before
shipping.

**Why deferred:** depends on the technical-feasibility
investigation above; depends on either regime-aware grid modes or
confidence-driven extension being in production (otherwise the
top-up doesn't have a consumer — current bot just trips its caps
and sits, top-up doesn't change that). Also: introduces a third
flow direction to the Harvester domain (deposit observation /
deposit initiation) which deserves its own ADR before code.

**Trigger:** post-v1.0, after a sustained-accumulation feature is
in production AND the technical-feasibility investigation has
landed on a viable path. Pairs with: regime-aware grid modes,
confidence-driven grid extension, harvester reconciliation.
