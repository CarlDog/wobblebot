# Architecture Components (Building Blocks)

Each component below is a high-level building block of WobbleBot.
They communicate through defined ports.

---

## Port Catalog

All ports are defined as abstract interfaces in `src/wobblebot/ports/`.

### Core Trading Ports
- **ExchangePort** – Market data, order execution, balance queries (implemented by Kraken Adapter)
- **StoragePort** – Persistence for trades, positions, config, logs (implemented by SQLite Adapter)

### Advisory & Intelligence Ports
- **AdvisorPort** – Interface for Strategy Advisor to receive summaries and return recommendations (`get_recommendation(summary) -> AdvisorRecommendation`; implementations are `OllamaAdapter` for single-LLM and `MoEAdvisorAdapter` composing 2+ AdvisorPort instances). Phase 3.2 / 3.4a.
- **DataCollectorPort** – Market metrics, historical data, derived analytics (aggregation layer). Phase 3.1.
- **NewsPort** – Interface for crypto news ingestion (`fetch_recent(since) -> list[NewsItem]`; implementations are `RssNewsAdapter` per feed + `CryptoCompareAdapter`). Phase 3.2.5.

### Treasury Ports
- **HarvesterPort** – Interface for Orchestrator to interact with Harvester (fund transfer management)

### Operational Ports
- **NotifierPort** – Alerts, notifications (email, Slack, Discord, etc.)

---

## 1. Orchestrator
- Central coordinator
- Manages lifecycle, scheduling, module interaction
- Aggregates logs + state transitions
- Only component allowed to coordinate between Bot Core, Strategy Advisor, and Harvester
- **Safety Role:** Gate-keeping layer that can veto trades even if Bot Core approved them (defense in depth)

## 2. Bot Core (Trading Engine)
- Deterministic micro-grid logic
- Position tracking, P&L, cycle management
- Does not know about LLM or Harvester
- **Enforces local safety constraints** (exposure caps, daily limits) before submitting trades
- Relies on:
  - **DataCollectorPort** for aggregated market data and metrics
  - **StoragePort** for persistence
- **Dependency Chain:** Bot Core → Data Collector → Exchange Adapter → Kraken API

## 3. Kraken Exchange Adapter
- Implements exchange-side ports
- Submits orders, fetches market/balance data
- Zero business logic
- Designed so it can be swapped with other exchanges via the same port interface

## 4. Data Collector (Service Layer)
- **Architectural Role:** Service layer component sitting between Bot Core and Exchange Adapter
- **Responsibilities:** Aggregation, caching, metric calculation
- **Phase 1-2:** Provides minimal market data (prices, balances) for trading
- **Phase 3+:** Extends to historical/derived metrics (volatility, cycle stats, flatness, drawdown)
- Prepares data for both Bot Core and Strategy Advisor
- **Implements DataCollectorPort** (consumed by Bot Core)
- **Depends on ExchangePort** (implemented by Kraken Adapter)
- Single source of market truth for the application

## 5. Strategy Advisor (LLM)
- **Implements AdvisorPort** (consumed by `cli/advise` daemon and `tools/run_advisor.py` one-shot)
- Receives sanitized `PerformanceSummary` only (no raw credentials or secrets)
- Produces JSON-based `AdvisorRecommendation` adhering to the `advisor_recommendation_v1` schema declared in `config/prompts/*.md` frontmatter
- Cannot send commands to Kraken or Harvester
- "Eyes only" module — suggestions flow through `cli/apply`, which the operator runs by hand
- **Advisory-only by design** – zero execution authority

### 5a. Single-LLM Adapter (`OllamaAdapter`, Stage 3.2)
- `adapters/ollama.py`. httpx-based; `MockTransport` test seam.
- Wraps a single Ollama model. Name-pattern detects "thinking" models (deepseek-r1, o1, qwq, etc.) and switches off Ollama's `format: "json"` constraint for them; pulls the final JSON object out of the free-text body via `json.JSONDecoder.raw_decode` walking.
- Reads `thinking` field as a fallback when `response` is empty (newer Ollama envelope for split-response models).
- `AdvisorError` wraps transport / HTTP / JSON-parse / Pydantic-validation failures.

### 5b. Mixture-of-Experts Adapter (`MoEAdvisorAdapter`, Stage 3.4a)
- `adapters/moe_advisor.py`. Composes 2+ specialist `AdvisorPort` instances (today all `OllamaAdapter`; future cloud adapters slot in via the same port).
- Fans out to every expert via `asyncio.gather`; per-expert failures get structured-field logging and the survivors aggregate. All-failed raises `AdvisorError`.
- Per-expert opinions ride on the aggregated `AdvisorRecommendation` via the recursive `expert_opinions` field — full audit trail without a side-channel API.
- Three aggregation strategies live in `services/aggregators.py`:
  - **voting** (pure function): per-key strict majority; ties / no-majority omit the key.
  - **weighted_confidence** (pure function): per-key confidence-weighted average for numerics (`high=3 / medium=2 / low=1`), weighted mode for categoricals.
  - **arbitrator** (async, one extra LLM call): serializes experts' opinions as `extra_context` and asks a separate arbitrator LLM to synthesize the final call.

