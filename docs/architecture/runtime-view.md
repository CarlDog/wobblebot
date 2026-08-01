# Runtime View

This document describes key runtime executions and interactions.

## Trading Cycle Sequence

1. Scheduler triggers Bot Core (via Orchestrator)
2. Bot Core requests market data from Data Collector (via DataCollectorPort)
3. Data Collector fetches fresh data via Kraken Adapter (via ExchangePort)
4. Data Collector returns aggregated/cached data to Bot Core
5. Bot Core evaluates micro-grid logic and applies local safety constraints
6. If conditions met → Bot Core generates trade intent
7. Orchestrator validates trade against global safety rules (defense in depth)
8. Kraken Adapter executes order (via ExchangePort)
9. Orchestrator logs cycle outcome (via StoragePort)

## Strategy Advisory Sequence

1. Orchestrator compiles sanitized performance summary (no secrets/credentials)
2. Summary passed to Strategy Advisor (via AdvisorPort)
3. LLM generates JSON recommendations (schema-validated)
4. Orchestrator stores recommendations in DB (via StoragePort)
5. Orchestrator may auto-apply bounded recommendations (if enabled and within limits)
6. Bot Core incorporates new configs in next trading cycle
7. All changes logged with before/after snapshots

## Harvester Sequence

1. Orchestrator queries Harvester for balance status
2. Harvester checks Kraken balance (via ExchangePort)
3. Harvester applies threshold logic (min liquidity, surplus scraping, top-up rules)
4. If withdrawal needed → Harvester builds transfer proposal
5. Orchestrator enforces safety caps and validates proposal
6. Harvester executes withdrawal via Kraken's withdrawal API (via ExchangePort with withdrawal permissions)
7. Transfer outcome recorded in DB (via StoragePort)
8. Orchestrator logs full audit trail

**Note:** Per ADR-004, Harvester uses Kraken's withdrawal endpoints (ACH/wire) rather than separate banking API.
