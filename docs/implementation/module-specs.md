# Module Specifications

This document explains how to write and maintain per‑module specifications.  Each major module should eventually have its own spec file—for example, `module-specs-bot-core.md` or `module-specs-harvester.md`.

## Purpose

Module specs bridge the gap between the high‑level architecture and actual code.  They answer: **What does this module do, what interfaces does it provide, and how is it configured?**

## Spec Template

When creating a module spec, follow this outline:

1. **Overview** – A high‑level summary of the module.  Describe the problem it solves and link to related architecture docs.

2. **Responsibilities** – List what the module owns and what it explicitly does *not* own.  Clarify boundaries to prevent scope creep.

3. **Interfaces / Ports** – Describe the public functions or methods exposed by the module.  List the port interfaces it implements or depends on.  Specify input and output schemas or data types.

4. **Data Flow** – Explain what inputs the module consumes and what outputs it produces.  Detail where data is persisted or forwarded.

5. **Error Handling & Safety** – Enumerate what can go wrong.  Describe how the module surfaces failures and what safety rules it enforces.

6. **Configuration** – List the configuration values that affect this module, including defaults and valid ranges.

7. **Dependencies** – Mention external services/APIs used and internal modules it interacts with (through ports).  Document any required credentials or environment variables.

8. **Testing Notes** – Highlight key edge cases and how to mock dependencies.  Provide guidelines for writing unit or integration tests for this module.

## Example – Bot Core (High‑Level)

**Overview:** Implements deterministic micro‑grid trading logic.  It manages grids for each asset, places orders via the exchange adapter, and tracks P&L and exposures.

**Responsibilities:** Decide when to place or cancel orders, enforce safety limits, update positions and cycle counters.  It does **not** know about LLM suggestions or funds transfers (handled by Advisor and Harvester).

**Interfaces:** Implements `ExchangePort`, `StoragePort`, and `NotifierPort`.  Provides methods like `run_cycle()`, `on_order_filled()`, and `calculate_grid_levels()`.

**Data Flow:** Consumes price and balance data from the Data Collector, reads configuration parameters, and emits order requests to the Kraken adapter.  Persists trades and positions to the storage adapter.

**Error Handling & Safety:** Catches API errors and logs them; retries with backoff.  Enforces max spend and exposure caps.  Cancels open orders on termination or on stop conditions.

**Configuration:** Per‑asset grid ranges, step sizes, and caps.  Global safety limits (e.g., max total exposure, max trades per cycle).  Polling interval for cycles.

**Dependencies:** Uses the Kraken adapter (via `ExchangePort`), SQLite adapter (via `StoragePort`), and Notifier adapters (via `NotifierPort`).

**Testing Notes:** When unit testing, mock the exchange and storage ports.  Test grid generation logic for various price scenarios.  Simulate partial fills and failure conditions.

## Where Module Specs Live

Per‑module specs can live in this folder as separate Markdown files with the naming convention `module-specs-<module>.md`.  If they grow large, consider placing them in a `modules/` subdirectory (e.g., `/docs/implementation/modules/bot-core.md`).

This file remains the canonical template and guideline.  Actual specs should reference this document when they are created.