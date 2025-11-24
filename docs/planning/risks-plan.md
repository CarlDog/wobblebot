# Project Risk Management Plan

This document tracks project‑level risks (schedule, scope, resources) and mitigation strategies.  Architectural risks are covered in `/docs/architecture/risks.md`.

## R‑001 – Scope Creep

**Description:** The temptation to add features (e.g., support for additional exchanges, complex strategies) before completing v1.0.

**Impact:** Delays the initial release, increases complexity, and introduces new failure modes.

**Mitigation:**
- Keep strict focus on phase goals and requirements.
- Park “cool ideas” in a post‑v1.0 backlog for future consideration.
- Use ADRs when scope changes are justified to document and review changes.

---

## R‑002 – Time & Capacity Constraints

**Description:** Personal availability and bandwidth may fluctuate.  Some stages may take longer than expected.

**Impact:** The roadmap slips, potentially causing loss of momentum.

**Mitigation:**
- Keep stages small and shippable to promote regular progress.
- Allow pausing between phases without leaving the system in a broken state.
- Prioritize tasks that maintain momentum (e.g., incremental improvements) when time is limited.

---

## R‑003 – External API or Platform Changes

**Description:** Kraken, banking, or LLM APIs may change endpoints, authentication methods, or rate limits.

**Impact:** Breaks adapters, causing downtime or unexpected behavior.

**Mitigation:**
- Encapsulate all external calls behind adapters.  Keep external endpoints and credentials in config files.
- Write automated smoke tests that fail loudly when external behavior changes.
- Regularly monitor API change logs and update adapters proactively.

---

## R‑004 – Overcomplication of Architecture

**Description:** Adding too much ceremony relative to the actual complexity of the bot.  Over‑engineered patterns could slow down development and onboarding.

**Impact:** Increased cognitive load, more code to maintain, slower iteration.

**Mitigation:**
- Periodically review the architecture docs against reality and prune unnecessary complexity.
- Allow pragmatic shortcuts when they are clearly safe and documented (e.g., using simpler patterns where appropriate).
- Use lightweight ADRs to justify major design decisions and avoid reinvention.

---

## R‑005 – Safety Misconfiguration

**Description:** Misconfigured thresholds could allow higher‑than‑intended exposure or transfers.

**Impact:** Potential financial loss or violation of safety rules.

**Mitigation:**
- Set conservative defaults for all risk parameters.
- Hard‑code absolute safety caps in code and configuration.
- Emit clear logs and notifications when approaching safety limits.
- Require manual confirmation for high‑impact mode changes (e.g., enabling Harvester active mode).

---

## R‑006 – Documentation Drift

**Description:** Docs fall out of sync with the system as it evolves.

**Impact:** Incorrect or outdated knowledge could lead to poor decisions or onboarding challenges.

**Mitigation:**
- Treat documentation updates as part of the definition of done for every change.
- Use code reviews to ensure that documentation reflects changes.
- Schedule periodic documentation reviews at the end of each phase to catch outdated information.

---

> This risk plan focuses on project management and process risks.  Technical risks related to architecture, data consistency, and performance are addressed in `/docs/architecture/risks.md`.