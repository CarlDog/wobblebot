# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Single source of truth: `docs/planning/roadmap.md`** — every stage carries a ✅
completion date. Do NOT duplicate project status here (per the documentation-discipline
rule); this section is a pointer, not a changelog.

- **Current:** read the latest dated entry in `docs/planning/roadmap.md`; do not infer
  phase or release status from this file. `docs/planning/phase-8-summary.md` records the
  v1.0 close, while `docs/release/v1.1/README.md` retains the historically named post-tag plan.
  Five releases exist — `v1.0.0` (2026-07-31), `v2.0.0` (2026-08-28), and `v2.0.1`,
  `v2.0.2`, `v2.0.3` (all 2026-09-03) — all tagged with published GitHub Releases. **The `v1.1` name is historical only: that branch's work
  shipped as `2.0.0`**, per `CHANGELOG.md`'s preamble.
- **Detail:** per-phase closing summaries at `docs/planning/phase-{2..8}-summary.md`;
  the day-by-day soak log lives in roadmap Stage 8.4.E; the v1.1-branch digest (2026-06-04
  onward) is in the same Stage 8.4.E section, clearly marked as branch-only.
- **Release docs:** `docs/release/v1.0-known-limitations.md`, `docs/release/v1.1/`
  (future improvements), `docs/release/v1.0-soak-runbook.md`, and
  `docs/planning/release-2.0-plan.md` (the 2.0.0 tag gate, the 2026-08-27/28
  external-repository assessments triaged into scheduled/parked/declined, the proposed
  2.1 phase, and a per-document plan for the documentation audit).
- **Deploying is two explicit steps, and neither is automatic.** A push to `main` does
  NOT reach the NAS: Portainer stack 158 is a file-based stack (deliberately detached
  from git 2026-08-31; no git poll, no webhook). A stack-file update + redeploy applies
  compose changes; an `IMAGE_TAG` bump applies code. Verify against `StackFileVersion`
  and the container's `org.opencontainers.image.revision` label.
- **An adversarial review gates every code deploy** — the standing rule lives at
  `~/.claude/rules/pre-deploy-review.md`; read it before shipping. Before an
  `IMAGE_TAG` bump carrying code you authored, run the multi-dimension review
  against the diff and resolve or explicitly accept every confirmed finding.
  Docs-only deploys skip it, but say the skip out loud. Any test claiming to pin a
  behavior is mutation-verified first: revert the behavior, prove the test goes red,
  restore. **Ratified 2026-09-03**, after a post-hoc review of the already-deployed
  2.0.2/2.0.3 raised 17 findings of which 7 survived refutation — a UI scope defect
  that could place unmanaged real orders, a regression that broke a documented
  command, two falsehoods rendered to the operator, and three same-day tests that
  passed against deliberately broken code. All of it had shipped green through CI,
  lint, mypy and a full suite; see roadmap item 13.
- Test counts, lint scores, src-file counts, and the real-money cost ledger are
  authoritative in the roadmap's per-stage entries — not duplicated here, to avoid drift.

**Before any non-trivial work:** read `docs/planning/roadmap.md`, confirm the request
matches the current stage, and name any drift before starting. If asked for Stage N+1
work while Stage N is in progress, flag it first.

Ratified design decisions live in two places: `docs/architecture/decisions.md` (formal
ADRs) and `docs/architecture/ratified-decisions.md` (operational decisions not yet ADRs —
Kraken adapter, dry-run semantics, caps split, etc.). Don't relitigate either without an ADR.

### Operator entry points

Twenty-two surfaces (sixteen `cli/` + six `tools/`). One-line index; full behavior in each
module's `--help` and the roadmap stage that shipped it.

