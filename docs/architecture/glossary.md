# Glossary

**Bot Core** – Deterministic trading engine implementing micro-grid strategy with local safety enforcement.

**Data Collector** – Service layer component that aggregates market data, caches pricing, and calculates derived metrics. Sits between Bot Core and Exchange Adapter.

**Strategy Advisor** – LLM-powered module producing JSON configuration recommendations (advisory-only, no execution authority).

**Harvester** – Module responsible for safe transfers between Kraken and bank accounts. Uses Kraken's withdrawal API (no separate banking integration needed per ADR-004).

**Ports & Adapters** – Hexagonal architecture pattern enabling modular design. Ports are abstract interfaces; adapters are concrete implementations.

**Orchestrator** – Central scheduler and manager of module interactions. Provides defense-in-depth safety gate-keeping.

**Micro-Grid** – Small buy/sell bands that capture micro volatility ("wobble").

**Exposure Cap** – Maximum allowable funds in play at a time.

**Sandbox Mode** – Mode where WobbleBot operates only with test funds or simulated calls.

**Defense in Depth** – Dual-layer safety model where Bot Core enforces local constraints and Orchestrator can veto at the global level.

### Port Definitions

**ExchangePort** – Interface for exchange interactions (market data, orders, balances). Implemented by Kraken Adapter.

**DataCollectorPort** – Interface for aggregated market metrics. Implemented by Data Collector service.

**StoragePort** – Interface for persistence. Implemented by SQLite Adapter (Phase 1-2).

**AdvisorPort** – Interface for strategy recommendations. Implemented by Strategy Advisor (LLM).

**NotifierPort** – Interface for alerts and notifications. Implementation TBD.

**Note on BankingPort:** Originally planned as a separate port for bank API integration. Per ADR-004, not needed—Kraken's withdrawal API handles bank transfers directly.
