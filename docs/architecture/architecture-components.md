# Architecture Components (Building Blocks)

Each component below is a high-level building block of WobbleBot.  
They communicate through defined ports.

## 1. Orchestrator
- Central coordinator  
- Manages lifecycle, scheduling, module interaction  
- Aggregates logs + state transitions  
- Only component allowed to coordinate between Trader, Advisor, and Harvester  

## 2. Bot Core (Trading Engine)
- Deterministic micro-grid logic  
- Position tracking, P&L, cycle management  
- Does not know about LLM or Harvester  
- Relies on:
  - **ExchangePort** for market data and order execution (via adapters)
  - **StoragePort** for persistence

## 3. Kraken Exchange Adapter
- Implements exchange-side ports  
- Submits orders, fetches market/balance data  
- Zero business logic  
- Designed so it can be swapped with other exchanges via the same port interface

## 4. Data Collector / Market Metrics
- **Phase 2:** provides minimal market data (prices, balances) for trading  
- **Phase 3+:** extends to historical/derived metrics (volatility, cycle stats, flatness, drawdown)  
- Prepares data for both Trading Core & Advisor  
- Uses Exchange Adapter as its only source of market truth

## 5. Strategy Advisor (LLM)
- Receives sanitized summaries only (no raw credentials or secrets)  
- Produces JSON-based recommendations adhering to a strict schema  
- Cannot send commands to Kraken or Harvester  
- “Eyes only” module – suggestions flow through Orchestrator, which may ignore or partially apply them

## 6. Harvester Module
- Bank ↔ Kraken balance manager  
- Executes controlled fund movements **only via its own banking/withdrawal ports**  
- Blind to trading implementation and LLM internals  
- Operates on:
  - Current balances (from ExchangePort + BankingPort)
  - Static rules + thresholds configured in Harvester config

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

## 9. Recovery & Reconciliation (Planned)
- Future responsibility (Phase 5) to:
  - Reconcile open orders and positions against Kraken on restart
  - Avoid double-execution of orders
  - Heal discrepancies between DB state and exchange reality  
- This logic will live in a dedicated “Reconciliation Service” or be part of Orchestrator services, and is tracked as an architectural risk until implemented