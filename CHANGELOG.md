# Changelog

All notable changes to WobbleBot are documented in this file. Format
is a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [SemVer](https://semver.org/spec/v2.0.0.html).
Pre-v1.0.0, all entries land under `[Unreleased]` until a tagged
release exists; per-stage receipts in
[`docs/planning/roadmap.md`](docs/planning/roadmap.md) carry the
canonical completion dates.

## [Unreleased]

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
