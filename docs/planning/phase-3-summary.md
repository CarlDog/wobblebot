# Phase 3 — Closing Summary

**Status: ✅ Complete (2026-05-15).** Eight Phase 3 stages closed in one
sustained session (3.0 / 3.1 / 3.2 / 3.2.5 / 3.3 / 3.4a / 3.4b / 3.5) on
top of the config-consolidation audit that landed 2026-05-14. The
advisor surface ships end-to-end: data collection → news ingestion →
single-LLM and MoE advisor → operator-in-the-loop auto-apply gate.
**Phase 3 added zero real-money risk over Phase 2.** Per ADR-002 +
ADR-007 the advisor cannot execute trades; per ADR-012 the auto-apply
gate is operator-triggered (`cli/apply --commit`) and defaults off.
Total real-money cost across the project still **$0.08** (unchanged
from Phase 2 close).

This document is the Stage 3.5 deliverable per the roadmap's
"advisor-in-the-loop run" charter. Consolidates the per-stage receipts,
the end-to-end chain verification, and the entry conditions for
Phase 4 (Harvester & treasury management).

## Per-stage outcomes

| Stage | Closed | Slices | Verification |
|---|---|---|---|
| 3.0 Observer & Shadow Mode | 2026-05-14 | 2 (per ADR-008) | `cli/observe` polling live Kraken; `cli/shadow` running the live engine against synthetic balances. Day-1 observation captured ~600 snapshots, no daemon deaths. |
| Config consolidation audit | 2026-05-14 | 8 | Schema-drift tests green; every CLI loads YAML via `load_resolved_config(...)`; profiles deep-merge correctly; `--profile moe-advisor` resolved end-to-end. |
| 3.1 Data Collector & Metrics v2 | 2026-05-15 | 4 | `services/metrics.py` pure functions verified against `cli/observe`'s 600+ price tape (volatility, max_drawdown, flatness, cycle_stats); `tools/show_metrics.py` printed numbers consistent with the operator's mental model of the window. |
| 3.2 Advisor Port + single-LLM Ollama | 2026-05-15 | 5 | Six local Ollama models surveyed on the same BTC/USD 6h window; five converged on `spacing_percentage: 1.2`. Slice E thinking-model detection landed after `deepseek-r1:14b` returned `{}` under `format: "json"`. |
| 3.2.5 News Ingestion | 2026-05-15 | 5 | 131 items pulled in one poll across all four sources (CoinDesk 25 + Decrypt 37 + The Block 19 + CryptoCompare 50). Pivot from paid (CryptoPanic + Whale-alert) to free codified in ADR-010. |
| 3.3 Passive Advisory Workflow | 2026-05-15 | 4.5 | `cli/advise` ran a real cycle against the operator's observe + news DBs → phi4:14b-q8_0 emitted `{spacing 1.1, levels±4}` in ~50s → `tools/show_suggestions.py` printed it back cleanly. |
| 3.4a Mixture of Experts | 2026-05-15 | 4 | Live MoE with three local Ollama experts (phi4 quant + granite4.1 risk + deepseek-r1 news + phi4 arbitrator): `weighted_confidence` aggregated `spacing 1.29% / order $8 / levels±4` in 194s; `arbitrator` aggregated `spacing 1.4% / order $9` in 191s citing ADR-007's news auto-apply restriction in its rationale. |
| 3.4b Bounded Auto-Tuning Gate | 2026-05-15 | 3 | `cli/apply` against operator's real advise.db correctly surfaced the latest BTC suggestion and rejected every key with "auto-apply disabled" — gate default-off posture holds end-to-end through the CLI. |
| 3.5 Phase 3 Integration Check | 2026-05-15 | 1 (this doc) | End-to-end chain verified: 6520 price snapshots → 131 news items → fresh advisor cycle (39s, news-aware) → cli/apply gate output. |

## Integration-check receipts (Stage 3.5, 2026-05-15)

The chain runs left-to-right; each link reads what the previous one
wrote. Numbers below are the live state at audit time.

