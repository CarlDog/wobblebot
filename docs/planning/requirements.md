# Requirements & Scope

This document enumerates WobbleBot’s functional and non‑functional requirements.  Each requirement may reference a target phase for initial implementation.

## Functional Requirements (FR)

### FR‑001 – Micro-Grid Trading

WobbleBot **SHALL** implement a configurable micro-grid strategy for each tracked coin:

- Grid ranges and step sizes are defined per asset.
- Orders are placed as limit orders via the exchange adapter.

**Target Phase:** 2

---

### FR‑002 – Multi‑Asset Support

WobbleBot **SHALL** support concurrent trading in multiple whitelisted assets (e.g., DOGE, ADA, SOL, MATIC, ETH).

**Target Phase:** 2

---

### FR‑003 – Safety Limits

WobbleBot **SHALL** enforce safety limits:

- Per‑asset maximum funds in play.
- Global maximum exposure across all assets.
- Per‑day spend caps.

**Target Phase:** 2

---

### FR‑004 – Advisor Integration (Read‑Only)

WobbleBot **SHALL** send summarized trading metrics to an LLM Advisor and store the Advisor’s JSON recommendations.

**Target Phase:** 3

---

### FR‑005 – Advisor Suggestions (Optional Auto‑Apply)

WobbleBot **SHALL** support optional auto‑application of Advisor suggestions within **strict, preconfigured bounds**.  Auto‑applied changes MUST be constrained to:

- A predefined whitelist of fields (e.g., grid spacing, grid width, per‑asset exposure caps).
- Per‑field minimum and maximum limits defined in configuration.
- Changes that do **not** violate global exposure or safety limits.

All auto‑applied changes SHALL be:

- Schema‑validated against the Advisor JSON schema.
- Range‑checked against configured bounds.
- Logged with before/after values and the triggering suggestion ID.

**Target Phase:** 3

---

### FR‑006 – Harvester Balance Management

WobbleBot **SHALL** monitor Kraken versus bank balances against configurable thresholds and generate transfer proposals indicating:

- Proposed direction (Kraken → bank or bank → Kraken).
- Proposed amount.
- Reason (e.g., “Kraken balance above target band”).

**Target Phase:** 4

---

### FR‑007 – Guarded Transfers

When Harvester is in active mode and the user has opted in, WobbleBot **SHALL** perform **automated withdrawals from Kraken to bank** conforming to:

- A maximum transfer per action.
- A maximum transfer per day.
- Minimum and maximum exchange liquidity thresholds.

Initial implementations should prioritize secure **Kraken → bank** withdrawals.  Automated **bank → Kraken** deposits may be added later and **MUST** be governed by an ADR and additional safety review.

**Target Phase:** 4

---

### FR‑008 – Observability

WobbleBot **SHALL** provide:

- Structured logs for all major events.
- Persisted trade and transfer history.
- Metrics suitable for dashboards.

**Target Phases:** 2–5 (incremental)

---

## Non‑Functional Requirements (NFR)

### NFR‑001 – Determinism

Given identical configurations and historical data, the Bot Core **SHOULD** produce identical results.  This influences architecture, testing, and logging.

---

### NFR‑002 – Safety & Isolation

- The LLM Advisor **SHALL NOT** execute trades or move funds.
- The Harvester **SHALL** be the only module with withdrawal capability.
- The system **SHALL** separate concerns via ports and adapters to prevent cross‑module interference.

---

### NFR‑003 – Performance

WobbleBot **SHOULD** run comfortably on a Synology NAS with reasonable CPU and memory usage.  This influences polling intervals, data aggregation, and the size of the LLM model.

---

### NFR‑004 – Recoverability

On restart, WobbleBot **SHALL** be able to:

- Reload positions and open orders.
- Resume trading safely without double‑executing actions.
- Reconcile the database with the exchange state (Phase 5).

**Target Phase:** 5

---

### NFR‑005 – Configurability

All operational behaviors (coins, grids, thresholds, modes) **SHALL** be controlled by configuration files or environment variables.  Default values and ranges must be documented.

**Target Phases:** 1–2

---

> **Authoritative Source:**  This file is the canonical source for requirements.  The roadmap, milestones, and design documents reference these requirements rather than re‑stating them.  Changes to requirements should be applied here first and then propagated to other docs.