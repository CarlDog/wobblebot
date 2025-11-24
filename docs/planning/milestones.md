# Milestones & Deliverables

This document defines concrete checkpoints tied to the roadmap phases and stages.  Each milestone represents a reviewable, demo‑able snapshot of the project.

## M1 – Phase 1 Complete: “Skeleton Walker”

**Covers:** Phase 1 (Stages 1.1–1.5)

**Definition of Done:**

- The repository structure exists with linting and formatting configured.
- Core domain models and ports are defined.
- SQLite adapter works with basic read/write operations.
- A mock exchange supports simulated trades.
- An end‑to‑end simulated run works from config → core → mock → database → logs.

**Demo:**

Run a canned scenario where WobbleBot “trades” in pure simulation and outputs a summarized report of trades and P&L.

---

## M2 – Phase 2 Complete: "Micro-Grid Live"

**Covers:** Phase 2 (Stages 2.1–2.5)

**Definition of Done:**

- Kraken adapter integrates with the exchange's public and private endpoints (read‑only key).
- Micro-grid strategy is operational with at least one coin.
- Multi‑asset support works with configured caps.
- The bot can run in either full paper mode or tiny‑size live mode.

**Demo:**

Run WobbleBot against the real Kraken exchange for a defined window and show logs and database entries for completed cycles.  Demonstrate risk controls (e.g., daily spend caps).

---

## M3 – Phase 3 Complete: “Advisor in the Loop”

**Covers:** Phase 3 (Stages 3.1–3.5)

**Definition of Done:**

- Data collector produces volatility and performance metrics per coin.
- AdvisorPort and LLM adapter are working with an enforced JSON schema for recommendations.
- Suggestions are recorded in the database and visible in logs or UI.
- Optional safe auto‑tuning is enabled via configuration with bounded changes.

**Demo:**

Show a trading session in which the Advisor generates suggestions and the operator can review or auto‑apply them.  Illustrate how bounds prevent unsafe changes.

---

## M4 – Phase 4 Complete: “Harvesting Profits”

**Covers:** Phase 4 (Stages 4.1–4.5)

**Definition of Done:**

- Harvester domain model and thresholds are defined.
- Read‑only balance monitoring is operational.
- Transfer proposals are logged in passive mode.
- Guarded withdrawals from Kraken to the bank are working with safety caps; deposits may remain manual or disabled initially.
- All harvester actions are fully auditable and follow configured limits.

**Demo:**

Demonstrate a sequence where trading increases the Kraken balance and the Harvester proposes (and possibly executes) a withdrawal to the bank.  Show that the logs and database reflect the correct transactions.

---

## M5 – Phase 5 Complete: “v1.0 Operational WobbleBot”

**Covers:** Phase 5 (Stages 5.1–5.5)

**Definition of Done:**

- A dashboard or UI is available for monitoring and basic controls.
- Startup/shutdown and restart behavior are robust.  Reconciliation logic properly reloads positions and open orders without duplicating actions.
- Performance is tuned for the Synology NAS environment.
- Documentation is updated across architecture, planning, and implementation.
- A v1.0 release tag is created with a changelog.

**Demo:**

Run WobbleBot “in the wild” over an extended period with the full stack (trading, advisor, harvester).  Show the dashboard visualizing live data and confirm stability and correctness.
