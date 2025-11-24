# Architectural Decision Records (ADR)

This file tracks major system decisions.

## ADR-001 — Use Hexagonal Architecture  
**Status:** Accepted  
**Context:** Need for modularity & testability  
**Decision:** Use Ports/Adapters pattern across all modules  
**Consequences:** Clean module isolation, easier long-term extensibility  

## ADR-002 — LLM Is Advisory-Only  
**Status:** Accepted  
**Decision:** LLM cannot generate executable commands  
**Reason:** Safety and determinism  

## ADR-003 — Separate Withdrawals into Harvester Module  
**Status:** Accepted  
**Context:** Need to keep Kraken trading key safe  
**Decision:** Only Harvester key may initiate transfers  
**Consequence:** Strong compartmentalization of financial power