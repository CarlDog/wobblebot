# ADR-044 — Settings layout and POLICY authority boundaries

**Status:** POLICY amendment proposed, not ratified. Counter-cap repair and
behavior-preserving settings layout implemented locally on 2026-09-08.
**Date:** 2026-09-08
**Decider:** Operator
**Scope:** Review repository settings organization and refine the accepted
[ADR-040](decisions.md#adr-040--three-tier-configuration-env--settings-hard-limits--policy-mutable-operating-points)
before its writable POLICY tier. This does not open G6 or accept the next phase.

## Context and current implementation

ADR-040 already accepts ENV / SETTINGS / POLICY and exactly seven initial POLICY
fields: six trading caps and `order_size_usd`. The distinction is **an operating
cap may change inside an operator-owned ceiling**. It does not give the database
authority over the ceiling itself. The read-only Capital Reporter shipped;
[backlog G6](../planning/backlog.md) still gates writable Stage 2 on a second
qualifying manual POLICY edit and a refreshed validation fixture. This review and
the advisor fallback edits are not evidence that the trigger occurred.

Pre-change review snapshot, local `main` at `15679b1` plus the uncommitted fallback work:

| Surface | Observed state |
|---|---|
| `config/settings.yml` | 1,129 lines; 638 comment lines; 21 top-level sections; five named profiles |
| `config/settings.example.yml` | 1,493 lines; 1,002 comment lines; the same 21 sections and five profiles |
| Organization | `llm` follows `maintenance` in the operator file but precedes `web` in the example; both embed ten repeated fallback candidate blocks |
| Resolution | Base YAML → one selected profile → explicit CLI flags → Pydantic validation; mappings merge, lists replace completely |
| Runtime | Loaded at startup. No implemented `policy_changes` table, policy resolver or writable POLICY command path was found in `src/` |
| Web `/settings` | Updates the user's timezone in `user_preferences`; it does not edit bot configuration |
| Existing writers | `cli.apply --commit` rewrites grid settings; `cli.recalibrate --commit` also rewrites selected safety, session-loss and treasury values |

The example is a template, not a copy of deployed values. Both files resolve
`moe-advisor` to local experts, `cloud-only-moe` to cloud experts, and `cpu-only`
to a **single cloud advisor with the cascade engine**. Provider selections differ
between template and operator file. `cpu-only` describes host hardware, not offline
inference. These are repository observations, not a fresh NAS deployment receipt.

Evidence: [runtime loader](../../src/wobblebot/config/runtime.py),
[profile resolver](../../src/wobblebot/config/resolver.py),
[web settings](../../src/wobblebot/web/routes/settings.py),
[rewriter](../../src/wobblebot/services/settings_rewriter.py),
[calibrator](../../src/wobblebot/services/calibrator.py),
[SQLite schema](../../src/wobblebot/adapters/sqlite_storage_schema.py).

## Findings that must shape the design

1. **Restoring SETTINGS on a database error can loosen an approved limit.** If the
   hard ceiling is 100, the initial operating cap 60 and approved POLICY 30, losing
   the database must not silently restore 60 or 100. ADR-040's fallback stays within
   the ceiling but does not preserve the latest restriction. Absence at first setup,
   intentional reset, missing migrated data and read failure are different states.
2. **Smaller is not uniformly safer.** `max_orders_per_coin` and the two order-book
   exposure caps gate SELLs as well as BUYs. Lowering them can block counter exits.
   Lowering order size can violate exchange minimums. Daily BUY spend and inventory
   caps have different semantics. ADR-040's blanket automatic-tightening claim needs
   a field-specific proof, not a numeric comparison alone.
3. **The review baseline charged the wrong counter-order notional (repaired locally).**
   `GridEngine._try_place` accepted an explicit amount, but `_check_safety` charged
   `coin_cfg.order_size_usd`; `_place_level` used the explicit amount. A mock exchange
   and in-memory SQLite reproduced a $10 BUY counter amount (0.0002 BTC at $50,000) being placed with
   configured size $2 and all USD caps $5. The follow-up now resolves the base
   quantity once and charges price × quantity in all five USD cap checks before
   passing that same amount to placement. See the implementation receipt below.
4. **There are existing ways to rewrite or override the supposed ceiling.**
   `cli/live` accepts four safety-cap overrides and session-loss overrides. The
   generic rewriter accepts existing dotted paths, and recalibration currently
   changes protected values. Documentation saying “no process can alter hard limits”
   does not enforce that boundary. This is a migration obligation, not a claim that
   those existing, operator-invoked tools were unauthorized.
5. **Profiles and historical records need explicit identities.** A risk value from
   shadow, another account or a different deployment cannot become live policy.
   A settings hash alone cannot reconstruct yesterday's limits: operator settings
   are gitignored. Keep a sanitized immutable receipt of effective bounds and a
   policy revision with the decision that used them.

Evidence for findings 2–3: [cap checks](../../src/wobblebot/services/grid_engine.py),
[amount override](../../src/wobblebot/services/grid_engine.py#L1783),
[counter sizing](../../src/wobblebot/services/grid_engine.py#L1311), and
[CLI overrides](../../src/wobblebot/cli/live.py#L2344). The offline reproduction
used no real funds, credentials or network and does not establish production impact.

## Proposed field ownership

### ENV: credentials and deployment identity

Keep Kraken reader/trader/harvester secrets, provider API keys, Discord bot token,
web session secret, external endpoint wiring and image selection in deployment
environment configuration. Keep their current per-service credential isolation.
Logging/diagnostic environment switches also stay out of POLICY.

Environment-variable **names** such as `harvester.api_key_env_var` remain protected
SETTINGS references; their secret **values** remain ENV. Do not add parallel ENV
copies of numeric trading ceilings. Reconcile the current Ollama endpoint split:
advisor uses `OLLAMA_BASE_URL`, while operator assistant reads
`operator.assistant.base_url`. Standardizing that ownership belongs in the deployment
wiring slice, with explicit compatibility tests, not a silent precedence change.

### SETTINGS: hard limits, structure and bootstrap defaults

All fields remain SETTINGS unless an explicit registry admits them to POLICY.
“Stored in YAML” does not mean “a hard safety limit”; label these subcategories:

| Category | Fields / families | Rule |
|---|---|---|
| Trading hard ceilings | The six `safety.max_*` cap members listed below; explicit new min/max bounds for order size | Operator-controlled files; database values cannot raise ceilings or lower floors |
| Other trading protections | `safety.sell_guard.*`, `safety.max_spread_percentage`, `live.max_session_loss_usd`, `live.max_runtime_minutes`, `live.dead_mans_switch_seconds`, `live.cool_down_minutes` | Remain protected SETTINGS, including flags or nulls that disable a guard |
| LLM spending and permission gates | `llm.cost.*`, including `enforce`; `advisor.auto_apply.*`; `advisor.gremlin.enabled` | Keep outside the initial POLICY set; an LLM/account interruption must not raise its own spending authority |
| Treasury authority and limits | `harvester.enabled`, liquidity floor, surplus/top-up triggers, withdrawal daily cap, destinations, credential references, proposal age | All stay SETTINGS; no POLICY shortcut into the Harvester's transfer authority |
| Grid structure | `grid.default` / `grid.coins.*` spacing, levels, counter-target mode and enabled membership; `live.symbols`, sweep priority | Static geometry/universe. Pausing a symbol remains a command, not an edit to this structure |
| Model structure | `advisor` engine/type, experts, aggregator, prompts, providers/models, ordered `fallbacks`, inference parameters; operator assistant and gremlin model configuration | Static and reviewed. Choosing an already configured fallback is execution of this configuration, not a POLICY edit |
| Scheduling and resources | `schedules.*`, ticks, polling/heartbeats, database and log paths, retention/archive/backup settings | SETTINGS; changing cadence, retention or storage ownership needs its own lifecycle review |
| Authentication and command controls | `operator.auth.*`, confirmation TTL, web bind/session/rate-limit/bcrypt settings | Protected SETTINGS; neither LLMs nor generic policy writers can change access or approval gates |
| Simulation / presentation defaults | `shadow` balances and fees, sandbox/preflight/status settings, web display defaults and cost assumptions | SETTINGS today; simulation values must never become live POLICY through shared storage |

Coverage of every current top-level section:

| Section(s) | Authority |
|---|---|
| `application` | SETTINGS identity/mode |
| `safety`, `grid` | SETTINGS protections/structure/defaults plus the seven prospective POLICY operating values below |
| `schedules` | SETTINGS cadence |
| `live`, `shadow`, `sandbox`, `preflight`, `status` | SETTINGS execution mode, resources and limits |
| `observe`, `news`, `screener` | SETTINGS collection/analysis configuration |
| `llm`, `advise`, `advisor` | SETTINGS budgets, daemon wiring and model assignments |
| `harvest`, `harvester` | SETTINGS treasury wiring and authority |
| `operator`, `web`, `maintenance` | SETTINGS service configuration; existing DB preferences/state remain separate |
| `profiles` | SETTINGS overlays selected at startup; not a database-editable mode switch |

### POLICY: exactly seven operating values at first

Keep ADR-040's membership. Scope every record to the deployment/account capital
book, execution mode and strategy identity; the table adds a symbol only where shown.
Global caps must remain global across symbols, not one independent allowance per coin.

| Current path / prospective policy key | Scope within that identity | Existing meaning to preserve |
|---|---|---|
| `safety.max_orders_per_coin` | Symbol | Open-order count; applies to both sides |
| `safety.max_per_coin_exposure_usd` | Symbol | Open-order notional for the symbol, both sides |
| `safety.max_total_exposure_usd` | Capital book | Open-order notional across symbols, both sides |
| `safety.max_daily_spend_usd` | Capital book / UTC day accounting | Committed BUY notional; completed sales do not refund this counter |
| `safety.max_per_coin_inventory_usd` | Symbol | Held inventory at cost plus open BUY notional; BUY-only gate |
| `safety.max_total_inventory_usd` | Capital book | Same cost-based measure across symbols; BUY-only gate |
| `grid.{default,coins.*}.order_size_usd` → `order_size_usd` | Symbol, with a static default | Budget for newly sized orders; counters retain actual filled base quantity |

Membership permits a bounded operator-approved change; it does not prove autonomous
changes safe. Do not add spacing, levels, symbol selection, model seats/fallbacks,
withdrawals or LLM budgets merely because editing them in a UI would be convenient.

### Existing database preferences and state

`user_preferences.timezone` and hidden-symbol display preferences belong in the DB
already. They affect presentation, not trade eligibility. Users/authentication data,
orders, balances, pending commands, pause/hold state, snoozes, audit records and
LLM routing history are their own data models; they are not a generic POLICY store.
The web UI should distinguish **Preferences**, **Operating policy** and **Limits /
configuration**. The last is read-only; credentials never appear in these views.

## Proposed authority and resolution rules

1. **Hard limits have one owner.** Protected numeric bounds live in base SETTINGS.
   Profiles may choose stricter operating defaults, but cannot raise those bounds.
   CLI cap flags become temporary restrictions within both the hard ceiling and
   current effective policy; they cannot loosen policy without the approved path.
   Their lifetime is the invocation and their source is visible in its snapshot;
   they never overwrite stored POLICY or the hard limit.
   These changes amend ADR-009's unrestricted override precedence for protected keys.
   Legacy recalibration/apply must reject hard-bound writes and may emit a reviewable
   file diff instead. Daemons receive read-only settings mounts per ADR-041.
2. **Separate ceiling, bootstrap default and effective value.** Proposed Stage 2
   naming: keep the six `safety.max_*` numbers as hard ceilings and add
   `operating_defaults.safety.<same_key>` for initial operating caps. Keep existing
   `grid.default/coins.*.order_size_usd` as bootstrap sizes and add explicit
   `order_size_min_usd` / `order_size_max_usd` alongside them. These are proposed
   fields, **not valid current configuration**. Their bounds stay SETTINGS-owned.
   Every default validates inside its bounds. Migration does not invent headroom:
   retain old effective values, initially freeze unspecified order-size bounds at
   that size, and require an operator decision for broader allowable ranges or
   old profiles exceeding the chosen ceiling.
3. **Validate and clamp on every read.** Resolve one coherent policy revision for
   each placement decision; validate key, scope, type, bounds and cross-field rules.
   Enforce the file ceiling even against a direct database write. Invalid negative,
   non-finite, wrong-type or wrong-scope values are errors, not silent defaults.
   Check the actual price × final order amount against caps. Exchange size/precision
   requirements and filled-quantity counters still apply after policy resolution.
4. **Require approval for every initial POLICY change.** This narrows ADR-040's
   automatic-tightening permission until field-specific tests prove safety, including
   effects on exits and exchange minimums. Preserve the possibility of a separately
   accepted deterministic tightening controller; do not grant write access to an LLM.
   Reset, expiry and rollback are changes too: classify against the current effective
   value, not just against the original default. No expiration may silently loosen.
5. **Do not widen on missing/unreadable policy.** Explicit static compatibility mode
   keeps today's behavior. First-time initialization has a known, validated bootstrap
   revision. Once POLICY is enabled, a missing expected table/row, invalid record,
   failed read or inconsistent migration blocks new order submissions; cancellation
   and reconciliation remain available. Keep the last known revision for diagnosis,
   not as permission to resume with looser defaults. This amends ADR-040 property 4
   and its empty/missing/unreadable validation rule. Operator recovery is explicit.
6. **Make batch changes atomic and reconstructable.** Keep validity intervals from
   ADR-040, plus revisions, scope and sanitized settings identity. Approval binds the
   exact payload and expected prior revision. One authorized consumer atomically
   claims/applies it, rejects stale approval and records one transition; no overlapping
   active intervals. Preserve the limits/defaults actually used with decisions;
   historical snapshots in the DB are evidence, never the source of hard-limit authority.
   Coordinate the existing command lifecycle work (N3) before relying on this path.
7. **Show requested and effective values.** An operator view should show the current
   operating value, hard bounds, provenance, scope, policy revision, clamp/degraded
   state and restart requirement. The engine, reporter and advisor must describe the
   same effective snapshot. A file edit must not be presented as already active in
   a running daemon; no full settings/environment dumps are needed.

The POLICY reader/writer uses a port; database implementation remains in the storage
adapter. Place application/approval rules in services. Preserve the existing global
versus symbol cap split, cost-basis semantics, news firewall and Harvester isolation.

## Settings layout implemented without changing current behavior

The readability pass retains one file and ADR-009's per-CLI sections. The operator
file and template now use the same section order, grouped with comments rather
than new YAML parent keys:

| Order | Sections | Purpose |
|---|---|---|
| 1 | `application`, `safety`, `grid`, `schedules` | Identity, trading envelope, geometry and cadence |
| 2 | `live`, `shadow`, `sandbox`, `preflight`, `status` | Trading/simulation and manual checks |
| 3 | `observe`, `news`, `screener` | Data acquisition and screening |
| 4 | `llm`, `advise`, `advisor` | LLM budgets, advisor daemon and role configurations together |
| 5 | `harvest`, `harvester` | Treasury daemon and constraints together |
| 6 | `operator`, `web`, `maintenance` | Operator surfaces and maintenance |
| 7 | `profiles` | Named complete overlays, with a short purpose index |

Add a short opening map explaining startup loading, how to locate the selected
profile, list replacement and which sections contain limits. Mark guard/budget
sections “operator-controlled SETTINGS”; mark the seven operating values “future
POLICY candidate, static today.” Keep all existing keys, values and profile names.

Retain the copyable cloud/Ollama candidate IDs and inference settings beside each
`fallbacks: []`. Shorten repeated historical evidence to a role-specific warning
and a [seat-register link](../reference/advisor-seats.md#fallback-candidates). Do not
remove candidate qualification warnings or move all practical configuration into docs.
Keep long runbooks in documentation; example comments should explain how to set a
field and what it changes. Canonical examples must still contain all required keys.

Document profiles by purpose: `conservative`/`aggressive` tune risk/geometry,
`moe-advisor`/`cloud-only-moe` select advisor modes, and `cpu-only` targets host
resources. Only one profile is selected today; these dimensions do not compose.
Do not rename profiles, add YAML includes/anchors, change inheritance, or create a
generic settings database as a side effect of improving readability.

## Options considered

| Option | Benefit | Cost / decision |
|---|---|---|
| Keep all operating values in startup YAML | Existing implementation; simple deployment | Leaves restarts for sizing/cap changes; acceptable until G6 opens |
| Put all mutable-looking settings in the DB | Broad UI editability | Reject: loses explicit authority and bypasses ADR-040 membership/lifecycle tests |
| Seven bounded POLICY values with protected SETTINGS | Accepted direction, fewer restarts, auditable operation | Recommended after the amendments/prerequisites above; adds explicit approval, resolver and failure semantics |
| Split YAML into many files or a shared runtime namespace now | Shorter individual files | Defer: loader/precedence/rewriter changes outweigh the immediate layout benefit; conflicts with ADR-009 unless separately justified |

## Implementation order and acceptance gates

1. **Repair the existing counter-amount cap check — implemented locally:** bounded code change
   with BUY/SELL, old/new size, partial fill and recovery-counter regressions against
   actual notional. This is a prerequisite even though POLICY is still deferred.
2. **Readability pass — implemented locally:** reorder/comment only in both YAML files. Preserve exact
   resolved models for base and every profile, credential references and all active
   values; check comment-preserving rewriter fixtures and config drift. No DB work.
3. **Ratify this amendment and G6 entry:** establish the documented trigger and a
   replacement fixture. Agree on scope identities, immutable ceilings, defaults and
   failure semantics. Reconcile existing CLI/profile/writer behavior explicitly.
4. **Build and test POLICY after those gates:** exact seven-key allowlist; read-path
   clamp mutation test; coherent revisions; immutable approvals and one claim;
   changed settings/expired approval rejection; wrong-account/shadow contamination;
   empty/new versus missing/unreadable store; rollback/expiry with no silent widening;
   order minimum/actual counter-size tests; legacy database and settings migration.
5. **Deploy separately:** validate reader/writer mounts, multi-daemon agreement,
   audit reconstruction, interruption/recovery and placement latency in an isolated
   rehearsal before operator-authorized live activation. Readability does not require
   a trading restart; changed behavior must not be claimed without deployment evidence.

## Adversarial review and verification

Review findings 1–5 were checked against current source and the accepted ADRs, not
only against names such as `max_*`. The proposal closes the design loopholes by
separating defaults from ceilings, treating absence/failure distinctly, requiring
initial approval, binding changes to a scope/revision, and preserving actual order
notional. POLICY remedies remain proposed; only the independent counter-cap repair
and layout pass below are implemented.

The inventory loaded and validated base plus all five profiles in each file and
checked that the example's 21 top-level keys match `WobbleBotConfig`. The mock
counter-size reproduction used real `GridEngine`, `MockExchangeAdapter` and
in-memory `SQLiteStorageAdapter`; its initial open-order-only query omitted the
immediately filled mock order, so verification uses all persisted orders. No
production trading outcome was inferred. Settings remained byte-identical during
that initial review. The subsequent authorized implementation is recorded below.

### Local implementation receipt — 2026-09-08

`GridEngine` resolves each proposed quantity once, uses price × quantity for cap
checks and passes that quantity to placement. The configured size still derives
initial layout quantities; counters still preserve the executed base quantity.
Order-count, BUY-only spend/inventory and existing cost-basis accounting semantics
are unchanged. A higher-priced SELL counter can now correctly be refused even
when its original BUY freed exactly the configured dollar budget. This repair
does not change how ordinary versus recovery counter refusals are retried.

[Counter regressions](../../tests/services/test_counter_order_caps.py) cover all
five USD caps with existing commitments, below/at/above remaining headroom, both
configured-size directions, BUY/SELL differences, order-count limits, normal and
startup-recovery counters, partial fills and retained refused recovery counters.
After correcting frozen test-config construction, **29 of 63 cases failed on the
old implementation** for the notional mismatch. The corrected code passes all 63.
An older engine test incorrectly assumed a counter at a higher price kept the
original dollar notional; it now asserts refusal at the existing cap.

Both YAML files now share all 21 section positions and a five-profile purpose
index. Repeated section histories and candidate evidence are shortened; practical
settings and qualification warnings remain. Raw YAML data and full resolved model
hashes are identical before/after for the base and every profile in each file.
All ten candidate pairs per file are unchanged and validate when enabled in memory
for their selected mode; MoE keeps the base single-advisor fallback list empty.
Active fallbacks remain disabled. The operator file is 999 lines and the example
1,212 lines, down from 1,129 and 1,493 respectively. Cross-process fingerprints
sort the schema's set-valued authorization IDs; direct model equality avoids
mistaking JSON set iteration order for a configuration change.

Targeted engine, exposure, rewriter and full config checks passed **570 tests**
with strict config drift enabled. The [roadmap](../planning/roadmap.md) carries the
full validation receipt. Work is local and uncommitted; no POLICY migration,
model activation, deployment or phase acceptance is implied. G6 remains closed.