- `cli.sandbox` — Phase 1 mock-exchange paper-trade cycle (no real money).
- `cli.status` — read-only Kraken price + balance check.
- `cli.preflight` — one engine step via Kraken `validate=true` (nothing placed). **Run before every live session.**
- `cli.live` — **real-money** multi-asset grid trading. `--symbols` comma-list; hard caps; clean SIGINT cancels every open order. Exit codes: 0 clean / 1 loss-cap / 2 missing creds.
- `cli.observe` — read-only price/balance data collection.
- `cli.lurker` — one-line alias of `cli.observe` today (own `__main__`); reserved to grow advisor commentary on pure observation later.
- `cli.news` — long-running news poller (RSS + Kraken status feed; CryptoCompare retired 2026-07-31, paid-only upstream — off by default everywhere); persists `news_items` with `(source, external_id)` dedup; feeds the advisor.
- `cli.shadow` — same engine, `ShadowExchangeAdapter` (live prices, synthetic ledger). Backtest sandbox.
- `cli.advise` — MoE advisor daemon; writes suggestions, **never executes** (ADR-002).
- `cli.apply` — operator-gated auto-tune. Dry-run default; `--commit` rewrites `settings.yml`. Default-off gate; news-role never auto-applies.
- `cli.harvest` — treasury daemon and the **only** module that can move money (Harvester key, ADR-003). Two ways in, one implementation of the seven defense layers (`cli/harvest_execute.py`): `--execute <id>` from a terminal, or an operator-approved `execute_proposal` row queued by the web UI and picked up by its 15s command poll (ADR-034).
- `cli.operator` — Discord interaction daemon (ADR-013). Intent → `pending_commands`; `WHERE status='approved'` is the ADR-002 firewall.
- `cli.web` — FastAPI dashboard (ADR-016/017). Read-mostly; mutations firewalled via `pending_commands`. Needs `WOBBLEBOT_WEB_SESSION_SECRET`.
- `cli.recalibrate` — scale USD-denominated knobs to a new target balance (operator-initiated; dry-run default).
- `cli.maintenance` — scheduled database hygiene, backup/verification, Kraken reconciliation,
  capital reporting, and ledger-sync work. The module's `--help`, settings schema, and roadmap
  receipts are authoritative for the current task set.
- `cli.screener` — rank observed symbols by grid-suitability (P2 slice 5). Read-only, offline, advisory (ADR-002); log-table output.
- `tools/first_real_trade.py` — one-shot live round-trip diagnostic.
- `tools/run_cloud_check.py` — one-shot cloud-LLM smoke test (`--provider`/`--role`/`--model`/`--dry-run`).
- `tools/import_kraken_history.py` — stream the local OHLCVT dump into `ohlc_bars`/`price_snapshots` (P2 slice 2; the only deep-history path).
- `tools/auditor.py` — replay `settings.yml` through the real `GridEngine` over stored bars (ADR-028; directional, not exact).
- `tools/reconcile_trade_history.py` — one-shot Kraken-vs-live.db trade/ledger diff (2026-08-22). The manual, deeper companion to `cli.maintenance`'s daily reconcile task; its docstring carries the backfill runbook a reported gap points at.
- `tools/scan_logging.py` — audit log calls against `docs/implementation/logging-conventions.md`. `--check rule1` (default) exits 1 on data stranded in `extra=` and is enforced by a test; `--check decimal` lists money values interpolated without `fmt_decimal` (a review list — the heuristic trips on ints and float durations).

### Operator handoff: from dry-run to live trading

1. **Mint a Kraken trading key**, separate from the read-only key (per ADR-003-style separation). Permissions: Query Funds + Query open & closed orders & trades + Create & modify orders + Cancel & close orders. **Withdraw must stay off** — that scope is exclusive to the separate Harvester key. Recommended: enable IP address restriction.
2. **Stash credentials in `.env`** as `KRAKEN_TRADER_API_KEY` / `KRAKEN_TRADER_API_SECRET` (separate from the existing `KRAKEN_READER_API_KEY` / `KRAKEN_READER_API_SECRET` so the read-only key can keep being used for `cli/status`).
3. **Run `cli/preflight`** — confirm Kraken accepts the grid config without spending anything. Exit 0 means every layout order would be accepted by Kraken's matching engine.
4. **Run `cli/live`** with eyes on the Kraken Pro Orders + Trade History tab. Defaults: $10 per order, 1% spacing, 3 above + 3 below = $60 total exposure, $5 max session loss, 60 minute max runtime, 5s tick. The first session is the highest-risk session — watch it.

## Commands

The Windows-friendly Makefile uses `.venv/Scripts/python.exe` — if your shell can't run `make`, invoke the same commands directly through the venv interpreter or activate it first.

**First-time setup on a fresh clone** — once, before your first commit:

```bash
./scripts/install-hooks.sh        # or scripts\install-hooks.ps1 on PowerShell
```

