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
- **AdvisorPort** – Interface for Strategy Advisor to receive summaries and return recommendations
- **DataCollectorPort** – Market metrics, historical data, derived analytics (aggregation layer)

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
- **Implements AdvisorPort** (consumed by Orchestrator)
- Receives sanitized summaries only (no raw credentials or secrets)
- Produces JSON-based recommendations adhering to a strict schema
- Cannot send commands to Kraken or Harvester
- "Eyes only" module – suggestions flow through Orchestrator, which may ignore or partially apply them
- **Advisory-only by design** – zero execution authority

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
- SQLite database (Phase 1–2)
- Stores:
  - Market snapshots
  - Trade logs
  - Advisor outputs
  - Harvester proposals/actions
- Architecture allows future Postgres (or similar) adapter via the same StoragePort
- Must support retention policies to avoid unbounded growth

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
