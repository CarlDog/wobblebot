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

## ADR-004 — Use Kraken API for Fund Transfers (No Separate Banking Integration)
**Status:** Accepted
**Date:** November 24, 2025
**Context:** Initial assumption was that Harvester would need to integrate with separate bank APIs (ACH, wire transfer systems) in addition to Kraken. Upon creating a live Kraken account with $100 deposit, discovered that Kraken's API provides withdrawal endpoints that handle bank transfers directly.
**Decision:** Harvester will use Kraken's withdrawal API for all fund transfers (exchange → bank). No separate BankingPort or banking adapter is needed.
**Alternatives Considered:**
- Build separate banking API integration (rejected: unnecessary complexity, YAGNI)
- Abstract BankingPort for future flexibility (rejected: premature abstraction for Phase 1-5)
**Consequences:**
- **Positive:** Simpler architecture, single integration point, less code, easier testing
- **Positive:** Phase 4 implementation is significantly simpler
- **Positive:** Single API key strategy works (Harvester key has withdrawal permissions)
- **Negative:** Tightly coupled to Kraken (acceptable for Phase 1-5, can abstract later if multi-exchange needed)
**Implementation:** Harvester depends only on ExchangePort (with withdrawal-enabled Kraken adapter) and StoragePort

## ADR-005 — Align Domain Models with Kraken API Data Structures
**Status:** Accepted
**Date:** November 24, 2025
**Context:** Phase 1.2 domain model design required decisions on entity IDs (UUID vs string), order status vocabulary, timestamp formats, and field naming conventions. Research into Kraken REST API v0 revealed specific data structure patterns that affect adapter implementation complexity.

**Decision:** Adopt Kraken-aligned data structures in domain models with strategic abstractions:

1. **Dual ID Strategy:**
   - Internal `id: UUID` for database primary keys
   - External `exchange_id: str | None` for Kraken transaction IDs (txid format)

2. **Order Status Values:**
   - Use Kraken's canonical: `"pending"`, `"open"`, `"closed"`, `"canceled"`, `"expired"`
   - Replace `"filled"` → `"closed"` (Kraken's term)
   - Replace `"cancelled"` → `"canceled"` (American spelling matches Kraken)
   - Remove `"failed"` (Kraken doesn't use this; submission failures don't create order records)

3. **Trade Model:**
   - `id: str` (Kraken trade txid, not UUID)
   - `order_id: str` (Kraken parent order txid)
   - `fee: Decimal` (simplified from `Amount` — fee currency is always quote)
   - Add `cost: Decimal` field (Kraken provides this as price × volume)

4. **Timestamp Extensions:**
   - Add `Timestamp.to_unix_seconds()` method for Kraken API format (float seconds)

5. **Position Model:**
   - Defer to Phase 3+ (margin-specific, not needed for spot trading)

**Alternatives Considered:**
- Pure domain-driven design with custom vocabulary (rejected: adapter complexity)
- Abstract exchange interface with factory pattern (rejected: premature for single exchange)

**Consequences:**
- **Positive:** Minimal adapter mapping code, direct API compatibility
- **Positive:** String IDs are industry-standard for crypto exchanges
- **Positive:** Dual ID strategy preserves multi-exchange flexibility
- **Positive:** Clearer semantics (`"closed"` is self-explanatory)
- **Negative:** Domain models favor Kraken conventions (acceptable trade-off for Phase 1-4)
- **Negative:** Breaking change for existing tests (not yet merged, safe to update)

**Compliance:** Aligns with ADR-001 (hexagonal architecture), ADR-003 (safety constraints), and Constraint C-003 (no adapter dependencies in domain).

**References:**
- [Kraken API Reference](../reference/kraken-api-reference.md)
- [Kraken REST API Docs](https://docs.kraken.com/rest/)
- krakenex Python client (reference implementation)