This points `core.hooksPath` at `.githooks/`, enabling the repo-specific
pre-commit hook (gitleaks + PII pattern check + author-identity guard).
Without it, only the global `.git/hooks/pre-commit` runs, which only does
gitleaks — missing the PII/identity checks required for this repo.

| Task | Command |
|------|---------|
| Install (editable + dev extras) | `pip install -e ".[dev]"` |
| Run all tests | `pytest` |
| Run unit tests only | `pytest -m unit` |
| Run integration tests only | `pytest -m integration` |
| Run a single test | `pytest tests/path/to/test_file.py::TestClass::test_name` |
| Tests with coverage HTML | `pytest --cov=wobblebot --cov-report=html` |
| Format | `black src/ tests/ && isort src/ tests/` |
| Format check (no writes) | `black --check src/ tests/ && isort --check-only src/ tests/` |
| Type check | `mypy src/` |
| Lint | `pylint src/` |
| All pre-commit checks | `make check` (format + lint + test) |

**Pytest config gotchas** (`pyproject.toml`):
- `addopts` always runs with coverage enabled (`--cov=wobblebot`) — slow runs are expected even for single tests.
- `filterwarnings = ["error", ...]` — warnings other than `DeprecationWarning` fail the suite.
- `--strict-markers` — only `unit`, `integration`, `slow` markers are valid.

**Mypy config:** strict (`disallow_untyped_defs`, `strict_optional`, `warn_unused_ignores`). The `tests/` tree is excluded; `src/` must be clean.

## Architecture

Hexagonal (Ports & Adapters). Layer boundaries are load-bearing — violating them defeats the safety design.

```
src/wobblebot/
  domain/      # Pure business logic; ZERO imports from adapters/services
  ports/       # Abstract interfaces (ABCs) — the contracts adapters implement
  adapters/    # Concrete implementations (Kraken, SQLite, LLM, ...) — depend on domain + ports
  services/    # Orchestrators wiring ports to flows; the only place that knows multiple modules exist
  cli/         # Entry points
  config/      # Pydantic schemas + loaders
tests/         # Mirrors src/ structure
```

**Hard rules:**
- `domain/` must not import from `adapters/`, `services/`, or `cli/`. Run `grep -r "from wobblebot.adapters" src/wobblebot/domain/` — output should be empty.
- Dependencies flow inward only: adapters depend on ports, services depend on ports + domain, nothing depends on adapters.
- All cross-module wiring happens via constructor dependency injection of port interfaces.
- **Documented exception — LLM plumbing.** The cloud-LLM adapters (`adapters/openai.py`, `anthropic.py`, `google.py`, their `*_assistant.py` variants) and `adapters/moe_advisor.py` import shared *leaf* helpers from `services/` (`llm_cloud_call`, `llm_cost_gate`, `llm_pricing`, `llm_retry`, `aggregators`). This bends "nothing flows out of adapters" but creates **no import cycle** — those helpers never import the adapters back — and centralizes one cost-gate / retry / pricing implementation instead of copying it per provider. This is the one sanctioned outward edge; new LLM adapters may reuse these helpers, but don't introduce fresh adapter→service dependencies outside this plumbing.

### Financial Power Fragmentation (Safety Design)

This is the single most important invariant. No one module controls both trading and money movement:

| Module | What it does | What it CANNOT do |
|--------|-------------|-------------------|
| **Bot Core** | Trading decisions, micro-grid logic, P&L | Initiate transfers; knows nothing of LLM or Harvester |
| **Strategy Advisor (LLM)** | Produce JSON recommendations | Execute trades, initiate transfers, hit Kraken directly |
| **Harvester** | Initiate Kraken→bank withdrawals on thresholds | See trading logic internals or LLM suggestions |
| **Orchestrator** | Coordinate the three modules; aggregate logs | Bypass any port |

**Non-negotiables:**
1. Only Harvester initiates fund transfers. Per ADR-004, it uses Kraken's withdrawal API via `ExchangePort` — there is no separate banking adapter or `BankingPort`. The web UI may *queue* a withdrawal for approval but never performs one (ADR-034); `ExecuteProposalCommand` sits outside the LLM-emittable `OperatorCommand` union, so no assistant parse can originate a transfer.
2. The Kraken **trading** API key must NOT have withdrawal permissions. Withdrawal permissions live on a separate Harvester key.
3. LLM output is JSON-schema-validated and bounded by configured min/max ranges before any auto-application.
4. Max exposure caps and daily spend limits are enforced inside Bot Core, not by adapters.

