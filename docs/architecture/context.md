# System Context & Boundaries

WobbleBot interacts with a small set of external systems.
This document defines boundaries, actors, and environment.

## External Actors

- **Kraken Exchange API**
  Provides market data, account balances, and order execution.

- **Banking API (Harvester)**
  Optional; used only for controlled deposits/withdrawals.

- **Local LLM (Ollama)**
  Provides strategy recommendations in JSON format.

- **User (Human Operator)**
  Oversees operations, approves certain actions, reviews logs.

## Context Diagram (ASCII)

```
                 +---------------------+
                 |      Human User     |
                 +----------+----------+
                            |
                            v
+---------------------------------------------------------------+
|                         WobbleBot                             |
|                                                               |
|   +-----------+     +----------------+      +--------------+  |
|   |  Bot Core |<--->|   Orchestrator |<---->|   Dashboard  |  |
|   +-----------+     +----------------+      +--------------+  |
|         ^                    ^                      ^          |
|         |                    |                      |          |
|   +-----------+      +--------------+      +---------------+   |
|   | Data Coll.|<---->| Kraken Adap. |<---->|  Kraken API   |   |
|   +-----------+      +--------------+      +---------------+   |
|         ^                    ^                      ^          |
|         |                    |                      |          |
|  +--------------+     +--------------+      +---------------+  |
|  |  LLM Advisor |<--->|   Harvester  |<---->|  Banking API  |  |
|  +--------------+     +--------------+      +---------------+  |
|          ^                    ^                                 |
|          \--------------------/                                 |
|                 (via Orchestrator only)                         |
+---------------------------------------------------------------+
```

## Note on Boundaries

- **Strategy Advisor and Harvester NEVER talk directly.**
  All coordination flows through the **Orchestrator**, which:
  - Prepares sanitized summaries for the Strategy Advisor (via AdvisorPort).
  - Receives Harvester proposals/actions (via HarvesterPort).
  - Enforces safety constraints and mode controls.
  - Provides defense-in-depth validation beyond module-level checks.

- **Data Collector intermediates market data:**
  - Bot Core depends on DataCollectorPort (not directly on ExchangePort).
  - Data Collector depends on ExchangePort (Kraken Adapter).
  - Enables caching, aggregation, and metric calculation without coupling Bot Core to exchange details.

## System Boundary

Inside WobbleBot:
- Trading logic
- Advisory logic
- Harvester logic
- Orchestration
- Storage & logs

Outside WobbleBot:
- Exchanges
- Bank accounts
- LLM models
- Humans
