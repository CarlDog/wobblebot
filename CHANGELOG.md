# Changelog

All notable changes to WobbleBot are documented in this file. Format
is a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [SemVer](https://semver.org/spec/v2.0.0.html).
Per-stage receipts in [`docs/planning/roadmap.md`](docs/planning/roadmap.md)
carry the canonical completion dates.

**`v1.0.0` was tagged 2026-07-31** (see the `[1.0.0]` section below and
`docs/planning/phase-8-summary.md`). The work developed on the `v1.1`
branch since then — P1's safety-hardening backlog, the ADR-022 advisor
reorientation, and a full web UI expansion — ships as **`2.0.0`**, not
`1.1.0`: it includes a breaking config-schema change
(`EmergencyStopConfig` removed, ADR-032) and a full replacement of the
advisor's decision architecture (ADR-022), both of which warrant a
major bump under this file's stated SemVer discipline. The branch
itself keeps its `v1.1` name for history (git branches, ADR numbers,
and the `docs/release/v1.1/` planning directory aren't renamed) — only
the released version number changed.

**Fold note (2026-08-28).** This file briefly carried two unreleased
sections — `[2.0.0]` for the branch work and `[Unreleased]` for
post-merge changes on `main`. Since nothing on `main` had ever been
released, the split described one development line, not a release and
a patch on top of it. They are now one `[2.0.0]` section, per
`docs/planning/release-2.0-plan.md` §1a. Post-2.0.0 work lands under a
fresh `[Unreleased]` heading created at that time.

## [2.0.0] - 2026-08-28

### Added

- **ADR-041 — the deployment enforces the capability matrix** (2026-08-28).
  A single `x-wobblebot-defaults` YAML anchor had been injecting *every*
  credential into *all nine* services: `web`, `news`, `advise`, `operator`,
  `maintenance`, and the one-shot `tools` container each held the
  withdrawal-enabled Harvester key alongside the reader and trader keys, every
  cloud-LLM key, the Discord token, and the web session secret — plus
  read-write mounts of the entire `data/` and `config/` trees. ADR-003's
  financial-power fragmentation was enforced in the Python and merely *assumed*
  at the container boundary; reaching the withdrawal credential required no
  Python bug, only a foothold in any daemon. Each service now declares exactly
  what its code reads (Harvester key: 9 services → 1; trader: 9 → 2; reader:
  9 → 3), `/app/config` is read-only for all eight daemons with only the
  settings-rewriting `tools` container keeping write access, and the terminal
  withdrawal path moves to `docker compose run --rm harvest …` so withdrawal
  authority appears in one service definition.
  `tests/deployment/test_compose_capability_matrix.py` asserts the matrix as an
  allowlist — an *extra* credential fails as loudly as a missing one — with a
  structural guard that no shared anchor may carry a credential at all.
  Verified beyond the unit test by rendering `docker compose config` against a
  synthetic canary environment. The matrix was derived from the source, not
  from the compose file's own comments, which had drifted in three places.
  Found by the external-repository assessments below and re-verified directly.
- **A v1.0 → 2.0 upgrade-survivor gate** (2026-08-28,
  `tests/deployment/test_v1_to_v2_upgrade_survivor.py`). Every migration had
  unit coverage for its own column; nothing exercised what an operator actually
  does — open a v1.0-written database with the 2.0 artifact. The fixture is
  built from the *real* tagged schema (`git show v1.0.0:…`) rather than a
  hand-copy that would drift, and skips rather than passing green if the tag is
  unreachable. Asserts that the database opens, integrity holds, seeded rows
  survive, a second migration pass is a no-op (an interrupted upgrade must be
  resumable), and — the one that matters most — an operator-**approved**
  `pending_commands` row is still `approved` with a NULL `dispatched_at`
  afterward, so migrating can never execute a live ADR-002 instruction as a
  side effect.

### Fixed

- **A config carrying an ADR-retired key is now refused, with the fix in the
  error** (2026-08-28). ADR-032 deleted `safety.emergency_stop` precisely
  because a silent dead safety knob is worse than none — but no config model
  sets `extra="forbid"`, so Pydantic's `extra="ignore"` default meant an
  upgrading operator's `settings.yml` kept the block, loaded without a word,
  and went on claiming a balance floor that does not exist. ADR-032's own
  stated problem survived the fix that retired it. A `mode="before"` validator
  on `WobbleBotConfig` now rejects a named registry of retired keys (inactive
  `profiles.*` sub-trees included) and names the ADR and the superseding
  setting. Deliberately *not* `extra="forbid"` everywhere, which would turn a
  targeted upgrade check into a broad compatibility break. Found by the
  upgrade-survivor gate above; the live NAS config, the local checkout, and
  `settings.example.yml` were all verified free of the key before shipping a
  hard failure into a `restart: unless-stopped` fleet.

### Previously under `[Unreleased]`

- **External-repository assessments and manual operator tooling** (2026-08-27 →
  2026-08-28). Added source-backed Ollama, OpenClaw, and NemoClaw assessments under
  `docs/reference/`, with explicit non-fit decisions so research does not silently become
  backlog. Added the official Atlas Cloud CLI as an optional `vendor/atlascloud-cli` git
  submodule for shell-side balance/model/connectivity checks; WobbleBot's runtime adapter
  still calls the Atlas Cloud API directly. Added the documented
  `docs/reference/fixtures/gremlin-directional-forecast-prompt-2026-08-23.txt` manual
  directional-forecast fixture. None of these artifacts expands LLM
  authority or changes the live trading path.
- **Anthropic prompt caching, sending half** (2026-08-16, ADR-033
  amendment — the accounting half shipped 2026-08-02).
  `AnthropicAdvisorAdapter` now ships its system prompt as a
  `cache_control: {"type": "ephemeral"}` content block (5-minute TTL
  only — the ledger's single cache-write pricing column models the 5m
  1.25× premium, and a `ttl: "1h"` key would silently under-price;
  a test pins its absence). New `prompt_caching: bool = True`
  constructor knob; default ON because no deployed config path reaches
  Anthropic (inert in production) while probe batteries and cloud
  checks — many calls sharing one system prompt within minutes — read
  the cache immediately at ~0.1× input rate. Caveat documented in code
  and ADR: the cacheable floor is model-dependent and non-monotonic
  (opus-5 512 / sonnet-5 1024 / **haiku-4-5 4096** tokens), and
  under-floor prefixes silently don't cache, so current ~620–1,050-token
  role prompts are a no-op on haiku-4-5. The assistant adapter
  deliberately does not cache (its system string embeds the volatile
  engine-state snapshot; restructuring first is ADR-033 trigger (a)'s
  prerequisite, documented at the build site).
- **Data retention shipped** (2026-08-16, ADR-036). v1.0 pruned only
  `price_snapshots`; measurement against the live NAS at ~91 days of
  soak showed the real growth was the *multipliers*, not the tables —
  a rotation-less archive dir, a 7× backup rotation whose fire-on-start
  behavior burned 4 of 7 "daily" slots on deploy day, and ~200 MB of
  never-truncated WAL. New: per-table 90-day archive-then-delete for
  `news_items` / `conversation_turns` / `notifications` via
  `maintenance.retention:` (horizon days only — table names must match
  the code-side prunable registry in `services/retention.py`, and
  forensic tables like `trades` / `orders` / `transfer_*` /
  `pending_commands` are not in it, so config can never name the money
  ledger); all new archives write gzipped (`.csv.gz`, ~6-8× smaller);
  backups dedupe same-day (`min_backup_interval_hours`, age read from
  the filename stamp, not mtime); VACUUM now checkpoints-and-truncates
  the WAL, and every storage connection caps the WAL file via
  `journal_size_limit`. Steady-state footprint lands ~2.3 GB with the
  archive dir growing ~90 MB/year. Unblocks the P3-gated disk-space
  awareness item.
- **ADR-039 inventory caps** (2026-08-17). The four safety caps bound
  the order book; production held $262.65 of inventory at cost against
  a $150 total cap that saw $90 of open orders (BTC $111 vs the $40
  per-coin cap — 2.8×), accumulated through a one-way valve: buys fill
  while the ADR-032 sell guard defers the counter-sells below basis.
  Two additive caps now bound the POSITION at average cost basis
  (`max_per_coin_inventory_usd` $40, `max_total_inventory_usd` $300):
  BUY placement requires inventory@cost + open BUY notional + proposed
  ≤ cap; sells are never blocked (they release headroom); cost basis
  not MTM (an MTM cap re-opens buying as price falls). Reuses the sell
  guard's `replay_average_cost`. At current holdings BTC and ETH
  freeze for new buys immediately — the intended posture.

- **ADR-038 live fee rates** (2026-08-17). Kraken doubled Tier-1 spot
  fees effective 2026-07-09 (0.25/0.40 → **0.40% maker / 0.80% taker**)
  and the copied constants drifted silently for five weeks. Now:
  `ExchangePort.get_fee_rates` (Kraken `TradeVolume` — the account's
  own per-pair rates) feeds the sell guard at session start with the
  constants as logged fallback; a per-fill fee-drift tripwire pages on
  the first fill matching neither believed rate; constants + shadow
  defaults corrected to Tier-1; the aggressive profile's spacing moves
  0.6% → 1.8% (the new 0.80% fee floor made it structurally
  unprofitable); the spacing validator floor doubles accordingly.

- **ADR-037 auth-failure escalation** (2026-08-17, from the reader-key
  incident). Kraken errors now classify (`ExchangeError.codes` +
  `is_permanent_auth_error` / `is_temporary_lockout`); a shared
  3-strike `PermanentAuthHalt` stops observe's balance poll and
  harvest's hourly balance read from retrying a dead key (each retry
  re-armed the account-wide lockout) with one critical page;
  `cli/live` gains lockout backoff (30s→10min, DMS ping exempt), a
  DMS-failure-streak critical alert, and the decision-6 trader-key
  pause (3 permanent-auth strikes pause ALL placement); and the grid
  engine holds a symbol whose book vanished externally
  (`held_book_vanish` → paused, operator resume only) instead of
  silently re-laying at a stale anchor — the incident's ~40 churn
  cycles become one page and one Discord resume. New optional
  `observe.operator_db` wires the pages; recovery notices emit when
  an episode ends.

### Changed

- **Money now renders in one dialect everywhere** (repo-quality pass,
  2026-08-15). One USD value had four costumes: web templates fixed
  `%.2f`/`%.4f`/`%.8f` with no thousands separators, Discord embeds
  `:,.2f`/`:,.4f` *with* them, session PnL differing between two embeds,
  logs adaptive. Fixed 2dp was destructive below $1 — DOGE's grid levels
  3% apart all rendered "$0.09"; the ladder was illegible on the
  dashboard. New `fmt_usd` / `fmt_qty` in `domain/value_objects` (beside
  `fmt_decimal`) back Jinja filters (`usd` / `usd_signed` / `qty`), the
  Discord renderers, and the operator status text: `$63,237.60`,
  `$0.0698`, `+$0.13`, quantities zero-stripped at 8dp. The withdrawal
  confirm dialog deliberately uses `usd_exact` (never rounds — a
  money-out approval must show the amount that will actually dispatch,
  and proposal amounts carry Kraken's 4dp). `format_signed_usd`
  (a local duplicate) is deleted.

- **Repo-quality sweep, same pass:** the missing-section → exit-2
  contract moved from seventeen hand-copied blocks into
  `_common.missing_section_exit` (every CLI now also names the
  settings.example.yml template in the error); `cli/screener` reports a
  missing section through the logger like its fifteen siblings instead
  of a raw stderr write; the SQLite schema migrations moved to
  `adapters/sqlite_migrations.py` with their contract (additive +
  idempotent, race-tolerant, forensic data never silently destroyed)
  stated once; `web/dependencies.py`'s eleven `type: ignore`s became
  typed assignments. Audit verdicts recorded so they aren't re-run:
  vulture found zero dead code at 80% confidence; all 18
  `scan_logging --check decimal` hits are false positives (ints,
  durations, currency codes).

### Added

- **Standing Kraken-vs-local trade reconciliation** (2026-08-22) — a
  fifth `cli/maintenance` scheduled task
  (`schedules.maintenance_reconcile`, default daily; implementation in
  the new `cli/maintenance_reconcile.py` so the DB-hygiene module
  keeps its single concern) diffing ONE account-wide Kraken
  `TradesHistory` fetch per cycle against the locally recorded trades
  for every symbol in `live.symbols`, notifying at `critical` on any
  Kraken trade with no local row. Both silent-loss incidents that
  motivated it went undetected for weeks: an orphaned SOL fill for
  seven, and 18 BTC trades from `tools/first_real_trade.py` (which
  imports no storage layer at all, by design) until a manual audit
  went looking. The diff is by trade id deliberately — a binary
  present/absent check with no dust tolerance to argue about, after an
  earlier quantity-versus-balance comparison produced two false
  positives from Kraken's `total`-vs-`available` semantics. A Kraken
  trade whose order is still locally OPEN is *deferred* (the engine
  persists trades only at terminal order status per ADR-023, so a
  partial fill resting on the book is persistence-pending, not a gap)
  — logged, never paged. Symbols come from `live.symbols` — the
  actually-traded set — rather than a config list of their own, so the
  check cannot drift from what is traded (the branch's first cut used
  `grid.coins`, a per-coin *override* map that is wrong in both
  directions; caught by the pre-merge review). `Ledgers` is
  deliberately NOT consulted (staking accrual and dust conversions are
  expected noise, not a correctness signal). Reader key, read-only
  storage handle, one `get_trade_history` call per cycle (the endpoint
  is account-wide with no pair filter, so per-symbol fetches would
  re-walk identical pages against the account-wide limiter) — the task
  cannot place, cancel, or move anything. Fault isolation is stricter
  here than in the sibling tasks because this one parses third-party
  JSON: `_main_async`'s `asyncio.gather` has no `return_exceptions`, so
  an escaping exception would take vacuum/prune/backup/verify down with
  it. `tools/reconcile_trade_history.py` is the deeper one-shot manual
  diagnostic (adds `Ledgers` for non-trade balance moves, and carries
  the backfill runbook a reported gap points at) and shares the same
  diff via `services/trade_reconciliation`, so the two cannot diverge.
  NB for existing deployments: `maintenance.reconcile_source_db` is a
  NEW key — an operator `settings.yml` written before 2026-08-22 must
  add it or the task silently never runs. (`af95f48`, `0abeeb1`, plus
  pre-merge review fixes)

- **`SQLiteStorageAdapter(..., read_only=True)`** (2026-08-22) — opens
  via SQLite's `mode=ro` URI, skipping the pragmas, schema DDL, eight
  migrations and commit that `connect()` otherwise runs. Those are
  correct for a daemon that owns its DB and wrong for one reading a DB
  another daemon writes: without it the reconcile task above holds a
  write lock on `live.db` during its migration transaction, and since
  no `busy_timeout` is set anywhere in `src/`, a collision surfaces as
  an immediate `SQLITE_BUSY` on `cli/live`'s own write rather than a
  wait. `mode=ro` additionally refuses to open a missing file instead
  of creating an empty one, so a typo'd path fails loudly rather than
  reporting every expected row as absent. Mirrors the idiom
  `daemon_health._latest_iso_timestamp` already used for exactly this
  situation; it simply wasn't reachable through the adapter. (`90eae3f`)

- **The MoE risk expert now receives the exposure data its prompt promised.**
  `risk.md` told the model it was handed "current open exposure vs the
  configured caps … and daily spend so far vs the daily cap"; of those,
  `PerformanceSummary` carried none — which is why the live risk expert
  confabulated cap headroom fluently enough to read as rigour. Seven
  fields added (`total_exposure_usd`, `coin_exposure_usd`,
  `daily_spend_usd`, their three caps, and `max_orders_per_coin`),
  computed by a new `services/exposure.py` that `GridEngine._check_safety`
  now shares — so the advisor can never report headroom the engine
  disagrees exists, including the committed-funds rule that excludes
  canceled/expired BUYs. `cli/advise` gains an opt-in `advise.orders_db`;
  unset, the fields go to the model as `null`, which `risk.md` now
  explicitly defines as "unknown", never zero. The clause promising
  "time-to-recovery from the last loss" is **removed** — no such metric
  exists, and inventing one to match prose was the wrong direction.

- **`live.symbol_priority` — the per-tick sweep is now an operator choice.**
  `cli/live` swept `live.symbols` in config order, so the first-listed
  symbol claimed the exposure and daily-spend caps every tick; the measured
  production starvation gradient ran exactly down the list (ETH 5/6 orders
  placed, SOL 2/6, ADA 0/6 — BTC, listed first, had first claim for
  months). Three strategies: `config_order` (the default — upgrading
  changes nothing), `round_robin` (rotates first claim; removes the bias,
  optimises nothing), and `screener` (grid-suitability composite from
  `services/screener`, tiebroken by proximity to the nearest grid level in
  ATR; requires the new `live.observe_db`, enforced at config load).
  Ordering redistributes scarcity — it cannot create capacity — and every
  failure path (unopenable observe DB, thin bars, a bug in the scoring
  math) degrades to config order rather than stopping a tick. The
  `sweep order updated:` log line names each symbol's
  `(composite|proximity)` so a reorder is explainable by diffing two
  consecutive lines. (`af2907d`, `7f800d2`, `c8ce723`, `b9ec699`)

### Fixed

- **Malformed Kraken responses raised bare builtins, bypassing every
  graceful-degradation handler in the codebase** (2026-08-22, found by
  auditing the root cause behind a defensive patch in the new reconcile
  task). Kraken's JSON is untyped at the wire and `KrakenAdapter`
  coerced it into domain values unguarded, so a malformed payload raised
  `decimal.InvalidOperation` / `TypeError` / `KeyError` / `IndexError` /
  `OverflowError` / pydantic `ValidationError`. None of those subclass
  `WobbleBotPortError`, which is what every caller catches, so none of
  the designed containment applied. A caller audit found 13
  high-severity paths: `cli/live`'s per-tick handlers (designed to skip
  a symbol, would instead exit the loop and kill the real-money daemon
  mid-session); `cli/live`'s **shutdown** `finally` block, where the
  balance read precedes `_cancel_all_open`, so a bad `BalanceEx` entry
  aborted the block and left real orders resting on Kraken;
  `cli/harvest`, the only module with transfer authority (ADR-003),
  whose `asyncio.gather` has no `return_exceptions`; and
  `grid_engine.cancel_open_orders`' trade-history fetch, which runs
  *after* the cancels have executed — the exact silent-fill-loss shape
  the `save_fill` entry above closes. Nine parse sites now guard against
  a shared `_PARSE_ERRORS` tuple, shared because that exception set is a
  subtle correctness rule that had **already drifted**:
  `_ensure_pair_metadata` caught `(KeyError, ValueError)` and so missed
  the `InvalidOperation` its own `Decimal()` calls raise
  (`InvalidOperation` is an `ArithmeticError`, not a `ValueError`), and
  `get_ohlc` had the mirror-image hole — catching only `ValidationError`
  while its sibling `Decimal()` calls escaped, with the timestamp
  coercion outside the `try` entirely. `_unwrap_envelope`'s error-code
  comprehension is additionally `isinstance`-gated: a truthy
  non-iterable `error` (Kraken sending `1`) made the envelope
  normalizer — the one place every call passes through — raise a bare
  `TypeError`. Deliberately not a decorator or context manager: a
  blanket wrapper on `place_order` would swallow the
  `InsufficientBalance` translation and convert a domain exception into
  an `ExchangeError`. `ExchangeError` is likewise excluded from the
  tuple so `_symbol_for_pair_key`'s own message passes through
  un-rewrapped. 19 regression tests asserting both the `ExchangeError`
  and the `__cause__` chain (contractual per `ports/exceptions.py`,
  previously unverified for Kraken); 17 confirmed failing against the
  pre-fix adapter. The pre-merge full-branch review then found four
  residual escapes the first pass missed — a non-dict order/trade
  entry (`.get()` on a string raises bare `AttributeError`, and the
  builders' first lines sat OUTSIDE their guards), an unhashable
  `pair` value (a JSON array reaches `_symbol_for_pair_key`'s dict
  lookup as a bare `TypeError`), and a NaN/Infinity `count` (parsed
  fine by `json.loads`, passes the `isinstance` gate, then
  `int(nan)` raises bare `ValueError`) — all closed (`math.isfinite`
  gate; builders' bodies moved fully inside their guards) with five
  more regression tests, including one for the
  `validate_assignment` branch of `_apply_kraken_order_update` (an
  unknown `status` literal raising `ValidationError` at assignment)
  that the first pass cited in a comment but never tested.

- **Silent fill loss: an order's terminal status and its trades were
  persisted as two independent writes** (2026-08-22, found by a
  financial-correctness audit, root-caused and fixed same day).
  `GridEngine._detect_fills` saved the order as `closed` and *then*
  looped saving its matched trades. A failure between the two left the
  order permanently `closed` with its trade never written — and a closed
  order never becomes a fill candidate again, so nothing ever retried
  it. Confirmed live on XRP: the `orders` row carried the correct
  `status`/`filled_amount` while Kraken's matching trade was absent from
  `trades` entirely, silently corrupting that symbol's cost-basis replay
  and therefore the SellGuard's allow/defer decisions. New
  `StoragePort.save_fill(order, trades)` wraps both writes in one SQLite
  transaction, so a failed trade insert rolls the order's status change
  back with it and the order is re-resolved on the next pass instead of
  losing the fill. Wired into all three call sites with that shape
  (`GridEngine._detect_fills`, `GridEngine.cancel_open_orders`,
  `reconciler.apply_reconciliation`). Separately,
  `cancel_open_orders`'s "not tracked in local storage" branch
  discarded a real fill at INFO — ADR-018 still forbids adopting an
  untracked order, so such a fill stays unrecoverable, but it now pages
  at ERROR rather than vanishing. Regression tests pin the rollback:
  a two-trade `save_fill` whose second insert fails must roll back the
  first trade too, never committing a closed-order-with-missing-trade
  half-state. The pre-merge review added two engine-level pins the
  storage-layer tests couldn't give: the recovery loop itself (a
  transient `save_fill` failure leaves the row open, the NEXT tick
  re-resolves it, and the trade lands exactly once), and an ordering
  fix it caught — the ADR-037 `_external_cancels` increment had moved
  ahead of the persist, so a failed persist plus retry double-counted
  one external cancel into the operator's book-vanish page; the
  counter now increments only after the successful persist
  (exactly-once, verified failing against the pre-fix ordering).
  (`7285edb`, plus pre-merge review fixes)

- **ADR-021/ADR-037 alerting-fidelity gaps: DMS-alert reset bug,
  book-vanish message honesty, held-symbol reminder** (2026-08-20,
  production incident, root-caused and fixed same day). A real
  ~6-minute Kraken outage failed the DMS reset (`CancelAllOrdersAfter`)
  ~40 times back to back; Kraken's own server-side timer lapsed and
  auto-cancelled every order (working as designed) and ADR-037
  correctly HELD five symbols. Three related alerting gaps: (1) the
  "DMS resets failing" critical never fired, because the per-tick
  OpenOrders success (unrelated to DMS) shared a reset with the
  DMS-specific streak, wiping it before it could reach the alert
  threshold despite the sustained real failure — DMS-health tracking
  is now split from generic private-call success; (2) the "Book
  vanished" notification read identically alarming whether the cause
  was Kraken's own DMS firing (self-resolving) or a genuinely
  unexplained external cancel — it now compares wall-clock time against
  Kraken's own PROMISED auto-cancel deadline (not a failure count,
  which a code-review pass showed was too short-lived and vulnerable to
  a same-tick recovery erasing the evidence) to pick the honest
  framing, with severity and the HOLD itself unchanged; (3) a held
  symbol got exactly one notification ever, with no reminder it was
  STILL held — production went ~18h with 5 symbols held and only the
  initial alerts to notice by. A new aggregate "still paused" reminder
  now fires on a configurable cadence (`live.held_reminder_seconds`,
  default 4h) while any symbol remains paused — deliberately not
  scoped to book-vanish holds only, since that reason doesn't survive a
  restart. Full detail in `docs/planning/roadmap.md`'s Stage 8.4.E
  entry. 18 new regression tests, including three verified failing
  against the relevant pre-fix code and passing against the fix.

- **The re-anchor banner contradicted itself after a guard-vetoed
  re-anchor** (2026-08-17, diagnosed live). A re-anchor that executed
  cleanly but had its near levels vetoed (ADR-039 inventory cap on the
  BUYs, cost-basis guard on the nearest SELL) left drift-to-nearest-order
  at 2.0 spacings, so the banner re-rendered identically — still urging
  "consider re-anchoring" while showing the anchor AT the current price,
  with the honest result tally visible only in the notifications bell.
  The banner now renders the most recent successful `reanchor` command
  result for its symbol (≤1h, the engine's audit message verbatim), and
  when that result proves another re-anchor structurally cannot reduce
  drift (anchor within ~1 spacing of price, drift persisting) the
  heading and foot note switch the recommendation to snooze / pause /
  wait for recovery. Annotation only, per the banner's standing
  invariant: severity, presence, and both action buttons never change.
  The grid-anchor stat gained a tooltip explaining that drift is
  measured to the nearest open order, not the anchor.

- **A paused symbol was blind to its own fills** — a real-money production
  bug. `GridEngine.step` returned `skipped_paused` *before* fill
  detection, so a BTC BUY that executed on Kraken 2026-08-11 while the
  symbol was paused sat `open` in storage for four days: no trade row, no
  counter order, three phantom "open" orders on the dashboard. A paused
  symbol now still observes — fills are detected, recorded, and WARNed —
  it just places nothing until resumed. The stale production row was
  repaired from the real Kraken execution data, not synthesized.
  (`652de27`)

- **A restart silently resumed trading on paused symbols.** Pause state
  lived only in engine process memory; the `engine_state` rows written
  every tick for the dashboard (ADR-030) were never read back. `cli/live`
  now restores pauses from `engine_state` at startup — deliberately with
  no freshness window, because a pause is operator intent, not a cache
  entry, and expiring it re-creates exactly the failure being fixed.
  (`4f32ae6`)

- **The harvester's withdraw-scope refusal named a cause the probe can't
  know.** `has_withdraw_scope() == False` proves Kraken denied
  `WithdrawMethods` for a *valid* key — it cannot distinguish "this key
  lacks the permission" from "this env var holds a different key than
  intended" (the second is what actually happened in production: the
  fix was minting/re-pasting the key in the Portainer stack env, and the
  harvest daemon then started clean for the first time). The message now
  names both causes and the discriminating next step. (`ee9ceda`, PR #98)

- **Anthropic adapters 400'd on every Claude 5 call.** Anthropic deprecated
  the `temperature` field starting with the Claude 5 generation; both
  `AnthropicAdvisorAdapter` and `AnthropicAssistantAdapter` sent it
  unconditionally, so any Claude 5 model configured as the advisor
  escalation seat or the Discord operator assistant would have failed
  100% of calls with `400 invalid_request_error` — surfaced as a generic
  transport error, not "unsupported model". Latent rather than an active
  outage: shipped config runs `claude-sonnet-4-6` (generation 4), so the
  bug was armed for the first Claude 5 upgrade. Fixed via
  `anthropic.supports_temperature()`, which parses the major generation
  out of the model id — `claude-haiku-4-5` contains a "5" but is
  generation 4 and still takes the field, so a substring check would have
  broken every 4.5-tier model. Found by the 2026-08-10 model-roster run.

### Added

- **Pricing entries for `claude-opus-5` and `claude-sonnet-5`**
  (`services/llm_pricing.py`), so the ADR-014 gate admits them — it
  *raises* on an unpriced model rather than estimating, so this is what
  makes them runnable at all. Sonnet 5 is deliberately billed at its
  standard `$3/$15` rather than the introductory `$2/$10` in effect
  through 2026-08-31: over-pricing is this module's stated safe
  direction, and the 180-day freshness test cannot catch a price that
  changes on a known future date. Sonnet 5 spend therefore reads ~50%
  high until 2026-09-01, when the entry becomes exact on its own.

### P3 — ops/observability/UX, STARTED (2026-08-08)

- **Slice 1 — stale-heartbeat Discord push alert.** `cli/operator` grows a third
  background loop that checks every daemon's heartbeat freshness each minute (reusing
  `/health`'s exact staleness definition) and pushes Discord alerts on stale
  transitions — `critical` for the `restart:"no"` money-path daemons (live/harvest),
  `warning` for the rest, 6h repeat while down, recovery notice on return. Closes the
  gap that let the 2026-07-20 NAS reboot leave live+harvest dead for 11 unnoticed days.
  Its first live check caught a real one: cli/harvest down since the 2026-08-05 bump
  (the 2.0.0 key-scope gate refusing a Harvester key without Withdraw scope, ADR-003).
- **Slice 2 — alert-quality follow-up.** `operator.heartbeat_alert_mute` (explicit
  expected-down list — muting silences the Discord push only; `/health` keeps showing
  the truth) and cli/news reclassified from content-freshness to a real liveness
  heartbeat (`news.operator_db`) — a quiet news night no longer reads as a stale
  daemon. `fetch_daemon_freshness` drops its now-unused `news_db` parameter.
- **Slice 3 — `engine_state` keystone (ADR-030).** New per-symbol visibility table in
  operator.db: `cli/live` publishes paused/offside/anchor state each tick (best-effort,
  read from engine accessors), and the dashboard renders PAUSED/OFFSIDE badges from
  rows fresher than ~3 engine ticks — a dead engine's state ages out within one
  dashboard refresh, and the "web sees all symbols active" gap is closed. Unblocks the
  re-anchor chain (ADR-031 command → banner button → state-aware pause/resume).
- **Slice 4 — operator-initiated re-anchor command (ADR-031).** "Re-anchor BTC" over
  Discord now re-centers a parked grid on the current price without a restart (no DMS
  bounce): cancel-first atomically — any failed cancel aborts with the old anchor
  untouched — then a fresh grid placed in-process, through the same confirm-before-
  execute firewall as every command. Bundled: a three-way catalog drift test (typed
  unions ↔ help catalog ↔ intent-parser prompt). Live e2e verified 2026-08-09
  (operator-approved BTC re-anchor on the NAS; three findings queued — see
  `docs/planning/roadmap.md`).
- **Slice 5 — re-anchor banner action button + snooze.** The dashboard's re-anchor
  recommendation banners grow their two buttons: **Re-anchor** routes through the
  same `pending_commands` confirm page as every mutation (ADR-002 firewall — the
  button itself moves nothing), and **Snooze 24h** suppresses that symbol's banner
  via a new UI-local `reanchor_snoozes` table in operator.db (deliberately NOT a
  firewall write — hiding a banner moves no money; survives daemon bounces; a
  snooze-lookup failure shows every banner rather than hiding one). Each banner
  now also carries the fee-only decision economics line the operator asked for:
  projected cost ≈ 0.40% taker on the cancelled + re-laid ladder notional
  (paper-loss-on-stranded-inventory rejected as misleading — cancelling sells
  nothing).
- **Slice 6 — state-aware per-symbol pause/resume buttons.** The status card
  renders exactly one action per symbol from the fresh `engine_state` row: resume
  on paused symbols, pause on everything else — including absent or stale state,
  the deliberate safe default (an extra pause is an idempotent no-op; a blind
  resume could restart trading on a dead engine's old claim). Paused sections dim.
  Offside remains a badge, never a button — the lever against offside is
  re-anchor. Pure template + CSS; the ADR-002 firewall path is unchanged.
- **Slice 7 — bespoke notification embeds + command-result echo.** The proactive
  Discord cards (session start/end, fills, loss cap, harvester proposals,
  withdrawals) get the per-event embed treatment the query responses got in v1.0:
  a typed `NotificationEvent` union rendered by `match` — green for wanted
  activity, red for stop-the-presses, amber when money moved — replacing the
  one-size-fits-all title+dict-dump card. Zero schema migration: events ride the
  existing `context_json` column; pre-existing rows and the deliberately-generic
  raise sites (heartbeat alerts, maintenance) keep the legacy card. And the ✅
  finally gets its receipt: every dispatched command now echoes its result back
  to Discord as a typed `command_result` card ("re-anchored BTC/USD: … placed
  0/6") instead of recording it only in the web history — closing the gap that
  hid a zero-order re-anchor outcome during the 2026-08-09 live test.
- **Banner redesign (slice 5 follow-up).** The re-anchor banner's prose
  paragraph became a structured card in the dashboard's own idiom: title +
  severity chip, a scannable stat row (price, drift, anchor, order age,
  projected fee), and a proper action hierarchy — filled severity-tinted
  Re-anchor, quiet ghost Snooze. Operator-requested after a live design review.
- **Dashboard polish.** A fill that arrives between refreshes flashes once so
  you catch it, then settles. The advisor page collapses older suggestions to
  their headers — symbol, time, model and confidence stay visible — so a busy
  advisor no longer buries the page. The `/cost` page refreshes smoothly
  instead of hard-cutting.
- **Stopping everything no longer looks like pausing one symbol.** Confirming
  `stop`, `pause all`, or `cancel open orders` now carries a warning that it
  affects every symbol. Both confirm screens also name the action the same
  way — one of them used to show the internal code (`stop`) where the other
  said "Stop the engine".
- **Re-anchor banners now say whether re-anchoring would actually help.** The
  banner already told you a grid was misplaced; it now also shows how much the
  market is moving relative to your grid spacing, over two windows — the last
  2 hours and the last two weeks. A reading well under 1× on both means a
  freshly-placed ladder would just sit there, so pausing the symbol may beat
  re-anchoring it. The banner still shows at full severity either way: a
  drifted grid in a dead market is still idle capital, and the number is there
  to inform your call, never to hide the warning.
- **Each symbol card shows what you're holding.** A line in every symbol header
  now reads `holding 0.00131400 BTC ≈ $101.83`, so an `insufficient balance`
  refusal is explainable from the same card that shows the orders and the
  price. The per-symbol figures are derived from the same rule as the
  scoreboard's "in positions" total, so they always add up to it.
- **Recent Fills gained an age column and a summary line.** Each fill shows how
  long ago it happened, and a subhead above the table gives the buy/sell split,
  the signed USD flow, and total fees for exactly the fills shown. The
  "last fill X ago" freshness signal moved down here from the card header,
  where it couldn't say which of six symbols had filled.
- **Recent Cycles says when a profit is drift, not grid spread.** A cycle that
  sat on inventory for days realizes mostly price movement, but it lands in
  "today's PnL" because that's the day it closed — one such row once showed
  +$0.3460 among +$0.05 neighbours and read like a great day. Those rows now
  carry a **"held 3d 0h"** tag, and a separate **"inferred"** tag marks cycles
  where the matcher had to guess which BUY was closed (pre-engine inventory,
  manual fills, or a counter canceled by a cap trip or re-anchor). The PnL
  numbers themselves are unchanged.
- **Notifications can be marked read, and the bell agrees across devices.** The
  unread dot used to live in your browser, so clearing it on the desktop left
  your phone still dotted — and simply opening the notifications page counted
  as reading everything. Read state is now stored server-side: each row gets an
  **Acknowledge** button, the page gets **Mark all read**, unread rows are
  visibly distinct, and the bell reflects a real count. Notifications also
  link to the page that explains them — fills and cap trips to the dashboard,
  proposals and withdrawals to the Harvester page. Marking something read is a
  local UI action; it never enters the command queue that daemons act on.
- **Status reports and session-start cards are readable on mobile.** The eight
  tallies at the bottom of a Discord status report pack into three rows instead
  of sixteen stacked lines, so the narrative you asked for is no longer buried
  under the counters. The session-start card's four figures got the same
  treatment.
- **The logging audit is now enforced, not just documented.** The scan that
  drove it ships as `tools/scan_logging.py`, and a test asserts the package
  stays at zero violations — so log quality can'''t quietly drift back.
- **Money figures in logs are readable.** An average-cost line printed
  `73390.78543435964243143764881`; it now reads `73390.78543`, and a loss
  percentage reads `8.74%` instead of 28 digits. Applied across the engine,
  reconciler and daemon logs.
- **The logging sweep is complete.** Every module and severity now names its
  entity in the message itself, so the log tail is readable without a JSON
  viewer.
- **Daemon and withdrawal logs now say what happened, not just that it did.**
  Warnings and errors from the live, operator, and harvest daemons carry the
  symbol, order, proposal id and the actual numbers in the message itself, so
  the log tail is readable without a JSON viewer. A refused withdrawal now
  reads "refusing p-x: $342.18 would push today's withdrawals ($700) past the
  $1000 daily cap" instead of a bare "refusing". Money amounts also render as
  $342.18 rather than $342.18000000 (or $1E+2).
- **Clearer error when a model reply is cut off.** A truncated JSON response now
  says so — and points at the output-token cap — instead of reporting the same
  "no parseable JSON" message used when a model ignores the schema entirely.
  The two need opposite fixes.
- **A decided Discord confirmation now looks decided.** Approving or rejecting
  rewrites the card itself — green "Approved", grey "Rejected", amber "Not
  applied" when the decision arrived too late to take effect — instead of
  leaving the original "Confirm command" card sitting underneath looking like
  it was still waiting for an answer. The original request is carried forward
  on the card so the message still records what was decided.
- **Discord confirmations are buttons now.** Approving a command in Discord is a
  click on Approve or Reject instead of a ✅/❌ reaction, and the buttons keep
  working after the bot restarts — previously a restart silently orphaned any
  confirmation still waiting for you. Buttons also answer back: approved,
  rejected, expired, or "you're not on the allowlist", instead of a reaction
  that did nothing without saying why. Requires discord.py 2.4+.
- **Execute a transfer proposal from the web (ADR-034).** The Harvester page now
  offers an Execute button on actionable proposals: confirm the amount and destination
  in a card, approve, and watch cli/harvest carry it out — the same flow the dashboard
  actions use. The web never withdraws; it queues a command that only the Harvester (the
  sole module with a withdraw-scoped key) can execute, and the daemon re-checks the
  approved amount and destination against the stored proposal before moving anything.
  The page says so plainly when the harvest daemon isn't running.
- **Readable links.** Links inside page content and the footer used the browser's
  default dark blue, which was effectively invisible on the dark theme — including
  the /health links the app points you at when something needs checking. They now
  use the theme's link colour.
- **Slice 13 — modal-card action flow.** Dashboard actions no longer navigate
  through interim pages: pause/resume, banner Re-anchor, and Emergency Stop
  open a card over the dashboard — confirm in place, watch the execution in
  the same card, and the status card refreshes the moment it completes.
  Closing the card at any point (Close, Cancel, Escape, backdrop) also
  refreshes the status card, so an early close never leaves the dashboard
  showing pre-action state. Progressive enhancement: without JavaScript
  every full-page flow still works exactly as before.
- **Slice 12 — web actions wait for completion.** Approving a command in the
  web UI now follows it to the actual outcome: the result page watches the
  row until cli/live executes it and shows the real result ("executed —
  paused BTC/USD"), warns honestly when pickup is slow (with a pointer to
  /health), and never executes anything itself — the firewall's poll remains
  the only path to the engine.
- **Slice 11 — layout starvation back-off.** A grid layout that places zero
  orders (funds reserved elsewhere + sells cost-basis-deferred) now enters a
  starved state: one warning with the full breakdown, then a retry every ~5
  minutes instead of the silent every-tick busy loop the 2026-08-09 re-anchor
  test uncovered. Any successful placement clears it. Closes the last finding
  from that live test.
- **Slice 10 — LLM health on /health + cold-start parse fix.** The /health
  page gains an "LLM Endpoints" card probing whatever is configured — Ollama
  and any cloud provider with a key — via free endpoints on a 60s cache; a
  dead endpoint turns the dot yellow with a plain-English detail ("key
  rejected (rotated?)") instead of waiting for a Discord "Sorry, I couldn't
  process that." And that failure mode itself shrinks: the operator
  assistant now retries once on a read timeout, converting the
  first-message-after-restart cold-cache miss (verified live 2026-08-09)
  into a slow success.
- **Slice 9 — logging-quality audit, installment 1 (engine path).** Every
  state-change line in `grid_engine`/`reconciler`/`cost_basis` now says
  what/which/how-much in the message itself — fills carry symbol/side/
  amount/price, re-layout completions carry placed-vs-target counts, offside
  and sell-guard transitions name the symbol and numbers — so the container
  tail is finally a readable activity view (the bare `grid fill` / anonymous
  `grid offside; parking` era ends). Conventions ratified in
  `docs/implementation/logging-conventions.md`. Log text only; zero behavior
  change.
- **Slice 8 — Docker HEALTHCHECKs on all 8 services.** A wedged-but-alive
  daemon (stuck socket, blocked Ollama, deadlocked write) used to show green
  in Portainer forever; now every container runs `tools/healthcheck.py` —
  daemons classify their own heartbeat/content freshness through the exact
  machinery the /health page uses, and the web container does a liveness GET
  against a new unauthenticated, content-free `/healthz`. Exit codes strictly
  0/1 (Docker reserves 2). Interval operator-tunable via
  `HEALTHCHECK_INTERVAL`.

### P2 — data-infrastructure spine, COMPLETE (2026-08-07 → 2026-08-08)

Full P2 phase of `docs/release/v1.1/README.md`'s plan, six slices, per-slice receipts
(with commit hashes and live-verification notes) in `docs/planning/roadmap.md`'s v1.1
Track item 4. 2767 tests passing by the end (was 2609 pre-P2). Real-money cost
$0.00 — every slice runs on public/read-only or offline data.

- **Slice 1** — `cli/observe --backfill` ergonomics: `--days`, `--catchup`, progress,
  `--rate-limit-seconds`, `--resume` (interval-scoped cursor), `--intervals`, horizon
  WARN. Surfaced: Kraken's live OHLC endpoint retains only ~720 bars/interval.
- **Slice 2** — `tools/import_kraken_history.py` + `OHLCBar` validator +
  `StoragePort.get_ohlc_bars` read-side; the only deep-history path.
- **Slice 3** — `services/ta_metrics.py` (8 hand-rolled indicators) → 16 TA fields on
  `PerformanceSummary` via `SummaryBuilder`; staleness guard. Follow-up: hourly 60m-bar
  top-up in `cli/observe` (`bar_topup_enabled`); backfill mode split to
  `cli/observe_backfill.py`.
- **Slice 4** — `tools/auditor.py` (ADR-028): replay `settings.yml` through the real
  `GridEngine` over stored bars; directional, not exact.
- **Slice 5** — `cli/screener` v1: rank observed symbols by grid-suitability
  (band-distance vol/ATR%, flatness, correlation annotation); offline, advisory.
- **Slice 6** — configurable counter-order target (ADR-029): `counter_target_mode` on
  `GridLevels`, `spacing_up` (default) | `top_sell` (BUY-fill counter → band ceiling).
  Auto-apply-excluded by construction; `!grid` surfaces it; inventory-accumulation risk
  documented in known-limitations.

### Post-merge fleet-review fixes + ADR-033 (2026-08-05 → 2026-08-07)

- **#30** — web: optional-DB warning no longer crashes the whole dashboard.
- **#31** — services: LLM retry-exhaustion no longer crash-loops the daemon.
- **#32** — adapters: additive column migrations tolerate cross-process races
  (`_add_column_if_missing`).
- **ADR-033** (#33) — cache-aware LLM cost accounting: OpenAI automatic-cache hits are
  now priced at the cached-input rate in the ADR-014 ledger (they were billed at full
  input rate); Anthropic `cache_control` enablement stays deferred with triggers.

### The `v1.1`-branch phases (P1–P4)

### P1 — Safety-hardening + ready-now backlog, COMPLETE (2026-07-31 → 2026-08-01)

Full P1 phase of `docs/release/v1.1/README.md`'s plan (see that doc's P1 table and
`docs/planning/roadmap.md`'s v1.1 Track item 2 for per-item receipts + commit hashes). One
focused commit per item; full gate (pytest/mypy/pylint 10.00/black/isort) green before
each. 2582 tests passing by the end (was 2369 pre-P1). Real-money cost $0.00.

- **ADR-032** — cost-basis sell guard added; the dead, silently-inert `EmergencyStopConfig`
  retired (an operator reasonably believed it was an enforced balance floor; it did
  nothing).
- **ADR-023** — unified terminal-order resolution: one shared `_resolve_terminal_order`
  fixes both the startup reconciler's fill-vs-cancel disambiguation AND the live
  `_detect_fills` gate's partial-fill Trade-drop (the F1 defect) — the same root cause,
  two call sites, one fix.
- **ADR-024** — session-loss-cap cool-down period: new `cap_trips` table, pre-loop gate,
  new exit code 4, fail-open on a storage error so a DB hiccup can't crash-loop the daemon.
- **ADR-025** — pre-placement slippage/spread guard via a new `get_ticker` port method and
  a pre-tick spread gate; a same-day follow-up fixed an uncaught exchange-side placement
  error that was aborting an entire tick's remaining levels instead of skipping just the
  one refused order (worse than the original bug report suggested).
- **ADR-026** — harvester `--execute` replay guard, DB-enforced via a UNIQUE index on
  `transfer_results.proposal_id` — the highest-blast-radius hole in the codebase (no
  "already executed for this proposal_id" check existed before this).
- **ADR-027** — Kraken rate-limit backoff + inter-cancel pacing, reusing the ADR-015
  cloud-LLM retry shape.
- **ADR-007 amendment** — structural MoE news-firewall fix: a `news_materially_drove` flag
  now blocks `role='aggregated'` auto-apply whenever news was the effective driver of the
  aggregated number, closing a gap where ADR-007's "news cannot drive an auto-applied
  change" intent was enforced only by the arbitrator prompt, not the code.
- Dead-man's-switch arm confirmation (logs Kraken's `triggerTime` instead of discarding
  it), boot-time stale-anchor WARN on restart re-layout, per-tick price-fetch dedup (one
  fetch threaded through instead of two), partial-grid insufficient-balance refusal
  demoted WARN→DEBUG, today's-PnL fetch limit raised 100→10,000 (was silently undercounting
  on any day with >100 trades).
- Engine ordermin-awareness: the **containment** half only — an uncaught exchange-side
  ordermin/costmin rejection was aborting an entire placement loop, not just the doomed
  order; now caught and logged as a refusal. The proactive half (bump volume to clear
  ordermin, or skip with an INFO, before attempting) was not built.
- Content-Security-Policy middleware, monthly backup-restoration smoke test (opens the
  latest backup, runs `PRAGMA integrity_check` + representative SELECTs), a Kraken
  exchange-status news adapter (`status.kraken.com` → tagged `news_items`), and a footer
  "update available" indicator (polls GitHub releases every 6h).
- **Dashboard session-cap card** — a durable Session banner sourced from the existing
  `cap_trips` table: last trip's timestamp + PnL, plus whether the ADR-024 cool-down gate
  is currently active. Fixes the soak's "cap tripped unnoticed ~1.5h" failure mode (a trip
  during a missed Discord ping now persists on next dashboard load instead of only having
  existed as a transient notification). Deliberately scoped to this durable-signal tier,
  not a live "% of cap consumed" gauge — that needs a new per-tick write from the hot
  trading loop, a materially larger change than this fix earns.
- Five test-hardening additions closing gaps the 2026-06-02 test-honesty audit found
  (decision logic was pinned, the consequence/orchestration wiring around it wasn't): a
  loss-cap-trip end-to-end test (asserts the exit code AND that every resting order is
  actually canceled, AND the strict `<`-not-`<=` cap boundary), a preflight-gate
  orchestration test (the ADR-003 key-scope gate pre-empts both the reference-price fetch
  and `engine.step`), an operator firewall-bypass negative test (a bare command intent
  never actions the engine without an explicit confirm dispatch), a reconciler fail-soft
  continuation test (one bad row's `StorageError` doesn't abort reconciling the rest of the
  batch), and the F1 partial-fill test that came free with the ADR-023 unification.
- **Explicitly not built**, per the plan's own open questions rather than oversights:
  `cli/up` (one-command daemon orchestrator — no restart friction reported) and additional
  Kraken pairs (which coins / what capital split is an operator risk-budget call).

### LLM pricing re-verification (2026-07-23)

- **Cleared the 2026-01-15 pricing anchor.** The freshness watchdog
  (`test_llm_pricing_freshness`) tripped on 2026-07-14 at the 180-day threshold, failing
  the whole suite and blocking every open dependency PR. All seven entries re-verified
  against the providers' own pages; six confirmed unchanged: `claude-sonnet-4-6`
  ($3.00/$15.00), `gpt-4o` ($2.50/$10.00), `gpt-4o-mini` ($0.15/$0.60), `o1`
  ($15.00/$60.00), `o3-mini` ($1.10/$4.40), and `gemini-2.5-pro` ($1.25/$10.00 at the
  ≤200k tier we bill against). `claude-sonnet-4-6` had been re-verified separately on
  2026-07-12; confirming it again in this sweep folds it onto the same anchor, so the
  pricing table is byte-identical to the one on `main` and won't conflict at merge.
- **Fixed: `gemini-2.5-flash` was over-billing thinking tokens by 1.4x.** It carried a
  $3.50/1M thoughts override — correct when Flash was in preview and billed thinking
  separately from its then-$0.60 output rate. Google has since folded the two together
  ("Output price (including thinking tokens)", $2.50/1M), so the override was charging
  thoughts at $3.50 against a rate that already included them.
  `reasoning_per_million_usd` is now `None` (falls back to output) per the module
  convention. Recorded cost for a 100-in/100-out/300-thought Flash call drops from
  $0.001330 to $0.001030.
- **No live entry overrides the reasoning rate any more.** The override column stays —
  the next provider to unbundle will need it — and `test_reasoning_uses_explicit_override`
  now exercises that branch against a synthetic price point instead of a real entry, so
  it no longer depends on what providers happen to bill. New
  `test_no_live_entry_overrides_reasoning_rate` guards the docstring's claim and points
  at both places to update if that changes.
- **OpenAI source URL corrected.** `openai.com/api/pricing` now 403s, and its successor
  (`developers.openai.com/api/docs/pricing`) lists only the gpt-5.x line — the four legacy
  models are priced on their individual `docs/models/<id>` pages. Noted inline so the next
  re-verification doesn't hit the same dead end. Only dated snapshots
  (`gpt-4o-2024-08-06`, `o1-2024-12-17`, `o3-mini-2025-01-31`) are tagged Deprecated; the
  floating aliases we bill against are live.

### Dead man's switch (2026-06-01, ADR-021)

- **Server-side dead man's switch.** New
  `ExchangePort.set_dead_mans_switch(timeout_seconds)`; `KrakenAdapter` calls
  `/0/private/CancelAllOrdersAfter`, synthetic adapters no-op (shadow deliberately does
  not arm a real timer on the wrapped live account). `cli/live` pings it every tick and
  disarms only on a confirmed-clean shutdown cancel — if the host dies (crash, power
  loss, network partition) Kraken auto-cancels all open orders once the timer lapses, the
  failure mode the `finally`-block cancel cannot cover (2026-05-19 outage). On by default
  at 60s (`live.dead_mans_switch_seconds`; `null` disables; floor
  `max(10, 2 × tick_seconds)`). Note: Kraken's timer is account-wide. Real-money cost
  $0.00.

### Planning (2026-06-01)

- **v1.1 plan + index.** `docs/release/v1.1/README.md` — the sequenced master plan
  (phases P0–P4, the dependency spine, the parked register, guardrails, open questions),
  synthesized from a full inventory of the v1.1 backlog. The per-area docs in that folder
  remain the detail; the roadmap's v1.1-track section points here. (The owed v1.1
  consolidation pass.)

## [1.0.0] - 2026-07-31

### LLM pricing re-verification (2026-07-23)

- **Cleared the 2026-01-15 pricing anchor.** The freshness watchdog
  (`test_llm_pricing_freshness`) tripped on 2026-07-14 at the 180-day threshold, failing
  the whole suite and blocking every open dependency PR — including the Starlette security
  bumps. All seven entries re-verified against the providers' own pages; six confirmed
  unchanged: `claude-sonnet-4-6` ($3.00/$15.00), `gpt-4o` ($2.50/$10.00), `gpt-4o-mini`
  ($0.15/$0.60), `o1` ($15.00/$60.00), `o3-mini` ($1.10/$4.40), and `gemini-2.5-pro`
  ($1.25/$10.00 at the ≤200k tier we bill against).
- **Fixed: `gemini-2.5-flash` was over-billing thinking tokens by 1.4x.** It carried a
  $3.50/1M thoughts override — correct when Flash was in preview and billed thinking
  separately from its then-$0.60 output rate. Google has since folded the two together
  ("Output price (including thinking tokens)", $2.50/1M), so the override was charging
  thoughts at $3.50 against a rate that already included them.
  `reasoning_per_million_usd` is now `None` (falls back to output) per the module
  convention. Recorded cost for a 100-in/100-out/300-thought Flash call drops from
  $0.001330 to $0.001030.
- **No live entry overrides the reasoning rate any more.** The override column stays —
  the next provider to unbundle will need it — and `test_reasoning_uses_explicit_override`
  now exercises that branch against a synthetic price point instead of a real entry, so
  it no longer depends on what providers happen to bill. New
  `test_no_live_entry_overrides_reasoning_rate` guards the docstring's claim and points
  at both places to update if that changes.
- **OpenAI source URL corrected.** `openai.com/api/pricing` now 403s, and its successor
  (`developers.openai.com/api/docs/pricing`) lists only the gpt-5.x line — the four legacy
  models are priced on their individual `docs/models/<id>` pages. Noted inline so the next
  re-verification doesn't hit the same dead end. Only dated snapshots
  (`gpt-4o-2024-08-06`, `o1-2024-12-17`, `o3-mini-2025-01-31`) are tagged Deprecated; the
  floating aliases we bill against are live.

### Stage 8.6 (2026-05-30) — Advisor hardening + grid widen (pre-soak)

Acts on the grid-backtest verdict before the v1.0 gating soak. Rescoped
from "advisor regime reorientation" to hardening-only after the
regime-switching research arc closed (heuristic regime detection doesn't
beat buy-and-hold), then narrowed further by measurement during the
slices. Full account:
`docs/reference/grid-strategy-research-synthesis-2026-05-30.md`.

- **Widened the live BTC grid 1.0% → 3.0%** (`grid.default.spacing_percentage`,
  synced across `settings.example.yml` ↔ the deploy-master `settings.yml`).
  3% is the least-bad *static default* — it survives every regime; the
  backtest showed no static spacing beats hold over full cycles. Exposure
  unchanged ($60 = 3+3 × $10). ADR-006 park-when-offside unchanged.
- **Documented the heuristic lookback coupling** instead of "fixing" it.
  Measurement reversed the planned window-widen: at 3% the grid completes
  only ~0.2–0.4 cycles/day, so `dont_fix_working` (cycles_min 8) is
  unreachable in any volatility-current window, and widening the window
  would make the −5% drawdown guards fire on ordinary daily noise.
  `advise.metrics_lookback_hours` stays at 6h; `dont_fix_working` stays
  enabled but documented-dormant at wide spacing (it auto-re-arms for the
  MoE world's tight grids). A `config/heuristic/quant.yml` comment only.
- **Deferred the vol→spacing curve recalibration** to the Oracle/regime
  track. Recalibrating to "rest at 3%, never tighten" would bake in a
  false absolute (a tight grid chosen in chop and pulled before the trend
  works — proven live + a +164.6% perfect-foresight oracle), and would
  invalidate the blessed 20-fixture judgment battery. The advisor is
  advisory-only (`auto_apply` off) during the soak, so its mis-calibrated
  curve is harmless log-noise.
- **Ratified ADR-019** — advisor purpose: regime reader + transparent
  guardrail, not a volatility tuner; posture-output-advisory-only
  invariant; refines ADR-002/007. **ADR-020** (regime as a first-class
  metric) deferred with the parked regime/Oracle track.

No code paths changed; advisor stays advisory-only. Real-money cost
$0.00 (offline backtests only).

### Stage 8.5 (2026-05-29) — Advisor heuristic+LLM cascade (pre-soak)

A pre-soak value-add so the v1.0 gating soak runs on the real advisor.
An investigation settled "would an LLM advisor help?" empirically: no
local CPU model reasons well enough for the advisor role (best 16/36 on
the 12-fixture battery; a constant scores ~19/36), but a frontier
reasoning model + a complete prompt is genuinely good (`o3` and
`claude-opus-4-8` each 4/4 on the held-out conflict discriminators). The
operator chose `o3`, then refined to a heuristic+LLM cascade. Design +
as-built: `docs/planning/stage-8.5-advisor-cascade-design.md`.

- **Operator-tunable heuristic spec** — `config/heuristic.py`
  (`HeuristicSpec` + `load_heuristic_spec`) + committed default
  `config/heuristic/quant.yml`. The ideal-spacing-vs-volatility curve,
  fee floor, hold deadband, and four guards (with per-guard on/off
  toggles) are operator-editable DATA; the guard algorithm + priority
  order stay in code. Same ownership model as the prompt files.
- **`HeuristicAdvisorAdapter`** (`adapters/heuristic_advisor.py`) — a
  deterministic, $0 advisor. Loaded from the shipped spec it reproduces
  both probe batteries (core 36/36, held-out 24/24) and exposes a
  `clear_match` escalation signal.
- **`CascadingAdvisorAdapter`** (`adapters/cascading_advisor.py`) —
  heuristic-first; escalate ambiguous calls to the LLM; fall back to the
  heuristic on LLM failure / cost-cap (so the advisor never stalls).
- **`advisor.engine`** (`heuristic | llm | cascade`, default `llm` for
  back-compat) + **`advisor.heuristic_file`**, wired through
  `cli/advise`. The `cpu-only` profile now runs `cascade` + cloud `o3`.
- **Fixed a pre-existing daemon-crash bug:** `cli/advise` caught only
  `AdvisorError`, so a tripped `LLMCostCapExceeded` (a domain exception
  the cloud adapters bubble raw) would have crashed the `engine: llm`
  cloud path. Now caught + skips the tick.

2225 unit tests pass; mypy clean; pylint 10.00/10.

### Soak Day 11 events (2026-05-29) — NAS advisor model-sweep tooling

Built tooling to pick the advisor-role model empirically — the inverse
of the operator-role sweep: for a 4-hourly advisory daemon, accuracy +
schema-fidelity matter and latency doesn't. Rather than swap to a
guessed model, measure.

- `tools/probe_advisor.py` rebuilt around a **12-fixture battery**
  (balanced 4 widen / 4 hold / 4 tighten) with a no-partial-credit
  rubric (max 36). Current spacing is decoupled from direction via
  overlap fixtures, so a constant or do-nothing answer scores ~chance
  (33%) while a genuine reasoner clears ~75%. New `--base-url` /
  `--timeout-seconds` / `--json` flags + per-call latency capture so
  the production timeout can be sized to the winner.
- `tools/pull_and_probe_advisors.py` retargeted at a remote (NAS)
  Ollama over HTTP (`/api/pull` streaming, `/api/tags`, `/api/delete`)
  — no local `ollama` CLI dependency. Adds `--tier1` (accuracy-leaders
  fast pass), `--no-pull`, `--rm-after` (disk-bounding), atomic
  resumable `summary.json`, and a 30B+ size gate.
- `tests/tools/test_probe_advisor_scoring.py` (new, 10 cases) locks the
  rubric invariants: oracle 36/36, always-hold = chance, constant
  ceiling, no direction/magnitude dead zones.

Validated by **two multi-agent verification workflows**: 5-agent blind
fixture adjudication (**12/12 unanimous both rounds**) + adversarial
code review. Round 1 caught 4 real defects — including a high-severity
bug that laundered a truncated model pull into a fake "0/36, OK" row
that would have polluted the ranking. Round 2 confirmed the fixes and
caught a low-severity non-atomic-write issue (now fixed). The advisor
sweep itself runs next (operator-paced; `operator`/`advise` paused
during the run).

### Soak Day 11 events (2026-05-28) — advise daemon advisor-timeout fix

The NAS advise daemon had failed **100% of its advisor calls** since
the Day-10 deploy (every ~4h tick logged `advisor call failed (error:
Ollama request failed: )`). Root-caused from Portainer + Ollama logs:
the cpu-only single-LLM advisor (`llama3.1:8b-instruct-q4_K_M`)
generated toward `num_predict=512` at ~4 tok/sec on the CPU-only NAS,
exceeding the 120s client timeout on every (cold) tick — Ollama's GIN
log showed `500 | 2m0s` from the advise container's IP at the exact
failure timestamps. The operator daemon was unaffected because its
post-sweep `qwen2.5:1.5b` model finishes in seconds.

**Fixes (this push):**
- `config/prompts/quant.md`: new constraint #5 caps `rationale` at
  ≤2 sentences (~50 words) so the model stops well short of the
  512-token ceiling — completing in ~40–60s even on a cold load.
  This also matters because `OLLAMA_NUM_PARALLEL=1` makes a long
  advisor call block the interactive operator (Discord) intent-parse
  queued behind it.
- `config/settings.example.yml`: cpu-only advisor `timeout_seconds`
  120 → 180 (cold-load + contention margin); corrected the stale
  "200-tok / ~35s" comment to the measured reality.
- `adapters/ollama.py` + `adapters/ollama_assistant.py`: wrap
  transport errors with `type(exc).__name__` so a bare `ReadTimeout`
  (empty `str()`) no longer renders as an uninformative
  `Ollama request failed: `. This incident needed Ollama's own log
  to diagnose precisely because the wobblebot-side message was blank.

Config + prompt are bind-mounted on the NAS, so the operator applies
the `settings.yml` / `quant.md` edits there; the adapter logging fix
ships in the image via the next GHCR build + stack redeploy.

### Soak Day 6 events (2026-05-23) — graceful-shutdown bump + logging audit + news pipeline

Heaviest doc-and-polish day of the soak. Three workstreams shipped
in 18 commits without disrupting the v1.0 boundary work:

**1. v1.1.A bumped to v1.0 — graceful-shutdown timeout for daemons**
(`49e53a7` / `34c9619` / `a998b71` / `8a85cbd` / `516f4f8`). After
4 observed `cli/web` hang-after-SIGINT instances during the soak
(each requiring `Stop-Process -Force`), the operator made the call
to bump the entry from v1.1.A into v1.0.

New `wobblebot.cli._common.safe_shutdown(cleanups, *,
timeout_seconds=10.0, logger)` helper takes a list of
`(phase_name, async_callable)` cleanup tuples, runs them
sequentially under `asyncio.wait_for`, and on timeout logs a
WARNING naming the in-progress phase + calls `os._exit(1)` to
release the terminal. Wired into 8 daemons:

- `cli/observe`, `cli/news`, `cli/advise`, `cli/harvest`,
  `cli/operator` (slice 2): poll-loop daemons' finally blocks
- `cli/maintenance` (slice 3): single `close_operator_storage`
  phase
- `cli/web` (slice 4): finally + uvicorn
  `timeout_graceful_shutdown=5` (caps in-flight-request waiting);
  combined budget ~15s worst case vs the 3+ minute soak hang
- `cli/live` (slice 5): OUTER finally only — INNER finally with
  `cancel_all_open` intentionally NOT routed through safe_shutdown
  because Kraken cancellation is the most safety-critical cleanup

7 unit tests + 80 existing daemon tests pass unchanged. Soak
runbook updated: "shutdown hung beyond timeout; forcing exit"
WARNING is now the expected protection-firing signal (capture the
phase name for the bug report; the WARNING is the recovery, the
stuck phase is the actual defect).

**2. Logging audit + level rebalancing** (`c9e7781` / `664fbfb` /
`28c903e` / `6b0f770` / `042f51b`). Codebase audit of all 257
`_LOGGER.*` calls across 23 files. Pre-audit ratio was 99 INFO /
124 ERROR / 32 WARNING / 2 DEBUG — upside down. Adjustments:

- Per-tick "tick complete" in cli/live + cli/shadow → DEBUG
  (was flooding operator terminal at 5s cadence)
- 4 INFO → DEBUG (per-tick non-events: harvest "no proposal",
  maintenance "no prune_source_db configured", observe "account
  empty" + "snapshot saved")
- 22 ERROR → WARNING (recoverable transient failures across
  cli/live, cli/shadow, cli/harvest, cli/observe, cli/news,
  cli/advise, cli/maintenance, cli/operator, grid_engine — the
  loop continues, next tick retries, or reconciler is the
  recovery path)
- 1 missing DEBUG (cli/live `_process_pending_commands`
  empty-list case)
- news.py "news item deduped" → DEBUG (per-dedup chatter; can be
  100+ per cycle in steady-state)

CRITICAL-tier calls verified correctly tagged (cap trips,
withdrawal-submitted-but-audit-lost, Kraken /Withdraw rejection,
Discord transport failure, startup reconciliation failure).

**3. News pipeline: publisher attribution + click-through URLs**
(`9dd8640`). Two related v1.1 candidates promoted in one commit:

- `NewsItem` gains `publisher: str | None` and `url: str | None`
  (additive; frozen-compatible)
- New `_migrate_news_items_publisher_url` adds both columns to
  `news_items` via idempotent PRAGMA-checked ALTER TABLE; existing
  ~3882 cryptocompare + 460 RSS rows stay valid with both columns
  NULL
- CryptoCompareNewsAdapter extracts `source_info.name` →
  publisher (CoinDesk / Bloomberg crypto / etc.) and top-level
  `url` → url
- RssNewsAdapter extracts entry `link` → url; publisher stays
  None (source_id IS the publisher for direct RSS)
- `/news` web view: headlines wrap in
  `<a target="_blank" rel="noopener noreferrer">` when url
  present; publisher renders as small italic muted label next
  to the source tag
- 9 new tests (5 cryptocompare extraction + 1 rss happy-path
  extension + 3 storage tests including a legacy-table migration
  test verifying the operator's existing rows survive intact)

**4. v1.0-future-improvements.md doc reorg** (`a52feb2` earlier this
day, plus catalog updates today). The monolithic 2716-line doc
split into feature-area files under `docs/release/v1.1/` (10
files: standing-rules / adaptive-grid / news-pipeline /
harvester / engine / observability / operator-ux / trading-scope
/ infrastructure / external-triggers). The original path at
`docs/release/v1.0-future-improvements.md` rewritten as the slim
catalog INDEX so external links stay valid. Pinned OC project
memory captures the new structure + the "OC + repo layered
model" pattern.

**Numbers at end of day 6 morning**: 1922 unit tests pass (1907
→ 1922, +15 from publisher/url + safe_shutdown tests); mypy 110
src files clean; pylint 10.00/10; black + isort clean. Real-money
cost stays at $0.085018 (no live trades this day).

**Schedule context.** The operator's move next weekend with
uncertain internet availability reframes the current soak as a
pre-cursor; the v1.0-gating soak will restart ~2026-06-01 with the
new code in place. The bumps and audit shipped today therefore
land BEFORE the real soak begins, giving them their own soak
window rather than burning the current one.

**Day 6 afternoon — cycle_matcher fix + Kraken reconciliation
closed** (`a9f3af1` / `35bc9c4` / `e094a3b` / `a01966c`).

Initial commit `a9f3af1` added a "Today's PnL" header + Recent
Cycles panel on the status card backed by a new
`services/cycle_matcher.py` (FIFO BUY→SELL pairing with realized
per-cycle PnL + `today_realized_pnl` UTC-day filter). Operator-
driven audit against live.db caught two display bugs and one
real algorithmic bug:

- **Threshold drift.** The 0.5¢ flat threshold + `%.2f` format
  was masking real sub-cent PnL: a +$0.0035 day rendered as
  `Today: $0.00`. Tightened to `$0.0001` threshold + `%.4f` on
  both the header and the cycle rows so they reconcile by eye.
- **"cost" column was gross-of-fee.** Recent Fills was showing
  `Trade.cost` (price × amount) under a column labeled "cost",
  but the operator's USD ledger actually moves by `cost ± fee`.
  Column relabeled "net USD" and rendered as `cost + fee` for
  BUYs / `cost − fee` for SELLs; tooltip exposes the math. Rows
  now reconcile directly against a Kraken account statement.
- **cycle_matcher amount-equality pairing** (`35bc9c4`). The
  matcher was FIFO-cheapest; the engine pairs counter-orders
  by amount (ADR-006 decision 2: counter sized to filled
  amount). On live.db that meant the matcher mispaired BUY #2
  ($76,859.50, amount 0.00013010) with SELL #7 ($76,874.60,
  amount 0.00013139) producing a fake −$0.0483 loss cycle, when
  the engine's actual pair was BUY #3 ($76,105.80, amount
  0.00013139) producing a real +$0.0508 win. Today's PnL was
  reading +$0.0035 (one win + one fake loss) when engine truth
  was +$0.1025 (two wins). Fix: amount-equality primary
  heuristic, FIFO-cheapest fallback for pre-engine / manual
  fills. 1 new regression test (`test_amount_match_beats_price_fifo`)
  reproduces the live.db mispairing exactly.

`a01966c` logged a cost-honesty dashboard v1.1 entry in
`docs/release/v1.1/observability.md` — sketch of a card that
puts realized PnL side-by-side with all-in operating cost
(trading fees + LLM API + operator-declared infrastructure)
so the operator can see "is this thing actually net positive?"
at a glance. Motivated by the operator's 2011-2012 GPU-mining
scar where electricity outran mined-coin value; the v1.0
infrastructure cost is mostly moot at current capital but
becomes important when scaling or toggling cloud-LLM advisors.

`e094a3b` logged a `cli/reconcile` v1.1 entry in
`docs/release/v1.1/engine.md` (augmenting the existing
"Mid-session reconciliation" entry per the standing rule).
Sibling daemon to cli/maintenance, polls Kraken
`/0/private/Ledgers` periodically and refid-diffs against
live.db trades. Provides external ground truth for trade
reconciliation that the existing `services/reconciler.py` —
open-orders-only, startup-only per ADR-018 — does not.

**Kraken ledger reconciliation:** operator pulled Kraken Pro
ledger + portfolio + funding-history; bot's live.db matches
Kraken within fee precision ($79.92 USD + 0.00026 BTC = $100.36
total). Funding history: single $100 deposit on 11/22/25, zero
withdrawals. The $10.54 phantom-BUY gap I claimed earlier in
the session was 100% my misreading of Kraken's "Available
balance" (it's USD-equivalent of unreserved assets across both
USD and BTC, NOT USD-only). Memory pattern captured.

**Numbers at end of day 6 afternoon**: 1941 unit tests pass
(1922 → 1941, +19 across cycle_matcher regression + the day-6
morning commits' tests counted at the boundary); mypy 110 src
files clean; pylint 10.00/10; black + isort clean. Real-money
cost stays at $0.085018.

**Day 6 evening — observe expansion + symbol validation + health
UX + code-reuse audit closure** (`dc0d428` through `29e3550`,
12 commits across ~5 hours).

`dc0d428` expanded `observe.symbols` from BTC-only to 12 major-cap
USD pairs (BTC / ETH / SOL / XRP / DOGE / ADA / AVAX / LINK / DOT
/ POL / LTC / BCH). The Day-2-outage cli/observe restart had been
running BTC-only for 3+ days, silently losing observation data on
every other pair — no upside; storage cost is ~5MB/pair/year. POL
substituted for MATIC after the Polygon-migration delisted
MATICUSD on Kraken (caught manually before commit; the next commit
prevents this kind of typo from crashing daemons).

`0007fc3` added `KrakenAdapter.partition_known_symbols` + a
graceful-degrade pattern in cli/observe / cli/live / cli/shadow
startup: hits `/0/public/AssetPairs` once, logs WARNING listing
any symbols Kraken doesn't trade, refuses to start only when EVERY
configured symbol is bad. The fix for the MATIC/POL discovery
above turned into a v1.0 daemon-resilience feature: a future typo
or delisting in settings.yml produces a clear startup warning
instead of a cryptic mid-poll `EQuery:Unknown asset pair`
traceback.

`4af26af` + `d2d40de` consolidated health UX. The status card's
inline traffic-light icon was redundant with the navbar's
heart-pulse icon; removed the card variant, upgraded the navbar
dot from binary red to tiered yellow/red (no dot when healthy)
with size 8→10px ("tiny-ish" per operator). LIVE badge gained
symmetric `margin-right: 6px` to balance the breathing room.
Single source of truth for health UX, consistent with the bell's
red-dot alert vocabulary.

**Code-reuse audit closure (4 commits).** The 2026-05-23 multi-
agent audit identified 12 duplication patterns across CLI /
adapter / web / test surfaces. Closed 6 patterns across 4 commits
and deferred 3 to v1.1 with rationale:

- `98ff7a9` (audit #1+#2) extracted `install_signal_handlers` (8
  daemons) + `run_with_clean_exit` (9 daemons) to
  `wobblebot.cli._common`. Replaces verbatim copies of the SIGINT/
  SIGTERM wiring + the `asyncio.run` + KeyboardInterrupt +
  `os._exit` wrapper. The wrapper specifically was the e3a11ce
  hotfix shape — 8 daemons literally had comments saying "matches
  the 2026-05-23 cli/web hotfix pattern (commit e3a11ce)". Now one
  helper means the next bug-fix lands once, not nine times. Net
  −140 LOC across daemons.

- `86837b8` (audit #3) extracted
  `partition_or_exit + SymbolPartitioner` Protocol from the
  symbol-validation block just shipped in 0007fc3 (3 daemons).
  Helper composes `safe_shutdown` for cleanups so failure-exit
  carries the same named-phase diagnostic as normal shutdown.

- `b2e972d` (audit #4+#9) made the operator's timezone preference
  actually apply site-wide. Two problems fused into one fix: the
  per-route `prefs = storage.get_user_preferences(user.id)` lookup
  duplicated 8x → new `get_user_preferences` FastAPI dependency in
  `wobblebot.web.auth` consumed via `Depends()`. Same commit fixes
  9 templates that used raw `.strftime("%Y-%m-%d %H:%M:%S UTC")`
  → `| tz_format(operator_tz, ...)`. The settings page was
  silently no-op on Audit / Advisor / Harvester / News / Command
  pages prior to this fix. Follow-ups `627025b` stripped "(UTC)"
  from column headers; `f948f2b` appended `%Z` to news published
  column so timezones travel with values.

- `d70e0a8` (audit #10) consolidated web-test login boilerplate
  into `tests/web/_helpers.py` (`TEST_USERNAME` / `TEST_PASSWORD`
  / `CSRF_RE` / `login_as` / `csrf_from`). 12 test files updated;
  net −228 LOC. The drift risk was real — `test_auth_routes.py`
  had been hardcoding a different bcrypt cost than the other
  files, which would have silently masked a production bcrypt-
  minimum change.

- `29e3550` (audit #4-cloud+#5+#6) extracted 3 cloud-LLM helpers
  into `services/llm_cloud_call.py`: `wrap_provider_errors`
  context manager (6 sites of httpx → port-error translation);
  `INTENT_ADAPTER` shared `TypeAdapter[OperatorIntent]` (4 sites);
  `execute_assistant_call` end-to-end orchestrator (3 cloud
  assistant adapters). Adapter LOC diff −92; new helper code +20
  (rest is docstring).

`cb4f916` deferred 3 audit items (#7 WiredSnapshot base, #8
load_with_degrade helper, #11 storage fixture consolidation) to
`docs/release/v1.1/infrastructure.md` with concrete triggers
(naming inconsistency between `wired` vs `live_wired` makes #7
half-broken without a rename pass; #11 saves 160 LOC but at the
cost of renaming the `storage` parameter in every test method
signature across 38 files; #8 saves only ~18 LOC alone without
the #7 base class). Honest rationale for why each one isn't
worth the v1.0 boundary spend right now.

**Reconciliation closure**: live.db ledger matches Kraken Pro
within fee precision; per-cycle PnL is ~$0.05 net at current
maker rates × $10 leg × 1.0% spacing; operator timezone preference
now actually propagates through every dashboard page.

**Numbers at end of day 6 evening**: 1946 unit tests pass (1941 →
1946, +5 across the partition_known_symbols regression suite);
mypy 111 src files clean; pylint **10.00/10**; black + isort
clean. Real-money cost unchanged at **$0.085018**.

**Day 6 night — operator-flagged regression + 2 small follow-ups**
(`bec097d`, `67daf68`, `a7d8f14`, plus the end-of-day docs pass
in `be46dd9`).

Operator noticed Today's PnL on the dashboard reading $0 after
all of today's work. Hypothesis: the timezone fix shipped earlier
this evening (b2e972d) only converted the display layer; the
``today_realized_pnl`` helper kept filtering by UTC day. After
UTC midnight but before local midnight, cycles from earlier "today"
in operator-tz silently fell out of the header — operator saw
"Today: $0.00" while the cycle rows below still showed today's
fills. Reproduced against live.db at 04:17 UTC: UTC filter
returned 0; America/Chicago filter returned $0.1025. Hotfix
`bec097d` adds an ``tz_name`` parameter to ``today_realized_pnl``
that scopes the day boundary by IANA timezone (defaults to UTC
for backward compat; unknown name falls back to UTC). Status
routes thread ``operator_tz=prefs.timezone`` through. Two
regression tests pin the UTC-midnight boundary case + the
unknown-tz fallback.

After PnL fix landed, operator also noticed cli/live's terminal
output had been silent for ~10 hours (only fills + 1 WARN logged
since 1:32 PM start). Today's morning logging-rebalance audit
(c9e7781) had correctly demoted "tick complete" from INFO to
DEBUG to cut 5s-cadence noise — but the side effect was an idle
bot looking indistinguishable from a hung process in plain format.
`67daf68` adds a ``live.terminal_heartbeat_seconds`` knob (default
900s = 15 min) and a periodic INFO line that proves the loop is
alive without flooding the terminal::

    periodic heartbeat: tick 1839, elapsed 7h 22m, symbols BTC/USD

Separate from the operator.db daemon_heartbeats row that backs
the /health page; this one's just the terminal-visible equivalent.
Also inlined the WARN-extras for the pending_commands poll WARN
on the same path so plain-format consumers see what failed.

Operator-initiated cli/live restart at 23:31 CDT (04:31 UTC May
24) surfaced two more concerns worth logging:

- ``startup reconciliation`` marked 5 storage-only orders as
  canceled, but live.db inspection showed those were actually 3
  unique ``exchange_id``s with duplicate rows at different
  precision strings. Likely a reconciler edge case from one of
  today's restart cycles; queued for tracing.
- ``order refused by exchange: insufficient balance`` WARN on the
  3rd SELL of the fresh grid layout. Engine handled it correctly
  (placed 3 BUYs + 2 SELLs with the BTC inventory it had;
  did not retry-loop or crash). The behavior is correct
  degraded-state for short BTC inventory; the WARN-level alarm
  over-states the severity. `a7d8f14` logs a v1.1 entry in
  ``docs/release/v1.1/engine.md`` proposing demotion to INFO
  with a "partial grid placed (3 BUYs + 2 SELLs of 3 target);
  BTC inventory below full SELL layout target" summary message
  so operator immediately sees the degraded-but-correct state.

End-of-day state: 5 open orders on Kraken matching live.db, bot
healthy, partial grid is the correct response to BTC inventory
short of the full 3-SELL layout target. cli/live restarted on
fresh code that includes the PnL fix + terminal heartbeat.

**Numbers at end of day 6 night**: 1948 unit tests pass (1946 →
1948, +2 across the today_realized_pnl tz regression suite);
mypy 111 src files clean; pylint **10.00/10**; black + isort
clean. Real-money cost unchanged at **$0.085018**. **21 commits
shipped today** (morning audit + afternoon code-reuse-audit
closure + evening hotfix + night follow-ups).

### Stage 8.4.B-D + soak Day 1-3 events (2026-05-18 → 2026-05-20)

Documentation-freeze sub-slices closed plus the operator-driven
soak that began 2026-05-18 and is currently mid-flight.

**8.4.B — v1.0 documentation freeze** (2026-05-18, `f154f39`).
Two operator-facing docs under new `docs/release/`:
- `v1.0-known-limitations.md` captures the v1.0 boundary
  honestly. Architectural / operational / observability /
  tooling / process boundaries; schema notes; soak-window
  boundary.
- `v1.0-future-improvements.md` lists v1.1+ candidates grouped
  by motivation (earned by soak data / operator feedback / code
  review / external triggers). Cross-reference index at bottom.

**8.4.C — Pre-1.0 one-shot audit** (2026-05-18, `c139f1b` +
`5d3d8d0`). LICENSE clean (MIT, 2025-2026, holder CarlDog),
pre-commit hook clean (gitleaks + PII + author-identity guard),
full-history author sweep clean, community-standards files all
present. **One audit finding fixed**: README significant drift
(test counts 1214/26 → 1785/29; Phase status table missing
Phases 6 / 7 / Stages 7.6 / 8.0-8.3; "Eleven CLIs" → "Fifteen
entry points"; ADR count 9 → 18; mypy file count 69 → 104).
Follow-up commit `5d3d8d0` added missing `live.operator_db` +
`harvest.operator_db` documentation to `settings.example.yml`
(gap surfaced during soak Day 1 spot-check). pyproject.toml
version bump deferred to 8.4.F per design decision 10.

**8.4.D — Soak runbook** (2026-05-18, `8ec10fc`).
`docs/release/v1.0-soak-runbook.md` operator-facing playbook.
Pre-soak checklist (8 hard gates incl. Harvester-key separation),
recommended low-risk config, daily check-in (5 questions),
hard-stop / soft-watch / info-only categorization, abort + restart
procedure, pass criteria (~2-4 weeks operator-decided).

**8.4.E — Operator-driven soak in progress (started 2026-05-18).**

Day 1 — start (2026-05-18): operator launched 7 daemons in bare
PowerShell terminals (cli/operator stayed off due to Discord bot
config issues; supported degraded mode). Engine placed 3 BUYs at
$76,185 / $75,415 / $74,646 around anchor $76,954 (BTC = 0 so
no SELLs).

Day 1 → Day 2 outage (2026-05-19): thunderstorm-induced power
outage took the host's DNS resolution down. Top buy at $76,185
filled overnight (0.00013126 BTC) before network died. cli/live
crashed at ~04:31 UTC May 19 via `httpx.ConnectError` inside
`_session_usd_balance` during the `finally` block — uncaught
exception propagated and **skipped the subsequent
`_cancel_all_open` call entirely**. 2 remaining buys left open
on Kraken.

Day 2 recovery (2026-05-19): operator-driven manual recovery.
DBs all passed `PRAGMA integrity_check` cleanly (WAL +
synchronous=NORMAL did their job through the power loss).
Operator canceled the 2 stragglers in Kraken Pro; deleted
`grid_state` row; restarted cli/live → fresh anchor at $76,894
with 3-buy + 1-sell layout (sell uses the orphaned BTC inventory
from the missed fill).

**Two soak-surfaced findings** addressed in focused commits per
stage-8.4-design.md decision 3:

1. [`e2b6cfc`] **Defect fix** — cli/live + cli/shadow `finally`
   block restructured so each cleanup step gets its own
   `try/except WobbleBotPortError`. A transient
   `_session_usd_balance` failure no longer skips
   `_cancel_all_open`; the cancel attempt at least runs and
   logs per-symbol failures. Honest ending-USD reporting ("PnL
   unavailable") when the balance fetch fails instead of zero.
   Same structural fix in cli/shadow for consistency.
   Regression test in `TestSessionEndResilience` covers the path.
   1786 tests pass (was 1785, +1).

2. [`9eea1b8`] **Known limitation documented** — reconciler
   matches storage-open vs exchange-open by `exchange_id` only;
   it doesn't query Kraken's closed-orders or trade-history
   endpoints to distinguish fill from cancel. Storage-only
   orders get marked `canceled` regardless of cause, so a
   fill-while-down leaves BTC inventory orphaned from the grid
   strategy. Added to `v1.0-known-limitations.md` under the
   engine-reconciliation subsection + to `v1.0-future-improvements.md`
   Group 3 as a v1.1 candidate (extend reconciler to query
   `/0/private/ClosedOrders` + replay counter-placement).

**Three v1.1 candidates added during soak** based on operator
feedback:

- [`c0ff561`] **Operator-initiated re-anchor command** (Group 2).
  ADR-006 rejected AUTO re-anchor (safety) but operator-initiated
  re-anchor (with confirm gate) is a different policy. Today the
  procedure is SIGINT + DELETE grid_state + restart; codifying as
  a single typed command (Discord / web button) routes through
  `pending_commands` per ADR-013.

- [`91d8538` + `99d79b9`] **Web UI per-entity action buttons**
  (Group 2). Apply / Execute / Approve / Acknowledge labels per
  domain (matches existing project vocabulary: `cli/apply` +
  `AppliedSuggestion`; `cli/harvest --execute` + `TransferResult`;
  `pending_commands.status='approved'`; `notifications.forwarded=1`).
  Reject universal across surfaces. All route through
  `pending_commands` per ADR-013 — ADR-002 firewall intact.

Soak window currently mid-flight. Minimum useful end approximately
**2026-06-02** (+1d for outage interruption); comfortable end
approximately **2026-06-15**. Daily check-in via web UI `/audit`
view (cli/operator off, no Discord forwarding).

**Day 3 (2026-05-20) — heavy soak-period polish.** Nineteen
commits across morning + evening sessions, all within
documentation-freeze (no behavior changes; UX + reliability
only). Morning: 3 transient-failure-resilience hotfixes
(`e2b6cfc` / `a9b9e43` / `ae58c52`), 1 config validator
(`8c1acfa` rejects spacing ≤ 2 × maker fee — caught ETH=0.5%
misconfig), 4 operator-requested web UI features (`031fb55`
Kraken Pro nav link + refresh button, `da8b1e4` news fuzzy
dedup + `rapidfuzz` runtime dep, `20a8bd8` Kraken trading fees
on cost dashboard, `746c6cc` per-user settings + timezone
preference + new `user_preferences` table + `tzdata` Windows
runtime dep). Pre-relaunch process audit revealed three daemons
had died silently at earlier points; soak runbook updated to
mandate `Get-CimInstance` audit after any crash, not just
terminal checks. Evening (post-relaunch): WobbleBot brand mark
shipped as PNG raster (`ddaefc5` initial SVG superseded →
`eec7f6b` openart.ai/Qwen-approved PNG pivot → `c7fba08`
tighter crop + larger navbar render → `39b0ac2` Cache-Control
headers + 178KB→64KB resize + preload to fix per-nav reload
beat) — first visual identity for wobblebot. One CSS bug fix
(`13e660e` `.btn-link:hover` invisible on white cards). Three
status card UX adds (`b5cedfe` current-price per symbol from
observe.db + new `humanize_duration` Jinja filter so "last fill
40883s ago" renders as "11h 21m"; `7014ad1` removed redundant
`@observed_at` timestamp; `cae4405` green ▲ / red ▼ trend
arrows over 15-min lookback with ±0.1% flat threshold). Three
v1.1 plan additions (`3b83bfa` per-order delta column,
`aca4b95` multi-coin status card layout, `3c28673`
graceful-shutdown timeout for daemons — surfaced when cli/web
hung 3+ minutes after SIGINT during a bounce). Nightly check
confirmed all 7 daemons healthy. USD balance: $89.92 → $99.87 across Day 3 — but
that is BTC→USD reclassification, not profit. The Day-1 BUY at
$76,185 converted ~$10 USD into ~0.000131 BTC; the Day-3 SELL at
$77,663 reversed it. Total portfolio value moved from ~$100 to
~$100.06 over the week (+$0.0647, +0.06% per Kraken snapshot).
Actual round-trip cycle yielded ~$0.14-0.20 in spread minus
fees. Two outage events handled cleanly (May 19 storm DNS
failure, May 20 15:03 UTC httpcore.ReadTimeout). Real-money
cost stays at $0.085018.

**Day 5 morning (2026-05-22) — soak-surfaced math defect, focused
hotfix.** cli/live crashed at 09:18:31 with "session loss cap
exceeded" immediately after a $10 BUY filled at the Day-4 fresh
anchor ($77,635.90). Root cause: the `max_session_loss_usd` cap
checked USD-balance delta only — a BUY fill *is* a USD→base
conversion, so the first BUY of any session where
`order_size_usd > max_session_loss_usd` would trip it. Same class
of math error caught on Day 3 (USD-balance delta ≠ profit). Fix
swaps the cap check to mark-to-market portfolio value: USD
balance + Σ (base × current_price) for each configured symbol.
The cap fires honestly on actual realized + unrealized
drawdowns; an asset conversion no longer reads as a loss.
Logs gained `starting_value_usd` / `ending_value_usd` /
`session_pnl_usd` (the last is now mark-to-market; was
USD-delta). Same fix mirrored in cli/shadow. New helpers
`_session_portfolio_value_usd` (live) / `_shadow_portfolio_value_usd`
(shadow); dedupes repeated base across symbols. 6 new regression
tests. settings comments updated to drop the "USD-balance
delta" framing. Real-money cost: $0.085018 unchanged.

**Day 5 afternoon (2026-05-22) — heavy observability + Discord
restoration day.** 20 more commits stacked on the morning's cap
hotfix:

- **Engine resilience (2 more fixes).** `e936f2b` engine auto
  re-layout when no open orders remain — after the morning's
  cap-trip + restart, cli/live had grid_state but every order in
  canceled status; _tick only handles fills, so the engine
  ticked silently for ~1.5h. _tick now detects empty open-orders
  + not offside and re-places the layout at the existing anchor.
  `3ac3757` max_daily_spend_usd ignores canceled BUYs — the cap
  was counting every BUY row created today regardless of status;
  Day-5 churn (11 canceled rows totalling $110 notional) blocked
  legitimate placement against a $100 cap.
- **Health observability (10 commits).** New `/health` page +
  Kraken SystemStatus probe with TTL cache (`d2da41a`) + dashboard
  traffic-light icon (`d938044`). Operator iteration tightened
  the page across 9 follow-ups: dot moved right of LIVE
  (`4d33cbe`), pure yellow (`4d33cbe`), inline-render dot to
  eliminate refresh flicker + drop /health/icon endpoint
  (`cc7d87b`), View Transitions API smoothing (`cc322b7`),
  symbol-section framing + caption trim (`a7bd01b`), title-case
  daemon labels (`a722fd4`), colgroup column-width alignment
  (`b70e855` + `fb6e45f`), status card AGE column lock
  (`0cb9b58`). `a544d8d` made thresholds read schedules.* so
  operator-tuned cadences flow into the health UI without code
  changes (fixes the operator-surfaced "this should NOT be
  yellow" hardcoded-thresholds problem). `9bc4b7f` extended
  coverage from 3 daemons to all 7 via a new
  `daemon_heartbeats` SQLite table — cli/live + cli/harvest +
  cli/operator + cli/maintenance each upsert at the top of their
  tick loops.
- **Discord restoration (task #84 closed end-to-end).** 4 days
  dark since Day 1. Token was valid; private channel needed
  explicit per-bot permission grant. cli/operator now forwards
  notifications + parses operator queries; integration restored.
  `61b50ca` cli/operator catches KeyboardInterrupt at top level
  (no more shutdown traceback). Two v1.1 candidates logged
  during the restoration: `e48430f` configurable counter-order
  target (advisor-driven strategy regime — operator-floated
  "what if SELLs landed at top of grid?") and `f100369`
  Discord response quality (StatusQuery data + embed rendering +
  model attribution footer — surfaced when the first end-to-end
  query came back with empty fields and as a JSON blob).
- **Visual identity.** `3291330` shipped a WobbleBot squircle
  icon (1024 / 512 / 256 PNG variants) + og:image meta tags so
  shared dashboard URLs render with the icon as the link
  preview. Browser-tab favicon kept as the simpler W-with-sweep.

**8.4.F — Post-soak release ceremony** (pending soak pass).
Future commit: `docs/planning/phase-8-summary.md`,
`pyproject.toml` 0.1.0 → 1.0.0, CHANGELOG `[Unreleased]` →
`[1.0.0] - YYYY-MM-DD`, annotated `git tag -a v1.0.0`.

**Numbers through soak Day 5**: 1907 unit tests pass (+121 from
soak-period work across Day 1-5; +74 today); mypy 110 src files
clean; pylint **10.00/10**; black + isort clean. **Real-money
cost delta this period: essentially flat** (round-trip cycles
on the orphan BTC inventory netted ~$0.14-0.20 in spread minus
fees; total portfolio value moved +0.06% over the week per
Kraken snapshot). Running project cost stays at **$0.085018**.

### Stage 8.4 kickoff — Phase 8 / v1.0 Release Check (2026-05-18)

Final pre-v1.0 stage opens. Stage 8.3 closed all behavior /
performance work for v1.0; Stage 8.4 is the release ceremony
plus the multi-week operator-driven soak that gates the v1.0.0
tag.

**No new ADR.** Release ceremony, not architectural change. The
known-limitations doc *captures* prior ADR-deferred decisions
(single-operator web auth, no separate banking adapter,
harvester reconciler deferred, etc.) — it doesn't introduce new
ones. Decisions ratified in `docs/planning/stage-8.4-design.md`
only.

**Design ratifies 10 implementation decisions:**

1. Soak duration is operator-decided, not Claude-mandated — the
   runbook describes *what to watch for*, not *how long to
   watch*.
2. Low-risk soak configuration ratified in the runbook (single
   coin, conservative order_size_usd + spacing, hard caps tuned
   via cli/recalibrate, harvester enabled).
3. Documentation freeze, not codebase freeze — if the soak
   surfaces a defect, fix it in a focused commit, update v1.0
   docs to reflect corrected behavior, then tag.
4. Known-limitations doc covers every ADR-deferred decision +
   v1.0-specific items (CryptoCompare 90-day eval still pending
   2026-08-13, no CI perf regression check, no remote backup
   destinations in v1.0, etc.).
5. Future-improvements doc grouped by motivation: earned by
   soak data / earned by operator feedback / earned by code
   review.
6. Pre-1.0 audit findings ship in focused commits per the global
   phase-end-audit rule, not an omnibus cleanup commit.
7. Author-identity audit is across all branches + history via
   `git log --all --pretty='%ae' | sort -u`.
8. Phase 4 Harvester-key separation verified live with the
   operator (ADR-003 load-bearing invariant).
9. v1.0.0 tag is annotated (`git tag -a`), not lightweight;
   signed (`-s`) if operator's GPG configured.
10. `pyproject.toml` version bump goes in the same commit as the
    tag annotation so `git show v1.0.0` includes the version-bump
    diff.

**Slicing:** 8.4.A kickoff (this commit) → 8.4.B known-limitations
+ future-improvements docs → 8.4.C pre-1.0 one-shot audit →
8.4.D soak runbook → 8.4.E operator-driven multi-week soak
(deferred from Claude-session scope) → 8.4.F post-soak release
ceremony (`phase-8-summary.md` + CHANGELOG `[Unreleased]` →
`[1.0.0] - YYYY-MM-DD` + `pyproject.toml` 1.0.0 + annotated
`v1.0.0` tag).

**Explicitly out of scope:** new code, new tests, new ADRs, new
features the operator asks for during the soak (logged as
future-improvements candidates), refactor "while we're here"
drift, a v1.0.1 plan, CI / GitHub Actions setup.

No code in this commit. Stage 8.4.B work follows.

### Stage 8.3 close — Performance & Resource Tuning (2026-05-18)

Three sub-slices closed (A kickoff already in unreleased below;
B SQLite pragmas; C index audit + profile harness) plus this
close commit (D). Universal SQLite easy wins land + an operator-
runnable measurement tool against which Stage 8.4's soak test
will compare.

**B — SQLite performance pragmas.** `SQLiteStorageAdapter.connect()`
applies two new pragmas after the existing `foreign_keys=ON` step:
`PRAGMA journal_mode=WAL` (concurrent readers don't block the
writer — cli/maintenance's backup task can read while cli/live
ticks) and `PRAGMA synchronous=NORMAL` (fsync at WAL checkpoint
boundaries instead of per-commit; ~50x faster commit throughput
per published SQLite guidance). Both skip for `:memory:` and
anonymous on-disk DBs — WAL is a no-op there and confuses test
fixtures that introspect `journal_mode`. 5 new tests in
`TestStage83Pragmas`.

**C — Index audit + `tools/profile_storage.py`.** Six
`EXPLAIN QUERY PLAN` audits in `TestStage83IndexAudit` assert
every hot read uses `SEARCH` (index access), never `SCAN` (full
table scan). All six queries clean against the current schema —
`get_open_orders(symbol)` hits `idx_orders_symbol`,
`get_trades(symbol+time)` hits `idx_trades_symbol`,
`pending_commands by status` hits `idx_pending_commands_status`,
`notifications forwarder` hits `idx_notifications_forwarded`,
`llm_calls 24h window` hits `idx_llm_calls_timestamp`, and
`price_snapshots by symbol+time` hits
`idx_price_snapshots_symbol_time`. No new indexes needed; the
tests act as a regression gate so future query additions can't
silently slip into table-scan territory.

New `tools/profile_storage.py` operator-runnable harness times
each hot operation N times against an in-memory or operator-
specified on-disk DB (the latter copied to a temp file first so
the live DB can't be polluted with fixture rows). Reports one
structured log record per operation:
`{operation, n, p50_ms, p99_ms, mean_ms, total_seconds}`. Pre-
seeds 1000 closed orders / 20 open / 200 trades by default so
timings reflect realistic index-vs-scan behavior under load
instead of the empty-table O(1) zone. Smoke-tested locally:
`get_open_orders` p50 0.26ms / p99 0.60ms against 1020 seeded
rows; `save_order` p50 0.06ms. Operator's Synology numbers will
differ — Stage 8.4's soak test has its baseline. 11 new tests
for the timing helpers (5 percentile_ms + 2 summarize + 3
_profile_op + 1 _seed_fixtures).

**Numbers.** 1785 unit tests pass (was 1763 at Stage 8.3 entry,
+22). mypy clean across 104 src files; pylint **10.00/10**.
black + isort clean across `src/` + `tests/`. **No new operator
entry points** — `tools/profile_storage.py` is a diagnostic per
design decision 7, not a daemon. **Stage 8.3 total real-money
cost: $0.00.** Running project cost stays at **$0.085018**.

### Stage 8.3 kickoff — Performance & Resource Tuning (2026-05-18)

Phase 8 continues. Stage 8.2 stabilized DB hygiene; Stage 8.3
ships universal performance easy wins + a baseline measurement
tool against which Stage 8.4's soak test can compare.

**No new ADR.** Operational tuning, not cross-cutting policy.
Decisions ratified in `docs/planning/stage-8.3-design.md` only.

**Design ratifies 8 implementation decisions:**

1. WAL mode for all on-disk DBs — concurrent readers don't block
   writers (Stage 8.2's backup task can run while cli/live ticks).
2. `synchronous=NORMAL` over the default `FULL` — ~50x faster
   commits per the SQLite docs; durability tradeoff acceptable
   for this use case (Stage 8.1's reconciliation catches last-
   tick drift on next startup).
3. `foreign_keys=ON` — cheap v1.1 insurance; no current FK
   constraints but enabling now avoids legacy-default surprise.
4. Skip pragmas for in-memory DBs — irrelevant + can confuse
   test fixtures that introspect journal_mode.
5. Index audit covers engine hot path first (`get_open_orders`,
   `get_trades`, `save_order`); verify via `EXPLAIN QUERY PLAN`.
6. Profile harness reports p50/p99 in ms — operator's mental
   model, not statistician's.
7. `tools/profile_storage.py` not `cli/profile_storage` —
   diagnostic, not daemon (convention from `tools/show_*`).
8. No CI perf regression check in v1.0 — CI runner variance
   makes it untrustworthy. Operator's deployment is the canonical
   measurement surface.

**Slicing:** 8.3.A (this commit) → 8.3.B SQLite pragmas → 8.3.C
index audit + profile harness → 8.3.D close. ~15 new tests. No
new operator entry points.

**Explicitly out of scope** (defer until Stage 8.4 soak provides
measurement data): caching layers, async query parallelism, batch
APIs, connection pooling, Synology-specific tuning hardcoded into
defaults.

No code in this commit. Stage 8.3.B work follows.

### Stage 8.2 close — Background Maintenance Worker (2026-05-18)

Four sub-slices closed (A kickoff already in unreleased above; B
maintenance services; C backup service; D cli/maintenance daemon
+ log rotation) plus this close commit (E).

**Fifteenth operator entry point lands:** `python -m
wobblebot.cli.maintenance`. Long-running daemon with three
concurrent scheduled tasks (vacuum / prune+archive / backup) via
the Stage 8.0.C `run_poll_loop` helper. Each task pulls its
cadence from the `schedules:` block (defaults 7d / 1d / 1d).

**B — services/maintenance.py.** Three helpers: `vacuum_database`
(raw `sqlite3.execute("VACUUM")` with explicit close() to dodge
the unraisable-warning trap on Windows asyncio); `archive_price_snapshots_to_csv`
(pure CSV writer that refuses to overwrite); `prune_price_snapshots`
(archive-then-delete discipline; rows only get DELETEd after the
CSV write succeeds). New `StoragePort.delete_price_snapshots(before)`
method per ADR-001 hex-layer discipline. 9 new tests.

**C — services/backuper.py.** `backup_database_locally` uses
SQLite's online `.backup` API for atomic point-in-time copies
without locking the source DB — `cli/live` can keep ticking
through the backup window. `prune_old_backups` retention with
per-db-stem scoping (multiple DBs each get independent retention).
`BackupDestination` Protocol declared for v1.1 remote variants
(S3 / rclone). 10 new tests.

**D — cli/maintenance daemon + log rotation.** New
`MaintenanceConfig` Pydantic block (7 knobs). `asyncio.gather`
runs three `run_poll_loop` tasks concurrently; SIGINT/SIGTERM
flips the shared stop_event so every task exits cleanly at its
next loop check. Per-cycle error isolation: missing DBs are
logged + skipped (not fatal); one bad backup doesn't kill the
others. `configure_logging` gains opt-in `rotating_file_path`
kwarg using `TimedRotatingFileHandler` ALONGSIDE the stderr
stream handler. Idempotent handler replacement closes the old
file descriptor BEFORE installing the new one (surfaced during
test development; fixed in both the production helper-replace
loop AND the test fixture). 13 new tests.

**Settings:** `settings.example.yml` + `settings.yml` gain the
new `maintenance:` block (7 knobs) and three new schedule keys
under `schedules:` (`maintenance_vacuum: 7d`, `maintenance_prune:
1d`, `maintenance_backup: 1d`). Schema-drift tests stay green.

**Numbers.** 1763 unit tests pass (was 1732 at Stage 8.2 entry,
+31 across B + C + D). mypy clean across 104 src files (was 101 —
three new modules). pylint **10.00/10**. black + isort clean.
**Stage 8.2 real-money cost: $0.00** (pure local-FS + SQLite
operations).

Stage 8.3 (Performance & Resource Tuning) follows. The 8.2
maintenance daemon's VACUUM cadence + retention pruning give 8.3
a stable baseline against which to profile heavy processes
(metrics computation, multi-coin tick).

### Stage 8.2 kickoff — Background Maintenance Worker (2026-05-18)

Phase 8 continues. Stage 8.1 closed reliability + reconciliation;
Stage 8.2 builds the long-running maintenance daemon on top.

**No new ADR.** The four subsystems (VACUUM, prune + archive,
backup, log rotation) carry implementation-level decisions but
none are cross-cutting commitments future stages need to re-ratify.
Decisions land in `docs/planning/stage-8.2-design.md` only.

**Design ratifies 10 implementation-level decisions:**

1. One daemon, multiple scheduled tasks (three concurrent
   `asyncio.Task`s via the Stage 8.0.C `run_poll_loop` helper).
2. CSV archive format. Zero new deps; operator converts to
   parquet downstream if they want.
3. Only `price_snapshots` gets pruned in v1.0. Every audit
   table (`orders`, `trades`, `llm_calls`, etc.) stays forever.
4. Local-only backups in v1.0. `BackupDestination` Protocol
   for v1.1 remote variants.
5. Backup retention: keep last N daily (default 7). Tiered
   retention deferred to v1.1.
6. VACUUM uses raw `sqlite3.Connection.execute("VACUUM")` —
   can't run inside `aiosqlite`'s transaction wrapper.
7. Operator-started daemon (matching `cli/live`, `cli/operator`,
   etc.) — not auto-spawned.
8. Default cadences: vacuum 7d, prune 1d, backup 1d.
9. Archive + backup live under `data/archive/` + `data/backups/`.
10. Log rotation opt-in via `configure_logging(rotating_file_path=...)`.
    Default stays stdout-only.

**Slicing:** 8.2.A (this commit) → 8.2.B (services/maintenance.py
with vacuum + prune + archive) → 8.2.C (services/backuper.py with
local SQLite .backup) → 8.2.D (cli/maintenance daemon + log
rotation) → 8.2.E (close). ~25-35 new tests. **Fifteenth operator
entry point** lands at close: `python -m wobblebot.cli.maintenance`.

No code in this commit. Stage 8.2.B work follows.

### Stage 8.1 close — Reliability & Recovery (2026-05-18)

Three sub-slices closed (A kickoff already in unreleased above;
B persistence-on-cancel; C reconciler + CLI wiring) plus this
close commit (D). cli/live + cli/shadow now have robust
startup-reconciliation against the exchange's authoritative
view + proper persistence-on-cancel at shutdown.

**The 2026-05-18 shadow-session repro is fixed.** Run a fresh
shadow session, inspect shadow.db at exit: all cancelled orders
show `status="canceled"`. Run a second session immediately
after — the reconciler reports `storage_canceled=0,
orphan_count=0` because the previous session left the storage
view clean.

**ADR-018 in action.** Real-data path:

- Shutdown: cli/live + cli/shadow now call
  `storage.save_order(o.model_copy(update={"status": "canceled"}))`
  after every successful `adapter.cancel_order()`. Don't-lie-in-the-
  audit-trail: cancel-raised → storage stays open so the reconciler
  catches it next session.
- Startup: cli/live + cli/shadow call
  `services.reconciler.apply_reconciliation()` between storage
  open and signal handler install, AFTER adapter construct and
  BEFORE engine first tick. Adapter timeout inherits (10s for
  Kraken); failure propagates → daemon exits with code 1 rather
  than ticking against unreconciled state.
- Reconciler diff classes: **storage_only** (storage has open,
  exchange doesn't) → marked canceled with reason
  `not_on_exchange_at_startup`. **exchange_only** (exchange has,
  storage doesn't) → logged at ERROR with per-orphan line +
  one summary line; engine does NOT adopt; operator manually
  reviews via Kraken Pro per ADR-018 decision 3.
- Configured-symbols filter: orphan logging filters to the
  engine's actual trade set; manual orders on unrelated coins
  stay silent. Storage-only reconciliation still scans ALL
  storage rows regardless of the filter (stale rows in any
  symbol should clear).

**Numbers.** 1732 unit tests pass (1711 → 1732, +21: 5 persistence
+ 16 reconciler). mypy clean across 101 src files (+1 reconciler
module). pylint **10.00/10**; black + isort clean. **Stage 8.1
real-money cost: $0.00** (shutdown discipline + read-only adapter
queries; no live engine operations triggered).

Stage 8.2 (Background Maintenance Worker) follows. Persistence-on-
cancel + startup reconciliation give 8.2's maintenance worker a
known-good state to assume at boot — the worker can VACUUM /
prune / backup without tripping over stale-open rows from a
prior session's shutdown bug.

### Stage 8.1 kickoff — Reliability & Recovery (ADR-018) (2026-05-18)

Phase 8 continues. Stage 8.0 (deferred refactors) just closed
green; Stage 8.1 takes on the reliability work the refactors set
up.

**ADR-018 — Engine reconciliation strategy.** Seven decisions
ratified at kickoff:

1. Exchange (Kraken / synthetic ledger) is authoritative for
   "what orders exist." Storage gets updated to match.
2. Storage-only orders (not on exchange) → marked `canceled` at
   startup. Fixes both the 2026-05-18 shutdown bug AND
   out-of-band exchange cancellations during downtime.
3. Exchange-only orders (on exchange, not in storage) → log
   loud ERROR + continue startup. Do NOT adopt. WobbleBot
   doesn't manage orders it didn't place; operator must
   manually review via Kraken Pro.
4. Reconciliation runs once at engine startup. The engine tick
   logic handles ongoing drift.
5. Same policy applies to cli/shadow against its synthetic
   adapter ledger.
6. Harvester pending-transfer reconciliation deferred to v1.1.
   Operator manually reconciles via Kraken Pro in v1.0.
7. Policy lives in a pure `services/reconciler.py` module +
   thin async orchestrator; CLI wiring is one helper call.

Plus an 8-decision implementation contract in
`docs/planning/stage-8.1-design.md` covering:

- Per-symbol vs global reconciliation pass (global, one Kraken
  call).
- Persistence-on-cancel uses in-memory `Order` object, not a
  storage re-read.
- Reconciliation runs as the LAST step before engine kickoff,
  AFTER storage + adapter ready but BEFORE signal handlers
  install.
- Pure function `reconcile_open_orders()` + async wrapper
  `apply_reconciliation()` (same Stage 2.2 split that paid off
  for grid layout).
- `ReconciliationReport` carries metrics for session-start
  logging.
- Per-orphan ERROR logging + one summary line ("review Kraken Pro").
- No special timeout — reconciliation inherits adapter timeout;
  refusing to start on Kraken-down is correct.
- Symbol scope: orphans outside the configured-symbols set are
  silently skipped (operator's manual orders on unrelated coins).

**Slicing:** 8.1.A (this commit) → 8.1.B (persistence-on-cancel
fix, ~1h) → 8.1.C (reconciler module + wiring, ~2-3h) → 8.1.D
(close). Estimated ~4-5 hours; ~19 new tests; no real-money
risk.

No code in this commit. Stage 8.1.B work follows.

### Stage 8.0 close — Deferred Phase-5-audit refactors (2026-05-18)

Three medium refactors landed in sub-slices A → B → C, with this
close commit (8.0.D) doing the doc updates.

**Why three sub-slices, why now.** The Phase 5 close audit punch
list surfaced R5 (split ports/operator.py), R3 (storage-fallback
helper), R2 (poll-loop helper). Each was queued for proper
planning rather than silent reworking during the audit. After
Phase 6 + Phase 7 + Phase 7.6 polish proved the patterns kept
accreting — and with Phase 8.1's reliability work about to edit
shutdown discipline across seven CLIs — it was time to consolidate.

**Pure code organization. Zero behavior change.** Every existing
test stays green. Every existing import path keeps resolving.
No operator-facing surface changes; no new CLIs; no config
changes.

**8.0.A — ports/operator.py split.** 734-line file became three
focused modules. `ports/operator_intents.py` (367 lines) carries
Command + Query + Intent variants plus the three discriminated
unions. `ports/operator_results.py` (302 lines) carries per-query
Result types + entry types + QueryResult + CommandResult. The
surviving `ports/operator.py` (244 lines) keeps the OperatorPort
ABC + PendingCommand + module-level re-exports preserving every
existing import path. All 41 backward-compat names resolve from
`wobblebot.ports.operator`.

**8.0.B — degraded-result factories.** Three module-level factory
functions in `services/operator_service.py`
(`_empty_recent_suggestions`, `_empty_recent_news`,
`_empty_recent_proposals`) centralize the "what does graceful-
degrade look like" contract. Each query handler's degraded-path
shrinks from ~5 lines of inline result construction to one
`return _empty_X(query)`. `HarvesterStatusQuery` stays inline —
its degraded path is genuinely different (still fetches balance +
classifies band). 6 new factory tests.

**8.0.C — `cli/_common.run_poll_loop` helper.** Six loops across
five daemons (cli/observe, cli/news, cli/advise, cli/harvest, plus
cli/operator's notification forwarder + TTL expirer) used to
hand-roll the same `while not stop_event.is_set(): await do_one();
await wait_for(stop, interval)` body. They now share one helper.
Each migration is structural: the inner per-cycle work moves into
an async closure so the surrounding scope's counter increments
and mid-sweep stop_event checks stay in place; the helper wraps
the loop. Session-start/end try/finally stays at the call site
since metrics shape varies. **Phase 8.1's reliability refinement
now has one edit point for shutdown-discipline changes instead
of seven** — the persistence-on-cancel fix queued in the
8.1 backlog (`docs/planning/roadmap.md` Stage 8.1) fits this
pattern. 5 new helper tests.

**Numbers.** 1711 unit tests pass (was 1700 at Stage 8.0 entry,
+11 across A + B + C). mypy clean across 100 src files (was 98).
pylint **10.00/10** maintained throughout. black + isort clean.
**Stage 8.0 real-money cost: $0.00**; running project cost stays
at **$0.085018** unchanged from Phase 6 close.

Stage 8.1 (Reliability & Recovery) follows. Existing backlog:
the persistence-on-cancel fix surfaced 2026-05-18 in the
shadow-session repro; the broader stale-open reconciliation
question across cli/live + cli/shadow startup paths.

### Phase 8 kickoff — Hardening & v1.0 Release (2026-05-18)

After Phase 7 + Stage 7.6 polish closed, Phase 8 needed a design
doc for Stage 8.0 (deferred Phase-5-audit refactors) before any
code, mirroring the Phase 5/6/7 kickoff pattern.

**Phase 8 doesn't introduce cross-cutting ADRs at kickoff.** The
existing ADRs cover Phase 8 scope. Stage 8.1's reliability work
(reconciliation logic + the persistence-on-cancel fix already
queued from the 2026-05-18 shadow session) may warrant an ADR-018
at its own stage kickoff; that decision defers to 8.1.

**Stage 8.0 design ratified** (`docs/planning/stage-8.0-design.md`):

- **8.0.A (R5)** — split `ports/operator.py` (734 lines) into
  three focused modules: `operator_intents.py` (Command + Query +
  Intent variants + three discriminated unions),
  `operator_results.py` (per-query Result types + entry types +
  CommandResult), and a slimmer `operator.py` (OperatorError +
  PendingCommandStatus + PendingCommand + OperatorPort ABC).
  Module-level re-exports preserve every existing import path.
- **8.0.B (R3)** — extract an async context manager around the ~6
  near-identical graceful-degrade blocks in
  `services/operator_service.answer_query`.
- **8.0.C (R2)** — extract `cli/_common.run_poll_loop()` shared
  across five CLI daemons (`cli/operator`'s forwarder + TTL
  expirer, `cli/harvest`, `cli/observe`, `cli/news`,
  `cli/advise`). Phase 8.1 then has one edit point for any
  shutdown-discipline refinement instead of seven.
- **8.0.D** — stage close.

Five sub-slices total (A/B/C are refactors, D is close). Zero
behavior change across all of them; goal is "every existing test
stays green." ~10-15 new tests possible for refactor mechanics
(re-export coverage, context-manager helper, poll-loop helper) but
no new feature surface. pylint 10.00/10 + mypy clean + black +
isort all stay green as acceptance signals.

Phase 8 remaining roadmap (per `docs/planning/roadmap.md`):

- 8.1 Reliability & Recovery — startup/shutdown reconciliation,
  including the persistence-on-cancel fix surfaced 2026-05-18
  during a 60-minute shadow session.
- 8.2 Background Maintenance Worker — `cli/maintenance --loop` for
  DB hygiene, log rotation, local + remote backups.
- 8.3 Performance & Resource Tuning — Synology NAS resource
  constraints, profiling heavy processes.
- 8.4 Phase 8 / v1.0 Release Check — extended soak test, v1.0 tag,
  v1.0 changelog, known-limitations doc.

No code in this commit. Stage 8.0.A work follows.

### Stage 7.6 — cli/recalibrate (operator-initiated balance scaling) (2026-05-18)

Polish slice inserted between Phase 7 close and Phase 8 start. Doesn't
reopen Phase 7's commitments; adds operational ergonomics on top.
**14th operator entry point:** `python -m wobblebot.cli.recalibrate`.

The operator's settings.yml encodes a policy calibrated for a
particular starting balance. When the balance moves (drawdown,
intentional scale-down for a small-balance experiment), every
USD-denominated knob should move proportionally to keep the same risk
posture. Stage 7.6 is the math + CLI that does it.

**7.6.A — Calibrator service.** New
`services/calibrator.recalibrate()` pure function takes current
balance + target balance + current `WobbleBotConfig`, computes the
scale factor (`target/current`), walks every USD knob in the config,
and emits a frozen `RecalibrationProposal` enumerating per-knob
deltas. Scales:

- `grid.default.order_size_usd` + every `grid.coins.<COIN>.order_size_usd`
- `safety.max_{total,daily,per_coin}_exposure_usd`
- `safety.emergency_stop.min_exchange_balance_usd` (skipped when 0)
- `live.max_session_loss_usd`
- All four `harvester.*_usd` thresholds

Does NOT scale (policy invariants, not money): spacing percentages,
level counts, `max_orders_per_coin`, `max_loss_percentage`,
`max_runtime_minutes`, the entire `shadow.*` block. Quantizes to cents.
Preserves the harvester `min<topup<surplus` ordering invariant since
scaling by a positive ratio preserves ordering. 22 new unit tests.

**7.6.B — `cli/recalibrate` dry-run + commit.** Default reads live
Kraken USD balance via the read-only `KRAKEN_READER_API_KEY` (same path
`cli/status` uses); `--current-balance` overrides for what-if analysis
without hitting the API. Dry-run prints a per-knob delta table;
`--commit` rewrites `settings.yml` via the new
`apply_dotted_overrides()` companion to `apply_grid_overrides()` in
`services/settings_rewriter` — round-trips ruamel.yaml preserving
every comment + quoting style + atomic temp-file-rename. Refuses to
create new keys (a typo'd path raises rather than silently appending
a new field).

Exit codes: 0 dry-run/commit success; 1 Kraken balance read failed;
2 config/argparse/rewriter refusal.

Per ADR-012's auto-tuning gate: this is operator-initiated (explicit
CLI invocation), not LLM-initiated, so the gate's bounds don't apply.
The gate exists to defend against LLM proposals slipping through, not
against the operator's own intent.

18 new unit tests. Live verification against operator's real $99.92
balance: `--target-balance 10` produces 14 changes including
`grid.default.order_size_usd $10→$1.00`,
`harvester.surplus_threshold_usd $500→$50.04`. Kraken balance read
verified working end-to-end.

**Numbers.** 1694 unit tests pass (was 1656 at Phase 7 close, +38
across the two sub-slices); mypy clean across 98 src files; pylint
**10.00/10**; black + isort clean. **Stage 7.6 total real-money cost:
$0.00** (read-only Kraken balance read; no orders, no withdrawals).
Running project cost stays at **$0.085018** unchanged from Phase 6
close.

### Phase 7 close — Web UI / Dashboard (2026-05-18)

Phase 7 complete. Five stages closed across two evenings (7.1 →
7.5). Server-rendered FastAPI + Jinja2 + HTMX dashboard ships
end-to-end: auth-protected shell → cost + status dashboards +
ADR-013-firewalled mutation flow → advisor + harvester read-only
views → news + audit-log views → integration check.

**Phase 7 spent $0.00 of real money.** Dashboard is read-mostly;
mutations are firewalled per ADR-013 (web UI never calls
`OperatorService.dispatch_command` directly — every state mutation
crosses `pending_commands` so cli/live's `WHERE status='approved'`
poll remains the single source of truth for "intent → engine").
Running project total: **$0.085018** unchanged from Phase 6 close.

**Stage 7.5 — Phase 7 close + integration check (2026-05-18).**
This commit. End-to-end TestClient walkthrough in
`tests/web/test_phase7_e2e.py` exercises every Phase 7 surface in
a single test: anonymous root redirect → login → all six pages
(dashboard / cost / advisor / harvester / news / audit) →
pause→confirm→approve mutation flow → **ADR-013 firewall verification**
(the row is now `approved` in operator.db, which is what cli/live's
`WHERE status='approved'` poll picks up) → logout → re-verified
session gone. One test, many assertions. Plus Phase 7 closing
summary at `docs/planning/phase-7-summary.md` mirroring
phase-{2,3,4,5,6}-summary.md precedent. Roadmap +
CLAUDE.md + project_state memory updates.

**Numbers.** 1656 unit tests pass (1460 at Phase 6 close →
1656 at Phase 7 close, +196 across the five stages). 29 integration
tests opt-in (unchanged — Phase 7's e2e walkthrough is a unit test
against in-memory storage). mypy clean across 96 src files; pylint
**10.00/10**; black + isort clean.

**Six new runtime deps** in Stage 7.1.B (biggest dep-add since
Phase 5's `discord.py`): `fastapi>=0.115`, `uvicorn[standard]>=0.30`,
`jinja2>=3.1`, `python-multipart>=0.0.12`, `bcrypt>=4.2`,
`itsdangerous>=2.2`.

Next: **Phase 8 — Hardening & v1.0 Release.** Five stages: 8.0
deferred Phase-5-audit refactors (R5 ports/operator.py split, R3
storage-fallback helper, R2 generic poll-loop helper) → 8.1
reliability & recovery → 8.2 background maintenance worker → 8.3
performance & resource tuning → 8.4 v1.0 soak + tag.

### Stage 7.4 — News + audit log views (2026-05-18)

The final two read-only Phase 7 surfaces.

**7.4.A — `/news`.** `routes/news.py` reads `news.db`'s
`news_items` (limit 100). Filter form: source dropdown (populated
from a wider unfiltered slice so the dropdown stays stable across
filtered views) + free-text coin filter that runs case-insensitive
substring match against `NewsItem.mentioned_coins` server-side.
Graceful-degrades when `news_storage` is unwired.

**7.4.B — `/audit`.** `routes/audit.py` reads `operator.db`'s
`pending_commands` + `notifications` (limit 100 each, newest first).
Replaces the Stage 7.1.D `/audit` stub. Each pending command shows
its lifecycle state with a color-coded status tag; each notification
shows level + forwarded state.

Cleanup: `pages.py` shrinks to just `/` → `/dashboard`;
`templates/stub.html` removed. Layout nav adds `/news`.

13 new unit tests (7 news + 4 audit + 2 refactored root tests).
Total 1655 (was 1648); mypy clean across 96 src files; pylint
**10.00/10**; black + isort clean.

### Stage 7.3 — Advisor + harvester views (2026-05-17)

Two read-only views surface the Phase 3 advisor output + Phase 4
treasury activity.

**Advisor.** `routes/advisor.py` reads `advise.db`'s
`advisor_suggestions` (limit 50, newest first). Template renders
the aggregated recommendation per row + per-expert opinions when
MoE-derived (`AdvisorRecommendation.expert_opinions` populated by
`MoEAdvisorAdapter` per ADR-007). Single-LLM rows hide the opinions
section. Confidence tags color-coded.

**Harvester.** `routes/harvester.py` reads `harvest.db`'s
`transfer_proposals` + `transfer_results` (limit 50 each). Template
renders two cards: proposals (with direction + rationale + balance
context) and executed withdrawals (with status tags + refid).
Read-only — per ADR-003 `cli/harvest --execute` remains the only
path that moves money.

Both routes graceful-degrade when their cross-DB storage is
unwired. Nav links added to layout.html.

11 new unit tests. Total 1648 (was 1637); mypy clean across 94
src files; pylint **10.00/10**.

### Stage 7.2 — Cost + status dashboards + mutation flow (2026-05-17)

Three sub-slices delivered the first real-data dashboards + the
architecturally significant mutation flow.

**7.2.A — Cost dashboard.** `routes/cost.py` reads `operator.db`'s
`llm_calls` (Phase 6 ledger), rolls up to 24h totals + per-day
trends + per-provider/role breakdown. Pure-function `_rollup`
keeps the math testable. Two routes: `/cost` (full page) and
`/cost/card` (HTMX fragment for polled refresh).

**7.2.B — Status dashboard.** `routes/status.py` replaces the 7.1
`/dashboard` stub. Reads `live.db`'s open orders + recent 20 trades
via the optional `live_storage` dep; degrades gracefully to an
"unwired" card when `live_db` isn't configured. `dashboard.html`
combines operator-actions card + HTMX-polled status card.

**7.2.C — Mutation flow.** `routes/commands.py` wires
pause/resume/stop through the ADR-013 firewall:

1. GET /commands/<verb> renders a form.
2. POST creates a `PendingCommand` row in `awaiting_confirmation`
   (`channel_id="web"` to distinguish from Discord-originated rows).
3. GET /commands/<id>/confirm summarizes the pending command.
4. POST /commands/<id>/confirm transitions to `approved` or
   `rejected`.

The web UI **NEVER** calls `OperatorService.dispatch_command`
directly; every mutation crosses `pending_commands` so cli/live's
`WHERE status='approved'` poll stays the single source of truth.
Idempotency: re-confirming a row already in a terminal state
surfaces the existing status, never mutates twice (handles the
Discord-confirmed-first race). 10-minute TTL on web-originated
rows. CSRF protected on every POST.

29 new unit tests. Total 1637 (was 1608); mypy clean across 92 src
files; pylint **10.00/10**; black + isort clean.

### Phase 7 kickoff — Web UI / Dashboard (ADR-016 + ADR-017) (2026-05-17)

After Phase 6 closed, Phase 7 needed two architectural decisions
ratified before code, mirroring the Phase 5 + Phase 6 kickoff
pattern (ADR-013 + Phase 5 design doc; ADR-014/015 + Phase 6
design doc).

**ADR-016 — Web UI architectural commitments.** FastAPI + Jinja2 +
HTMX (no SPA / no Node / no build step). Server-rendered HTML
with HTMX for partial updates where useful. Routes consume the
existing ports via DI; **no business logic in route handlers**.
Read-mostly with ADR-013-firewalled mutations: pause/resume/stop
buttons create `PendingCommand` rows in `awaiting_confirmation`
(same state machine as Discord's ✅/❌); a UI-side two-click
confirm flow transitions to `approved`. **The ADR-002 firewall
stays intact** — `cli/live`'s `WHERE status='approved'` poll
remains the only path from intent to engine. New `cli/web` daemon
runs uvicorn bound to 127.0.0.1:8000 by default; operator opens
to LAN via their own reverse proxy. v1 mutation catalog: Pause,
Resume, Stop (per-symbol pause/resume + global stop).

**ADR-017 — Web UI authentication.** Session cookies (Starlette
`SessionMiddleware` + `itsdangerous`-signed) + single-operator
bcrypt-hashed password (cost factor 12) in a new `users` SQLite
table in operator.db. Password seeded via `cli/web create-user`
subcommand (interactive stdin prompt; refuses duplicate usernames).
Login form + CSRF protection via synchronizer-token middleware.
Rate-limited login: 5 attempts / 60s per IP. Session lifetime
7 days sliding. Cookie attributes: HttpOnly + SameSite=lax +
Secure (set when X-Forwarded-Proto=https). TLS is the operator's
reverse proxy (no bundled TLS).

**Stage 7.1 design** at `docs/planning/stage-7.1-design.md` slices
the substrate work into five sub-slices: 7.1.A users table +
domain model + StoragePort; 7.1.B WebConfig + web infrastructure
scaffolding; 7.1.C login/logout/session/CSRF + bcrypt; 7.1.D
`cli/web` daemon + `--create-user` + three stub pages; 7.1.E
stage close. ~10-12 hours; ~80-100 new unit tests planned.

**Roadmap rewrite** drops the "provisional" tag from Phase 7 and
expands the five stages (7.1 skeleton+auth → 7.2 cost+status +
mutation buttons → 7.3 advisor+harvester → 7.4 news+audit → 7.5
close). CLAUDE.md Project Status moves Phase 7 from "Next:" to
"in progress 2026-05-17."

**Six new runtime dependencies will land in Stage 7.1** —
`fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`,
`bcrypt`, `itsdangerous`. Biggest dep-add since Phase 5's
`discord.py>=2.3,<3`.

No code in this commit. Stage 7.1 sub-slice work follows.

### Stage 7.1 — Web app skeleton + auth (2026-05-17)

Five sub-slices delivered the web layer substrate Phase 7's feature
stages will wedge into. Thirteenth operator entry point landed:
`python -m wobblebot.cli.web` with `serve` (default) + `create-user`
subcommands. **No real data dashboards yet** — Stage 7.2+ lights up
cost / status / advisor / harvester / news / audit views against
this scaffold.

**Architecture.** Per ADR-016 the FastAPI app lives at
`src/wobblebot/web/`, sibling to `cli/`, exposing `create_app(...)`
as a factory so each test gets a fresh instance. Routes consume
ports via FastAPI DI — no business logic in handlers. Three
navigable empty stub pages (`/dashboard`, `/cost`, `/audit`) prove
the shell ships; each renders the layout chrome + a "Phase 7.X
placeholder" body. The `/` root redirects to `/dashboard` which
auth-redirects to `/auth/login` if no session.

**Auth (ADR-017).** Single-operator-v1: bcrypt-hashed password
stored in `operator.db`'s new `users` table; session cookie signed
by Starlette's `SessionMiddleware` (itsdangerous under the hood);
per-IP login rate-limit (5 attempts / 60s by default); CSRF
synchronizer-token middleware (`csrf_input` Jinja2 global so every
form gets a token without per-template wiring). CSRF token rotates
on login + logout (session-fixation guard).

**Sub-slices:**
- **7.1.A — Users table + domain model + StoragePort methods.**
  `domain/users.py` ships `User` + `UserCredentials` Pydantic
  models (both `frozen=True`). New `users` SQLite table with
  `UNIQUE(username)` + `CHECK(length(password_hash) > 0)`. Three
  StoragePort methods: `create_user(username, password_hash)`,
  `get_user_by_username(username) -> User | None`,
  `update_user_last_login(user_id, last_login_at)`. 28 new unit
  tests; pure persistence, no web.
- **7.1.B — WebConfig + web/ package scaffolding.** `WebConfig`
  Pydantic block in `config/cli.py` (13 fields across serving /
  auth / presentation / cross-DB-path groups; bounds-checked
  validators). `WobbleBotConfig.web: WebConfig | None`. Six new
  runtime deps in `pyproject.toml`: `fastapi>=0.115`,
  `uvicorn[standard]>=0.30`, `jinja2>=3.1`,
  `python-multipart>=0.0.12`, `bcrypt>=4.2`, `itsdangerous>=2.2`
  (biggest dep-add since Phase 5's `discord.py`). New
  `src/wobblebot/web/` package — `app.py` factory skeleton,
  `middleware.py` + `auth.py` skeletons, `dependencies.py` (8 DI
  factories), `routes/__init__.py` + empty `auth.py` / `pages.py`.
  `templates/base.html` + `templates/layout.html` + `static/htmx.min.js`
  placeholder + `static/base.css` (login + dashboard styles)
  committed. 25 new unit tests for the config block.
- **7.1.C — Login / logout / session middleware / CSRF.**
  `web/auth.py` — `hash_password` / `verify_password` (bcrypt
  direct, no `passlib`), `current_user` + `require_user` FastAPI
  deps, `AuthRedirectRequired` exception. `web/middleware.py` —
  CSRF synchronizer-token helpers (`get_or_create_csrf_token`,
  `require_csrf_token`, `rotate_csrf_token`) + `LoginRateLimit`
  (`asyncio.Lock`-guarded per-IP token bucket; resets on
  successful login). `web/routes/auth.py` — `GET /auth/login`
  renders form with CSRF; `POST /auth/login` runs rate-limit →
  CSRF → bcrypt → session set → last-login bump → 302 /dashboard;
  `POST /auth/logout` clears session + rotates CSRF. `web/app.py`
  registers the `AuthRedirectRequired` exception handler +
  instantiates `LoginRateLimit` on `app.state` + exposes
  `csrf_input` as a Jinja2 global. `templates/login.html` extends
  `base.html` directly (not `layout.html`) so the nav chrome
  doesn't appear pre-auth. 108 new unit tests (FastAPI
  `TestClient` against in-memory SQLite).
- **7.1.D — `cli/web` daemon + create-user + stub pages.**
  `cli/web.py` with two argparse subcommands. `serve` (default)
  opens `operator.db` plus four optional cross-DB paths and hands
  the FastAPI app to `uvicorn.run`. `create-user` prompts on
  stdin for username + on the terminal (via `getpass.getpass`)
  for password — twice for confirmation — hashes via bcrypt at
  the configured cost, inserts via `StoragePort.create_user`.
  Duplicate username + EOF + DB-open failures all exit 2 with a
  clean error message — no raw tracebacks. `web/routes/pages.py`
  fleshed out: `/` → 302 /dashboard, plus three auth-gated stubs
  using `require_user` so anonymous round-trips to /auth/login.
  New shared `templates/stub.html`. 40 new unit tests (14 pages +
  26 cli/web). Per-test logger-state-restore fixture keeps
  `configure_logging` side effects from leaking into downstream
  caplog-based tests.
- **7.1.E — Stage close.** Roadmap + CLAUDE.md + this CHANGELOG
  + `config/settings.example.yml` (new `web:` block) + `.env.example`
  (new `WOBBLEBOT_WEB_SESSION_SECRET` var) + project_state memory
  all reflect Stage 7.1 ✅. Schema-drift tests pass clean.

**Deprived-env walkthrough green** (`cli/web` exit codes, all exit
2 with no tracebacks): bad `--config` path; bad `--profile` name;
missing `web:` block in settings; missing
`WOBBLEBOT_WEB_SESSION_SECRET` env var (error includes the
`python -c "import secrets; print(secrets.token_urlsafe(32))"`
mint command); EOF on stdin during `create-user`.

**Numbers.** 1608 unit tests pass (was 1460 at Phase 6 close,
+148 across the five sub-slices); 29 integration tests opt-in;
mypy clean across 89 src files; pylint **10.00/10**; black +
isort clean. **Stage 7.1 total real-money cost: $0.00** (no live
ops; the dashboard is read-mostly and mutations are firewalled
per ADR-013). Running project cost **$0.085018** unchanged from
Phase 6 close.

### Stage 6.5 — Phase 6 integration check + close (2026-05-17)

Closing stage of Phase 6. Two sub-slices: smoke-test scaffold +
audit-driven refactor (6.5.A); live verification + closing summary
(6.5.B). All three cloud providers validated end-to-end against
real APIs under live cost-cap enforcement. **$0.005018 of real
money spent** across three smoke-test calls.

**6.5.A — Smoke-test scaffold + audit-driven refactor.**

*Audit-driven refactor pass* (Phase-6-close per the global rule).
Three more shared patterns promoted out of per-provider modules
on top of Stage 6.3.A's `execute_cloud_call` extraction:
- `services/llm_pricing.estimate_cost_ceiling(provider, model,
  prompt_text, max_tokens)` — three byte-identical copies pre-
  refactor.
- `services/llm_cloud_call.parse_advisor_recommendation(raw_text,
  fallback_role, provider_name)` — three byte-identical copies
  pre-refactor for the AdvisorPort parse path.
- `services/llm_cloud_call.parse_intent_dict(raw_text,
  provider_name)` — three byte-identical copies pre-refactor for
  the AssistantPort parse path.

Net: ~270 LOC of mechanical duplication collapsed. Per-provider
modules now own only their genuinely-different surface — HTTP wire
shape, token-count normalization, response text extraction.

*Operator smoke-test tool.* `tools/run_cloud_check.py` — one-shot
live smoke test against any of the three cloud providers. Args:
`--provider` / `--role` / `--model` (cheap defaults) /
`--max-tokens 100` (low floor) / `--dry-run` (gate-disable, NOT
no-call) / `--daily-cap` / `--session-cap` / `--log-format`. Reads
provider-specific API key from env; clean exit 2 on missing key.
Persists the receipt to operator.db's `llm_calls` table.

*Integration test stubs.* `tests/integration/test_cloud_llm_live.py`
— three integration-marked tests (one per provider), each opt-in
via the provider's API-key env var. Same shape as
`test_kraken_trading_live.py` skip-when-key-missing pattern.

*Live verification.* Operator's environment had all three keys
loaded via `.env`; smoke test ran against each provider:

  | Provider  | Model              | In   | Out | Reason | Cost USD  |
  | --------- | ------------------ | ---- | --- | ------ | --------- |
  | anthropic | claude-sonnet-4-6  | 1321 |  19 |      0 | 0.004248  |
  | openai    | gpt-4o-mini        | 1171 |  15 |      0 | 0.000185  |
  | google    | gemini-2.5-flash   | 1281 |  20 |     43 | 0.000585  |

Google's `tokens_reasoning=43` correctly normalized through the
additive convention from `extract_google_tokens`.

**6.5.B — Phase 6 close.** Closing summary at
`docs/planning/phase-6-summary.md` (~250 lines; mirrors
phase-{2,3,4,5}-summary.md precedent). Roadmap closes Phase 6 ✅
and Stage 6.5 ✅. CLAUDE.md Project Status updated. project_state
memory bump. CryptoCompare 90-day evaluation **deferred to its
scheduled 2026-08-13 date per ADR-010** — the proper observation
window hasn't elapsed yet; closing it now without 90 days of
real usage would be premature.

**1460 unit tests** pass (was 1455 at Stage 6.4 close; +5 from
`estimate_cost_ceiling` test class); 29 integration tests opt-in
(was 26; +3 cloud-llm-live); mypy clean (79 src files); pylint
10.00/10; black + isort clean. **No new runtime deps**. Phase 6
real-money cost: **$0.005018** (smoke test); running project total
$0.08 → **$0.085018**.

**Phase 6 architectural payoff:** three providers on one shared
orchestrator (`services/llm_cloud_call.py`). Adding a fourth
provider in any future phase would cost ~250-500 LOC — just the
provider-specific HTTP shape + token normalization + response
parsing, plus the dispatch branch wiring. Cost-tracking, retry,
persistence, session tracking, and JSON parsing all stay in
`services/`.

### Stage 6.4 — Google Gemini adapter (2026-05-17)

Third and final cloud provider; closes the per-provider work
ahead of Stage 6.5's integration check. Two sub-slices (down from
three for the previous stages — the shared helper extracted in
Stage 6.3.A has paid off enough that wiring + close fits in one
slice).

**6.4.A — Google advisor + assistant adapters.** New
`adapters/google.py` with both `GoogleAdvisorAdapter` (AdvisorPort)
and `GoogleAssistantAdapter` (AssistantPort) sharing all the
Gemini-specific helpers in one module. API target is Google
Generative AI REST (`generativelanguage.googleapis.com`); Vertex
AI is out of scope (avoids the OAuth + GCP-project ceremony for
a hobby-tier bot).

Provider-specific helpers:
- `extract_google_tokens` — the simplest reasoning-token
  normalization of the three Phase 6 providers. Gemini reports
  `thoughtsTokenCount` separately from `candidatesTokenCount` and
  these are **additive natively** — no subtraction needed (unlike
  OpenAI which had to subtract from completion, unlike Anthropic
  which lumps inside output_tokens). The extractor records both
  as-is.
- `parse_candidate_text` — concatenates `text` parts from
  `candidates[0].content.parts`, filtering non-text parts
  (inlineData / executableCode / etc.).
- `post_generate_content` — POST to
  `/v1beta/models/{model}:generateContent` with `x-goog-api-key`
  header (the v1beta-preferred shape; cleaner than the `?key=`
  query-string fallback). Model id is embedded in the URL path,
  not in the body.
- `_build_generate_body` — composes the Gemini-shaped body:
  `systemInstruction.parts` (separate top-level field, NOT a
  message in `contents`), `contents` array of role+parts dicts,
  `generationConfig` for temperature + maxOutputTokens.
- `_user_part` + `_model_part` — note that Gemini uses role=`model`
  (NOT `assistant`) for assistant turns. The assistant adapter
  maps operator→user / assistant→model on the wire.

24 new unit tests focused on the Google-specific bits:
- Pure helpers: cost ceiling math vs gemini-2.5-pro pricing;
  token extraction across no-thinking / additive-thinking /
  zero-thinking / empty-usage / missing-responseId;
  parse_candidate_text basic + multiple-parts + non-text-parts
  filter + empty.
- Wire shape: x-goog-api-key header + URL endpoint with model
  embedded; systemInstruction separate from contents; user-vs-model
  role mapping verified explicitly.
- Advisor happy path: round-trip records cost (gemini-2.5-pro);
  additive thinking tokens (100 visible + 300 thoughts both
  recorded; cost uses the gemini-2.5-flash explicit thinking-rate
  override from llm_pricing — $3.50/1M for thoughts vs $2.50/1M
  for regular output); prose-wrapping JSON.
- Advisor failures: 403 wraps as AdvisorError with http_403;
  empty candidates raises.
- Assistant: command + query intents round-trip; non-operator
  prompt rejected; empty api_key rejected; cost-cap trips before
  call.
- Construction guards.

**6.4.B — CLI dispatch wiring + Stage 6.4 close.**
`cli/advise._build_advisor_adapter` adds the `google` branch with
`GOOGLE_API_KEY` env-var validation; `cli/operator._build_assistant`
does the same. `AssistantLLMConfig.provider` Literal closes with
all four providers (`ollama`, `anthropic`, `openai`, `google`).
`_UNIMPLEMENTED_PROVIDERS` is now empty — the only error path
left in the dispatcher is "missing `llm:` block" for cloud
providers. Test refactor:
`test_unimplemented_cloud_provider_rejected` becomes
`test_google_without_cloud_wiring_rejected` since the
"not implemented" surface no longer exists.

**1455 unit tests** pass (up from 1431 at Stage 6.3 close; +24 across
Stage 6.4's two sub-slices). mypy clean (79 src files). pylint
10.00/10. black + isort clean. **No new runtime dependencies** —
Google adapter is pure httpx + pydantic. Phase 6 real-money cost
still **$0.00** (Stage 6.5 is the first real API call); running
project total **$0.08** unchanged.

All three Phase 6 cloud providers now ship. Each adapter file
lands at ~530-580 lines including both Advisor + Assistant
implementations + provider helpers — the shared
`execute_cloud_call` orchestrator carries the cost-flow weight.
Stage 6.5 (Phase 6 integration check + first real API calls)
remains.

### Stage 6.3 — OpenAI adapter + shared cloud-call helper (2026-05-17)

Second cloud provider lands plus an extracted shared orchestrator
so Stages 6.4 (Google) and any future cloud provider reuse the
ADR-014/015 flow instead of re-implementing it. Three sub-slices:

**6.3.A — Shared cloud-call helper + refactor Anthropic.** New
`services/llm_cloud_call.py`:
- `CloudCallContext` frozen dataclass bundles storage +
  session_tracker + cost_config + retry_config + role + provider +
  model (the per-adapter identity).
- `classify_error(exc) -> str` pure function promoted out of the
  Anthropic adapters where it was duplicated.
- `execute_cloud_call(ctx, estimated_cost_usd, call_fn,
  extract_tokens)` runs the full ADR-014/015 sequence: check_budget
  → retry_with_backoff(call_fn) → on success build+persist
  LLMCallRecord from extracted tokens + update tracker → on failure
  build+persist failure record with classified error_kind + re-raise.
  Provider-specific shape lives in two closures: `call_fn`
  (zero-arg async returning the parsed envelope) and
  `extract_tokens` (envelope → (in, out, reasoning, request_id)
  tuple).

Anthropic adapters refactored to use the helper — each
`get_recommendation` / `parse_intent` shrinks ~80 lines of
cost-flow boilerplate to ~30 lines of provider-specific body
building + a single `execute_cloud_call` call. New module-level
`extract_anthropic_tokens` carries the Anthropic-specific
normalization (tokens_reasoning=None because the API lumps thinking
with output). Zero behavior change — all 39 Anthropic tests stay
green.

21 new helper tests covering: classify_error matrix (parametrized
5xx + 4xx codes + every transient httpx type + ValueError fallback),
happy path (record persisted with real tokens + cost + tracker
updated; reasoning tokens flow through the extractor), cost gate
(daily + session trips before the call), failure path (permanent
4xx + retry exhaustion + connect error all record failure with
classified error_kind + re-raise).

**6.3.B — OpenAI advisor + assistant adapters.** New
`adapters/openai.py` with both `OpenAIAdvisorAdapter` (AdvisorPort)
and `OpenAIAssistantAdapter` (AssistantPort). Provider-specific
helpers:
- `is_reasoning_model` — name-pattern detection (`o1`, `o3` prefixes).
  Drops `temperature` from the request body for reasoning models;
  always uses `max_completion_tokens` for forward-compat.
- `extract_openai_tokens` — the meaningful provider-specific
  normalization. OpenAI's o-series returns `completion_tokens` that
  INCLUDES reasoning, with `completion_tokens_details.reasoning_tokens`
  reporting the subset. To satisfy the
  `tokens_reasoning is additive to tokens_out` convention, the
  extractor subtracts reasoning from completion. Cost math via
  `cost_for()` applies output rate to both — matching how OpenAI
  bills o-series.
- `parse_message_content` — pulls assistant text from
  `choices[0].message.content`, handling both the string shape and
  the multimodal list-of-parts shape.
- `post_chat_completion` — `Authorization: Bearer <key>` (not
  Anthropic's `x-api-key`) plus optional `OpenAI-Organization`
  header.

Both adapters ~530 lines total. 31 new unit tests covering pure
helpers + wire shape + advisor happy path + reasoning-token
recording + parse failures + assistant intent variants + multi-turn
ordering + construction guards + cost-cap trip.

**6.3.C — CLI dispatch wiring + stage close.**
`cli/advise._build_advisor_adapter` adds `openai` branch with
`OPENAI_API_KEY` + optional `OPENAI_ORGANIZATION` env-var reads.
`cli/operator._build_assistant` does the same.
`AssistantLLMConfig.provider` Literal extends from
`["ollama", "anthropic"]` to
`["ollama", "anthropic", "openai"]`. `_UNIMPLEMENTED_PROVIDERS`
shrinks to `("google",)`. `.env.example` documents the optional
`OPENAI_ORGANIZATION` env var. Test refactor:
`test_unimplemented_cloud_provider_rejected` switched from `openai`
(now implemented) to `google`.

**1431 unit tests** pass (up from 1379 at Stage 6.2 close; +52 across
Stage 6.3's three sub-slices — 21 helper + 31 OpenAI). mypy clean
(78 src files). pylint 10.00/10. black + isort clean. **No new
runtime dependencies** — OpenAI adapter is pure httpx + pydantic
on existing dependencies. Phase 6 real-money cost still **$0.00**
(Stage 6.5 is the first real API call); running project total
**$0.08** unchanged from Phase 2 close.

### Stage 6.2 — Anthropic adapter (2026-05-17)

First real cloud-provider adapter under Phase 6. Both
`AnthropicAdvisorAdapter` (AdvisorPort) and `AnthropicAssistantAdapter`
(AssistantPort) ship with the full ADR-014 cost-tracking flow
internalized: estimate → `check_budget` → `retry_with_backoff` (per
ADR-015) → persist `LLMCallRecord` → update `SessionCostTracker`.
No real API call yet — Stage 6.5 is the first.

Three sub-slices, each landed in its own commit:

**6.2.A — Anthropic shared client + AdvisorAdapter.** New
`adapters/anthropic.py` carrying the shared Messages-API helpers
(`estimate_cost_ceiling`, `parse_text_blocks`, `build_call_record`,
`post_messages`) plus `AnthropicAdvisorAdapter`. Constructor takes
storage + session_tracker + cost_config + retry_config alongside
the usual model/prompt/role; `get_recommendation` runs the full
flow inline. Anthropic thinking tokens recorded as
`tokens_reasoning=None` (the API lumps them with `output_tokens` +
bills at output rate; cost is correct via the pricing fallback).
Reuses `extract_last_json_object` from `adapters/ollama`
(module-public since Stage 5.3). New `SessionCostTracker` mutable
class in `services/llm_cost_gate.py` — one per CLI process
lifetime, shared across every adapter the CLI builds. 32 new
unit tests covering pure helpers + happy paths + cost gate
(daily + session caps, dry-run posture) + retry/backoff (5xx +
429 transient, 4xx permanent, exhaustion propagates
`LLMRetryExhausted`) + parse failures + construction guards.

**6.2.B — AnthropicAssistantAdapter.** New
`adapters/anthropic_assistant.py` implementing `AssistantPort`.
System prompt = operator prompt body + engine state snapshot;
recent turns mapped operator→user / assistant→assistant; current
operator message as final user turn. Same cost-tracking flow as
the advisor adapter, role=operator on every LLMCallRecord.
Module-level `TypeAdapter[OperatorIntent]` for the two-level
discriminator resolution. Constructor refuses non-operator-role
prompts + empty api_key. 17 new unit tests covering every
OperatorIntent variant + wire-shape verification + cost-tracking
+ retry + parse failures.

**6.2.C — CLI dispatch wiring + stage close.**
`cli/advise._build_ollama_advisor` → `_build_advisor_adapter`
with provider dispatch (`ollama` / `anthropic`; `openai` and
`google` still raise "not implemented"). New `_CloudWiring`
frozen dataclass bundles storage + tracker + LLMConfig and
threads through `_build_advisor` + `_build_expert_entry` +
`_build_arbitrator_entry`. `_main_async` opens an extra
operator.db storage when `config.llm` is set; errors at startup
if `config.llm` is set without `config.operator`. `cli/operator`
gains `_build_assistant` helper dispatching on
`OperatorConfig.assistant.provider`. `AssistantLLMConfig.provider`
Literal extends from `["ollama"]` to `["ollama", "anthropic"]`.
Test refactor: `test_unimplemented_cloud_provider_rejected`
switched from `anthropic` (now implemented) to `openai`; new
sibling test `test_anthropic_without_cloud_wiring_rejected`
verifies the clear error message when an `llm:` block is missing.

**1379 unit tests** pass (up from 1334 at Stage 6.1 close; +45 across
Stages 6.2's three sub-slices — 32 advisor + 17 assistant + 5
SessionCostTracker, with -9 from refactor/dedup). mypy clean (76
src files). pylint 10.00/10. black + isort clean. **No new
runtime dependencies** — Anthropic adapter is pure httpx +
pydantic. Phase 6 real-money cost still **$0.00** (Stage 6.5 is
the first real API call); running project total **$0.08**
unchanged from Phase 2 close.

### Stage 6.1 — Shared cloud-LLM infrastructure (2026-05-17)

First Phase 6 implementation stage; pure foundation with **zero real
API calls**. Lays down the substrate every cloud-provider adapter
(Stages 6.2-6.4) will consume: cost accounting, budget enforcement,
retry/backoff, per-provider config schemas, and operator inspection.
Five sub-slices, each landed in its own commit:

**6.1.A — Cost-tracking domain + storage.** `LLMCallRecord` frozen
Pydantic value object (caller-minted UUID id + timestamp + 7-way role
Literal + 3-way provider Literal + tokens triple [in/out/reasoning] +
Decimal cost_usd + request_id + success + error_kind). `llm_calls`
SQLite table + three indexes (timestamp / provider+model / role).
`StoragePort.save_llm_call` + `get_llm_calls(since, role, provider,
limit)` with newest-first ordering. `LLMCostCapExceeded` domain
exception carrying budget state for self-explanatory operator
notifications. 33 new unit tests. Drive-by: fixed pre-existing
`implicit-str-concat` in `sqlite_storage.get_conversation_turns`;
file-level `# pylint: disable=too-many-lines` on sqlite_storage.py
(now 1037 lines; adapter is naturally many-methods).

**6.1.B — Pricing table + cost gate.** `services/llm_pricing.py`
with the 8 in-scope models (Claude Sonnet 4.6 + Opus 4.7, gpt-4o +
gpt-4o-mini + o1 + o3-mini, gemini-2.5-pro + gemini-2.5-flash), each
entry comment-annotated with the provider's pricing-page URL and a
`verified_date`. `cost_for()` applies (input + output + reasoning)
rates with reasoning falling back to output rate unless overridden
(Gemini-flash thinking carries an explicit higher rate). Unknown
(provider, model) raises `PricingLookupError` — silent zero would
defeat ADR-014. `services/llm_cost_gate.py` with `LLMCostConfig`
(defaults $1.00/day + $0.50/session + `enforce=True`) and
`check_budget(storage, role, estimated_cost_usd, session_spent_usd,
config)` returning `GateAllow | GateDeny`. Session cap checked first
(in-memory, no DB round-trip); daily cap uses sliding 24h window
via `storage.get_llm_calls(since=now-24h)`. `enforce=False`
short-circuits to allow (ADR-014 decision 8 dry-run posture).
`test_pricing_freshness.py` watchdog fails CI when any entry's
`verified_date` is >180 days behind today. 38 new unit tests.

**6.1.C — Retry/backoff helper.** `services/llm_retry.py` with
`LLMRetryConfig` (max_retries=3, initial_backoff_seconds=1.0,
backoff_multiplier=2.0, all frozen + validated). `default_classifier`
per ADR-015: httpx Connect/Read/Write/Pool/RemoteProtocol → transient;
HTTPStatusError 429+5xx → transient, other 4xx → permanent; everything
else permanent (don't retry bugs). `retry_with_backoff(fn, config, *,
classifier, sleep_fn)` runs `fn` up to 1+max_retries times, sleeps
between attempts with `initial * multiplier ** attempt`, re-raises
permanent immediately, raises `LLMRetryExhausted` chaining `__cause__`
when transient retries exhaust. `sleep_fn` injection keeps tests
millisecond-fast. 36 new unit tests.

**6.1.D — Config schemas + env wiring.** `config/llm.py` with
`LLMConfig` composing `cost: LLMCostConfig` + `retry: LLMRetryConfig`
(both children carry their own defaults). `WobbleBotConfig.llm:
LLMConfig | None = None` (None = pure-Ollama deployment, gate
inactive — opt-in posture matching ADR-012's `auto_apply.enabled`
default). `.env.example` cloud-LLM-keys comment block refreshed for
Phase 6 + ADR-014/015 framing alongside the existing Phase 3 MoE
framing. `config/settings.example.yml` gains a documented `llm:`
block between `operator:` and `profiles:` with comments explaining
the dry-run posture and retry-defaults formula. Existing
schema-drift tests guard example/operator alignment automatically.
13 new unit tests.

**6.1.E — Inspection tool + stage close.** `tools/show_llm_costs.py`
operator inspection (`--db-path`, `--since-hours`, `--provider`,
`--role`, `--limit`, mutex `--by-provider | --by-role`,
`--log-format`). Default mode: per-row print + grand-total footer.
Rollup modes sort desc by cost. Deprived-env walkthrough green:
missing DB → exit 2; empty table → exit 0 + "no rows match"; seeded
rows → properly formatted output; mutex flags enforced by argparse.
Roadmap / CLAUDE.md / project_state memory updated.

**1334 unit tests** pass (up from 1214 at Phase 5 close; +120 across
Stage 6.1's five sub-slices). mypy clean (74 src files). pylint
10.00/10. black + isort clean. No new runtime dependencies — pricing
table is data, everything else is pure Python on existing httpx +
pydantic. Real-money cost still **$0.00 for Phase 6** (Stages 6.2-6.5
are the first to make actual API calls); running project total
**$0.08** unchanged from Phase 2 close.

### Phase 6 kickoff — Cloud LLM Integration (ADR-014 + ADR-015) (2026-05-17)

After Phase 5 close + the Phase 8.0 refactor slot decision, Phase 6
(Cloud LLM Integration) needed two architectural decisions ratified
before code, mirroring the Phase 5 kickoff pattern (ADR-013 +
`stage-5.1-design.md`).

**ADR-014 — LLM cost caps.** Per-day + per-session USD caps via
`services/llm_cost_gate.check_budget` against a new `llm_calls`
SQLite table in `operator.db`. Hard-stop on cap trip (raises
`LLMCostCapExceeded`). Single-pool across roles in v1; per-role
split deferred. Pricing table is **code, not config** — entries
carry `verified_date` + comment-annotated pricing-page URLs; a
`test_pricing_freshness` watchdog fails CI when entries are >180
days old. `enforce=False` dry-run posture for the first week of
cloud usage.

**ADR-015 — Cloud LLM provider failover policy.** Default policy:
fail loudly + retry on transient errors only. Transient = HTTP 429 /
5xx + httpx connection/timeout exceptions. Permanent = HTTP 4xx
(non-429) + every other exception class. Up to 3 retries with
exponential backoff (1s, 2s, 4s by default formula
`initial * multiplier ** attempt`). **No cross-provider failover.**
**No silent cloud-to-Ollama failover** — silent model substitution
breaks audit provenance. Retries draw from the same ADR-014 cost
pool (one budget check per logical call, not per attempt).
Per-provider auth lives in env (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
/ `GOOGLE_API_KEY`). Single shared `LLMRetryConfig` across providers
in v1.

**Stage 6.1 design + roadmap rewrite.**
`docs/planning/stage-6.1-design.md` slices Stage 6.1 into five
sub-slices. Roadmap drops the "provisional" tag from Phase 6 and
expands the five stages (6.1 infrastructure → 6.2 Anthropic → 6.3
OpenAI → 6.4 Google → 6.5 integration check). CLAUDE.md Project
Status moves Phase 6 from "Next:" to "in progress 2026-05-17."

No code in the kickoff commit. Stage 6.1 sub-slice work follows.

### Phase 5 kickoff — Operator Interaction Engine (ADR-013) (2026-05-16)

After Phase 4 close, the operator surfaced a broader vision than the
roadmap's narrow Stage 5.1.5 (outbound Discord notifier) + Stage 5.2
(structured slash commands): Discord should be a bidirectional
**interaction engine** with multi-turn conversational LLM intent
parsing, ADR-002-preserving confirm-before-execute, and DB-mediated
decoupling between `cli/operator` and `cli/live`.

This becomes the whole of Phase 5. The originally-scoped Phase 5
stages (dashboard, reliability, maintenance, performance, v1.0
release) reorganized into three downstream phases: **Phase 6 Cloud
LLM Integration** (cloud assistant + cloud advisor adapters),
**Phase 7 Web UI / Dashboard**, **Phase 8 Hardening & v1.0 Release**
(reliability + maintenance worker + performance tuning + v1.0 soak).

Kickoff commit landed `ADR-013` (10 architectural commitments
including OperatorPort + AssistantPort split, OperatorIntent strict
typed sum, confirm-before-execute as the ADR-002 firewall, DB-mediated
decoupling, multi-turn conversation state with prompt-context pronoun
resolution, user+channel allowlist auth, pluggable LLM provider with
Ollama in Phase 5 / cloud in Phase 6, `discord.py` as the Gateway
client), `docs/planning/stage-5.1-design.md` (full slicing plan and
implementation-level decisions), and the roadmap rewrite to seven
Phase 5 stages plus the new Phases 6 / 7 / 8.

### Stage 5.7 — Phase 5 Integration Check + Phase 5 Close (2026-05-16)

Seventh and final Phase 5 slice. Closes Phase 5 with TTL expirer +
end-to-end integration test + the per-precedent phase summary
document. Three sub-slices:

**5.7.A+B (bundled — small enough to land together).**

  **TTL expirer for pending_commands.** cli/operator gains a third
  background asyncio.Task (alongside the notification forwarder and
  Gateway client). The expirer scans pending_commands WHERE
  status='awaiting_confirmation' AND ttl_expires_at < now every
  ttl_expirer_poll_seconds (default 30s) and transitions matches to
  'expired'. Per ADR-013 decision 3 the operator's ✅/❌ reaction is
  the ONLY way out of awaiting_confirmation, so without TTL expiry
  abandoned commands accumulate forever. OperatorConfig gains
  ttl_expirer_poll_seconds: float = 30.0 (positive).

  **End-to-end integration test suite.**
  tests/integration/test_phase5_operator_e2e.py exercises the full
  operator-interaction round-trip without a real Discord Gateway,
  Ollama LLM, or Kraken exchange — the test stubs the LLM and the
  Discord transport but uses real SQLite + real GridEngine + real
  OperatorService + real cli/operator handler functions + real
  cli/live poll helper.

  Five scenarios covered:
  - test_full_pause_round_trip: "pause BTC" → confirm embed → ✅ →
    cli/live picks up approved command → engine actually pauses →
    row marked dispatched with success.
  - test_reject_flow_does_not_dispatch: ❌ reaction → marked
    rejected → cli/live's poll skips it → engine never pauses.
  - test_multi_turn_conversation_records_history: two operator
    messages → 4 conversation_turns; second invocation's context
    sees the first turn pair.
  - test_notification_persisted_and_forwarded: SqliteNotifierAdapter
    writes → forwarder reads + posts embed + marks forwarded.
  - test_ttl_expiry_skipped_by_dispatch: expired commands never
    dispatch even when cli/live polls.

  5 + 5 new tests (5 unit for ttl_expirer + 5 integration for the
  e2e suite). cli/operator module docstring trimmed to keep the
  file under pylint's 1000-line cap (was 1006 after the expirer
  addition; now 990).

**5.7.C — Phase 5 close.** New
docs/planning/phase-5-summary.md (~200 lines) consolidates:
- Per-stage outcomes table for all seven stages + kickoff.
- The Phase 5 reframe story (originally seven small stages →
  one cohesive interaction-engine phase mid-kickoff).
- ADR-013 commitments × shipped reality (all 10 ratified
  commitments held intact).
- v1 limitation flagged for the future: cli/operator's stub
  engine can't see cli/live's in-memory pause state, so
  StatusQuery reports all symbols as 'active'. Fix path
  documented (~50-line slice persisting pause state to a shared
  SQLite table). Probably Phase 8 hardening.
- Test + code health delta (792 → 1214 unit tests, +422; 60 → 69
  src modules; pylint 10.00/10 cleared the pre-existing
  too-many-lines flag on sqlite_storage.py mid-stage).
- Real-money cost ledger: Phase 5 added $0.00; running total
  unchanged at $0.08.
- Entry conditions for Phase 6 (Cloud LLM Integration): the
  AssistantPort is provider-neutral by construction;
  AssistantLLMConfig.provider extends from Literal["ollama"] to
  include the cloud providers; no new SQLite tables needed for the
  cloud adapters themselves, though Phase 6 likely adds an llm_calls
  cost-tracking table.

**Health at Phase 5 close:** 1214 unit tests pass (was 792 at
Phase 4 close, +422 across Phase 5's seven stages); 26 integration
tests opt-in (was 21, +5 from the e2e suite); mypy clean across 69
src files (was 60, +9 new modules); pylint **10.00/10** with no
outstanding warnings; black + isort clean. New runtime dep:
`discord.py>=2.3,<3` (gated under operator interaction).

**Phase 5 closing summary at `docs/planning/phase-5-summary.md`.**
Mirrors phase-2/3/4 precedent.

**Phase 5 total real-money cost: $0.00.** Every test stubs Discord
/ Ollama / Kraken; the live verification "real operator types in
real Discord" is operator-driven and tracked separately.

Running project real-money cost still **$0.08** unchanged from
Phase 2 close.

Phase 5 stages closed: 5.1 / 5.2 / 5.3 / 5.4 / 5.5 / 5.6 / 5.7.
Phase 6 entry conditions met.

### Stage 5.6 — cli/operator Daemon (2026-05-16)

Sixth Phase 5 slice. The long-running CLI that ties together every
piece Phase 5 has shipped — DiscordTransport (5.2) +
OllamaAssistantAdapter (5.3) + OperatorService (5.4) +
SqliteNotifierAdapter (5.5). Four sub-slices + close:

**5.6.A — conversation_turns table + StoragePort.** Third Phase 5
SQLite table: id PK (UUID), channel_id, user_id, role (CHECK in
operator/assistant), content, intent_json (nullable; populated
for parsed operator turns), timestamp. Two indexes — composite
(channel_id, user_id, timestamp) for the prompt-assembly scope
read and (timestamp) for forensic queries. Three new StoragePort
methods: save_conversation_turn (upsert via ON CONFLICT DO UPDATE
so the typical save-on-receipt + re-save-with-intent flow works
without losing the row), get_conversation_turns(channel_id,
user_id, limit) (returns chronologically; when limit is set the
adapter fetches newest-N via DESC+LIMIT then reverses in Python).
row_to_conversation_turn uses a new module-level
TypeAdapter[OperatorIntent] for discriminator rebuild. 10 new
unit tests covering round-trip (operator with intent / assistant
without), nested IntentCommand preservation, scope isolation,
chronological ordering, limit returning newest-N-chronologically,
CHECK rejects unknown role, upsert replaces content + intent.

**5.6.B — OperatorConfig schema.** Three new Pydantic models in
config/cli.py:
- AssistantLLMConfig — provider (ollama for Phase 5), model,
  prompt_file (default config/prompts/operator.md), base_url,
  temperature (0.3 default per the operator.md hint), max_tokens
  (512), timeout_seconds.
- OperatorAuthConfig — bot_token_env_var (default
  DISCORD_BOT_TOKEN), allowed_user_ids, allowed_channel_ids (both
  frozenset, deny-by-default per ADR-013 decision 6),
  outbound_channel_id (where confirm embeds + forwarded
  notifications go; daemon validates at startup that it's in
  allowed_channel_ids).
- OperatorConfig composing both + operator_db (the daemon's own
  pending_commands/notifications/conversation_turns DB) + optional
  live_db/advise_db/news_db/harvest_db for cross-database queries
  + the ADR-013 knobs (context_window_turns 10 capped 1-50,
  confirm_ttl_seconds 300, forwarder_poll_seconds 2.0).
WobbleBotConfig gains operator: OperatorConfig | None = None. 18
new unit tests across defaults, required fields, bounds (temp 0-2,
context window 1-50, positive TTL + poll), frozenness.

**5.6.C — cli/operator daemon.** New cli/operator entry point with
three concurrent concerns:

  Notification forwarder (background asyncio.Task):
  _forwarder_loop polls notifications WHERE forwarded=0 every
  forwarder_poll_seconds, posts each as a color-coded Discord embed,
  marks forwarded on success. Per-row failures logged + batch
  continues — losing one forward beats stopping the daemon.

  Conversation flow (Discord on_message handler):
  _handle_inbound_message persists the operator turn, composes a
  ConversationContext (current message + recent N turns from
  storage + engine state snapshot from live_storage), calls
  AssistantPort.parse_intent, re-saves the turn with parsed
  intent, routes via match/case:
  - IntentCommand → write PendingCommand (awaiting_confirmation)
    + post confirm embed; record message_id → pending_id in
    in-memory map for the reaction handler
  - IntentQuery → OperatorService.answer_query + post result embed
  - IntentConversational → post reply_text as plain message
  - IntentUnparseable → surface "I couldn't parse that: <reason>"

  Confirmation flow (Discord on_raw_reaction_add handler):
  _handle_reaction looks up the in-memory map; on hit fetches the
  pending row and transitions awaiting_confirmation → approved
  (✅) or rejected (❌) with the confirming user_id + timestamp.
  Already-transitioned rows ignored (idempotency vs duplicate
  reactions). action='remove' ignored (we only care about adds).

Per ADR-013 decision 3 cli/operator NEVER calls
OperatorService.dispatch_command directly — every state mutation
crosses pending_commands so cli/live's WHERE status='approved'
poll (Stage 5.4's ADR-002 firewall) is the only path from intent
to engine.

_main_async wires storage + assistant + stub OperatorService +
DiscordTransport + the forwarder Task + SIGINT/SIGTERM handlers.
discord.py's Client.start() runs the Gateway connection until
transport.close().

v1 limitation documented in code: cli/operator's stub engine
can't see cli/live's in-memory pause state, so StatusQuery
reports all symbols as 'active'. Persisting pause state to
storage is a future-stage enhancement.

14 new unit tests for the testable seams (helper functions called
directly with synthetic InboundMessage/ReactionEvent): summarizer
output, forwarder happy path + empty + per-row failure isolation,
message routing through each IntentVariant, reaction confirm /
reject / unknown-id / double-reaction-no-overwrite /
remove-action-ignored.

**5.6.D — tools/show_pending.py + close.** Operator inspection
script in the show_*.py family pattern. Args: --db-path (default
data/wobblebot-operator.db), --status (filter to one of the six
lifecycle states), --limit (default 20), --log-format. Safe
against the live operator DB while cli/operator is running —
SQLite handles concurrent readers; no write surface.

**Health at Stage 5.6 close:** 1209 unit tests pass (was 1167 at
Stage 5.5 close, +42 across the four sub-slices); 21 integration
tests opt-in; mypy clean across 69 src files; pylint **10.00/10**
with no outstanding warnings; black + isort clean.

Running real-money cost unchanged at $0.08. cli/operator and
cli/live haven't been wired end-to-end against the operator's real
Discord + Kraken yet — that's Stage 5.7's integration check.

### Stage 5.5 — Outbound Notifications (2026-05-16)

Fifth Phase 5 slice. Lands the persistence + wiring for outbound
notifications from cli/live and cli/harvest. cli/operator (Stage 5.6)
will forward these rows to Discord; until then they accumulate in
SQLite for operator inspection. Two sub-slices + close:

**5.5.A — notifications table + SqliteNotifierAdapter.** New
`notifications` SQLite table (id PK, level CHECK against the
NotifierPort vocabulary, title + message + timestamp + context_json,
forwarded flag + forwarded_at + created_at; two indexes — forwarded
+ created_at for cli/operator's poll, timestamp for forensic
queries). New `PersistedNotification` value object in
`ports/notifier.py` wraps a raw `Notification` with row-level
fields. Three new `StoragePort` methods: `save_notification` (returns
the assigned row id), `get_notifications(forwarded=..., limit=...)`
(ordered by created_at ASC so cli/operator forwards the oldest
unforwarded event first), and `mark_notification_forwarded`
(idempotent UPDATE; raises StorageError if row not found).
`adapters/sqlite_notifier.py` — thin SqliteNotifierAdapter wrapping
any StoragePort. `send_notification` calls
`storage.save_notification` and wraps StorageError as NotifierError.
`send_error_alert` synthesizes a critical Notification from the
exception (type name as title, str(exc) or repr(exc) as message,
operator-supplied context dict). 14 new unit tests.

**5.5.B — cli/live + cli/harvest notification wiring.** Both CLIs
gain an `operator_db: str | None = None` config field; when set
they open a second SQLiteStorageAdapter and wrap it with
SqliteNotifierAdapter. Both CLIs gain a local `_notify(notifier, ...)`
helper that swallows NotifierError / WobbleBotPortError so a broken
notifier can NEVER break the engine loop — Phase 5 treats
notifications as forensic ledger entries; losing one beats stopping
trading.

  cli/live emit points:
  - **session start** (info): symbols / tick_seconds / caps / starting_usd
  - **per-tick fills** (info): when `StepResult.fills > 0`, one
    notification per (symbol, tick) pair with fills + counters_placed
    counts
  - **cap trip** (error): right before _run_one_tick returns True
    on session-loss-cap path
  - **session end** (info or error depending on exit_code): ticks /
    duration / starting+ending USD / PnL / cancellation counts

  cli/harvest emit points:
  - **proposal generated** (info): every non-None TransferProposal
    in _run_cycle includes proposal_id / direction / asset / amount /
    rationale; message text hints "Run cli/harvest --execute <id>
    to act"
  - **withdrawal failed** (error): when Kraken /Withdraw rejects,
    paired with the failed TransferResult audit row
  - **withdrawal executed** (warning, not info — money moved, the
    operator wants it surfaced loudly): refid + destination +
    pending status

8 new unit tests (3 cli/live + 5 cli/harvest) covering the helper's
no-op-on-None behavior, persistence via SqliteNotifierAdapter, error
swallowing when the notifier raises, _run_cycle emitting on proposal
generation, and _run_cycle staying silent in the hold band.

Full suite **1167** passes (was 1145 at Stage 5.4 close, +22). mypy
clean across 68 src files. pylint **10.00/10** with no outstanding
warnings. black + isort clean.

Per ADR-013 decision 9 neither cli/live nor cli/harvest imports
discord.py — they write to the notifications SQLite table only.
cli/operator (Stage 5.6) is the only module that will ever read
those rows and post them to Discord. Running real-money cost
unchanged at $0.08 (notifications are forensic only; no
state-mutating side effects).

### Stage 5.4 — Engine Integration (2026-05-16)

Fourth Phase 5 slice. The first stage where Phase 5 code actually
touches money-adjacent state. Four sub-slices land the four pieces
the operator interaction layer needs to reach the engine:

**5.4.A — GridEngine operator-control methods.** Engine gains
`pause_symbol(symbol)` / `resume_symbol(symbol)` / `is_paused` /
`paused_symbols`, `request_stop()` / `is_stop_requested`, and
`cancel_open_orders(symbol | None) -> (cancelled, failed)`. New
`StepAction` value `"skipped_paused"` — paused symbols return without
touching exchange or storage. Pause state is per-session in-memory
(rebuild on restart) by design. Cancel reads the open-order set from
the exchange (authoritative per ADR-006 decision 3); per-order
failures are logged and counted without aborting the batch.

**5.4.B — `pending_commands` SQLite table + StoragePort.** New table
in `sqlite_storage_schema.SCHEMA` with id PK, command_kind
denormalized for filtering, command_json + result_json for
schema-evolution headroom, the full six-state CHECK constraint on
status (`awaiting_confirmation` → `approved` → `dispatched` with
`rejected` / `expired` / `failed` terminals), three indexes (status
poll, created_at, TTL cleanup). `StoragePort` gains
`save_pending_command` (upsert via `ON CONFLICT DO UPDATE`),
`get_pending_command(id)`, `get_pending_commands(status, limit)` —
ordered by `created_at` ASC so the polling cli/live picks up the
longest-waiting approval first. `row_to_pending_command` in
`sqlite_storage_rowmap.py` uses a module-level
`TypeAdapter[OperatorCommand]` to resolve the discriminated union on
read.

**5.4.C — OperatorService.** `services/operator_service.py`
implements `OperatorPort` via match/case dispatch. Six commands
(`PauseCommand` / `ResumeCommand` / `PauseAllCommand` /
`ResumeAllCommand` / `CancelOpenOrdersCommand` / `StopCommand`) call
through to the engine and return `CommandResult` with `success` /
`side_effects` reflecting state changes. Nine queries
(`StatusQuery` / `OpenOrdersQuery` / `RecentFillsQuery` /
`RecentSuggestionsQuery` / `RecentNewsQuery` /
`HarvesterStatusQuery` / `RecentProposalsQuery` / `GridConfigQuery` /
`HelpQuery`) compose typed `*Result`s from storage + engine state.
Cross-database queries (advisor suggestions, news, harvester
proposals) take **optional** `advise_storage` / `news_storage` /
`harvest_storage` constructor params; when unwired the corresponding
queries return empty result lists rather than raising. Domain misses
encode as structured `success=False` or empty-list results; protocol
failures wrap as `OperatorError`. `HelpResult` static catalog of 15
entries matches the operator prompt's command + query catalog.

**5.4.D — cli/live poll integration.** `LiveConfig` gains optional
`operator_db: str | None = None`. When set, `cli/live` opens a
second `SQLiteStorageAdapter` (kept independent from live.db per the
per-CLI DB pattern), constructs `OperatorService` with the engine +
live storage + active symbols + grid config + session-start
timestamp, and drains approved pending commands via the new
`_process_pending_commands` helper. **The `WHERE status='approved'`
filter on the SELECT is the literal confirm-before-execute gate** —
the ADR-002 firewall that ADR-013 documents. Per-row dispatch
failures wrap as `failed` `CommandResult`s without aborting the
loop. `engine.is_stop_requested` is checked after the poll so a
`StopCommand` processed this tick exits the loop cleanly without
one more engine step. When `operator_db` is None, cli/live behaves
exactly as before — Discord-ignorant, no operator integration.

**5.4.E — Stage close.** Roadmap ✅, CHANGELOG, CLAUDE.md Project
Status bump, project_state memory update.

57 new unit tests (14 + 10 + 25 + 8 across the four sub-slices).
Full suite **1145** passes (was 1088 at Stage 5.3 close). mypy
clean across 67 src files. pylint **10.00/10** with no outstanding
warnings. black + isort clean.

Running real-money cost unchanged at $0.08 — the new code paths
require an operator-confirmed `pending_commands` row, and no such
row has been written outside test fixtures. Stage 5.6's
`cli/operator` daemon brings the Discord side online; until then
the firewall is entirely operator-pen-and-paper.

### Stage 5.3 — Operator Assistant (Ollama) (2026-05-16)

Third Phase 5 slice. `OllamaAssistantAdapter` implementing
`AssistantPort` — the LLM-side intent parser that turns operator
natural-language messages into typed `OperatorIntent` payloads.
Sister adapter to the existing Stage 3.2 `OllamaAdapter` (which
implements `AdvisorPort` for the trading recommendation flow);
different port, different endpoint, different output type, different
prompt.

**Endpoint:** Ollama's `/api/chat`, not `/api/generate`. The chat
endpoint accepts role-tagged messages (`system` / `user` / `assistant`),
giving the LLM a structured multi-turn history instead of a
concatenated prompt — better behavior for context-sensitive intent
parsing where one turn references a prior turn ("now filter to ETH").

**Code reuse (per operator guidance "always reuse what makes
sense"):** the helpers shared with the advisor adapter were
extracted rather than duplicated:

- `is_thinking_model` and `extract_last_json_object` in
  `adapters/ollama.py` promoted from underscore-private to module-public.
- New `OllamaJsonExtractError` raised by the shared extractor — each
  adapter catches and wraps as its port-specific error
  (`AdvisorError` from the advisor side, `AssistantError` from the
  assistant side). Helper stays port-agnostic.
- The ~10 lines of HTTP boilerplate per adapter (init, aclose,
  envelope key extraction) stay duplicated because the envelope
  shapes for `/api/chat` vs `/api/generate` diverge enough that a
  shared wrapper would carry conditional logic for marginal DRY win.

**Prompt:** new `config/prompts/operator.md` with frontmatter
declaring `role=operator` and `response_schema=operator_intent_v1`.
Body documents all four `OperatorIntent` variants with concrete JSON
examples for every command + query in the v1 catalog. Hard
constraint: never invent commands not in the catalog; emit
`unparseable` instead.

**`PromptRole` literal** gained `"operator"`. One-line change in
`config/prompts.py`; test parametrize updated to match.

**Adapter behavior:**
- Constructor refuses prompts whose role != "operator" — fails
  loudly at wiring time rather than silently producing nonsense.
- `parse_intent` builds the role-tagged message list: system prompt
  body + engine state snapshot JSON in the system message; each
  recent `ConversationTurn` becomes a user/assistant message in
  chronological order; current operator message is the last user
  turn.
- Module-level `TypeAdapter[OperatorIntent]` validates the LLM's
  JSON output against the discriminated union (both nesting levels
  — outer `Command`/`Query`/`Conversational`/`Unparseable` and
  inner concrete command/query kind — resolve in one pass).
- Thinking-mode (R1, o1, etc.) + split-response-envelope handling
  matches the advisor pattern.
- Every layer's failure wraps as `AssistantError`. Per ADR-013 the
  conversational LLM is NOT in the money path; an `AssistantError`
  affects only the Discord chat surface — `cli/live` never imports
  this module.

19 new unit tests for the assistant adapter cover constructor
prompt-role validation; happy paths for each `OperatorIntent`
variant (command + query + query-with-args + conversational +
unparseable); multi-turn `ConversationContext` propagation as
role-tagged messages; engine state snapshot embedding in the system
message; thinking-mode drops `format=json` and walks free-text;
split-response envelope (empty `message.content`, JSON in
`thinking`); error paths (HTTP 5xx, malformed envelope, empty
content, invalid JSON, top-level non-object, schema validation
failure, thinking-mode no-JSON); `aclose` lifecycle for owned vs
borrowed clients. 2 existing advisor tests updated to expect the
port-agnostic `OllamaJsonExtractError`. 1 parametrize case added
for the `"operator"` role in `test_prompts.py`. `TestShippedPrompts`
extended to assert `operator.md` loads with
`response_schema=operator_intent_v1`.

Full suite **1088** passes (was 1067 at Stage 5.2 close, +21).
mypy clean across 66 src files. pylint **10.00/10** with no
outstanding warnings. black + isort clean.

Running real-money cost unchanged at $0.08 (Stage 5.3 is an LLM
adapter; tests use `httpx.MockTransport` so no real Ollama call
happened, and the assistant is structurally outside the money path).

### Stage 5.2 — Discord Transport Adapter (2026-05-16)

Second Phase 5 slice. The adapter wraps `discord.py`'s Gateway client.
Inbound Gateway events (messages, reactions) are normalized into typed
`InboundMessage` / `ReactionEvent` value objects, allowlist-filtered
(user + channel both required, empty allowlists deny-by-default, bot's
own user id always rejected), and dispatched to registered handler
callbacks. Outbound surface: `send_message`, `send_embed` (color-coded
by level), `send_confirmation` (amber-bordered embed + ✅ / ❌ reaction
buttons wired for the Stage 5.4 confirm-before-execute gate).

The adapter is concrete (not behind a port). Only `cli/operator`
(Stage 5.6) will consume it; an abstraction would be speculative. Per
ADR-013 decision 9, `cli/live` remains Discord-ignorant — it never
imports this module.

**New runtime dep:** `discord.py>=2.3,<3` (2.7.1 currently). MIT,
actively maintained, the de-facto Python Discord client. Pinned to
major 2 to avoid breaking-change drift. The `message_content` Intent
is enabled (privileged; must also be enabled in the Discord developer
portal for the bot account).

36 new unit tests cover config + value object construction /
frozenness / validation; `is_allowed` allowlist semantics including
bot self-rejection and empty-allowlist deny; handler dispatch +
filtering + per-handler exception swallowing; outbound `send_*`
against a `MagicMock` / `AsyncMock` injected `discord.Client`;
`_resolve_text_channel` fallback path (`get_channel` returns `None`
→ `fetch_channel`); send to non-text channel raises; `start` without
token env var raises; `close` idempotency. 90% module coverage
(uncovered: the Gateway-bound `on_message` / `on_raw_reaction_add`
event shims marked `# pragma: no cover`, and the
`discord.DiscordException` re-raise wrappers that require contrived
mocks). Full suite **1067** passes (was 1031 at Stage 5.1 close,
+36). mypy clean across 65 src files. pylint **10.00/10**.

Running real-money cost unchanged at $0.08 (pure-transport stage; no
real-money operations, no Gateway connection in tests).

### Stage 5.1 — Operator Domain & Ports (2026-05-16)

First Phase 5 slice. Pure-domain — no I/O, no Discord, no LLM call,
no SQLite table. Establishes the type contracts every later stage
consumes. Four sub-slices:

**5.1.A — Operator types + port.** New `ports/operator.py` defines
the full operator-interaction type contract: `OperatorCommand` typed
sum (`PauseCommand` / `ResumeCommand` / `PauseAllCommand` /
`ResumeAllCommand` / `CancelOpenOrdersCommand` / `StopCommand`),
`OperatorQuery` typed sum (nine variants from `StatusQuery` through
`HelpQuery`), `OperatorIntent` outermost union (`IntentCommand` |
`IntentQuery` | `IntentConversational` | `IntentUnparseable`),
per-query `*Result` types with `QueryResult` discriminated union,
`CommandResult`, `PendingCommand` with the six-state lifecycle
(`awaiting_confirmation` → `approved` → `dispatched`, with
`rejected` / `expired` / `failed` terminals), `OperatorPort` ABC
with `dispatch_command` + `answer_query`. New `OperatorError` in
`ports/exceptions.py`. `SymbolInput` / `OptionalSymbolInput`
BeforeValidator helpers accept `"BTC/USD"` strings as well as
`{base, quote}` dicts so the LLM can emit either form. 117 new unit
tests, 100% module coverage on `ports/operator.py`.

**5.1.B — Assistant types + port.** New `ports/assistant.py` defines
the LLM-side contract: `SymbolStateSnapshot` + `EngineStateSnapshot`
(read-only view `cli/operator` composes per inbound message to ground
the assistant's replies), `ConversationTurn` (id / channel_id /
user_id / role / content / `intent: OperatorIntent | None` /
timestamp), `ConversationContext`
(`current_message` + `channel_id` / `user_id` +
`recent_turns: tuple[ConversationTurn, ...]` for the multi-turn
prompt window + `engine_state_snapshot`), `AssistantPort` ABC with
`parse_intent(context) -> OperatorIntent`. New `AssistantError` in
`ports/exceptions.py`. 25 new unit tests, 100% module coverage on
`ports/assistant.py`. Per ADR-013 the conversational LLM is NOT in
the money path — an `AssistantError` affects only the Discord chat
surface; `cli/live` cannot observe it.

**5.1.C — `sqlite_storage.py` split.** Pre-existing pylint flag
(file at 1073 lines, threshold 1000) surfaced during 5.1.A's lint
check. Split out two sibling modules without changing the public
`SQLiteStorageAdapter` interface or its tests:
`adapters/sqlite_storage_schema.py` holds the `SCHEMA` constant
(every `CREATE TABLE` / `CREATE INDEX` the adapter runs at first
connect); `adapters/sqlite_storage_rowmap.py` holds pure row-to-domain
mapping helpers (`row_to_order` / `row_to_trade` / `row_to_price_snapshot`
/ `row_to_news_item` / `row_to_advisor_suggestion` /
`row_to_applied_suggestion` / `row_to_transfer_proposal` /
`row_to_transfer_result` plus the MoE expert-opinion JSON
serialize / deserialize pair). Dropped leading underscores on the
moved names since they cross module boundaries now; updated every
callsite in `sqlite_storage.py` to match. Migration helper
`_migrate_advisor_suggestions_expert_opinions` stays inline (tightly
coupled to `connect()`'s schema bootstrap). Main module:
**1073 → 753 lines**. No behavior change; 1031 tests still pass.

**5.1.D — Stage close.** Roadmap ✅, CHANGELOG entry, CLAUDE.md
Project Status bump, `project_state` memory update.

**Health at Stage 5.1 close:** **1031 unit tests** pass (was 892 at
Phase 4 close, +139 across 5.1.A and 5.1.B); 21 integration tests
opt-in; mypy clean across **64 src files** (was 60; +2 new
`ports/` modules, +2 new `adapters/sqlite_storage_*` modules);
pylint **10.00/10** with **no outstanding warnings** (the
pre-existing `too-many-lines` flag on `sqlite_storage.py` is gone);
black + isort clean.

Running real-money cost unchanged at $0.08 (pure-domain stage; no
real-money operations).

### Stage 4.5 — Phase 4 Integration Check + Phase 4 Close (2026-05-15)

Stage 4.5 audited the full Phase 4 path with the question "could anything move money the operator didn't intend?" and found one real defect. Then wrote `docs/planning/phase-4-summary.md` mirroring `phase-3-summary.md`'s shape.

**Defect found and fixed**: `cli/harvest --execute` would have called `KrakenAdapter.withdraw()` on a `bank_to_exchange` proposal. Kraken's `/0/private/Withdraw` is exchange→bank only — deposits are operator-pushed from the bank side using deposit instructions from Kraken Pro. Calling withdraw with a deposit-direction proposal would have moved money in the wrong direction (or, more likely, Kraken would have refused with a confusing error).

Fix: new defense layer 3 in `_execute_command` refuses any proposal whose direction isn't `exchange_to_bank`, with an operator-facing message pointing them to Kraken Pro's deposit instructions. The gate now has **seven** defense layers (was six). Test added: `tests/cli/test_harvest.py::TestExecuteGuardrails::test_bank_to_exchange_refused_no_api_call` asserts `adapter.withdraw_calls == []` after refusal.

Other Phase 4 paths verified end-to-end during the audit (all read-only against the operator's real account):
- `cli/harvest` read $99.92 USD via the Harvester key + classified as deficit + `persistence_enabled: true` confirmed
- `tools/show_proposals.py` reports "no proposals match" against empty table
- `tools/show_transfers.py` reports "no results match" against empty table
- All 8 (now 9 with the new test) execute-gate guardrails verified by unit tests with `adapter.withdraw_calls == []` assertions

**Phase 4 total real-money cost: $0.00** (no live withdrawal during slice work). The operator's first $1 ACH to "360 Performance Savings" is a separately-tracked event. Project running total still $0.08 unchanged from Phase 2 close.

Phase 4 stages closed: 4.1, 4.2, 4.3, 4.4, 4.5. Phase 5 entry conditions met.

### Stage 4.4 — Active Mode (Guarded Withdrawals) (2026-05-15)

Phase 4's biggest slice. **Money can finally move** — but only when the operator explicitly says so, and only after six defense layers clear. Four sub-slices:

**4.4a — `KrakenAdapter.withdraw()` + Harvester key wiring.**
- Implemented `/0/private/Withdraw` against Kraken's signed API. Returns Kraken's `refid` (withdrawal reference) for forensic linking to Kraken Pro's Funding history.
- `HarvesterConfig` gained `api_key_env_var` / `api_secret_env_var` (configurable for testing; default `KRAKEN_HARVESTER_API_KEY` / `_SECRET`) and `withdrawal_destinations: dict[str, str]` (asset → Kraken Pro destination label; the API only accepts labels from the operator's pre-registered address book).
- `cli/harvest` switched to loading the Harvester key (Withdraw + Query Funds scopes).

**4.4b — TransferResult storage + day-cap from real history.**
- New `transfer_results` SQLite table (UNIQUE on `transaction_id`, CHECK on status + direction).
- `TransferResult` gained denormalized `direction` and `asset` fields so the day-cap query stays single-table.
- `services.harvester.compute_today_total_withdrawn_usd()` — rolling 24h sum of exchange→bank withdrawals (status != failed).
- `cli/harvest._run_cycle` now feeds the real total to `propose_transfer()`. Pre-4.4b was always `Decimal("0")` — the day-cap was effectively never enforced.

**4.4c — `cli/harvest --execute <proposal-id>` operator-approval gate.**
- Mirrors the `cli/apply --commit` pattern: explicit per-call flag, multi-layer validation, persists outcome regardless of success or failure.
- Defense chain (any failure aborts; `adapter.withdraw()` NEVER called):
  1. `HarvesterConfig.enabled=True` required.
  2. Proposal exists in harvest db.
  3. Proposal not stale (≤ `proposal_max_age_hours`, default 24h).
  4. Destination label resolves in `withdrawal_destinations`.
  5. Current balance ≥ proposal amount (exchange→bank only).
  6. Day-cap headroom: `today_total + proposal.amount ≤ max_withdrawal_per_day_usd`.
- After all six clear, calls withdraw. `TransferResult` with `status="pending"` on success (Kraken hasn't settled yet) + Kraken's real refid; `status="failed"` on Kraken refusal with a synthetic `failed-<uuid>` transaction_id.
- The "**WITHDRAWAL SUBMITTED — money moved**" log message is the only place in the codebase that admits real money has moved.

**4.4d — Inspection + close.**
- `tools/show_transfers.py` mirrors `tools/show_proposals.py` shape (`--since-hours` / `--status` / `--direction` / `--asset` / `--limit` / `--log-format`).

**No real withdrawal happened during the slice work** — every test uses a stub `withdraw()`. The first live execution is operator-triggered: $1 ACH against the "360 Performance Savings" destination once balance enters surplus band (currently $99.92 USD, in deficit; would need a deposit or threshold adjustment).

888 unit tests pass (was 853 at Stage 4.3 close, +35 across the four slices). mypy clean (60 src files); pylint 10.00/10. No new runtime deps. Running real-money cost still $0.08 (unchanged from Phase 2 close until the operator's first `--execute`).

### Stage 4.3 — Passive Transfer Proposals (persistence + inspection) (2026-05-15)

Phase 4's third slice. Every non-None proposal from `cli/harvest` now persists to a new `transfer_proposals` SQLite table for operator review. **No transfers** — that's 4.4's job once the operator can approve+execute through an explicit gate. Zero new real-money risk.

- **Domain**: `TransferProposal` gained `created_at: Timestamp`. `services.harvester.propose_transfer()` populates it.
- **Storage**: new `transfer_proposals` table with `UNIQUE(proposal_id)` guard, `CHECK` on direction, indexes on `(created_at)` and `(direction, created_at)`. `StoragePort.save_transfer_proposal` / `get_transfer_proposals` (filter by `since / direction / asset / limit`; DESC by `created_at`).
- **`HarvestConfig.db`**: new field (default `data/wobblebot-harvest.db`) following the per-CLI DB convention (advise.db, news.db, etc.).
- **`cli/harvest`**: persists every non-None proposal on every tick. Storage write failures log + continue (the daemon's main job is observation; one missed audit row is less bad than killing the loop). Session-start log gained `persistence_enabled: true|false`.
- **Persistence ≠ execution**: `HarvesterConfig.enabled` does NOT gate persistence — that flag will gate Stage 4.4 execution. Operators can calibrate thresholds against a real proposal stream before flipping enabled.
- **`tools/show_proposals.py`**: new inspector mirroring `tools/show_suggestions.py` shape (`--since-hours / --direction / --asset / --limit / --log-format`).

**Verified live** against the operator's real Kraken account: daemon read $99.92 USD → deficit band → no proposal → `transfer_proposals` empty → `tools/show_proposals.py` correctly reports "no transfer proposals match the filters". `persistence_enabled: true` confirmed in session-start log.

15 new tests (10 storage round-trip + filters + UNIQUE + CHECK + Decimal precision; 5 cli/harvest persistence happy-path + no-proposal-no-persist + enabled-independence + storage-failure-isolation). 853 total unit tests pass (+15 since Stage 4.2 close); mypy clean (60 src files); pylint 10.00/10. No new runtime deps.

### Stage 4.2 — cli/harvest Read-Only Balance Monitor (2026-05-15)

Phase 4's second slice. Polls Kraken USD balance, runs the Stage 4.1 `propose_transfer()` decision, logs what *would* be proposed. **No transfers, no DB writes** — zero new real-money risk over 4.1. Uses the existing read-only `KRAKEN_READER_API_KEY`; the Harvester key with Withdraw scope isn't needed until Stage 4.4.

- **`HarvestConfig`** (per-CLI section): `log_format` only for now. Future stages may grow more knobs.
- **`schedules.harvest`**: new entry in the unified schedules block; defaults to `1h` in the example yml.
- **`cli/harvest._run_cycle`**: read balance → propose_transfer() → log. Returns `False` on a recoverable balance-read failure so the outer loop continues. Operator-facing band classification (`deficit / topup_band / hold_band / surplus`) included as a structured log field.
- Proposal log lines are tagged `"HYPOTHETICAL proposal (no money moved)"` so a glance at logs can't mistake them for real actions.
- Test stub's `ExchangePort.withdraw()` raises `NotImplementedError` with a `"Stage 4.2 must not call withdraw"` message — surfaces accidental cross-wiring as a hard test failure.

**Verified live** against the operator's real Kraken account: daemon read $99.92 USD (current state), correctly classified as `deficit` (below the $200 `min_exchange_liquidity_usd` threshold), logged "no proposal" with full band context. Below-floor is operator-only territory by design.

14 new tests. 838 total unit tests pass (+14 since Stage 4.1 close); mypy clean (60 src files); pylint 10.00/10. No new runtime deps.

### Stage 4.1 — Harvester Domain + Decision Logic (2026-05-15)

First Phase 4 slice. Pure-domain — no I/O, no Kraken calls, no withdrawals; **zero new real-money risk**.

- **`HarvesterConfig`** (`config/harvester.py`): four operator-tunable USD thresholds (`min_exchange_liquidity_usd / topup_threshold_usd / surplus_threshold_usd / max_withdrawal_per_day_usd`). Model validator enforces the `min < topup < surplus` ordering invariant at config-load. `enabled: bool = False` mirrors the auto-apply gate posture (ADR-012-style): operator opts in for anything that moves money.
- **`services/harvester.propose_transfer()`**: pure function taking `(balance_usd, config, today_total_withdrawn_usd)` and returning `TransferProposal | None` per four bands carved out by the thresholds:
  - **Deficit** (`< min`): no proposal — operator-only territory.
  - **Top-up band** (`min ≤ balance < topup`): propose `bank_to_exchange` to the midpoint of `(topup, surplus)`.
  - **Hold band** (`topup ≤ balance ≤ surplus`): no proposal.
  - **Surplus** (`> surplus`): propose `exchange_to_bank` scrape to the same midpoint.
- **Day-cap interaction**: proposals shrink to the remaining cap when `today_total_withdrawn_usd + desired_amount > max_withdrawal_per_day_usd`; cap exhausted returns `None`. Day-cap doesn't apply to deposits (inflows).
- Existing `HarvesterPort` interface (Phase 1.2) stays unchanged; 4.2+ adapter implementations will consume `propose_transfer()`.
- `settings.example.yml` harvester block reordered to match the new invariant and gained an operator-facing comment explaining the three bands.

24 new tests covering every band, every day-cap branch, config invariants, and proposal shape sanity. 824 total unit tests pass (+24 since Stage 3.6 close); mypy clean (59 src files); pylint 10.00/10. No new runtime deps.

### Stage 3.6 — Operational polish: indefinite runtime + multi-symbol advise (2026-05-15)

Two small slices to remove pre-Phase-4 operational friction.

**Slice 3.6a — indefinite runtime.**
- `LiveConfig.max_runtime_minutes` and `ShadowConfig.max_runtime_minutes` became `Optional[float]`. `None` means "no runtime cap." Pre-3.6a the field was `Field(default=60.0, gt=0)` and operators had to bump it to a sentinel like 525600 for "effectively forever" — `0` was rejected by Pydantic, and even if allowed the loop check `elapsed >= max_runtime_seconds` would have exited on tick 1.
- Loop logic in `cli/live._run_engine_loop` and `cli/shadow._run_loop` resolves `max_runtime_seconds` to `None` when configured and skips the per-tick comparison. SIGINT/SIGTERM, max_session_loss_usd, and the engine's safety caps still apply — this isn't a way to bypass safety.
- `settings.example.yml` comments flag `~null~` as the run-indefinitely value.

**Slice 3.6b — multi-symbol `cli/advise` with per-symbol-isolated LLM calls.**
- `AdviseConfig.symbol: Symbol` → `AdviseConfig.symbols: list[Symbol]`. CLI flag `--symbol` → `--symbols` (comma-separated, matching `cli/live`/`cli/shadow`/`cli/observe`).
- The daemon iterates serial per symbol within each tick: `for symbol in symbols: await _run_cycle(symbol=symbol)`. Each cycle builds a single-symbol `PerformanceSummary` so the LLM never sees more than one coin's context per call. Cross-contamination of opinions prevented by construction.
- Per-symbol cycle errors swallowed at the daemon layer (one bad coin can't kill the sweep) — matches `cli/live`'s Stage 2.4 discipline.
- `cli/apply` updated to filter `advisor_suggestions` by symbol — the multi-symbol advise daemon writes one row per coin per sweep, so a global "newest" pick could land on the wrong coin's row.
- **Verified live** against the operator's real advise.db: one sweep with `--symbols BTC/USD,ETH/USD` produced distinct recommendations per coin — BTC got `spacing 1.1 / order $12` (high confidence), ETH got `spacing 0.7 / order $15` (medium confidence). Different parameters AND different confidence levels prove per-symbol reasoning isolation end-to-end.

800 unit tests pass (was 792 at Phase 3 close, +8 across 3.6a's runtime tests and 3.6b's sweep tests). mypy clean (57 src files); pylint 10.00/10. No new runtime deps.

### Stage 3.5 — Phase 3 Integration Check + Phase 3 Close (2026-05-15)

End-to-end advisor-in-the-loop chain verified against live operator state, then Phase 3 closed.

**Chain verification:**
- **observe → metrics**: 6520 price snapshots accumulated by overnight `cli/observe` soak across BTC/USD + ETH/USD + DOGE/USD.
- **news → summary**: one `cli/news` poll cycle pulled 131 items (CoinDesk 25 + Decrypt 37 + The Block 19 + CryptoCompare 50; matches Stage 3.2.5 closing receipt to the row).
- **advise → suggestion**: one `cli/advise` cycle (39s wall-clock, phi4:14b-q8_0) produced `{spacing 1.1, levels±4}` with 20 news items in the summary's `recent_news`. Notable: same parameter recommendation as the previous cycle but `confidence` dropped from `high` (no news) to `medium` (news context present) — calibration shift even when proposed params hold.
- **apply → operator review**: `cli/apply` (dry-run) correctly rejected every key with reason "auto-apply disabled" — gate default-off posture holds end-to-end.

**Phase 3 close:**
- Closing summary at `docs/planning/phase-3-summary.md` (mirrors Phase 2's at `phase-2-summary.md`). Captures per-stage outcomes, MoE live verification numbers, design decisions ratified across the phase, health snapshot, what was deliberately not done, Phase 4 entry conditions.
- **Phase 3 real-money cost: $0.00** (advisor never executes per ADR-002). Running project total still **$0.08** unchanged from Phase 2 close.
- Phase 3 stages closed: 3.0, 3.1, 3.2, 3.2.5, 3.3, 3.4a, 3.4b, 3.5 (plus the config consolidation audit). Phase 4 entry conditions met.

### Stage 3.4b — Bounded Auto-Tuning Gate (2026-05-15)

Three-slice landing of the operator-in-the-loop apply surface. **Off by default** — `AutoApplyConfig.enabled=False` blanket-rejects every key, matching the conservative posture ADR-007 calls for. When the operator opts in, advisor suggestions can mutate the running grid within configured magnitude bounds. News-role suggestions never apply regardless of bounds.

- **Slice A — Auto-apply gate (pure service).** `services/auto_apply.py::evaluate_auto_apply(suggestion, current_grid, auto_apply_config, *, symbol) -> AutoApplyResult` decides what's eligible. Rules: `enabled=False` blanket-rejects; `role=="news"` blanket-rejects with the ADR-007 reason; whitelist for v1 is `spacing_percentage` + `order_size_usd` (level keys rejected with "no magnitude cap configured" until an operator extends `AutoApplyConfig`); `|delta|/current ≤ max_<key>_change_percentage / 100` with inclusive boundary. AutoApplyResult is a frozen Pydantic model carrying `enabled / role_eligible / symbol / applied_keys / rejected_keys / proposed_grid`. MoE-aggregated suggestions that contain a news opinion in `expert_opinions` still apply for whitelisted keys — the aggregated role IS the metrics-driven synthesis. 29 unit tests.
- **Slice B — `cli/apply` dry-run.** New module reads the latest (or `--recommendation-id`) AdvisorSuggestion from advise.db, runs it through the gate, and logs per-key APPLIED / REJECTED breakdowns with reasons. `--symbol` overrides advise.symbol so an operator with a BTC daemon can also evaluate the same suggestion against ETH's grid. Exit 2 on missing config sections / empty db / recommendation-id not found. 12 unit tests including the news-role safety endpoint.
- **Slice C — `--commit` + AppliedSuggestion audit + stage close.** Adds the `ruamel.yaml` runtime dep, `services/settings_rewriter.apply_grid_overrides()` (atomic .tmp + rename, comment-preserving round-trip, style-preserving integer/float, returns unified diff), `AppliedSuggestion` frozen domain model + `applied_suggestions` SQLite table + StoragePort methods. `cli/apply --commit` rewrites settings.yml AND persists an audit row in one logical operation; if the rewrite fails, no audit row writes. Stdouts the unified diff for operator review. 21 tests across rewriter + storage + cli wiring.

**Verified live**: `python -m wobblebot.cli.apply` against the operator's real `data/wobblebot-advise.db` correctly surfaced the latest BTC suggestion (phi4's `spacing 1.1 / levels±4`) and rejected all keys with reason "auto-apply disabled" — proving the gate's default-off posture holds end-to-end through the CLI.

792 unit tests pass (was 730 at Stage 3.4a close, +62 across the three 3.4b slices). mypy clean (57 src files); pylint 10.00/10. New runtime dep: `ruamel.yaml`.

### Stage 3.4a — Mixture of Experts (MoE) (2026-05-15)

Four-slice landing of the MoE advisor surface per ADR-007. Composes 2+ specialist `AdvisorPort` instances and aggregates their opinions via three strategies. Still advisory-only — Stage 3.4b's auto-apply gate is what eventually consumes these.

- **Slice A — Aggregator pure functions.** `services/aggregators.py` ships `aggregate_voting` (per-key strict majority; ties or no-consensus omit the key) and `aggregate_weighted_confidence` (per-key confidence-weighted average for numerics, weighted mode for categoricals). Confidence weights `high=3 / medium=2 / low=1`. Aggregated `role="aggregated"`. News-role opinions DO contribute to the math (the auto-apply exclusion lives in 3.4b's gate).
- **Slice B — `MoEAdvisorAdapter`.** Fans out to every expert via `asyncio.gather`; one vendor outage gets logged with structured fields and the MoE proceeds with the survivors. All-failed raises `AdvisorError`. Per-expert opinions ride on the aggregated recommendation via a new `AdvisorRecommendation.expert_opinions: list[AdvisorRecommendation]` field (recursive, enabled by `from __future__ import annotations`). The entry's `role` overrides whatever the LLM self-tagged. New `MoEExpertEntry` frozen dataclass wraps `(name, role, advisor)` — `AdvisorPort` stays the only abstraction; OllamaAdapter / future cloud adapters plug in directly.
- **Slice C — Arbitrator aggregator.** `aggregate_arbitrator` async function builds a JSON dump of the experts' opinions and feeds it to a separate arbitrator advisor as `extra_context`. OllamaAdapter gained an `extra_context: str = ""` kwarg (kept off `AdvisorPort` itself — a new `ArbitratorAdvisor` Protocol in `services/aggregators.py` formalizes the structural type). MoEAdvisorAdapter accepts an optional `arbitrator: MoEExpertEntry` required iff `aggregator="arbitrator"`, forbidden otherwise. The arbitrator's name shares the expert namespace (uniqueness enforced). If every expert fails, MoE raises before invoking the arbitrator.
- **Slice D — cli/advise MoE dispatch + audit persistence.** `cli/advise` now dispatches on `advisor.type=single` vs `advisor.type=moe`, building one OllamaAdapter per `ExpertConfig` and the arbitrator entry when configured. `advisor_suggestions.expert_opinions` column added (JSON array of `{role, confidence, recommendations, rationale}`); Stage 3.3 DBs upgrade in-place via a PRAGMA-check + `ALTER TABLE` in `connect()`. `model_name` persisted on the suggestion is a compact `moe[<aggregator>:<role>:<model>/...]` label. `tools/show_suggestions.py` gained an `experts=N[roles]` segment on the one-line summary. Cloud providers (anthropic / openai / google) raise at construction time with "not implemented" — they land later.

**Verified live end-to-end** against the operator's local Ollama lineup (phi4:14b-q8_0 quant, granite4.1:30b-q5_K_M risk, deepseek-r1:14b-qwen-distill-q8_0 news, phi4:14b-q8_0 arbitrator) via the new `tools/run_moe_check.py`:

- `--aggregator weighted_confidence`: 3 experts in 194s parallel dispatch. Quant: `spacing 1.1%, levels±4` (medium); risk: `spacing 1.2%, order_size $8` (high); news: `spacing 1.5%` (high, citing macro headlines). Aggregated: `spacing 1.29%, order_size $8, levels±4` (high confidence; weighted avg = 2.67).
- `--aggregator arbitrator`: 191s total. Same three experts; phi4 arbitrator synthesized `spacing 1.4%, order_size $9` (high) with the rationale: "Risk flagged drawdown approaching cap; quant agreed on tighter spacing. News context noted but not auto-applied per ADR-007." — the arbitrator even reasoned about news's auto-apply restriction.

730 unit tests pass (was 675 at Stage 3.3 close, +55 across the four 3.4a slices: 26 aggregator + 16 MoE adapter + 4 arbitrator-path + 3 storage round-trip/migration + 1 expert-opinions cycle + 5 cli/advise dispatch). mypy clean (54 src files); pylint 10.00/10.

### Stage 3.3 — Passive Advisory Workflow (2026-05-15)

Engine-decoupled advisor loop: `cli/advise` runs as a standalone daemon, periodically asks the configured LLM for a recommendation, and persists the result. **Nothing auto-applies** (ADR-002 + ADR-007). Operator reads with `tools/show_suggestions.py`.

- **Slice A — `AdvisorSuggestion` + storage.** New frozen domain model wraps an `AdvisorRecommendation` with audit context (`input_summary` as a forensic dict, `model_name` for provenance, `created_at`). New `advisor_suggestions` SQLite table; `StoragePort.save_advisor_suggestion` + `get_advisor_suggestions(since, model_name, role, limit)` DESC by created_at.
- **Slice B — `SummaryBuilder`.** Composes Stage 3.1 metrics + Stage 3.2.5 news + supplied grid config into a `PerformanceSummary`. New `NewsItemSummary` (narrowed `NewsItem` view — drops body / external_id / fetched_at) cuts the prompt-token cost of including news context by ~80%. Optional separate `news_storage` parameter lets the builder stitch prices from one DB and news from another.
- **Slice C.0 — Unified `schedules:` config.** Every periodic-task cadence moved to one top-level block in settings.yml. Duration strings (`30s` / `10m` / `4h` / `7d`); bare numbers parse as seconds; `0s` reserved for "disabled". Hard cutover — removed `observe.price_interval_seconds`, `observe.balance_interval_seconds`, `news.poll_interval_minutes`, `advisor.cadence_hours`. cli/observe and cli/news refactored to read from `schedules.*`.
- **Slice C — `cli/advise` daemon.** Long-running, mirrors cli/observe / cli/news shape. Three-DB design (read observe.db + news.db, write its own advise.db) keeps the per-CLI storage separation the project established earlier. Per-cycle fault isolation: advisor errors and storage errors are logged with structured fields and the loop continues. New `AdviseConfig` schema; cadence from `schedules.advise`.
- **Slice D — `tools/show_suggestions.py`.** Read-only operator inspection of recent suggestions. Filters by `--since-hours`, `--model`, `--role`, `--limit`.

**Verified live end-to-end:** `cli/advise` ran a real cycle against the operator's observe + news DBs → phi4:14b-q8_0 emitted a quant recommendation in ~50s (`spacing_percentage: 1.1`, `levels_above: 4`, `levels_below: 4`, confidence high) → persisted to `data/wobblebot-advise.db` → `tools/show_suggestions.py` printed it cleanly.

675 unit tests pass (was 619 at Stage 3.2.5 close, +56 across the four 3.3 slices including +21 for the schedules parser). mypy clean (52 src files); pylint 10.00/10.

Also bundled: Ollama Desktop update mid-stage retagged the local models with explicit quant suffixes (e.g. `phi4:14b` → `phi4:14b-q8_0`). Operator settings.yml updated; example yml already uses an explicit tag for clarity.

### Stage 3.2.5 — News Ingestion (2026-05-15)

Five-slice landing of news polling per ADR-007. **No LLM consumption yet** — Stage 3.4a's news expert is what reads from this. Persists items to a new `news_items` SQLite table with `UNIQUE(source, external_id)` dedup so re-polling across ticks is a no-op.

**Source pivot from ADR-007:** the original plan named CryptoPanic + Whale-alert; both moved to paid-only since the ADR was written (~$2,600/yr + ~$300/yr respectively). v1 pivots to **RSS + CryptoCompare** — all free. `NewsPort` stays abstract so paid sources can plug in later if you ever decide to.

- **Slice A — Domain + storage.** `NewsItem` frozen domain model (source, external_id, published_at, headline, body, sentiment_score, mentioned_coins, fetched_at). `NewsPort` ABC. New `news_items` table with `UNIQUE(source, external_id)`. `save_news_item` (idempotent via INSERT OR IGNORE) + `get_news_items(source, since, until, limit)` returning DESC by published_at.
- **Slice B — `RssNewsAdapter`.** One instance per feed. feedparser-based; httpx fetches the bytes with `follow_redirects=True` (the redirect handling caught CoinDesk during live verification). Mentioned-coin extraction via a whitelist regex over ten popular tickers (BTC/ETH/SOL/DOGE/ADA/XRP/DOT/MATIC/AVAX/LINK).
- **Slice C — `CryptoCompareAdapter`.** Polls `/data/v2/news/`. API key in the `authorization` header (never query string, to avoid upstream-log exposure). `sentiment_score: None` — CryptoCompare's upvotes/downvotes aren't a reliable sentiment signal; the news expert in Stage 3.4a derives tone from the body text. Mentioned coins extracted from the structured `categories` field, filtered to ticker-shaped tokens.
- **Slice D — `cli/news`.** Long-running daemon, same operational shape as `cli/observe`. Per-source fault isolation: one bad feed gets logged with structured fields and the loop continues with the rest. New `NewsConfig` + `RssFeedSpec` + `CryptoCompareSpec` schemas in `config/cli.py`.
- **Slice E — Example yml.** Default `news:` block with four RSS feeds (CoinDesk, Decrypt, The Block enabled; CoinTelegraph disabled as noisy) + CryptoCompare enabled. `CRYPTOCOMPARE_API_KEY` documented in `.env.example` with minimum-scope notes.

**Verified live in one poll across all four sources:** 25 + 37 + 19 + 50 = 131 fresh items into `wobblebot-news.db`. Per-source error isolation tested empirically (CoinDesk redirect failure on first try; rest of the loop continued).

619 unit tests pass (was 525 at Stage 3.2 close, +94); mypy clean (49 src files); pylint 10.00/10. New runtime dep: `feedparser`.

**90-day evaluation queued** (2026-08-13): CryptoCompare's source coverage substantially overlaps with RSS. Re-evaluate whether the additional aggregation earns its place vs. simply running more RSS feeds.

### Stage 3.2 — Advisor Port & Single-LLM Integration (2026-05-15)

Five-slice landing of the first LLM advisor surface. Single-LLM mode only — MoE arrives in Stage 3.4a. No new live-money risk (advisor cannot execute per ADR-002 + ADR-007).

- **Slice A — Schema reconcile.** `AdvisorRecommendation` now matches the wire format the prompt files already declared (`advisor_recommendation_v1`): `config_changes` → `recommendations`, `confidence: float` → `Literal['high','medium','low']`, new `role: str` field. `PerformanceSummary` extended with Phase 3.1 metrics (volatility, max_drawdown, flatness, latest_price, snapshot_count, lookback_hours) plus `CurrentGridParams` so recommendations can be delta-aware.
- **Slice B — OllamaAdapter.** New `adapters/ollama.py` implementing `AdvisorPort`. httpx-based with `MockTransport` test seam; transport, HTTP-status, JSON-parse, and Pydantic-validation failures all wrap as `AdvisorError`. Named `OllamaAdapter` per the `{Vendor}Adapter` convention (matches `KrakenAdapter`).
- **Slice C — Config single-mode.** `AdvisorConfig` gains `provider` / `model` / `prompt_file` / `inference_params` fields required when `type: single`. Example yml flips to `type: single` (Ollama + `quant.md`) as the Stage 3.2 default; the former MoE block moves to a `profiles.moe-advisor` profile alongside the existing `cloud-only-moe`.
- **Slice D — `tools/run_advisor.py`.** Reads observe DB + resolved config → builds PerformanceSummary via `services.metrics` → calls the configured advisor → prints + persists a JSONL receipt. Same pattern as `tools/first_real_trade.py` and `tools/show_metrics.py`.
- **Slice E — Thinking-model support.** R1-family / o1-style / "thinking" / "reasoning" / "thinker" models emit `<think>…</think>` reasoning before the answer; Ollama's `format: "json"` constraint forces the first token to start valid JSON, so they degenerate to `{}`. The adapter now name-detects thinking models, drops the format constraint for them, and walks the response with `json.JSONDecoder.raw_decode` to extract the last balanced `{…}` block. Robust to thinking preambles, code fences, illustrative JSON-shaped strings in the reasoning, and braces inside string literals.

523 unit tests pass (was 458 at Stage 3.1 close, +65); mypy clean (45 src files); pylint 10.00/10. `ports/advisor.py` and `adapters/ollama.py` both at 100% line coverage on the unit-test path.

Verified live against six local Ollama models (phi4:14b, qwq:32b, gemma3:27b, nous-hermes2-mixtral, mistral-nemo:12b, deepseek-r1:14b) on the same BTC/USD 6h window. Five working models converged on `spacing_percentage: 1.2` — striking agreement across genuinely different priors. Confidence calibration was the meaningful differentiator: phi4 / qwq / gemma3 reported `medium` (the honest answer given zero cycle history); mistral-nemo and nous-hermes2 reported `high` overconfidently. **phi4:14b set as the local default** based on this comparison — calibrated, fast (~27s), and the most accurate read of the metrics (correctly characterizing 0.044% per-period stdev as low volatility, where mistral-nemo got the direction wrong).

llama3.3:70b timed out at the default 60s — tunable, not a quality issue. Adding a configurable timeout is queued for whenever a 70B model becomes operationally interesting.

### Stage 3.1 — Data Collector & Metrics v2 (2026-05-15)

Four-slice landing of historical price reads + derived-metric math
on top of the price_snapshots tape that `cli/observe` has been
filling. Lands the read side of Phase 3 without touching the
advisor surface, so no new live-money risk.

- **Slice A — Storage read path.** `StoragePort.get_price_snapshots(symbol, start_time, end_time, limit)` with SQLiteStorageAdapter impl. New `PriceSnapshot` domain model (frozen, stays narrow — distinct from `MarketSnapshot` which is expected to grow). Reads return ASC by `observed_at` so callers can pipe directly into a chronological series.
- **Slice B — Pure-math metrics module.** New `services/metrics.py` exposes `compute_volatility` (sample stdev of simple returns), `compute_max_drawdown` (worst peak-to-trough fraction, ≤ 0), `compute_flatness` (1 − range/mean, clamped to [0, 1]), and `compute_cycle_stats` (FIFO per-symbol buy-then-sell matching → cycle_count / win_count / win_rate / total_pnl / avg_profit_per_cycle). No I/O, no port deps; deterministic golden-input tests.
- **Slice C — DataCollector v2 wiring.** `DataCollector(exchange, storage)` now exposes `get_price_history(symbol, lookback: timedelta)` plus windowed metric methods on `DataCollectorPort` (`get_volatility`, `get_max_drawdown`, `get_flatness`, `get_cycle_stats`). `CycleStats` moved from `services.metrics` to `domain.models` so the port can name it as a return type without closing a ports → services → adapters import cycle. `cli/status` updated to construct a `SQLiteStorageAdapter(":memory:")` to satisfy the now-required storage parameter.
- **Slice D — Inspection tool.** `tools/show_metrics.py` reads any wobblebot DB read-only, auto-discovers symbols from `price_snapshots`, and prints metrics per symbol over a configurable lookback. Safe to run against the live observe DB while `cli/observe` is polling.

458 unit tests pass (was 401 at Phase 2 close); mypy clean (44 src files); pylint 10.00/10. `services/metrics.py` and `services/data_collector.py` both at 100% line coverage.

Verified end-to-end against the live observe DB: 1383 snapshots/symbol over the past ~10h, BTC/USD vol=0.0364%, dd=−2.90%, flat=0.97; DOGE/USD vol=0.0847%, dd=−4.17%; ETH/USD vol=0.0490%, dd=−2.88%. Observer kept polling undisturbed across all four slice commits.

Also: Stage 5.3.5 (Background Maintenance Worker) added to the roadmap — `cli/maintenance --loop` covering periodic SQLite VACUUM, optional retention pruning, `TimedRotatingFileHandler` log output, and local + configurable-remote backups. Implementation deferred to Phase 5; slotted between 5.3 (Reliability) and 5.4 (Performance) before the v1.0 soak test.

### Post-audit infrastructure (2026-05-15)

Follow-up landed in the same window as the config consolidation
audit close. None of these change runtime behavior in a way that
affects live trading; all are operator-experience and project-
hygiene improvements.

- **User-facing docs refresh.** README rewritten to reflect current
  phase status and the full 7-CLI surface (which CLIs touch real
  money, which don't, what each is for); fixed placeholder clone
  URL; updated test commands to match the actual marker setup.
  SECURITY.md replaced GitHub's stock placeholder template with a
  real threat model + private-disclosure flow via GitHub Security
  Advisories. New CONTRIBUTING.md (lightweight; delegates to
  existing docs) and CODE_OF_CONDUCT.md (Contributor Covenant 2.1
  by reference). CHANGELOG moved from
  `docs/implementation/changelog.md` to repo-root `CHANGELOG.md`
  per Keep-a-Changelog convention. LICENSE copyright updated to
  `CarlDog`, year span `2025-2026`. GitHub repo description and
  10 discoverability topics set via the API.
- **Discord on the roadmap (ADR-pending).** Stage 5.1.5 added
  for Discord notifier (`NotifierPort` adapter at
  `src/wobblebot/adapters/discord_notifier.py`, outbound only,
  one-evening scope). Stage 5.2 expanded to cover bidirectional
  Discord control surface (slash commands, new `OperatorPort`).
  Stage 5.1 documents the web UI option's structural placement
  (`src/wobblebot/web/` as sibling of `src/wobblebot/cli/`, both
  presentation layers consuming existing ports).
- **Phase-end audit practice codified.** New global rule at
  `~/.claude/rules/phase-end-audit.md` defines per-phase /
  per-major-feature / quarterly / pre-1.0 audit cadences with
  process discipline (punch list first, fixes in separate commits
  per category, no scope creep into rewrites). Wobblebot's
  `CLAUDE.md` adds a project-specific extension covering all-CLI
  deprived-env walkthrough, schema-drift cleanliness, OC memory
  currency, and Phase 4 Harvester key scope verification when that
  phase lands.
- **Dependabot cleanup.** Removed the speculative
  `github-actions` ecosystem block from `.github/dependabot.yml`
  (no `.github/workflows/` exists yet, so GitHub's Dependency
  Graph was warning "Not all dependency manifest files were
  successfully processed"). Re-add when CI lands. Pip ecosystem
  unaffected — still 16 packages tracked, security alerts on,
  weekly Monday Python update PRs scheduled.
- **GitHub Sponsors + Ko-fi.** New `.github/FUNDING.yml` cloned
  from `openchronicle-mcp`'s setup. Enables the "Sponsor" button
  on the repo page.

### Phase 3 — Strategy Advisor & Analytics (in progress)

- **Stage 3.0 — Observer & Shadow Mode** (2026-05-14, ADR-008). Two
  non-money-touching entry points landed before advisor work begins:
  - `cli/observe` — pure data collection. Polls live Kraken Ticker
    on a configurable interval, persists prices + balance snapshots
    to a `price_snapshots` SQLite table. Read-only API key.
  - `cli/shadow` — shadow trading. Same engine code as `cli/live`
    but with a new `ShadowExchangeAdapter` that uses live Kraken for
    prices and matches orders against a synthetic balance ledger.
    Honest maker/taker fee modeling (default 0.26% / 0.40% — the
    rates Phase 2's first-trade receipt confirmed). Operator-supplied
    initial synthetic balances (no inference from real Kraken — the
    muscle-memory guard from ADR-008).
  - `cli/grid` renamed to `cli/live` to make the live-money
    distinction loud against the new `cli/shadow`.

#### Config consolidation audit (2026-05-14, ADR-009; eight slices, no live-money risk)

Pure infrastructure cleanup before Stage 3.1 to align the
operator-facing config story.

- **Slice 1.** `config/settings.example.yml` redesigned as the
  operator-facing API; ADR-009 ratifies the layering.
- **Slice 2.** Per-CLI Pydantic schemas — `LiveConfig`,
  `ShadowConfig`, `ObserveConfig`, `PreflightConfig`, `StatusConfig`,
  `SandboxConfig` — plus `AdvisorConfig` (with a ≥3-experts
  validator for MoE).
- **Slice 3.** Profile resolver with `deep_merge` semantics: dicts
  recurse, lists override entirely.
- **Slice 4.**
  - 4a — renamed `cli/simulate` → `cli/sandbox`,
    `cli/check` → `cli/status`, `cli/validate` → `cli/preflight` for
    operator clarity.
  - 4b — `wobblebot.config.runtime.load_resolved_config(...)` wired
    into `cli/live` as the YAML-loading pattern (base YAML →
    `--profile` deep-merge → CLI flag overrides).
  - 4c — same pattern wired into the remaining five CLIs. Profiles
    cover both `live` AND `shadow` so the same name (e.g.
    `conservative`, `aggressive`) is meaningful for any operational
    mode.
- **Slice 5.** Prompt-file infrastructure — new runtime dep
  `python-frontmatter`, four committed default prompts at
  `config/prompts/{quant,risk,news,arbitrator}.md`, loader at
  `wobblebot.config.prompts.load_prompt`. Skeletons; Stage 3.4a
  will wire the advisor to consume them.
- **Slice 6.** Schema-drift detection tests for both file pairs
  (`settings.example.yml` ↔ `settings.yml`, `.env.example` ↔
  `.env`). One-way default (operator stale keys fail; missing keys
  warn); `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` promotes warnings to
  hard failures for CI.
- **Slice 7.** `docker/env.example` moved to repo-root `.env.example`
  and refreshed for Phase 2.3 reality (`KRAKEN_TRADER_API_KEY`,
  cloud-LLM keys, harvester key for Phase 4).
- **Slice 8.** Docs + memory close.

#### Verifications (2026-05-14, post-audit)

- **Verification #24 — Deprived-env walkthrough.** Cycled all six
  CLIs through scenarios with no `.env`, no config, partial config,
  bad credentials, bad `--config` paths, bad `--profile` names.
  Surfaced and fixed two real defects:
  - SQLite-using CLIs crashed with raw 18-line traceback when
    `data/` directory didn't exist. Fixed: `SQLiteStorageAdapter.connect`
    now mkdir's the parent directory on demand. `:memory:` and
    empty-string paths pass through unchanged.
  - `load_dotenv()` walked UP from the package source location
    (python-dotenv default with `usecwd=False`), magically picking
    up the dev repo's `.env` from any cwd. Fixed: new
    `wobblebot.cli._common.load_operator_env()` helper composes
    `find_dotenv(usecwd=True)` with `load_dotenv(dotenv_path=...)`
    so discovery walks UP from the operator's cwd. All five
    env-using CLIs use the helper.
- **Verification #25 — PII scanner coverage.** Confirmed
  `.githooks/pre-commit` runs gitleaks + author-identity guard
  + PII pattern scan (Mac/Windows + Linux user-home paths +
  personal-email patterns). gitleaks against full git history (80
  commits): clean. Tracked-files PII sweep: zero hits. Working-tree
  leaks confined to operator's gitignored `.env`. Added missing
  `*.pfx`, `*.p12`, `*.pem` patterns to `.gitignore` per
  security.md spec. Repo is publication-ready from a PII/secret
  standpoint.

### Phase 2 — Core Trading Engine (closed 2026-05-14)

Total real-money cost across two live verifications: **$0.08**.
Closing summary at [`docs/planning/phase-2-summary.md`](docs/planning/phase-2-summary.md).

- **Stage 2.1 — Kraken Adapter (read-only).** DIY HMAC-SHA512
  signing on `httpx` (rejected `python-kraken-sdk`). `BalanceEx` not
  `Balance` (returns `hold_trade` per asset). Asset/symbol aliasing
  in the adapter via module-level `_INTERNAL_TO_KRAKEN_ALTNAME`
  + lazy `/0/public/Assets` cache. `pytest -m 'not integration'` is
  the default; live integration tests opt-in. `.env` loaded
  session-wide via `python-dotenv` in `tests/conftest.py`.
- **Stage 2.2 — Micro-Grid Engine** (ADR-006). Five slices: config
  schemas (`GridConfig`, `SafetyConfig`, YAML loader); pure grid
  math (`compute_grid_levels`, `next_counter_action`, `is_offside`);
  `GridEngine` service with `GridState` persistence; safety cap
  enforcement (per-coin / total exposure + daily-spend); end-to-end
  integration test (1000-tick oscillation, 500 cycles, positive
  realized P&L). Six ratified design decisions in ADR-006. Counter
  orders match filled-order base amounts.
- **Stage 2.3 — Live Paper / Tiny-Size Mode.**
  `KrakenAdapter(dry_run=True)` adds `validate=true` to every
  AddOrder request (auth + pair + precision + balance + ordermin
  + costmin validation without placing). Per-pair quantization
  mandatory; price/volume rounded DOWN before submission. Two
  separate Kraken keys (read-only + trade) live side-by-side in
  `.env`. Live taker fee is 0.40%, not the mock's 0.26% — discovered
  during the first-trade test. `cli/preflight` and `cli/live`
  shipped. Verified live: $0.08 round-trip on the operator's
  account, 148ms fill latency, perfect cleanup.
- **Stage 2.4 — Multi-Asset Support.** `cli/live` takes
  `--symbols` comma-separated. Each tick steps every symbol in
  series. Per-symbol step errors swallowed at the CLI layer (one
  bad coin can't kill the session). Caps split: `total` and `daily`
  are global across symbols; `per-coin` and `max_orders_per_coin`
  scoped per symbol. Five new multi-coin engine tests; engine
  layer required ZERO changes (every per-coin entity already keys
  by symbol).
- **Stage 2.5 — Phase 2 Integration Check.** Live multi-coin grid
  run for 5 minutes against the operator's account; 54 ticks per
  coin, 0 fills (price stayed within 1% of init reference for both
  BTC and ETH the entire window), session PnL $0.0000, all 6 open
  orders cleanly cancelled on runtime-cap shutdown. The
  `InsufficientBalance`-as-refusal fix was load-bearing — pre-fix
  the engine would have crashed at tick 1 because the account holds
  zero base inventory.

### Phase 1 — Foundation & Sandbox (closed 2026-05-13)

- **Stage 1.1 — Repo & Scaffolding.** `pyproject.toml`, dev tooling
  (black/isort/mypy/pytest), VS Code workspace.
- **Stage 1.2 — Hex Core Skeleton.** Domain models (`Order`,
  `Trade`, `Balance`) and value objects (`Symbol`, `Price`, `Amount`,
  `OrderSide`, `Timestamp`); six abstract ports (`ExchangePort`,
  `StoragePort`, `AdvisorPort`, `HarvesterPort`, `NotifierPort`,
  `DataCollectorPort`); ADR-005 alignment with Kraken vocabulary.
- **Stage 1.3 — Storage & Logging Backbone.**
  `SQLiteStorageAdapter` via `aiosqlite` (Decimal-as-TEXT precision,
  transaction rollback on partial-write failure, dual-ID UPSERT on
  `orders`, append-only balance-snapshot history). `configure_logging`
  in `wobblebot.config.logging` — stdlib-only, idempotent,
  plain/JSON switchable via `WOBBLEBOT_LOG_LEVEL` /
  `WOBBLEBOT_LOG_FORMAT`. Pre-commit hook with gitleaks + PII
  pattern check + author-identity guard. Port exception hierarchy
  in `ports/exceptions.py`.
- **Stage 1.4 — Kraken Mock & Simulation Mode.**
  `MockExchangeAdapter` with limit-order matching, configurable fee
  model (default 0.26%), scenario playback, balance tracking with
  locked-funds reservation. 23 unit tests.
- **Stage 1.5 — Phase 1 Integration Check.**
  `wobblebot.services.simulator.run_buy_dip_sell_rebound_cycle`
  wires `ExchangePort` + `StoragePort` to execute a hard-coded
  buy-low / sell-high cycle against a scripted price walk.
  `python -m wobblebot.cli.sandbox` is the operator-facing entry
  point. **Phase 1 complete.**

### Notable cross-cutting changes

- Domain exception signatures take `Decimal` (was `float`),
  preventing precision loss in balance violation reports.
- `Order.mark_closed` replaced by `Order.record_fill(cumulative_amount)`
  — partial fills correctly keep `status='open'` until full fill;
  matches Kraken `vol_exec` semantics.
- `Timestamp` normalizes any tz-aware input to UTC.
- `Balance` is an immutable point-in-time snapshot (`frozen=True`).
- `OrderSide` is a `StrEnum` (was a Pydantic wrapper).
- `ExchangePort.get_balance(asset)` returns `Balance | None` —
  distinguishes never-held from held-but-zero.
- Pydantic mypy plugin enabled in `pyproject.toml` (load-bearing).

**[This stale pre-Phase-8 `[v1.0.0] — TBD` placeholder, written when v1.0.0
was expected to land at the end of the original Phase 5, has been retired
— see the real `[1.0.0] - 2026-07-31` section above, `docs/planning/phase-8-summary.md`,
and `docs/release/v1.0-known-limitations.md` for the actual, current release
content.]**