Full constraint list: `docs/architecture/constraints.md`.

### Phase-Gated Development

Phases are strictly sequential. Do not implement later-phase work until the current phase's
documented gates are satisfied. Phase definitions, current position, acceptance criteria, and
completion receipts live only in `docs/planning/roadmap.md`.

### Domain Model Conventions (ADR-005)

Domain models are deliberately Kraken-aligned to minimize adapter translation:
- **Dual ID strategy:** `Order.id: UUID` for DB, `Order.exchange_id: str | None` for Kraken txid.
- **Order status vocabulary:** `pending | open | closed | canceled | expired` (Kraken's canonical terms — note American "canceled").
- **Trade IDs:** Plain Kraken txid strings (`Trade.id: str`), not UUIDs.
- **`Position` model is deferred** to Phase 3+ (margin-specific; spot trading doesn't need it).

Use Pydantic models for domain entities, value objects in `domain/value_objects.py` (`Symbol`, `Price`, `Amount`, `Timestamp`).

## ADRs to Read Before Major Changes

`docs/architecture/decisions.md` is short and dense. The ones that drive code structure:
- **ADR-001:** Hexagonal architecture (the layer rules above).
- **ADR-002:** LLM is advisory only.
- **ADR-003:** Harvester is the sole module with transfer authority.
- **ADR-004:** No separate banking adapter — Harvester uses Kraken's withdrawal API via `ExchangePort`.
- **ADR-005:** Kraken-aligned domain models (status values, ID strategy).

If you're about to add an abstraction "for future flexibility," check that an ADR doesn't already reject it (ADR-004 explicitly rejects a `BankingPort`).

## Where to Find Things

- **Architecture:** `docs/architecture/` (start with `README.md`, then `architecture-components.md`, `constraints.md`, `decisions.md` for ADRs, `ratified-decisions.md` for operational decisions not yet ADRs)
- **Implementation:** `docs/implementation/coding-guidelines.md`, `module-specs.md`, `development-workflow.md`
- **Planning:** `docs/planning/roadmap.md` (source of truth — current phase + per-stage detail), `requirements.md`, `testing-plan.md`, plus per-stage `stage-N.M-design.md` slicing docs
- **Kraken API reference:** `docs/reference/kraken-api-reference.md`
- **Which LLM holds which advisor seat:** `docs/reference/advisor-seats.md` (the
  register — holder, evidence, battery, and whether `settings.yml` agrees). Evidence
  lives in `advisor-llm-models.md` / `operator-llm-models.md`. Reconcile the register,
  the operator's current `settings.yml`, and the roadmap's deployment receipt before
  changing a seat; this file deliberately carries no point-in-time deployment claim.
- **Config example:** `config/settings.example.yml` (real `config/settings.yml` is gitignored). Per-CLI sections + grid/safety + advisor + profiles. Operators copy this to `settings.yml` and adjust values; comments and structure stay in sync per the schema-drift tests.
- **Prompt files:** `config/prompts/{quant,risk,news,arbitrator}.md` (committed defaults; operators edit freely). YAML frontmatter + Markdown body; loader in `wobblebot.config.prompts`.
- **Env vars example:** `.env.example` at the repo root (single source of truth — schema-drift tests verify operator `.env` files stay in sync)
- **Atlas Cloud CLI:** [github.com/AtlasCloudAI/cli](https://github.com/AtlasCloudAI/cli)
  (credit: AtlasCloudAI), vendored as a git submodule at `vendor/atlascloud-cli`.
  Manual dev/ops tool only (shell-side balance/model/connectivity checks) — the
  advisor's Atlas Cloud adapter calls `https://api.atlascloud.ai/v1` directly via
  `OpenAIAdvisorAdapter` and does not use this CLI.

## Project-Specific Conventions

- **Python 3.13+ required** (`requires-python = ">=3.13"`). Use `str | None`, `list[X]`, `match` statements — no `Optional`/`List` imports needed.
- **Never use `print()`.** Use the project logger (`wobblebot.config.logging.configure_logging`). Plain format renders message-only; put operator-facing data in the message string and structured fields in the `extra=` dict so JSON consumers see them too.
- **Pydantic v2 models** for structured data (domain entities, config schemas). The Pydantic **mypy plugin** is enabled in `pyproject.toml` and load-bearing — do not remove it.
- **Port error convention:** a domain-data miss returns `T | None`; a protocol/transport failure raises the port's error type (`ExchangeError`, `StorageError`, `DataCollectorError`, etc. — in `wobblebot.ports.exceptions`). More ratified conventions in `docs/architecture/ratified-decisions.md`.
- **Async ports:** `ExchangePort` and other I/O-bound ports are `async`. Use `pytest-asyncio` for tests of async code.
- **Line length 100** (black + isort + pylint all configured to this).
- **Keep files under ~300-400 lines.** Split modules that turn into junk drawers.
- **No `print()`, no swallowed exceptions, no real network calls in unit tests.** Use mocks/stubs (`httpx.MockTransport` is the test seam for `KrakenAdapter`). Integration tests carry the `integration` marker and are excluded from the default `pytest` run via `addopts`; run them explicitly with `pytest -m integration`.

## Phase-End Audit Checklist

Run a phase-end audit at every phase close (Phase 1 → Phase 2,
Phase 2 → Phase 3, etc.) before starting the next phase. The
**global rule lives at `~/.claude/rules/phase-end-audit.md`** —
read that first; the cadence table and process discipline apply
to every project. The wobblebot-specific items below extend it:

### Every phase end (wobblebot extras)

- **All 16 `cli/` entry points handle deprived envs cleanly.** Cycle
  each CLI through: no `.env`, no `config/settings.yml`, no `config/`
  directory at all, missing per-CLI section, empty credentials,
  bad `--config` path, bad `--profile` name. Expected: clean exit
  codes (2 for missing creds / config / section), no raw
  tracebacks. Verification #24 established the baseline 2026-05-15
  for the original 7 (sandbox / status / preflight / live /
  observe / shadow / first_real_trade); cli/apply added at Stage
  3.4b, cli/harvest at Stage 4.2, cli/operator at Stage 5.6 each
  carried their own deprived-env coverage in their slice work;
  cli/web, cli/recalibrate, cli/maintenance — plus the pre-existing
  cli/news + cli/lurker (observe alias) — round out the original 15;
  cli/screener (P2 slice 5, 2026-08-08) makes it 16 — it exits 2 on a
  missing `screener:` section / bad --config and needs no credentials.
  When new entry points ship, add them to this walkthrough.
- **Schema-drift tests pass clean.** `pytest tests/config/test_schema_drift.py`
  runs without warnings (or with documented justification).
  Operator `.env` and `settings.yml` keys are a subset of their
  example counterparts; `WOBBLEBOT_STRICT_CONFIG_DRIFT=1` for
  bidirectional strict mode in CI.
- **`settings.example.yml` reflects reality.** The drift test catches KEY
  drift but **not** value/comment staleness or dead pairs (`grid.coins.*`
  is exempt) — verified 2026-06-04 after the example silently carried stale
  per-coin overrides + a retired MATIC pair across several commits. On any
  strategy change, sync the example's affected values + comments in the SAME
  commit, and confirm no retired pairs linger (MATIC→POL). The example is a
  generic **template** (sensible defaults), **not** a mirror of live values:
  operator-specific caps/balances and the 4 identity fields
  (`operator.auth.*`, `harvester.withdrawal_destinations`) stay
  generic/placeholder. (Targeted enforcement tests — structural-parity,
  valid-pairs, value-invariant — are the durable guard; see
  `tests/config/`.)
- **Per-stage receipts have completion dates.** Every closed stage
  in `docs/planning/roadmap.md` carries a ✅ date. Phase summary
  document exists if the phase had real-money or architectural
  significance (per `docs/planning/phase-2-summary.md` precedent).
- **OC project memory current.** `mcp__openchronicle__project_list`
  → match repo URL → `mcp__openchronicle__onboard_git` to pick up
  any commits made outside Claude sessions. Project state memory
  reflects current phase + health metrics.
- **Ratified design decisions section in this file is accurate.**
  Don't relitigate; do flag if a new ADR superseded one. New ADRs
  added during the phase get a one-line mention.
- **Real-money cost ledger updated.** If any live-money operations
  ran, the running total in `docs/planning/roadmap.md` (the
  authoritative ledger) reflects reality.

### Quarterly (wobblebot extras)

- **Pre-commit hook reference comparison.** Diff
  `.githooks/pre-commit` against the canonical reference at
  `https://github.com/CarlDog/plex-mcp/blob/main/.githooks/pre-commit`
  (cited in the global `security.md` rule). If the reference gained
  checks, port them. The reference's PII patterns and
  author-identity guard are the load-bearing parts.
- **Live taker fee re-verification.** The live Kraken fee schedule
  could shift over time. If a tiny live trade (`tools/first_real_trade.py`)
  runs during the audit window, capture the actual fee rate from
  the receipt and confirm it still matches the **0.80% taker / 0.40%
  maker** Tier-1 rates (Kraken doubled the schedule 2026-07-09 —
  caught by this exact audit item 2026-08-17, five weeks late). Since
  ADR-038 the authoritative check is one `TradeVolume` call (the
  account's own rates; `scratchpad` probe or `cli/live`'s session-start
  receipt logs them), and the per-fill fee-drift tripwire pages on the
  first deviating fill — this item is now the backstop, not the
  detector. **`first_real_trade.py` writes only to its own JSONL log,
  never to `live.db`** (no `StoragePort` import at all) — every run
  silently desyncs BTC's cost-basis replay from its real Kraken
  balance, and an aborted Experiment B (the price-drift-check abort
  path) leaves real stranded BTC the engine's basis never sees. Found
  2026-08-22: 18 trades missing across nine prior runs, a confirmed
  0.00053613 BTC / ~2.8% average-cost gap, undetected for weeks until
  a manual audit went looking (incident receipt in
  `docs/planning/roadmap.md`'s v1.1 digest; detail in PR #102).
  `cli/maintenance`'s `reconcile` task (added the same day) now checks
  this automatically once a day, but don't rely on that cadence alone
  right after a session — run `python tools/reconcile_trade_history.py
  --symbols BTC` immediately and follow that tool's backfill runbook
  for any reported gap before trusting BTC's sell guard.
- **Cloud LLM pricing + model re-verification.** Cloud-provider pricing,
  model availability, and API shapes drift often. Re-confirm each priced
  `(provider, model)` in `services/llm_pricing.py` against the provider's
  current pricing page and bump its `verified_date` — the
  `tests/services/test_llm_pricing_freshness.py` 180-day CI gate is the
  backstop, not a substitute. Spot-check that the models named in
  `config/settings.yml` are still offered (a retired model now degrades
  to the heuristic fallback, but you'd want to know). Detection is
  automatable (see the LLM-provider-drift-watcher v1.1 entry in
  `docs/release/v1.1/infrastructure.md`); remediation stays human per
  ADR-014.
- **Output-token caps still fit the prompts.** A prompt edit can quietly
  outgrow a `max_tokens` set months earlier, and the failure does NOT
  look like a config problem — the model is cut off mid-answer and you
  get a parse error that reads like the model went stupid. Nothing gates
  this: the 180-day pricing-freshness test above doesn't check it, and
  the prompt and the cap live in different files. For each configured
  `(role, model)`, confirm the cap covers a COMPLETE response to that
  role's current prompt, then add headroom for two things that only grow:
  (a) prompts gain required fields over time; (b) **thinking-capable
  models bill reasoning against the same budget** — measured 2026-08-10,
  `gemini-2.5-flash` spent 980 of a 1024-token budget on thinking and
  left 40 for the answer, and the thinking budget is *dynamic*, expanding
  to fill whatever it is given. Re-check after ANY prompt change, not
  just quarterly. (Receipt: the three live cloud-LLM tests, which read as
  provider drift and were a stale cap — PR #64.)

### Standing credential verification (wobblebot extras)

- **Harvester key separation.** At each key rotation and security audit, confirm the
  Harvester key is genuinely separate from the trade key, has Withdraw scope on, and
  the trade key has Withdraw scope OFF. This is ADR-003's load-bearing invariant.

### Tracking

Each audit pass opens a tracked task ("Phase N close audit") with
findings as sub-tasks. Findings get fixed in separate commits per
category (per the global rule's process discipline). Audit-fatigue
mitigation: if a category goes three audits with no findings, drop
its cadence per the global rule.
