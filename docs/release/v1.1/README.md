# WobbleBot v1.1 — Plan & Index

**This file is the master plan and index for v1.1.** The sibling files in this
directory (`engine.md`, `adaptive-grid.md`, `harvester.md`, `news-pipeline.md`,
`observability.md`, `operator-ux.md`, `trading-scope.md`, `external-triggers.md`,
`infrastructure.md`) hold the *detail* for each candidate; this file holds the
*sequence* — which work happens when, in what order, and behind which gates.
`standing-rules.md` is durable posture (margin/futures gates, SDK stance, Kraken-UI
declines), **not** plan candidates.

- **Status source of truth:** `docs/planning/roadmap.md` (per-item completion dates land there).
- **Decision records:** `docs/architecture/decisions.md` (ADRs).
- **Written:** 2026-06-01. Living document — re-sequence as the soak surfaces facts; keep it honest.

> **⚠️ Strategy update (2026-06-02).** The work that was originally built on the `v1.1`
> branch turned out to be mostly **v1.0 hardening** (the dead man's switch, the preflight
> key-scope gate, the four-homes/schema-drift/retry audits, the o4 bug fix, the G1
> dead-config cleanup, the offside log-noise fix). It was **fast-forwarded into `main` as
> the v1.0 candidate** (`73e9388`) — `main` is **no longer frozen**. "v1.1" now refers to
> the **post-tag P1–P4 roadmap** below; those phases branch off `main` *after* the v1.0
> tag. The gating soak is being **restarted on the hardened candidate** (and going
> **multi-coin** for better engine coverage while BTC is parked).

