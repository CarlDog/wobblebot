# System Context & Boundaries

WobbleBot interacts with a small set of external systems.
This document defines boundaries, actors, and environment.

## External Actors

- **Kraken Exchange API**
  Provides market data, account balances, order execution, and fund withdrawals (ACH/wire).

- **Local LLM (Ollama)**
  Provides strategy recommendations in JSON format.

- **User (Human Operator)**
  Oversees operations, approves certain actions, reviews logs.

**Note:** Per ADR-004, no separate banking API integration is required. Kraken's withdrawal API handles bank transfers directly.

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
|  +--------------+     +--------------+                         |
|  |  LLM Advisor |<--->|   Harvester  |                         |
|  +--------------+     +--------------+                         |
|          ^                    |                                |
|          \--------------------/                                |
|         (via Orchestrator only)                                |
|                                                                |
|  Note: Harvester uses Kraken API for withdrawals (ADR-004)    |
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
