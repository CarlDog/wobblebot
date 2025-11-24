# Architectural Risks & Mitigations

## Risk 1 — Over-reliance on Kraken
**Mitigation:**  
Design adapters to be replaceable; multi-exchange support planned in later phases.

## Risk 2 — LLM behaves unpredictably
**Mitigation:**  
Enforce strict JSON schema, no executable commands, and bounded auto-apply behavior with range checks and full logging.

## Risk 3 — Harvester misconfiguration
**Mitigation:**  
Hard-coded safety caps + operator approval mode. Clear logging of all proposals and executions.

## Risk 4 — SQLite performance and growth
**Mitigation:**  
Define data retention policies for high-volume tables (e.g., fine-grained ticks).  
Archive or prune old records periodically.  
Keep a path open (ADR) to migrate to Postgres or another DB via the StoragePort if needed.

## Risk 5 — API rate-limit breaches
**Mitigation:**  
Backoff + queueing in Kraken adapter. Centralized rate-limit budget tracking.

## Risk 6 — Restart / Reconciliation Errors
**Description:**  
On restart, local DB state and Kraken state may diverge (open orders, balances), risking double-execution or incorrect position tracking.

**Mitigation:**  
Design a reconciliation routine (Phase 5) that:
- Fetches open orders and balances from Kraken
- Compares them with DB state
- Adjusts internal positions and state without placing new trades until consistency is proven  
Until then, treat restart as a higher-risk operation with manual checks described in the Operations Guide.