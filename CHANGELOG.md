# Changelog

All notable changes to WobbleBot are documented in this file. Format
is a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [SemVer](https://semver.org/spec/v2.0.0.html).
Pre-v1.0.0, all entries land under `[Unreleased]` until a tagged
release exists; per-stage receipts in
[`docs/planning/roadmap.md`](docs/planning/roadmap.md) carry the
canonical completion dates.

## [Unreleased]

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
  and refreshed for Phase 2.3 reality (`KRAKEN_TRADE_API_KEY`,
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

## [v1.0.0] — TBD

Per the [roadmap](docs/planning/roadmap.md), v1.0.0 lands at the end
of Phase 5 with: micro-grid trading engine, Kraken adapter (live),
multi-asset support, Strategy Advisor (single-LLM and MoE) with
guarded auto-tuning, Harvester with passive and active withdrawal
modes, centralized Orchestrator, Data Collector v2, observability
layer (structured logging, metrics, dashboard), Docker Compose
deployment, and complete documentation.

### Known limitations planned for v1.0.0

- Restart / reconciliation logic is basic; manual checks required
  after restarts until Phase 5 introduces robust reconciliation.
- Advisor JSON schema is draft; future schema versions may be
  incompatible with earlier ones.
- Automated bank deposits (bank → Kraken) are not supported in
  v1.0.0 — only Kraken → bank withdrawals via the Harvester (per
  ADR-004).