### 5c. Performance Summary Builder (`SummaryBuilder`, Stage 3.3)
- `services/summary_builder.py`. Composes:
  - Stage 3.1 metrics (volatility, max drawdown, flatness, cycle stats) from observe DB
  - Stage 3.2.5 recent news (narrowed `NewsItemSummary` view to keep prompt-token cost down)
  - Operator's current grid config (delta-aware recommendations)
  - into a `PerformanceSummary` consumed by `AdvisorPort.get_recommendation`.
- Optional separate `news_storage` parameter lets the builder stitch prices from one DB and news from another (the Stage 3.3 three-DB shape).

### 5d. Auto-Apply Gate (`evaluate_auto_apply`, Stage 3.4b)
- `services/auto_apply.py`. Pure function: takes (`AdvisorSuggestion`, current `GridLevels`, `AutoApplyConfig`) → `AutoApplyResult` with per-key applied / rejected breakdown.
- **Default off** (`AutoApplyConfig.enabled=False` blanket-rejects every key).
- **News-role blanket-rejected** per ADR-007 (`role == "news"` blocked regardless of bounds). MoE-aggregated suggestions with news in `expert_opinions` still apply for whitelisted keys — the aggregated role IS the metrics-driven synthesis.
- Whitelist for v1: `spacing_percentage` + `order_size_usd` with configured `max_*_change_percentage` caps. Level keys rejected until an operator adds a cap.
- Consumed by `cli/apply` (dry-run + `--commit`).

### 5e. Settings Rewriter (`apply_grid_overrides`, Stage 3.4b)
- `services/settings_rewriter.py`. Mutates the operator's `settings.yml` in place via `ruamel.yaml` round-trip (preserves comments, key order, and numeric style).
- Atomic write: temp file + rename, so a partial write can't leave the file half-rewritten.
- Returns a unified diff for operator review.
- Refuses to write on structural surprises (missing `grid.default`, etc.); raises `SettingsRewriteError`.

### 5f. News Ingestion (Stage 3.2.5)
- **`RssNewsAdapter`** (`adapters/rss_news.py`): one instance per RSS feed. feedparser-based; httpx fetches with `follow_redirects=True`. Mentioned-coin extraction via a whitelist regex over BTC/ETH/SOL/DOGE/ADA/XRP/DOT/MATIC/AVAX/LINK.
- **`CryptoCompareAdapter`** (`adapters/cryptocompare_news.py`): polls `/data/v2/news/`; API key in Authorization header.
- Both implement `NewsPort`. Items persist to the `news_items` table with `UNIQUE(source, external_id)` dedup; re-fetching across polls is a no-op at the storage layer.

## 6. Harvester Module
- Bank ↔ Kraken balance manager
- **Implements HarvesterPort** (consumed by Orchestrator)
- **Depends on:**
  - **ExchangePort** (Kraken Adapter with withdrawal permissions) for balance queries AND fund transfers
  - **StoragePort** for logging transfer proposals and outcomes
- **Executes withdrawals via Kraken's withdrawal API** (ACH, wire transfers)
- Blind to trading implementation and LLM internals
- Operates on:
  - Current Kraken balance (from ExchangePort)
  - Static rules + thresholds configured in Harvester config (min liquidity, surplus scraping, top-up)
- **Uses dedicated Kraken API key with withdrawal permissions** (separate from trading key)
- **Note:** Per ADR-004, no separate banking API integration is needed—Kraken handles bank transfers

## 7. Storage Layer (Persistence)
- SQLite via `SQLiteStorageAdapter`. One adapter per logical DB; the project convention
  keeps per-CLI DBs separated (`observe.db`, `news.db`, `advise.db`).
- Tables (Phase 3-current):
  - **`orders`, `trades`, `grid_state`** (Phase 1-2): trade ledger + per-symbol grid anchor.
  - **`balance_snapshots`** (Phase 2.1): periodic Kraken balance captures.
  - **`price_snapshots`** (Stage 3.0): observe-tape; metrics windows read from here.
  - **`news_items`** (Stage 3.2.5): `UNIQUE(source, external_id)` dedup; advisor's
    `recent_news` summary reads from here.
  - **`advisor_suggestions`** (Stage 3.3, 3.4a-extended): persisted `AdvisorSuggestion`
    rows with `expert_opinions` JSON column (in-place migration for pre-3.4a DBs via
    PRAGMA + ALTER TABLE).
  - **`applied_suggestions`** (Stage 3.4b): audit row for every `cli/apply --commit`.
    Carries per-key before/after + rejected keys with reasons + originating
    recommendation_id.
- Architecture allows future Postgres (or similar) adapter via the same `StoragePort`.
- Must support retention policies to avoid unbounded growth (Stage 5.3.5 maintenance worker).

## 8. Observability Layer
- Centralized structured logging
- Metrics exporters for trade cycles, P&L, volatility, harvester actions, advisor usage
- Grafana integration or custom web dashboard
- Orchestrator acts as primary correlation point for traces and audits

## 9. Recovery & Reconciliation Service
- **Status:** Planned for Phase 5 (placeholder component)
- **Purpose:** Ensure consistency between local state and exchange reality
- **Responsibilities:**
  - Reconcile open orders and positions against Kraken on restart
  - Avoid double-execution of orders
  - Heal discrepancies between DB state and exchange reality
  - Detect and report anomalies (missing trades, orphaned orders)
- **Depends on:**
  - **ExchangePort** (to query live exchange state)
  - **StoragePort** (to compare with DB state)
- **Integration:** May be part of Orchestrator startup sequence or a standalone service
- **Risk:** Until implemented, restart operations require manual verification (see Operations Guide)