> Built from a full inventory of every documented v1.1 candidate (213 item-rows →
> deduped). Nothing is dropped: work that isn't in an active phase below is in the
> [Parked register](#parked-register) with its trigger.

---

## Organizing principle

**Freeze-gated dependency layering, value-and-risk ordered within each layer.** Phases
are cut by the three hard boundaries that actually constrain the work:

1. **Tag gate** *(the original branch-freeze that shaped this plan was lifted 2026-06-02 —
   see the strategy update above; the "P0" hardening already merged into the v1.0
   candidate)* — the **P1–P4 work below is gated on the v1.0 tag**: it branches off `main`
   after the restarted soak passes and v1.0 ships.
2. **Dependency spine** — the four-homes audit must precede any storage-tier migration;
   the Kraken-history import must precede its DB consumers; OHLC+TA is the shared input
   gating the regime detector / auditor / screener / counter-target / historian.
3. **Data-time gates** — advisor outcome tracking needs 30–90d of applied-recommendation
   data; the regime detector needs a 60–90d shadow-run before any consumer wires into
   `cli/live`.

Within each phase, slices are ordered **value-and-risk first** (safety-critical +
soak-exposed defects ahead of speculative sophistication; effort is the tie-breaker,
never the sort key). **WIP limit: finish a phase before opening the next** — reprioritize
*within* a phase on a single soak observation, but don't fan out across phases.

## Current status

| | |
|---|---|
| `main` | = the **v0.1.0 candidate** at `38b8678` (advances with soak-hotfixes; **no longer frozen** since 2026-06-02) |
| v1.0 hardening | ✅ dead man's switch (ADR-021) · preflight key-scope gate · four-homes/schema-drift/retry audits · o4 fix · G1 cleanup · offside log fix — **all on `main`** |
| v1.0 soak-hotfixes | ✅ rate-limit batch fix + DMS disarm-on-failed-cancel fix (`abf3aa6`) · regression test (`8b25feb`) · DOGE ordermin workaround — **2026-06-02, on `main`** |
| v1.0 | gating soak **running multi-coin** (ETH/SOL/XRP/DOGE/ADA) on the hardened candidate |
| "v1.1" (post-tag) | the **P1–P4 roadmap below** — branches off `main` after the v1.0 tag |

## Phase map

| Phase | When | Theme | Status |
|---|---|---|---|
| ✅ Hardening (dead man's switch + P0.1–P0.5 + o4 + G1) | merged to `main` 2026-06-02 | Safety / Groundwork | **done** |
| 🚦 **GATE** | restarted (multi-coin) soak passes | **tag v1.0** | — |
| **P1** | post-tag | Safety + ready-now | active after tag |
| **P2** | post-tag | Data-infrastructure spine | active after tag |
| **P3** | post-tag (parallel to P2) | Ops / observability / UX | active after tag |
| **P4** | 30–90d post-tag | Advisor-feedback cluster | data-gated |

Effort key: **S** = hours · **M** = 1–2 days · **L** = several days · **XL** = a week+.

---

## ✅ Done — Dead man's switch (ADR-021)

Server-side `CancelAllOrdersAfter` safety net: `ExchangePort.set_dead_mans_switch` + per-tick
pet/disarm in `cli/live`, on by default at 60s. Kraken auto-cancels all open orders if the host
goes silent (crash/power/network loss) — the failure the `finally`-block cancel can't cover
(2026-05-19 outage). 4 commits on `v1.1` (`eb1cae7`→`db020be`). See ADR-021 + `engine.md`.

---

## ✅ Done — soak safety hotfixes (2026-06-02, on `main`)

The 2026-06-02 multi-coin soak restart surfaced two real safety defects, both **fixed on the
v1.0 candidate**:

- **Per-symbol `OpenOrders` rate-limit storm** — the engine fetched open orders once per symbol
  per tick; 5 coins × that tripped `EAPI:Rate limit exceeded`, blocking startup reconciliation
  and the shutdown cancel. Fix: one global fetch per tick distributed to every symbol (private
  calls now ~3/tick regardless of coin count). `abf3aa6`.
- **Dead-man's-switch disarmed on a failed shutdown cancel** — a rate-limited `_cancel_all_open`
  fetch-failure returned `(0,0)` → caller read `cancel_clean=True` → `set_dead_mans_switch(0)`,
  leaving orders open AND unprotected (~15 orders, ~10 min). Same `abf3aa6` fixes it (the global
  fetch now propagates → `cancel_clean=False` → switch stays armed); regression test `8b25feb`.
  The DMS arming itself was verified live (`tools/check_dead_mans_switch.py`: Kraken
  `triggerTime` = `currentTime` + timeout).

Also shipped: DOGE `order_size_usd` $5→$6 (Kraken's fixed 50-DOGE ordermin; $5 fell to 49.99
DOGE at ~$0.10) and the diagnostic `tools/check_dead_mans_switch.py`. See ADR-021 + `engine.md`.

---

## ✅ P0 — Branch-safe groundwork (DONE — merged to `main` 2026-06-02)

**Goal (as written):** land everything provably safe while v1.0 soaks — the two `v1.1`-start
gates, docs-only deliverables, and branch-only refactors. **Historical note:** this work turned
out to be v1.0 hardening and was fast-forwarded into `main`; the original "no `main` merge /
`main` frozen" constraint was lifted 2026-06-02 (see the strategy update at the top). Slices 1–5
are done; the optional refactors (#6/#7) + the Q1–Q3 externalizations are **post-tag cleanup**,
not blockers.

| # | Slice | Effort | Value | Safety | Notes |
|---|---|---|---|---|---|
| 1 | **Hardcoded-facts four-homes audit** | M | med | ⚠️ | ✅ **DONE 2026-06-01 → [`four-homes-audit.md`](four-homes-audit.md).** Safety carve-out held (pricing + fees stay code; review verified zero safety facts moved). Only move candidates = 3 model-ecosystem config externalizations (Q1–Q3 below), all queued (none trivial). Nothing unblocks a DB migration of a safety fact. |
| 2 | Schema-drift coverage for canonical profiles | S | med | | ✅ **DONE 2026-06-01** (`6f6097d`). Canonical = example profile names → `test_operator_has_all_canonical_profiles` (custom profiles exempt); **scoped** pre-commit drift gate (runs only when an example config is staged; strict-blocks; fail-soft on missing venv; verified end-to-end); `resolver.py` "profile not found" error now points at `settings.example.yml`. |
| 3 | `cli/preflight` ADR-003 key-scope verification | M | high | ⚠️ | ✅ **DONE 2026-06-01** (`07540ce`), Scope A. preflight refuses **exit 3** if the trade key holds Withdraw scope, via a read-only `WithdrawMethods` probe (`KrakenAdapter.has_withdraw_scope`; no test-withdrawal). **Harvest-key checks (has-withdraw + no-trade) deferred** to a follow-up (needs harvest creds + the lower-confidence AddOrder-validate probe). ⚠️ wants one operator confirmation run against the real key. |
| 4 | Solo-operator incident runbook | M | med | ⚠️ | ✅ **DONE 2026-06-01** → [`v1.0-incident-runbook.md`](../v1.0-incident-runbook.md). 6 scenarios (key/secret compromise, unexpected withdrawal, web exposed, gitleaks history, personal-info exposure, bot misbehavior) in a Detection/Stop/Assess/Recover/Document template; cross-links the preflight key-scope gate, dead man's switch, pre-commit hook, soak-runbook abort procedure. Solo runbook, not a team IR process. |
| 5 | Connectivity retry-policy audit | M | low | | ✅ **DONE 2026-06-01** → [`docs/architecture/retry-policy.md`](../../architecture/retry-policy.md). Only cloud LLM retries (ADR-015); Kraken/Ollama/RSS/CryptoCompare are single-attempt-with-timeout (contained by fault-isolation + next-cycle re-poll). **Headline finding (G1) ✅ RESOLVED:** the whole Kraken `exchange:` YAML block was **dead config** (`WobbleBotConfig` has no `exchange` field) — removed 2026-06-02. G2–G4 ticketed. |
| 6 | *(opt)* Test-fixture consolidation (`:memory:` storage) | M | low | | Branch-only refactor; ~40 per-file `storage()` fixtures → shared `conftest`. Inventory-deferred (invasive rename) — pull in **only if clean** in the quiet window. (`infrastructure.md`) |
| 7 | *(opt)* `WiredSnapshot` base + `load_with_degrade` | M | low | | Branch-only web refactor; rename `live_wired`→`wired`, extract a base for 6 snapshot dataclasses. Deferred-until-7th-copy — **only if clean**. (`infrastructure.md`) |

**Exit gate:** four-homes verdict written (each fact classified; pricing/fees confirmed
code-resident); profile drift asserted; preflight refuses exit 0 on scope divergence; runbook
+ retry-policy docs committed; optional refactors merged only if clean; tests + lint green per
commit; **no `main` merge.**

### Queued from P0.1 (the four-homes audit) — all branch-safe, optional during the soak

The audit produced three config-externalization candidates + code-health findings. They're
non-safety, branch-safe (no `main`/soak impact), and could be pulled during the soak or
deferred. Full detail in [`four-homes-audit.md`](four-homes-audit.md).

| # | Slice | Effort | Notes |
|---|---|---|---|
| Q1 | Model-compat lists → config | S–M | `KNOWN_INCOMPATIBLE`/`KNOWN_DEGRADED` + the embedded recommendation list (`ollama_assistant.py`) → one config section, fail-soft loader, schema-drift test. The prime candidate (verdicts have flipped on re-probing). |
| Q2 | Model-name patterns → config | S–M | `_REASONING_MODEL_PREFIXES` + `_THINKING_MODEL_PATTERNS` → config with a safe default. **o4 bug FIXED code-resident** (below); externalization itself still queued (low value for a solo operator-dev). |
| Q3 | News-coin whitelist → config | S | `_COIN_PATTERNS` (`rss_news.py`; MATIC→POL stale) → config, derived from / cross-checked against the traded symbols. |
| — | ✅ **o4 + gpt-5 reasoning-shape FIXED** (o4 2026-06-02, gpt-5 2026-06-03) | S | `is_reasoning_model` matches the whole o-series (`o<digit>`) + the gpt-5 family — regex now `^(o\d|gpt-5)`. o4-mini (priced, was unmatched) + future o5 handled; gpt-5 reasoning-shape **verified via context7** (OpenAI docs: reasoning shape applies to "gpt-5 and o-series") and folded in with a regression test (`gpt-5-chat` over-match caveat noted in code). |
| — | ⚠️ **Gemini 3.x drift** *(context7 sweep 2026-06-03)* | S | Current OpenAI/Google docs check found `gemini-3.5-flash` ("Gemini 3") is the current generation, but `llm_pricing.py` prices only `gemini-2.5-pro/flash` (verified 2026-01) and `google.py` examples still say 2.5 → a Gemini-3 config would `PricingLookupError`. Also: docs say "keep temperature 1.0 for Gemini 3" (our adapter sends a configurable temp). Needs **operator-verified** gemini-3 prices (human per ADR-014) + docstring refresh before any Gemini model is configured. Anthropic surface checked same day → clean (current `/v1/messages` shape, model IDs, version header). |
| — | 📝 **Ollama `think` param could retire name-pattern detection** *(context7 sweep 2026-06-03)* | M | Current Ollama `/api/chat`+`/api/generate` expose a native `think: true\|"high"\|"medium"\|"low"` param that returns reasoning **separately** from the answer, plus `format: <JSON schema>` for guaranteed structured output. Today we detect thinking models by **name pattern** (`is_thinking_model` — the `_THINKING_MODEL_PATTERNS` half of Q2, "flips on re-probing") and walk free-text for the last JSON object (`extract_last_json_object`). The native `think` param could **eliminate** both — stronger than Q2's "externalize the name list." **Not a bug** (current usage valid); a modernization, gated on the NAS Ollama version supporting `think` + handling non-thinking models. |
| — | Dedup smells (code-health) | S–M | Kraken fee ×4 (⚠️ touches the validator), Kraken URL ×3, Ollama URL ×4, Anthropic URL/version ×2, Discord colors ×2, OHLC intervals ×2; RSS UA `0.1`↛`__version__`. Consolidate *in code*, not move out. |

---

## 🔍 Queued from the v1.1 deep-scan (2026-06-02)

A 15-agent adversarial codebase scan (7 dimensions) + an 11-finding cross-reference against this
plan. Honest headline: **the codebase is clean for a solo-operator $100-test grid bot** — one real
correctness gap, two cheap robustness fixes, the rest one-liners; **zero new safety-critical
defects** in the financial-power-fragmentation design. Each finding was classified
*already-tracked / partial / net-new* against the docs above. Findings with a home went there;
the orphan one-liners (no other home) are detailed here.

**Promoted into existing homes** (detail at the link — not duplicated here):

- **F1 — live partial-fill Trade-drop** → P1 row above. ⚠️ *Partial-overlap trap:* the existing
  "Reconciler fill-vs-cancel" P1 row *looks* like it covers this but is **startup-scoped** and would
  not catch the live `_detect_fills` gate. Two separate fixes — don't check one off for the other.
- **F2 — auto-apply NaN guard** → P1 row above.
- **F3 — per-tick price-fetch dedup** → P1 row above.
- **F4 — explicit `busy_timeout`** → the *adapter half is already tracked* in `engine.md`
  (`SQLiteStorageAdapter.connect()` PRAGMA); the net-new sliver (the raw `sqlite3.connect` in
  `maintenance.py:70` / `backuper.py:115-117` has **no** timeout) is now a bullet there.
- **F7c — `pending_message_map` grow-only leak** → incidentally removed by the P3 "Discord buttons
  over reactions" row (the View migration deletes the in-memory map). UX-motivated item, so noted
  there as a correctness side-benefit; interim 1-line `.pop()` if it ever bites first.
- **F9 — web-route poll-query cost** (`/cost` 10k-trade rollup, `/news` two 1k scans) → Parked
  register → Performance. Immaterial at $100/few-coin volume + indexed; the SQL-rollup rewrite is
  over-engineering until volume nears the cap. *(The scan wanted to fold it into an "existing
  pagination note" — there wasn't one, so it's parked fresh.)*

**Branch-safe code-health one-liners** (no other home; do on the feature branch during the soak or
batch post-tag — none are soak hotfixes, so none land on `main` mid-soak):

- **F5 — harvester band-label off-by-equality.** ✅ **DONE 2026-06-03.** `operator_service.py`
  `_classify_band` used `<` where the authority `harvester.py` `propose_transfer` uses `>` — at exact
  `balance == surplus_threshold` it labeled the band "surplus" while the authority would HOLD.
  **Purely cosmetic** (label only drove a Discord embed color + a tally + log strings; no money path
  keyed off it). Aligned to `<=`.
- **F6 — delete the dead `HarvesterPort` ABC.** ✅ **DONE 2026-06-03.** `ports/harvester.py` carried a
  4-method ABC with zero implementers / injectors / annotations (the real design is free functions +
  a local `_TransferHistoryReader` Protocol that doesn't subclass it) — a DI seam the code abandoned.
  Deleted it + the two re-exports (`ports/__init__.py`); **kept** the co-located
  `TransferProposal`/`TransferResult` models. Doc-synced six architecture files
  (`architecture-components.md`, `glossary.md`, `context.md`, `architecture-intro.md`,
  `runtime-view.md`) + the `exceptions.py` docstring in the same commit; replaced the ABC with a
  module docstring explaining the Harvester-is-a-service rationale.
- **F7a — login rate-limit docstring + MFA rationale.** ✅ **DONE 2026-06-03.** The `LoginRateLimit`
  docstring promised per-IP isolation, but behind the recommended loopback+reverse-proxy posture
  `request.client.host` is the proxy IP → all logins share one global bucket (`_client_ip` reads
  `request.client.host` with no `X-Forwarded-For` parsing — confirmed). Rewrote the docstring to
  describe an effectively-global throttle (correct for one operator) and why `proxy_headers` stays
  off (forwarded header is spoofable); **did not** enable `proxy_headers`. Also corrected the
  MFA-deferral rationale in `operator-ux.md` — the "Trigger" line no longer leans on a false "per-IP"
  premise and now frames the throttle as anti-guessing, not isolation (which is the MFA upgrade).
- **F7b — dead `?attempted=` query param.** ✅ **DONE 2026-06-03.** `web/routes/settings.py`
  redirected with `&attempted={timezone}` but the GET handler read only `save` and no template
  rendered it. Not an XSS vector (autoescaping on). Param deleted.
- **F8 — document the `adapters → services` import exception.** ✅ **DONE 2026-06-03.** 5 LLM
  adapters + `adapters/moe_advisor.py` import `services.*` (`llm_cloud_call`, `llm_cost_gate`,
  `llm_pricing`, `llm_retry`, `aggregators`) against the `CLAUDE.md` inward-only layer rule — but
  there's **no import cycle** (the service helpers are clean leaves that never import the adapters
  back) and the LLM plumbing is well-factored. Added a "Documented exception — LLM plumbing" hard-rule
  bullet to `CLAUDE.md` so the rule matches the graph; relocation not warranted.

**Confirmed-fine — no action** (verified, recorded so they aren't re-flagged next sweep): double
`BalanceEx` is per-session not per-tick; `_check_safety` runs zero SELECTs on a no-fill tick
(≤36-row indexed table); sync VACUUM/backup is a separate process from the trading loop;
`feedparser.parse` has no loop competitor; web HTMX polls are single-operator + indexed, far below
caps. Several of the scan's louder framings — a "Ticker rate-limit storm twin", `busy_timeout`
instant-failure, sync-VACUUM as a live-trading risk — **did not survive verification.**

---

## 🚦 GATE — tag v1.0 (code-blocked, planning-open)

**v1.0 soak passes → tag v1.0 → P1–P4 branch off `main`.** The hardening + soak-hotfixes are
already on `main`; this gate is **code-blocked, not planning-blocked** — plan items below may be
written and refined during the soak (e.g. the P3 buying-power card, `38b8678`), but no P1+
*code* lands until the tag.

**Soak pass criteria** (resolves [Open question](#open-questions) 1): engine-correctness coverage
+ reconciliation across restarts + ≥1 of each daemon cycle + **no hard-stops** (per the soak
runbook). Profit and BTC direction are explicitly **NOT** criteria — alts are BTC-correlated, so
this measures *coverage, not profit* (decorrelation is the equities/Phase 9 play). Target
duration ~1 month on the multi-coin restart.

---

## P1 — Soak-exposed safety hardening + ready-now backlog

**Goal:** ship the high-value, soak-surfaced safety fixes and the ready-now items whose only
blocker was the tag. Each engine-safety item gets its own ADR + test-for-the-bug + focused commit.

| Slice | Effort | Value | Safety | Notes |
|---|---|---|---|---|
| **Reconciler fill-vs-cancel + F1 partial-fill (UNIFIED)** | L | high | ⚠️ | Highest-value safety defect — *same root* as F1 below (`canceled/expired + filled_amount>0`). **Blueprint settled 2026-06-03** (feature-dev ×2 + adversarial judge; detail in `engine.md`): one shared `_resolve_terminal_order` at both the startup reconciler + the live `_detect_fills` gate; **QueryOrders not `ClosedOrders`** (already exists, simpler); **counter-replay via the engine on first tick** (`needs_counter_order_ids`→`pending_counters`), **retry-on-failure** (judge: reconciler-direct placement breaches power-fragmentation AND, on a failed placement, re-triggers the auto-re-layout → *reproduces* the orphan); full safety caps; mock `inject_partial_cancel` fixture. Own ADR + regression test reproducing the orphan. |
| **Live partial-fill Trade-drop** *(deep-scan 2026-06-02)* | M | high | ⚠️ | **Distinct from the reconciler row above** — that one is *startup*-scoped; this is a **live `_detect_fills` bug**. The engine saves a `Trade` + places a counter only when the refreshed order is `closed` (full fill). A partially-filled order that refreshes to `canceled`/`expired` with `filled_amount > 0` — now *more* likely, since shutdown cancel-all + the ADR-021 dead-man's-switch both cancel partially-filled limits — drops the matching `Trade` rows (storage under-records a real fill) and skips the counter, corrupting cycle-matcher/dashboard PnL and drifting base-inventory vs Kraken's real holdings. The startup reconciler does **not** patch it (it diffs open-order status, never re-derives a dropped partial Trade). Floor fix: on a cancel/expire with `filled_amount > 0`, still save the trades + WARN about the unbalanced leg; optional counter sized to the partial when not offside. Own test for a canceled order carrying `filled_amount > 0` — **test-infra caveat (test-honesty audit 2026-06-02):** the `MockExchangeAdapter` cannot produce a canceled-with-`filled_amount>0` order today, so the F1 test must build that state via the Kraken adapter's raw `_apply_kraken_order_update` (where it's production-reachable) or extend the mock. Independently confirmed **unpinned** — no current test exercises this axis. **Now UNIFIED with the reconciler row above** under one shared `_resolve_terminal_order` helper (blueprint 2026-06-03): F1 (per-tick) + the reconciler (startup) are the same fix at two call sites — see `engine.md`. **Live evidence (2026-06-03 ~06:34 UTC):** an observed ADA dust fill (`sell 0.00006529 ADA @ $0.22`, net ~$0.0000) confirms Kraken fragments a single ordermin-compliant grid order into multiple **sub-ordermin** fills — the partial-fill mechanism this row addresses is occurring live and routinely. *(That particular fragment was recorded, not dropped — the drop is specific to the cancel/expire-with-`filled_amount>0` path — but it supplies a concrete real-world fixture shape for the F1 test instead of a purely hypothetical one.)* |
| Session-loss-cap cool-down period | M | high | ⚠️ | Operator-configurable cool-down after `cli/live` exits `exit_code=1`; `--ignore-cool-down` (terminal-only). **Blueprint 2026-06-03** (`engine.md`): new `live.db` cap-trips table (rejected state-file + notifications-parse), pre-loop gate in `_main_async`, **new exit code 4**, **fail-open** on storage error (crash-loop guard), scoped to exit 1 / not shadow-sandbox. Default window = operator's risk call (passes split 30 vs 60 min). New ADR. |
| Slippage / spread guard before placement | M | high | ⚠️ | **Blueprint 2026-06-03** (`engine.md`): a new **`get_ticker`** port method (bid/ask are free from the existing Ticker call — **no `get_order_book` round-trip**) + a `Ticker` value object + a **pre-tick** spread gate in `_step_unlocked` (skip the tick, not a per-order cap) + `max_spread_percentage` on `SafetyConfig` (default 1.0%) + offside-style log-flood guard. New ADR. Higher priority once multi-asset ships. |
| Partial-grid placement: WARN → INFO | S | low-med | | Demote the scary insufficient-balance WARN to an INFO summary (placed-vs-target). Reserve WARN for genuine refusals. |
| Backup verification — restoration smoke test | S–M | high | | Monthly `cli/maintenance` task: open latest backup, `PRAGMA integrity_check` + representative SELECTs, notify on failure. Backups written since Day 1, never verified. |
| Content-Security-Policy header | S | med | | ~10-line CSP middleware; defense-in-depth over Jinja2 autoescape (ASVS L3). |
| Kraken status news adapter | M | med-high | | Poll `status.kraken.com` JSON → `news_items` tagged `kraken_status`. Extends the proven news pipeline; standalone (feeds the parked auto-pause later). |
| One-command daemon orchestrator (`cli/up`) | M | med-high | | `honcho` + Procfiles with pre-launch preflight. Promote only if full-stack restart friction is real. |
| Footer "update available" indicator | S–M | low-med | | `release_checker` polls GitHub releases; disableable. Meaningless until a tag exists — lands right after it. |
| More Kraken crypto pairs | S | med | | Pure config (engine multi-symbol since Stage 2.4). Operator risk-budget call on which coins / what split. |
| **Engine ordermin-awareness** | S–M | med | ⚠️ | A fixed `order_size_usd` ÷ a rising price can slide under a pair's fixed-quantity `ordermin` (DOGE: $5 → 49.99 < 50 DOGE at ~$0.10, 2026-06-02 soak). The engine already holds pair metadata — bump the volume to clear `ordermin` (capped by the per-coin cap) or skip with a clear INFO, instead of submitting a doomed order. Operator worked around it per-coin (DOGE `order_size_usd: 6`). |
| **Dead-man's-switch arm confirmation** | S–M | high | ⚠️ | `set_dead_mans_switch` discards Kraken's `CancelAllOrdersAfter` response, so the bot doesn't *confirm* the arm took. (The 2026-06-02 non-firing was the disarm-on-failed-cancel bug, now **fixed in `abf3aa6`**; the arm itself is verified working via `tools/check_dead_mans_switch.py`.) Defense-in-depth: return + log Kraken's `triggerTime` on each arm in-loop; consider refusing to place orders when the switch isn't confirmed-armed. |
| **Harvester `--execute` replay guard** | S | high | ⚠️ | **Highest-blast-radius hole in the codebase** (2026-06-02 plan review). `cli/harvest --execute` runs gates 1–7 (enabled/lookup/direction/staleness/destination/balance/day-cap) then goes straight to `withdraw()` — no "already-executed for this `proposal_id`" check. A double-tap / shell re-run / retry-after-perceived-hang can double-withdraw; the rolling day-cap is the only accidental backstop. Fix = a cheap "layer 0": `SELECT TransferResult WHERE proposal_id=? AND status IN (pending,completed) → refuse`. Own ADR + test. (Not in `harvester.md`.) **⚠️ Now a HARD co-requisite of the P3 web-Execute button** (P3 judge 2026-06-03): web→pending_commands→cli/harvest-poll multiplies the double-withdraw vectors — do NOT ship web-Execute without this guard, and prefer a **UNIQUE constraint on `transfer_results.proposal_id`** (DB-enforced, concurrency-proof) over an app-layer-only check. |
| **Harvester-key separateness + withdraw-scope check** | S–M | high | ✅ | Symmetric inverse of the shipped P0.3 gate (which proves the *trade* key can't withdraw). **✅ DONE 2026-06-03 (v1.1):** `_verify_harvester_key` at `cli/harvest` startup (both daemon + `--execute`) refuses **exit 3** if the Harvester key lacks Withdraw scope (`has_withdraw_scope()`) OR byte-equals `KRAKEN_TRADER_API_KEY`. Fails SOFT on a transient probe error (logs + continues — no crash-loop, docker-rule #6); when the trade key isn't in the harvest env the byte-compare is skipped (deployment-level separation is the implicit guard). 5 unit tests. Promotes the P0.3 "harvest-key checks deferred" note to done. |
| **Today's-PnL truncation fix** | S | med-high | ⚠️ | `today_realized_pnl` reads `get_trades(limit=100)` (`operator_service.py:865`, `web/routes/status.py:252`); multi-coin makes >100 trades/day plausible, so the oldest legs silently drop and PnL **undercounts with no error** (the dashboard can show fees from trades whose PnL it isn't counting — the fee path already uses `limit=10_000` at `cost.py:214`). Fix: scope by time-window in operator-tz, not a fixed row count. Prerequisite to the P3 "Today's PnL split". |
| **`EmergencyStopConfig`: wire or document** | S | med | ⚠️ | `safety.emergency_stop.{max_loss_percentage,min_exchange_balance_usd}` ships in `settings.example.yml` but `grid_engine._check_safety` (line 629) enforces only the 4 caps — the field is read by nobody (only `calibrator.py` scaling + a `preflight.py` throwaway). An operator reasonably believes it's a hard balance floor; it does nothing. **A silent dead safety knob is worse than none** — wire `min_exchange_balance_usd` as a 5th cap, or document it as calibration-only in the schema + known-limitations. Pick one. |
| **MoE news-firewall: aggregated role bypasses the auto-apply block** *(MoE prompt review 2026-06-04)* | S–M | med-high | ⚠️ | ADR-007 says news "cannot drive an auto-applied parameter change," and the gate blocks `role=='news'` (`auto_apply.py:54-57`) — **but** the arbitrator's output is force-tagged `role='aggregated'` (`moe_advisor.py:161`), which is **not** in `_BLOCKED_ROLES`. So a news-driven *number* folded into the reconciled `recommendations` dict **can auto-apply**, violating the ADR's intent. Inert today (`auto_apply.enabled=false`), but a real gap once auto-apply is on. **Prompt mitigation SHIPPED 2026-06-04** (`arbitrator.md` Rule 2: reconciled numbers must be justifiable from quant+risk alone) — but that *trusts the arbitrator LLM*; the firewall was meant to be **structural**. Code-level option: have `MoEAdvisorAdapter` flag whether news materially drove the aggregated value (it already carries per-expert `expert_opinions` provenance the gate could inspect), and block/flag accordingly. Own small ADR + test. |
| **Kraken rate-limit backoff** | S–M | high | ⚠️ | The 06-02 global-fetch fix cut call *count* but not the error *class* — `_unwrap_envelope` still raises a generic `ExchangeError` on `EAPI:Rate limit exceeded`, and the shutdown still fires N `CancelOrder` back-to-back with zero spacing (can re-trigger the storm during the most safety-critical cleanup). Classify the rate-limit error as transient + bounded backoff (reuse the cloud-LLM retry shape, ADR-015) + inter-cancel pacing in `_cancel_all_open`. Own small ADR + test. (`retry-policy.md` G4 parked this under "perf"; the soak proved it's *resilience*.) |
| **Operator-cancel fetch-failure ambiguity** *(pylint review 2026-06-02)* | S | med | ✅ | `GridEngine.cancel_open_orders` returns `(0,0)` when the `get_open_orders` fetch itself fails (`grid_engine.py:267-273`) — **indistinguishable from "no orders to cancel."** Reached only by the operator command path (`operator_service._dispatch_cancel_open_orders`, Discord/web), NOT the DMS shutdown path (`cli/live._cancel_all_open` correctly propagates). So an operator issuing "cancel all" *during a rate-limit spike* gets back "cancelled 0, failed 0" — reads as all-clear while orders are still live on Kraken. Same lesson as the abf3aa6 DMS bug — *a `get_open_orders` failure must not masquerade as a clean empty result* — applied to the operator command path the fix didn't touch. Fix: signal the fetch-failure distinctly so the `CommandResult` says "couldn't read open orders — they may still be live; retry," not `(0,0)`. Per-order failures already count correctly (`failed += 1`). Surfaced by the 2026-06-02 pylint-disable audit (the only actionable finding; all other disables are complexity-threshold or documented-architectural). **✅ DONE 2026-06-03 (v1.1 branch):** `cancel_open_orders` now lets the `get_open_orders` `ExchangeError` propagate (no `(0,0)` masquerade); `_dispatch_cancel_open_orders` catches it → `success=False` + "may still be LIVE on Kraken; retry" message + `fetch_failed` side-effect. Also tightened the clean-result flag to `success = failed == 0` so a partial per-order cancel failure no longer reads green. 3 regression tests (engine raises on fetch-fail; dispatch reports failure-not-all-clear; partial-failure not success). |
| **Boot-time stale-anchor WARN on restart** | S | med-high | ⚠️ | On restart the engine re-lays the full grid at the *persisted* `reference_price`; "offside" is checked against that stale anchor, so a multi-day-old anchor that still brackets price passes silently. Cheap WARN at the auto-relayout site (drift % + anchor age, both already in scope there), optional refuse. The *detect* rail; the P3 operator-initiated re-anchor command is the *fix* flow. Closes the "stale-anchor-on-restart" class the soak flagged (BTC + the alts). |
| **Dashboard session-cap card** | M | med-high | ⚠️ | A **Session card** (PnL vs `max_session_loss_usd`, % consumed, tripped state): `web/` has zero `session_pnl`/`loss_cap` surface, so after a cap trip the operator who missed the bell/Discord ping has no visual signal (the "cap tripped unnoticed ~1.5h" soak miss). Promote ahead of the P3 buying-power card. **Scope corrected 2026-06-03:** this item used to also say "wire the LIVE badge to LIVE/STALE/STOPPED" — that was WRONG. The `LIVE`/`SHADOW` `mode-badge` is a trading-MODE indicator, made dynamic under the *mode-parameterized webui* item (P3), not a liveness one. A separate engine-liveness signal was **considered and dropped** (operator 2026-06-03) — the `/health` page + navbar heart-pulse dot suffice. So this item is now **just the Session card**. |
| **Auto-apply NaN guard** *(deep-scan 2026-06-02)* | S | med | ✅ | `evaluate_auto_apply` promises "never raises on bad input," but a NaN LLM recommendation (`json.loads` accepts a bare `NaN` token) became `Decimal('NaN')`, and `Decimal('NaN') <= 0` **raised** `decimal.InvalidOperation` — crashing the ADR-002 safety boundary. **✅ FIXED 2026-06-03 (v1.1):** `_coerce_numeric` now applies `is_finite()` so NaN/±Inf/sNaN degrade to a `RejectedKey`; regression tests drive NaN/Inf through both the helper and the full gate. |
| **Per-tick price-fetch dedup** *(deep-scan 2026-06-02)* | M | med | | Each `cli/live` tick fetches a symbol's price twice — `engine.step`→`_step_unlocked` and again in `_session_portfolio_value_usd` (loss-cap mark-to-market) — both uncached `/0/public/Ticker` GETs, one extra serial round-trip per *held* symbol. **Latency hygiene, not an outage risk**: public bucket (not the private bucket of the 06-02 storm), and gated on `base_balance > 0` so cost is `N + held`, not `2N`. Fix mirrors the OpenOrders snapshot pattern: fetch prices once at the top of `_run_one_tick` into a dict and thread through (`engine.step` gains an optional `prices=`, falling back to per-symbol fetch for shadow/test callers). |

### P1 test-hardening — consequence/orchestration coverage (test-honesty audit 2026-06-02)

A 12-path mutation-mindset audit (*does a regression actually fail a test?*) + a suite-wide hygiene
scan. **Hygiene is gold-standard** — zero tautological / over-mock / called-not-effect tests across
153 files, all 11 skips legitimate (live-cred/data gates), `filterwarnings=error` un-relaxed
(recorded so future audits don't re-litigate it). But **6 of 12 safety paths have a real coverage
gap, all the same shape: the decision logic is pinned, the *consequence / orchestration* is not** —
the tested unit is honest; the call-site wiring that makes it safety-critical is unverified. These
are test *additions* (P1 / branch-safe — not soak hotfixes), ≈one end-to-end test each, clustering
with the P1 safety work above.

| Add | Path | Sev | The regression that ships green today |
|---|---|---|---|
| Loss-cap consequence E2E | P3 | high | Nothing drives `_run_loop` through a real cap trip: `exit_code = 1`→`0` ships green (a watchdog auto-restarts into the losing market) and a regression skipping cancellation *on the trip path* leaves orders resting on Kraken. Detection is pinned; the consequence + the `< / <=` boundary are not. Test: trip the cap, assert `exit_code==1` AND orders canceled AND boundary at exactly `-cap`. |
| Firewall-bypass negative test | P7 | high | The ADR-002 "approved-only reaches the engine" SELECT is well-pinned, but "intent never dispatches directly" is prevented *only by code structure* (the handler holds no engine ref) — a future edit giving `_handle_command_intent` an engine reference bypasses silently. Test: after an intent with **no** confirmation, assert the engine was **not** actioned. (Also: the stray-emoji `else` branch at `operator.py:861` is uncovered.) |
| Preflight gate orchestration | P9 | med | `_audit_trade_key_scope` is solid, but nothing wires `preflight._run`: the "scope violation pre-empts the validate run" early-return (and "not gated by `dry_run`") is untested at the CLI level. Test: drive `_run`, assert the gate fires before `engine.step` and independent of `dry_run`. |
| Reconciler fail-soft | P5 | med | The "one bad row doesn't block boot" resilience branch (`reconciler.py:266-275`) is unexercised — `StorageError` is never injected. A `continue`→re-raise regression aborts daemon boot. Test: inject a `StorageError`, assert boot continues + `storage_persistence_failures` is counted. |
| F1 partial-fill *(cross-ref)* | P6 | high | **Unpinned** — see the "Live partial-fill Trade-drop" row above; the fix's test must construct the canceled-with-`filled_amount>0` state the mock can't currently produce. |

**Narrows (fails-safe or defensible — record, don't rush):** P2 intra-tick *ordering* of the DMS
pet is unpinned (frequency + the disarm gate are pinned); P4 per-coin *exposure* cap isn't asserted
symbol-scoped (a global-scope regression fails *safe*); P8 compound-failure redundancy + the layer-6
`current_balance is None` execute branch are unasserted (no wrong money movement under single-failure
inputs); P11 cross-symbol offside isolation on the non-raising parked path is untested.

**Well-pinned (no action):** P1 shutdown cancel-all, P2 DMS arm-each-tick, P4 caps enforcement, P8
harvester 7 layers, P10 auto-apply bounds (news firewall + cap direction verified by simulation),
P12 OpenOrders global-fetch — each with concrete failing-assertion coverage of its plausible
regressions.

---

## P2 — Data-infrastructure spine (post-tag, strict order)

**Goal:** build the shared data substrate in strict dependency order. **Everything here stays
read-only / recommend-only / operator-approval-only — nothing wires TA into autonomous
`cli/live` decisions** (that crosses into the parked regime track; ADR-002 intact).

Order is non-negotiable: **backfill ergonomics → import → OHLC+TA → consumers.**

| Slice | Effort | Value | Safety | Notes |
|---|---|---|---|---|
| `cli/observe --backfill` ergonomics + scenario catalog | M | med | | `--days`, `--catchup/--since=auto`, `--rate-limit-seconds`, progress, `--resume`, `--intervals`. Rides the shared write path; justified now because the import *is* the bulk-seed event. |
| **Import local Kraken historical dump** | M | med | | **Must precede consumers.** Stream the on-disk 2013–2025 + 2026Q1 dump into `ohlc_bars`/`price_snapshots` via the idempotent `save_ohlc_bars` path; reuse the `grid_backtest.py` CSV parser + altname map. $0, offline. |
| **OHLCBar integrity validator** | S | med | ⚠️ | **Must land WITH the import (above), not after.** `OHLCBar` has `ge=0` per field but no `low<=open/close<=high` / `low<=high` check — a garbled wire response or malformed 2013-era CSV row persists permanently via `INSERT OR IGNORE`, then every TA indicator (ATR/Bollinger/Stochastic key off high/low) propagates the corruption to advisor + auditor. One `@model_validator`; gates the entire spine. |
| **Proper OHLC + TA indicators** | L | **high** | | **Pivotal shared input.** RSI/MACD/Bollinger/MAs/ATR/ADX/Stochastic in `metrics_service` → `PerformanceSummary`. Single highest-leverage advisor candidate. Do NOT wire into `cli/live`. |
| Auditor — **config-replay half** | L | high | | Replay `settings.yml` over historical bars → fills/fees/PnL/drawdown/cycle-completion (`AuditorExchangeAdapter` over `GridEngine`). Operator's first use case (validate v1.0 config tweaks vs the soak period). Rec-scoring half → P4. |
| `cli/screener` — symbol-opportunity scanner | L | med | | Rank Kraken pairs by grid-suitability (vol, spread-vs-fee, volume, range-vs-trend, correlation). Operator gates every add (ADR-002). TA-based first cut; regime refinement parked. |
| Configurable counter-order target | M–L | med-high | ⚠️ | `GridConfig` modes `spacing_up` (default) + `top_sell`. Own ADR; auto-apply treats it as operator-approval-only. The advisor-picks-by-regime *adaptive* mode is parked. |

### P2 resolved blueprints (feature-dev ×7 agents + adversarial judge, 2026-06-03)

Design-ahead pass on the five design-worthy P2 items: a shared exploration → a **spine** architect (which *published the contract*) → three **consumer** architects designing against that fixed contract → a surgical adversarial judge on the auditor's one real-correctness fork. All gate-blocked (P2 is post-P1); this records the resolved design so the build has a spec.

**THE PUBLISHED CONTRACT** (the keystone — every consumer reads through it; build it ONCE in the spine):
- `StoragePort.get_ohlc_bars(symbol, interval_minutes, *, start_time=None, end_time=None, limit=None) -> list[OHLCBar]` (ASC, `[]` on miss). **Absent today** — only `save_ohlc_bars` exists; this read-side is the gate.
- `services/ta_metrics.py` (new module, not in `metrics.py`): `compute_rsi/macd/bollinger/sma/ema/atr/adx/stochastic(bars) -> float|None`; compounds return frozen `MACDResult`/`BollingerResult`/`StochasticResult`; **private `_compute_*_series` helpers** are how the auditor/screener get full series.
- 16 new `PerformanceSummary` TA fields (`float|None`).

**Spine (import + OHLC/TA + validator):** import is a **standalone `tools/import_kraken_history.py`** (not `backfill_range`, which fetches live), reusing the `grid_backtest._load_ohlc` 7-col parser + altname map (`XBT↔BTC`, `XDG↔DOGE`); validator (`@model_validator` on `OHLCBar`: `low<=open/close<=high`) folds into the import and uses **skip-and-log** on rejected legacy 2013 rows (fail-hard would abort an hours-long import on one bad row); **synthesize `price_snapshots`** from the import (bootstraps the existing vol/drawdown metrics); 60-min default interval; TA is **advisor-only, never wired into `cli/live`**. Build order: validator + `get_ohlc_bars` → import → TA.

**Auditor (config-replay):** `AuditorExchangeAdapter` replays historical bars through the **real `GridEngine`** (exercises production caps/offside/counter logic — the authoritative "what would my engine have done"). `:memory:` SQLite, **per-symbol** fresh instances, `tools/auditor.py`. Feed a 4-price sequence per bar (`open→low→high→close`) to recover intra-bar fills. **⚠️ Judge-found corrections (these are load-bearing):**
1. **Neuter `max_daily_spend_usd` for replay** (or override `_check_safety` to use bar-time, not `datetime.now(UTC)`) — else the daily cap exhausts after the first wall-clock "day" and **refuses every subsequent BUY for the rest of the replay → a silent near-zero-activity result that looks like "your config is ultra-conservative."** Decisive.
2. **Override `place_order` to suppress its on-placement immediate-fill** — the inherited mock fills a counter in the *same* bar if `close` already crosses it (over-counts cycles/fees). Negligible at 1m bars, **material at 1h/4h** — so the auditor is `_Sim`-equivalent only at 1m granularity.
3. **Warm-start the anchor at bar-0 *open*** (engine `_initialize` anchors to `close`; `_Sim` uses open → grid levels diverge for the whole replay).
   Confirmed sound: fills happen at the **order's limit price** (`_fill_order` discards the trigger price ✓). Dropped from the spec: the low-vs-high ordering rationale (false precision — fills within a bar are order-independent since counters defer to the next tick). Honest caveat: the audit is **directional, not exact**.

**Screener (`cli/screener`):** ships in **three phased cuts by dependency** — v1 (price-snapshot vol/flatness + ATR from the spine), v1.5 (+spread/volume when P1's `get_ticker` lands), v2 (+RSI/ADX/BB). Rank-based composite; **volatility scored as distance-from-band-center** (too low = no cycles, too high = caps trip — not monotonic); **correlation is a post-score *annotation*, not a factor** (Pearson from scratch). **No ADR, no DB table** in P2 (read-only one-shot, log-table output; `cli/status` template, READER key; new `ScreenerConfig`). Honest caveat: correlation needs observed history → mostly `n/a` for novel candidates until `cli/observe` is broadened.

**Counter-target:** `top_sell` is **asymmetric** — only the BUY-fill counter changes (→ SELL at `grid_ceiling = levels[-1].price`); SELL-fill counter unchanged. (Symmetric `bottom_buy` rejected — no operator demand.) `counter_target_mode` field on `GridLevels`, **read each tick, NOT anchored** in `GridState` (change without re-anchoring). Auto-apply exclusion is **automatic** (non-numeric → not in `_WHITELISTED_NUMERIC_KEYS` → rejected) — just a doc comment + a pin test. `cycle_matcher` unaffected (pairs by amount). Honest trade-off: fewer/larger cycles + **inventory-accumulation risk in a grinding downtrend** (SELLs cluster at the ceiling, don't fill until full recovery).

**Cross-cutting:** the contract-first staging worked — all three consumers independently specified the *same* `get_ohlc_bars` signature. **ADR numbering (RESOLVED 2026-06-05):** the placeholder "ADR-022" collision is assigned to one global sequence — **P1** = ADR-023 (reconciler/F1), 024 (cool-down), 025 (spread guard), 026 (harvester replay guard), 027 (rate-limit backoff) + an **ADR-007 amendment** (structural news firewall); **P2** = ADR-028 (auditor), 029 (counter-target); **P3** = ADR-030 (engine-state table), 031 (re-anchor command). ADR-023–031 + the ADR-007 amendment are all **written** in `decisions.md` (2026-06-05). The surgical judge re-earned the pattern: it caught the daily-cap bug that would have invalidated the operator's first audit.

---

## P3 — Ops / observability / UX (post-tag, parallelizable with P2)

**Goal:** harden the surfaces while the advisor cluster waits on its data window. All mutating
web/Discord actions keep the `pending_commands` two-click confirm (ADR-002 firewall).

The **re-anchor chain** is one ordered unblock: command → banner button + snooze → state-aware
pause/resume (all share an `engine_state` table for engine→web visibility). The **anomaly
detector** needs ~30d of baseline, so it tails the phase.

| Slice | Effort | Value | Safety | Notes |
|---|---|---|---|---|
| LLM health check on `/health` | M | med-high | | Ping each configured endpoint on a TTL cache; reuse `warmup()`. Independent. |
| Ollama hang-detection audit | S | med | | Confirm a hung Ollama can't block the event loop across `advise`/`operator`. Before Phase 9 raises LLM volume. |
| Docker HEALTHCHECKs on the 8 services | S | med | | None of the 8 containers has a `HEALTHCHECK`, so a wedged-but-alive daemon (stuck socket, blocked Ollama, deadlocked aiosqlite) shows green in Portainer forever; the in-app `/health` is pull-only. `HEALTHCHECK CMD curl -f localhost:8000/health` for web + a heartbeat-freshness `tools/healthcheck.py` for the daemons (reads `daemon_heartbeats`). The in-app anomaly detector does NOT substitute — it assumes the loop is still running. |
| **Logging-quality audit + enrichment pass** *(operator-flagged 2026-06-03)* | M | med-high | | App-wide sweep so every log line is self-explanatory (what/which/how-much in the message string + `extra=`), severities accurate. Soak proof: the live log showed bare `grid fill` lines with no side/price while the DB-backed dashboard showed the real fills + closed profitable cycles — the tail is an unreliable activity view. Audit-and-enrich, NOT a rewrite; umbrella over the partial-grid `WARN→INFO` item (`engine.md`) + the `grid fill` detail gap. Detail in `observability.md`. |
| Operator command catalog SSOT | S | med | | Schema-drift test resolving `_HELP_ENTRIES` ↔ `operator.md` drift. Wire when the catalog gains the next command (the re-anchor command). |
| **Operator-initiated re-anchor command** | M | med | ⚠️ | Root of the chain. **Blueprint 2026-06-03** (see "P3 resolved blueprints"): resolved to an **in-process** `ReanchorCommand` (cancel-FIRST → `save_grid_state` new anchor → engine re-lays), NOT the SIGINT+restart sketch (restart bounces the DMS). ⚠️ judge correction A: `request_reanchor` must place the layout **in-process**, not rely on the next-tick re-layout gate (else offside-at-new-anchor parks with zero orders, silently). Needs **two** ADRs (engine_state table + re-anchor command). |
| Re-anchor banner action button + snooze | M–L | med-high | ⚠️ | "Re-anchor" + "Snooze 24h" (`reanchor_snoozes`) + projected-loss line on the info banner. Auto-cancellation **rejected**. **Blueprint 2026-06-03:** snooze is UI-local (NOT a firewall mutation); projected-loss = **fee-only** (paper-loss-on-stranded rejected as misleading); reads the `engine_state` keystone. |
| State-aware per-symbol pause/resume buttons | M | low-med | ⚠️ | Render only contextually-relevant actions; needs `engine_state` (`cli/live` writes, `cli/web` reads). **Blueprint 2026-06-03:** consumes the `engine_state` keystone (see "P3 resolved blueprints"); offside is a **badge, not a button**; safe default = show pause when state absent/stale. |
| Web UI per-entity action buttons | L | high | ⚠️ | Apply/Execute/Approve/Acknowledge/Reject on review queues via `pending_commands`. Surfaced soak Day 2 (CLI roundtrip friction). **Blueprint 2026-06-03:** `ExecuteProposalCommand` dispatched **only by `cli/harvest`** (ADR-003). 🛑 **Two judge DECISIVE bugs** (see "P3 resolved blueprints"): (F) web-Execute is a **HARD co-requisite of the P1 Harvester replay guard** — double-withdrawal without it (UNIQUE on `transfer_results.proposal_id`); (E) each daemon must SELECT **only its own `command_kind`** (no atomic claim today → double-dispatch / silent-kill of approved withdrawals). Drop `cli/apply --daemon` (settings.yml doesn't hot-reload) — operator runs the one-shot. |
| Discord confirmation UX: buttons over reactions | M | med | ⚠️ | `discord.ui.View` Approve/Reject; removes `pending_message_map` (also drops its grow-only in-memory leak — deep-scan F7c; interim 1-line `.pop()` in `_handle_reaction` if it bites first); firewall intact. **Blueprint 2026-06-03:** `_ConfirmView` with **`interaction_check`** → `allowed_user_ids` (skipping = a firewall regression); `view.stop()` blocks double-approve; build the View in `cli/operator`, pass to a storage-free transport `send_embed_with_view`. |
| Notifications: server-side read-state + deep linking | M–L | low-med | | `read_at` column + read/read-all endpoints (cross-device badge) + deep links. Migration first, deep-linking second. |
| Today's PnL — split realization-day from earning-day | M | med | | Distinguish normal-grid from fallback (long-hold/re-anchor) cycles; add `pairing_method`/`hold_duration`. |
| **Buying-power / account card + per-symbol held inventory** | M | med-high | ◐ | A top "Buying Power" card (total account value + **free USD available-to-buy** + held-asset breakdown) above Trading Status, plus **per-symbol held inventory** in each symbol card. Two-sided framing makes flat-start `insufficient balance` refusals self-explanatory. **Source: `balance_snapshots` in `observe.db`** (web tier stays credential-free per ADR-016/017). **◐ AGGREGATE DONE 2026-06-03 (v1.1):** the top **scoreboard strip** shipped — account value + free USD + in-positions + today/lifetime PnL, from `observe.db` balance snapshots with an "as of HH:MM" stamp, held inventory valued via the observed prices. **Remaining:** the **per-symbol held inventory inside each symbol card** (the per-coin two-sided framing). |
| Lifetime / cumulative net-PnL number | S | med | ✅ | Only `today_realized_pnl()` existed — the cost card had all-time *fees* but no matching all-time *net PnL*. "Have I earned anything, ever?" **✅ DONE 2026-06-03 (v1.1):** lifetime realized PnL (sum of every matched cycle's net_pnl over the full trade history) shipped in the dashboard scoreboard strip. |
| Status card: per-order delta column | S | low-med | ✅ | **✅ DONE 2026-06-03 (v1.1):** a "vs mkt" column on each per-symbol open-orders table — signed-% distance from current market (`(cur-order)/order×100`, `%+.2f`), "—" when no price. Template-only (data already in `current_prices`); render test. |
| **Symbol-card price for parked/no-order symbols** *(operator-flagged 2026-06-03)* | S | med | ✅ | Per-symbol card header showed price + ▼/▲ only when the symbol had open orders; a parked symbol (BTC offside) rendered a bare `BTC/USD`. **✅ DONE 2026-06-03 (v1.1):** the snapshot now fetches prices for every symbol that renders a card (orders ∪ recent trades ∪ held bases), so a parked symbol shows its price + trend. Root cause was the price-fetch set, not the template. |
| **Mode-parameterized webui (reuse for live + shadow)** *(operator decision 2026-06-03)* | S–M | med | | Serve the SAME dashboard for all modes; switch the data source by mode (live vs `cli/shadow` ledger). DRY — the UI is already mode-agnostic. **✅ Badge-flip slice DONE 2026-06-03 (v1.1):** the badge reads `application.mode` (`live\|shadow\|sandbox`) — the **single** deployment-mode source — via the `trading_mode` Jinja global → `mode-badge` (`_status_card.html`); `cli/web` passes `config.application.mode` to `create_app`; tests cover all renders; CSS ships all three variants. (Reworked from a redundant `web.mode` knob per operator 2026-06-03 — one mode source, not two.) **Remaining:** data-source / mode-selection plumbing (point the loaders at the shadow ledger; likely a 2nd `cli/web` instance per `cli/up shadow`). Supersedes the "separate shadow page" code comments. Detail in `operator-ux.md`. |
| **Notification level-color inconsistency** *(review 2026-06-03, DEFECT)* | S | med | ✅ | `info` rendered blue on `notifications.html:39` but green (`ok`) on `history.html:82` — same notification, different color across pages. **✅ FIXED 2026-06-03** (`history.html` info → `info`/blue). |
| **Responsive / mobile CSS pass** *(review 2026-06-03)* | M | med | ✅ | `base.css` had NO `@media` except `prefers-color-scheme` — tables overflowed + navbar crowded on a phone. **✅ DONE 2026-06-03 (v1.1, minimal):** `@media (max-width:700px)` — cards scroll wide tables, primary nav scrolls sideways, padding/scoreboard/emergency-stop tighten. Usable on mobile (not a hamburger nav). |
| **Whole-UI design-review punch list** *(review 2026-06-03)* | — | — | | Consolidated frontend-design findings in `operator-ux.md`: dashboard scoreboard strip, per-symbol grid-band sparklines, `/cost` bar chart, fill-flash + Kraken-style fill toast, advisor collapse, typography/brand elevation, news-coin tags, emergency-confirm weight, CSS-debt cleanups. Tiered defect→elevation. |
| Status card: recent-fills enhancement | S | low | | Per-row age + summary stats; template-only. |
| Discord `status_report` tally compactness | S | low | | `inline=True` / single table; cosmetic. |
| **Bespoke notification-card renderers (proactive push embeds)** *(operator-flagged 2026-06-03; design 2026-06-03 via feature-dev)* | M | med | | Give the 7 real proactive events (`session_start`/`fill`/`loss_cap`/`session_end`/`harvest_proposal`/`withdrawal_failed`/`withdrawal_submitted`) the per-event embed treatment the v1.0 *query* responses got. Today all share one generic path (`_forward_pending_notifications` → title+message+`_render_context_fields` dict-dump+4-bucket color). **Recommended = Approach B (typed `NotificationEvent` union):** new `ports/notification_events.py` + `services/notification_embed_render.py` mirroring `QueryResult`/`discord_embed_render` (10 renderers, `match` over typed models, 35-test suite); event serializes into the **existing** `context_json` column via the `_COMMAND_ADAPTER` round-trip — **no schema migration, soak-safe**; old rows fall back to the legacy path. (`cycle_close`/`offside`/`dms_trip` aren't notifications today — those would be new raise sites.) Batch with tally-compactness + buttons-over-reactions. Full blueprint in `operator-ux.md`. |
| **Anomaly detector daemon** *(needs ~30d baseline)* | L | high | ⚠️ | `cli/anomaly`: deterministic Z-score/IQR cross-DB outlier watcher vs the operator's own baseline. Tails the phase once the clock matures. |
| └ Disk-space awareness *(needs data-retention first)* | S | low-med | | `shutil.disk_usage` warn/critical; bundles onto the anomaly daemon. |

### P3 resolved blueprints (feature-dev ×7 agents + adversarial judge, 2026-06-03)

Design-ahead pass on the three design-worthy P3 clusters: a shared 3-explorer sweep → three
consumer architects (re-anchor chain / action surfaces / observability) → a **surgical adversarial
judge** on the two genuinely-contested safety forks. All gate-blocked (P3 is post-tag, parallel to
P2). **The judge found two DECISIVE bugs** — what follows is the *corrected* design; build to this,
not to the un-annotated architect drafts.

**THE KEYSTONE — `engine_state` table** (engine→web per-symbol visibility; the re-anchor chain's
shared unblock):
- New `engine_state` table in **operator.db** (per-symbol `paused, offside, offside_ticks,
  reference_price, anchored_at, updated_at`, PK `(base,quote)`). `cli/live` upserts one row/symbol/tick,
  **best-effort** like `emit_heartbeat` (swallow `StorageError`) via a new `emit_engine_state`
  (`cli/_common.py`). `StoragePort.save_engine_state`/`get_engine_states`; `EngineStateRow` frozen
  dataclass (`domain/engine_state.py`). `StatusSnapshot` gains `engine_states`; the template applies a
  **freshness guard** (drop rows older than ~3 ticks → fall back to the safe "show pause" default).
  Closes the documented `cli/operator.py:295-298` "web sees all symbols active" gap. Per-tick cost = one
  extra local SQLite read/symbol — trivial (judge claim D CONFIRMED-SOUND).

**Re-anchor chain (4 items, one ordered unblock):**
- **In-process command, NOT SIGINT+restart** (resolved fork): `ReanchorCommand` (drops into the
  existing firewall with zero machinery change) → `_dispatch_reanchor` → new
  `GridEngine.request_reanchor(symbol)` under the per-symbol lock: **cancel-FIRST** (abort + `(False,msg)`
  if any cancel fails — never save a new anchor over live orders), then
  `save_grid_state(reference_price=current_price)`, clear `_offside_ticks`, auto-resume if paused.
  Restart rejected (bounces the DMS; non-atomic delete+restart strictly worse). Own ADR + a regression
  test pinning **`save_grid_state` is NOT called when `failed>0`**.
  - ⚠️ **Judge correction A (CORRECTION-NEEDED):** the architect leaned on the next-tick auto-re-layout
    (`grid_engine.py:407`, storage-driven `get_open_orders`) to place the new grid. It fires correctly
    *within the same tick* (dispatch precedes `engine.step`; storage already shows the canceled orders +
    new anchor) — **except** the re-layout block sits inside `if not offside:` (`:376`): if price moved
    past the band between the price-fetch and the tick, the engine **parks with zero live orders,
    silently**, right after the operator explicitly re-anchored. Fix: **`request_reanchor` places the
    initial layout itself** (in-process), not via the next-tick gate. (Fallback if not: document
    "re-anchor while offside leaves you parked.")
- **Banner action button + snooze:** `reanchor_snoozes` table (operator.db) + a snooze filter in
  `_compute_reanchor_recommendations`; `POST /commands/reanchor` (through pending_commands) +
  `POST /commands/snooze-reanchor` (**UI-local, NOT a firewall mutation** — snoozing a banner moves no
  money). **Projected-loss line = fee-only** (taker fee on open-order notional + re-lay) — honest at $100;
  paper-loss-on-stranded-inventory rejected (the asset isn't sold on cancel, so "loss" would mislead).
- **State-aware pause/resume:** template reads `engine_states.paused`; renders one icon; offside shown as
  a **badge not a button** (offside is engine state — the operator's lever against it is re-anchor); safe
  default = show pause when state absent/stale.

**Action surfaces (web buttons + Discord View) — the judge's decisive territory:**
- **Web per-entity buttons** (Apply/Execute/Approve/Acknowledge/Reject through pending_commands): new
  `ApplySuggestionCommand`/`ExecuteProposalCommand` variants. **Power-fragmentation resolved (ADR-003):**
  `ExecuteProposalCommand` is dispatched **only by `cli/harvest`** (the sole process holding the Harvester
  key) — `cli/live` must never dispatch it. Acknowledge skips pending_commands (passive read-marker, no
  engine/money path).
  - 🛑 **Judge DECISIVE-BUG F (double-withdrawal — highest blast radius):** routing Execute through
    web→pending_commands→cli/harvest-poll lets **two approved rows for the same `proposal_id`** (two
    clicks, or a web row + a manual `--execute`) both clear all 7 harvest layers and call `withdraw()`
    twice — `_execute_command` has **no `proposal_id` idempotency check** and the day-cap re-query races.
    **Web-Execute is a HARD co-requisite of the P1 "Harvester `--execute` replay guard"** — ship them
    together. Fix: a **UNIQUE constraint on `transfer_results.proposal_id`** (DB-enforced,
    concurrency-proof) + an app-layer pre-check before layer 7. Do NOT ship web-Execute without it.
  - 🛑 **Judge DECISIVE-BUG E (multi-daemon double-dispatch / silent-kill):**
    `cli/live._process_pending_commands` does a plain SELECT-then-separate-UPSERT — **no atomic claim**.
    With 3 daemons polling `WHERE status='approved'`, two can grab the same row; worse, cli/live hitting an
    `ExecuteProposalCommand` raises `OperatorError`→marks it **`failed` before cli/harvest sees it**,
    silently killing an operator-approved withdrawal. Fix: **each daemon SELECTs only its own
    `command_kind` set at the query level** (cli/live = the 6 trading commands; cli/harvest =
    `execute_proposal`) — the filter is the gate, NOT a `match`-case fallthrough.
  - **Judge CORRECTION G:** the architect's `cli/apply --daemon` is over-engineering — `settings.yml`
    writes don't hot-reload into the running `cli/live` session, so a polling apply-daemon is
    cosmetic-at-best/confusing-at-worst. **Drop it:** web writes the `ApplySuggestionCommand` pending row;
    the operator runs the existing one-shot `cli/apply --commit` to act.
- **Discord buttons over reactions:** `_ConfirmView(discord.ui.View)` with **`interaction_check`** gating
  to `allowed_user_ids` (skipping it = a firewall regression on top of ADR-002); `view.stop()` after click
  (no double-approve); deletes `_handle_reaction` + the `pending_message_map` grow-only leak (deep-scan
  F7c); firewall intact (button writes `status='approved'`; cli/live's poll stays the only engine path).
  Build the View in `cli/operator`, pass it to a storage-free transport `send_embed_with_view`. 5 reaction
  tests → `_ConfirmView` tests via a `FakeInteraction` fixture.
- **Notifications read-state + deep linking:** `read_at` column (separate from Discord `forwarded`) via a
  guarded `ALTER TABLE ADD COLUMN`; `mark_notification_read`/`mark_all`/`count_unread`; bell badge →
  server-side `GET /notifications/unread-count`; deep-linking (phase 2) adds `link_path`/`link_query` per
  raise site. Migration-first.

**Observability cluster (no contested fork — fail-soft, read-only; no judge pass needed):**
- **LLM health on `/health`:** `services/llm_health.py` mirrors `kraken_health.py` (TTL-cache, raw `httpx`
  probe — **no adapter import**, no billing): probe Ollama for real (`GET /api/tags`), represent cloud via
  the free `GET /v1/models` (200=ok / 401=unauthorized / missing-env=not-configured). Discover endpoints
  from advisor + MoE experts + arbitrator + operator assistant (dedup `(provider,model)`); new "LLM
  Endpoints" card; folds into `compute_overall_status`.
- **Docker HEALTHCHECKs:** one `tools/healthcheck.py` (`--mode web` → curl `/health/overall.json`, exit 1
  only on red/unreachable; `--daemon-name X --operator-db PATH` → read `daemon_heartbeats` mode=ro vs
  `DaemonHealthThresholds` defaults). Per-service `healthcheck:` on all 8 (+anomaly); no daemon code change.
- **Anomaly detector `cli/anomaly`** *(needs ~30d baseline; cold-start suppressed via `min_baseline_rows`)*:
  `services/anomaly_detector.py` = a fail-soft detector registry (Z-score/IQR, no numpy), 6 detectors
  (trade-fee / cancel-rate / LLM-cost / advise-output-gap / balance-drop / disk-space); `cli/anomaly`
  mirrors `cli/news`; **anomalies emit `Notification(level=warning)` rows** (reuse the existing Discord
  forwarder — no new table/route; recorded as a ratified decision) with a dedup window. No ADR.
- **Logging-quality sweep** (umbrella): per-module bisectable commits enriching the message string (the
  bare `"grid fill"` → `grid fill: buy ETH/USD 0.005 @ 3421.50 (kraken: …)`) + a `caplog` pin-test
  asserting fill/cap/offside lines carry their fields. Audit-and-enrich, not a rewrite.

**Cross-cutting:** the surgical judge re-earned the pattern decisively — **two money/state-corrupting bugs
(F double-withdrawal, E silent-kill/double-dispatch) that no single architect caught**, both born from the
multi-daemon `pending_commands` fan-out the action-surfaces design introduced. **ADR numbering:** the
re-anchor chain needs **two** ADRs (engine_state table + re-anchor command); assigned in the global
sequence (RESOLVED 2026-06-05) as **ADR-030** (engine-state visibility table) + **ADR-031** (operator
re-anchor command) — **written** in `decisions.md` (2026-06-05). See the P2 cross-cutting note.

---

## P4 — Advisor-feedback cluster (data-gated, 30–90d post-tag)

**Goal:** once enough applied-recommendation data exists to compute meaningful outcomes, build
the advisor feedback loop and the consumers it gates. **Schedulable only when the clock
matures**; until then it sits parked. Auto-action consumers remain firewalled behind their own
ADRs.

| Slice | Effort | Value | Notes |
|---|---|---|---|
| **Advisor outcome tracking** *(keystone)* | XL | high | `recommendation_outcomes` table + `advisor_evaluator` + per-model/per-role scoreboard. Needs 30–90d of applied-rec data **and an operator "success" definition**. |
| **Chaos Gremlin advisor** *(loose-reasoning, scored-not-applied)* | M | med-high | Standalone observe-and-score voice: same inputs, loose logic; own `gremlin` role in `_BLOCKED_ROLES`, **not** arbitrator-fed (avoids the `role="aggregated"` news-laundering hole). Emits a falsifiable directional call → cleanest first customer for the outcome ledger. **Role buildable in v1.1 with MoE-on for the 1.1 soak; scoreboard rides the keystone.** Detail in `adaptive-grid.md`. |
| Per-cycle LLM call tracing | S | low-med | `trace_id` on `llm_calls` + "by cycle" `/cost` toggle. **Shares the outcome-tracking migration — ship together.** |
| Auditor — rec-scoring half | M | high | Score past advisor recs vs realized outcomes. Needs the outcome ledger + the P2 config-replay half. |
| `weather_report` query | M–L | med | News + price-trend + advisor-suggestion summary over multi-day windows via `AssistantPort.summarize`. After `status_report` stabilizes. |
| `AssistantPort.summarize` cloud impls | M | low | Implement on Anthropic/OpenAI/Google (currently `NotImplementedError`); via the cost gate. Needed if `weather_report` runs on cloud. |
| LLM Historian *(90d+)* | XL | high | `cli/historian` synthesizing macro patterns → `historian_findings` + `/historian`. Read-only first; likely cloud long-context. |
| Data retention policy *(~6mo)* | M | high | Per-table retention + archive-then-delete. Gates the P3 disk-space awareness. |
| Daily summary (email / Discord DM) | M | low-med | "Yesterday in WobbleBot." Operator-demand. |
| Cost-honesty dashboard | M | med | Realized PnL beside fees + LLM spend + operator-declared infra (`cost_assumptions`) → net-vs-cost + annualized projection. |

---

## Recommended order

1. ✅ **Done:** dead man's switch (ADR-021).
2. **P0** (during soak, branch-only): four-homes audit **first** → schema-drift → preflight key-scope → incident runbook → retry-policy audit → *(refactors only if clean)*. No `main` merge.
3. 🚦 **GATE:** soak passes → tag v1.0 → merge P0 + dead-man's-switch → `main` unfreezes.
4. **P1**: reconciler fill-vs-cancel (ADR + test) → cool-down (ADR) → spread guard (ADR) → partial-grid WARN→INFO → backup smoke test → CSP → Kraken-status news → `cli/up` (if friction real) → footer indicator → more Kraken pairs.
5. **P2**: backfill ergonomics → import dump → OHLC+TA → auditor config-replay → screener → counter-order target.
6. **P3** (parallel to P2): LLM `/health` → Ollama-hang audit → catalog SSOT → re-anchor command → banner button+snooze → state-aware pause/resume → web action buttons → Discord UI buttons → notifications read-state → deep-linking → cosmetic leaves → *(~30d)* anomaly detector → disk-space awareness.
7. **P4** (data clock matures): outcome tracking + per-cycle tracing (one migration) → auditor rec-scoring → `weather_report` (+cloud summarize) → *(90d)* historian → *(~6mo)* data retention → daily summary + cost dashboard.
8. **Throughout:** pull parked items reactively **only** when their named trigger fires; never batch-build a parked cluster.

---

## Parked register

Externally-gated work. **Ship an item only when its named trigger fires** — never batch-build a
cluster. Full detail in the per-area docs.

### Auto-action cluster (needs P4 outcome data + own ADRs)
- **`cli/auto-tune` daemon** — operator demonstrates advisor trust + a no-value-for-checks use case + ADR removing the operator-trigger.
- **Auto-pause on news-role HIGH risk** — after the P4 evaluator + calibrated threshold + **ADR-002 ratified-with-exception**; consumes the P1 Kraken-status feed.
- **Confidence-driven grid extension** — post-P4 + regime detector + 60–90d data + own ADR; hard `max_extension_budget_usd`; no auto-apply.
- **Bot learning** (discussion stub) — after 60–90d of outcome data makes the shape choice data-informed (RL rejected).

### Regime / Oracle track (PARKED per 2026-05-30 — heuristic detection doesn't beat hold)
- **Market regime detector** — research produces detection that beats buy-and-hold **and** a 60–90d shadow-run validates it before any consumer wires into `cli/live`. Consumes OHLC+TA (P2).
- **Regime-aware grid modes** — detector live + shadow-run track record + outcome tracking + own ADR.
- **Counter-order target — adaptive mode** — detector live (the `spacing_up`/`top_sell` modes ship in P2).
- **Heuristic experts for risk/news/arbitrator** — MoE becomes production **or** an offline zero-cloud MoE is wanted.
- **Math-specialist LLM paths** — wire one host role at a time as its feature lands.
- **Reasoning-model support** — DROPPED 2026-05-26; re-open only on a promising new sub-7B reasoning model.

### Performance (soak/profile-triggered)
Storage caching layer · async query parallelism · batch save APIs · tick-latency alarming ·
WebSocket real-time updates · Kraken `SystemStatus` awareness · SQLite concurrency stress test →
`operator.db` `busy_timeout`+retry · mid-session reconciliation · `cli/reconcile` ledger diff ·
web-route poll-query aggregation (`/cost` 10k-trade rollup, `/news` two 1k scans — deep-scan F9;
defer the SQL-rollup rewrite until trade volume nears the cap).

### Harvester (`harvester.md`)
Reconciliation (soak drift evidence) · 8th defense layer (cumulative daily total) · top-up
deposits (bank→exchange — needs feasibility + new ADR + ADR-003 re-ratification for Deposit scope).

### Deployment / hosting / friend-onboarding
Always-on hosting topology (needs an ADR; gates SQLCipher) · **SQLCipher at rest** (needs hosting
ADR + the P0 four-homes verdict) · friend-deployment onboarding (Tier-0 runbook first) · first-run
admin wizard (Tier 1+) · multi-arch arm64 · configurable quote currency (EUR/GBP).

### Web/security extras
MFA (TOTP) · session cookie keyed by `user.id` · multi-operator auth · richer SVG charts ·
extra read-views · multi-coin status layout · Discord command shortcuts · multi-coin
`recalibrate` defaults · remote backup destinations · Prometheus export · PagerDuty/email/SMS
fallback · foreign-language support.

### CI / infra / vendor (`infrastructure.md`, `external-triggers.md`)
CI `make check` + wheel publish (contributor-triggered) · Kraken schema-drift coverage (pairs
with CI) · **LLM-provider-drift-watcher** (new models + pricing/API re-verify) · portainer-mcp
AutoUpdate flags (separate repo) · Python 3.14 compat · test-count growth · OpenClaw integration ·
news-source expansion (Messari/Reuters/stocks + publisher-attribution UI) · CryptoCompare 90-day
eval (**2026-08-13**) · Kraken API/fee change responses.

### Trading-scope (gated)
More exchange adapters · high-frequency memecoin grid · **margin (v1.2+)** & **futures (v1.3+)** —
gated out by the standing operator-experience rules; Claude pushes back if asked before the gates
clear. **Phase 9 (Kraken Securities equities)** is a committed *phase*, not a v1.1 item — pointer only.

### Already shipped (listed for reconciliation; no action)
Dead man's switch (ADR-021, 2026-06-02) · rate-limit batch fix + DMS disarm-on-failed-cancel fix
(`abf3aa6`) + its regression test (`8b25feb`) · DOGE ordermin $5→$6 workaround · graceful-shutdown
timeout (2026-05-23, into v1.0) · `cli/observe --backfill` substrate (2026-05-25) · Discord
response quality (2026-05-24, v1.0).

---

## Guardrails

1. **`main` = the v1.0 candidate (freeze lifted 2026-06-02):** the hardening + soak-hotfixes live on `main`, which advances with soak-surfaced *hotfixes only* during the soak — no speculative P1+ code until the tag (planning/doc work is open). Image rebuilds only on a push-to-`main` that touches a build-allowlist path (docs-only pushes don't rebuild); the deployed soak is pinned via the `IMAGE_TAG` stack env var, so a push doesn't auto-redeploy it.
2. **Advisory-only (ADR-002):** the LLM never executes trades or transfers. Auto-action features stay parked behind their own ADRs + accrued data; auto-pause needs an ADR-002 ratified-with-exception. `pending_commands WHERE status='approved'` stays the firewall on every mutation.
3. **Harvester sole transfer authority (ADR-003/004):** no `BankingPort`; trade key has no Withdraw; Withdraw lives only on the Harvester key. Top-up deposits parked behind a feasibility check + new ADR + ADR-003 re-ratification.
4. **Safety-critical facts stay code-resident:** the P0 audit keeps LLM pricing + Kraken fees in code; only non-safety facts may move, and only after the verdict.
5. **Margin/futures gated out** by the standing operator-experience rule; the soak going well is not a sufficient signal.
6. **Kraken-UI nudges declined** on the architectural reason (stop-loss anti-grid, take-profit redundant, staking custody-risk, conditional/OCO/iceberg unused).
7. **Sequencing constraints are non-negotiable** (audit→migration; import→consumers; OHLC+TA→regime/auditor/screener/target/historian; regime shadow-run before wiring).
8. **Trigger discipline:** parked items ship only when their trigger fires; no speculative scaffolding. Solo learning project, $100 test capital, not income — no over-engineering.
9. **Per-commit hygiene:** tests pass + lint clean (pylint 10.00, mypy strict, black/isort) before *each* commit; one finding-category per commit; engine-safety changes carry a test-for-the-bug.
10. **Docs-with-code:** commit STATUS/CHANGELOG/ADR receipts *with* the code in one push (the double-bounce rule); roadmap stays the single status source — no new `STAGE-*-COMPLETE` markers.
11. **WIP limit:** finish a phase before opening the next.

---

## Open questions

Resolve as we reach each phase; the plan stands without them, but a few shape it.

1. **Soak exit criteria** — ✅ **DEFINED 2026-06-02** (see the GATE above): engine-coverage + reconciliation-across-restarts + ≥1 of each daemon cycle + no hard-stops; profit/BTC-direction NOT a criterion. Open sub-question: the minimum *duration* (the multi-coin restart targets ~1 month).
2. **"Success" definition** for advisor outcome tracking (P4 keystone) — fill-cadence delta? realized-PnL? cap-trip avoidance? operator-regret?
3. **Four-homes migration scope** — after the audit, move approved facts in one wave or one at a time? (Bounds whether SQLCipher is in v1.1's horizon.)
4. **Branch-only refactor appetite** (P0 #6/#7) — pull into the soak window only if clean, or leave parked?
5. **`cli/up` priority** — promote in P1 only if real restart friction, else let it sink.
6. **More-Kraken-pairs activation** — which coins, what split of the $100?
7. **Auditor split** — confirm shipping the config-replay half in P2, deferring rec-scoring to P4.
8. **Hosting topology** — is the NAS the committed host, or is the laptop-decoupling ADR still open? (Gates SQLCipher; informs where Ollama lives.)
