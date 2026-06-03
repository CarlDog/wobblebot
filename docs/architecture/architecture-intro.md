# Architecture Introduction & Goals

WobbleBot is a deterministic, safety-first micro-trading system designed to operate inside a fully sandboxed environment. Its mission is to perform controlled “buy the dip / sell the rebound” cycles using strict guardrails, modular isolation, and complete transparency.

## System Goals

- **Deterministic bot core**
  Trading logic must operate identically across runs, environments, and datasets.

- **Safety-first financial design**
  No withdrawals from Kraken except via the Harvester module, which enforces strict thresholds.
  No LLM can execute trades or move money.

- **Hexagonal architecture**
  Each module is isolated behind ports and adapters.
  Implementation details are swappable without affecting business logic.

- **Transparent operations**
  Logging, dashboards, and auditability are built in at the architecture level.

- **Modularity and extensibility**
  New modules (e.g., new exchanges, broader strategies) can be added cleanly.

## Primary Architectural Values

1. **Isolation**
   - Bot Core does not know LLM exists.
   - Harvester is blind to trading logic.
   - Orchestrator is the only coordination point.

2. **Safety by design**
   - Never allow one module to hold too much power.
   - Bot Core enforces local safety constraints (exposure caps, daily limits).
   - Orchestrator provides defense-in-depth gate-keeping.
   - Withdrawals and deposits always gated through Harvester with strict thresholds.

3. **Sandboxed execution**
   - Dockerized environment with predictable resource limits.
   - SQLite local storage to ensure no external mutation.

## Architecture Overview

WobbleBot consists of the following domains:

- **Bot Core** – deterministic trading logic (enforces local safety constraints)
- **Data Collector** – service layer for market data aggregation, caching, and metrics
- **Kraken Adapter** – all exchange communication (implements ExchangePort)
- **Strategy Advisor (LLM)** – suggestion generation (implements AdvisorPort)
- **Harvester Module** – fund inflow/outflow management (a service; withdraws via ExchangePort per ADR-004)
- **Orchestrator** – central command, coordination, and safety gate-keeping
- **Storage** – SQLite database + structured logs (implements StoragePort)
- **Dashboard/Observability** – Grafana or custom UI (future)