### Link 1 — `cli/observe` → `price_snapshots`

- **Runtime:** ~24 hours background-running PID 44820 (overnight soak).
- **Symbols:** BTC/USD, ETH/USD, DOGE/USD (30s polling cadence per
  `schedules.observe_prices`).
- **Snapshots:** 6520 rows in `data/wobblebot-observe.db`.
- **Balances:** 110 balance snapshots (10min cadence per
  `schedules.observe_balances`) covering 220 individual asset balances.
- **No daemon deaths.** JSON logs in `data/observe-overnight.err` show
  continuous "price snapshot saved" entries up to 23:57 UTC.

### Link 2 — `cli/news` → `news_items`

- **Runtime:** one manual poll cycle for the integration check
  (the daemon's 30min cadence isn't load-bearing for verification).
- **Items pulled:** 131 in one cycle, matching the Stage 3.2.5
  closing-test receipt to the row:
  - CryptoCompare: 50
  - rss:coindesk: 25
  - rss:decrypt: 37
  - rss:theblock: 19
- **Dedup:** `UNIQUE(source, external_id)` enforced; second poll on
  the same window would be a no-op at the storage layer.

### Link 3 — `cli/advise` → `advisor_suggestions`

- **Runtime:** one cycle for the integration check (39s wall-clock).
- **Advisor mode:** `type=single` (operator's settings.yml default);
  model `phi4:14b-q8_0`.
- **Summary fed in:** Stage 3.1 metrics (`volatility=0.000201`,
  `flatness=0.9927`, derived from the 6h window of the observe tape)
  PLUS `recent_news=20` items (narrowed `NewsItemSummary` view from
  the news DB).
- **Output:** `{spacing_percentage: 1.1, levels_above: 4, levels_below: 4}`,
  confidence `medium`, role `quant`.
- **Calibration observation:** the same proposed params dropped from
  `confidence=high` (previous cycle, news context absent) to
  `confidence=medium` (this cycle, news context present). The news
  expert's contribution to confidence calibration showed up even when
  the parameter recommendation held — exactly the behavior the news
  inclusion was meant to deliver.

### Link 4 — `cli/apply` (dry-run) → operator review

- **Suggestion picked:** newest in `advise.db` (the
  just-produced one).
- **Gate outcome:** all three proposed keys rejected with reason
  "auto-apply disabled" (`AutoApplyConfig.enabled=False`, operator
  default per ADR-012). No settings.yml write. No audit row.
- **Exit code:** 0 (dry-run completed cleanly; gate's
  default-off posture is the load-bearing safety property and it
  fired correctly).

### What the chain demonstrates

- **No-money-touching observability** (Link 1) produces enough signal
  for the advisor (Link 3) to reason about.
- **News context is wired through** (Link 2 + Link 3) without being
  load-bearing — the advisor still produces a recommendation when
  news.db is empty (degraded mode), and gracefully includes news
  when it's present.
- **Operator-in-the-loop is preserved** (Link 4): even with the
  full chain working, nothing mutates running config without
  explicit `--commit` AND the operator having flipped
  `auto_apply.enabled=True`.

## Live MoE verification (separate from the integration check)

Stage 3.4a close verified the MoE path against three local Ollama
experts plus an arbitrator. Captured here for the phase summary:

| Aggregator | Latency | Aggregated output | Notes |
|---|---|---|---|
| `weighted_confidence` | 194s | `spacing 1.29% / order $8 / levels±4` (high) | Three experts in parallel via `asyncio.gather`; weighted avg confidence 2.67 ≥ 2.5 → high. |
| `arbitrator` | 191s | `spacing 1.4% / order $9` (high) | Arbitrator's rationale explicitly cited ADR-007's news auto-apply restriction — emergent behavior matching the prompt's spec. |

The MoE path produces longer wall-clock per cycle than single-LLM
(~50s) because of the three serial expert calls plus aggregator. For
30min cadences this is invisible; for shorter cadences the single-LLM
path remains the right default.

## Design decisions ratified across Phase 3

Captured in detail in `docs/architecture/decisions.md` (ADRs 7-12) and
`CLAUDE.md`. The top-level list:

- **ADR-007 — MoE + News Ingestion architecture.** Three specialist
  experts (quant / risk / news) with one of three aggregator
  strategies. News-derived recommendations never auto-apply.
- **ADR-008 — Observer + Shadow Mode** (closed 2026-05-14). Two non-money
  entry points before advisor work began.
- **ADR-009 — Config consolidation.** Per-CLI YAML sections + profiles
  with deep-merge + prompt files with frontmatter.
- **ADR-010 — News source pivot to free.** RSS + CryptoCompare instead
  of paid CryptoPanic + Whale-alert; ~$2,820/yr saved with acceptable
  signal loss (no whale-flow data, headline-only news).
- **ADR-011 — MoE without an Expert ABC.** `AdvisorPort` is the only
  port; `MoEExpertEntry` is a dataclass wrapping `(name, role, advisor)`.
- **ADR-012 — Auto-apply via `cli/apply --commit`.** Operator-in-the-loop
  rewriter (not a hot-tune daemon). Defaults off.

Phase 3 grew the codebase from ~33 source files (Phase 2 close) to 57
source files, with the test count going from 296 unit tests at Phase 2
close to **792 unit tests** at Phase 3 close.

## Hard constraints honored across the phase

- **Advisor cannot execute.** Per ADR-002, no path from `AdvisorPort`
  or any of its implementations into `ExchangePort` exists in the
  codebase. The only path advisor output can mutate running state is
  `cli/apply --commit` → `settings.yml` rewrite, which the operator
  has to invoke by hand and which is gated by `AutoApplyConfig.enabled`
  + magnitude bounds + the news-role blanket-rejection rule.
- **News-derived suggestions never auto-apply.** Implemented inside the
  `evaluate_auto_apply` gate (services/auto_apply.py); a single-LLM
  suggestion with `role="news"` is blanket-rejected with the ADR-007
  reason regardless of any other config.
- **Hexagonal layer boundaries preserved.** All Phase 3 additions
  obey: domain has zero adapter imports; adapters depend on ports;
  services orchestrate via ports; no port has changed its
  responsibility across Phase 3 (NewsPort is new; AdvisorPort grew
  `expert_opinions` as a recursive optional field, backward compatible).
- **Schema drift detection holds.** Schema-drift tests for
  `settings.example.yml↔settings.yml` and `.env.example↔.env` pass
  clean throughout. New `advisor:`, `news:`, `advise:`, `schedules:`
  sections all flow through the example file first.
- **Decimal precision throughout the new code.** AutoApplyConfig caps
  and grid params remain Decimal; the auto_apply gate uses Decimal
  arithmetic for the cap comparison (the float-Decimal conversion
  for `delta_pct` only happens for human-facing log output).

## Health snapshot at Phase 3 close

- **Tests:** 792 unit (was 296 at Phase 2 close, +496 across Phase 3).
  21 integration tests opt-in (unchanged).
- **mypy:** clean across 57 source files.
- **black/isort:** clean.
- **pylint:** **10.00/10** on `src/`.
- **Pre-commit:** gitleaks + PII pattern check + author-identity guard
  via `.githooks/pre-commit` (identical to canonical reference at
  audit time). gitleaks full-history sweep: 122 commits, no leaks.
- **History clean of personal email** post-audit (B finding fixed
  2026-05-15 via `git-filter-repo` mailmap rewrite + force-push to
  origin).
- **OC memory:** pinned milestone memories for Phase 1 close,
  Stage 3.4a close, and Stage 3.4b close; plus the design-decision
  corpus from earlier sessions.
- **Real-money cost:** **$0.08** unchanged from Phase 2 close. Phase 3
  added zero live trading.

## What was deliberately NOT done in Phase 3

- **No cloud-provider adapter implementations.** AnthropicAdapter,
  OpenAIAdapter, GoogleAdapter remain placeholder slots in
  `cli/advise._build_advisor` — they raise "not implemented" at
  build time. The MoE architecture is provider-agnostic
  (AdvisorPort is the contract), so adding a cloud expert is a
  single-adapter slice when the operator wants it. Not blocking
  Phase 4.
- **No hot-tune daemon.** Per ADR-012, mid-run config changes are
  explicitly rejected; `cli/apply --commit` rewrites settings.yml
  and the operator restarts `cli/live` to pick up the new config.
- **No multi-symbol advise daemon.** `cli/advise` is one-symbol-per-
  process today. Operators wanting per-coin advisory coverage run
  multiple processes — same pattern as `cli/live` had at Stage 2.3
  before 2.4 added multi-symbol support.
- **No cycle-pair detection in the advisor's metrics summary.** Per
  ADR-006 decision 6 the engine reports cycles per-fill, and the
  Stage 3.1 metrics roll those up directly. Pair-matching is a
  post-hoc query, not a live metric.
- **No auto-apply for level keys** (`levels_above` / `levels_below`).
  Stage 3.4b's whitelist covers `spacing_percentage` +
  `order_size_usd` only. Operators wanting level-key auto-apply
  need to extend `AutoApplyConfig` with a `max_levels_change`
  field; the gate then accepts those keys under that cap.
- **No advisor scheduling beyond the unified schedules block.**
  `cli/advise` runs its own daemon; no in-engine "ask the advisor
  every N ticks" path. That kind of mid-run advisor invocation is
  exactly the ADR-002 hazard the project is set up to avoid.

## Phase 4 entry conditions

Phase 4 — Harvester & Treasury Management — picks up with these
inputs:

1. **A working advisor surface that never executes.** Phase 4's
   Harvester is the third financial-power compartment alongside Bot
   Core (trading) and Advisor (recommendations). The "advisor never
   executes" property is enforced; Phase 4 extends the principle to
   "Harvester never trades."
2. **The dual-key separation already on the books.** Read-only key
   (`KRAKEN_READER_API_KEY`) and trading key (`KRAKEN_TRADER_API_KEY`) are
   side-by-side in `.env`; Phase 4 adds a third key with Withdraw
   scope (`KRAKEN_HARVESTER_API_KEY`) that the trading key MUST NOT
   have.
3. **`HarvesterPort` skeleton** in `src/wobblebot/ports/harvester.py`
   from Phase 1.2, with `TransferProposal` / `TransferResult` value
   objects.
4. **ADR-003** (Harvester is the sole module with transfer authority)
   + **ADR-004** (Harvester uses Kraken's withdrawal API; no separate
   banking adapter). Both already ratified.
5. **`cli/apply --commit` precedent for operator-in-the-loop mutation
   of risk-bearing state.** Phase 4 follows the same posture: the
   Harvester proposes; the operator (or a configured threshold)
   approves.

Begin Phase 4.1 with the Harvester domain model + HarvesterPort
implementation skeleton. Then 4.2 wires it up read-only against
live Kraken balances. Real withdrawals don't land until 4.4 with
explicit operator sign-off and the new Harvester key minted.

## Cycle-time notes for future planning

- **Single-LLM advisor cycle:** ~50s wall-clock (phi4:14b-q8_0).
  Fits comfortably under the default 30min advise cadence.
- **MoE cycle:** ~190s parallel for three local Ollama experts +
  arbitrator (one extra serial call). Cap on cadence is the
  arbitrator latency, not the parallel fan-out.
- **News poll:** ~10-20s for 131 items across four sources;
  effectively free at any reasonable cadence.
- **Observe price tick:** ~150ms per symbol; 30s cadence has 200×
  headroom.

## CryptoCompare 90-day evaluation queued

Due **2026-08-13**. Decision point per ADR-010: CryptoCompare's
50 items/poll overlap substantially with the RSS feeds; the
sentiment_score field is intentionally unused. Re-evaluate at
90 days whether the API key + maintenance earns its place vs
adding more RSS feeds.
