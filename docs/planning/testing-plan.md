# Testing Strategy

This document defines WobbleBot’s testing approach across phases.  Tests are essential to ensure deterministic behavior, safety, and reliability.

## Testing Levels

1. **Unit Tests**
   - Cover core domain logic: grid engine calculations, safety checks, threshold computations.
   - Validate ports and adapters with mocks.

2. **Integration Tests**
   - Exercise end‑to‑end flows using mock adapters (e.g., mock exchange with withdrawal capability).
   - Limited live API tests against Kraken when using real keys; these must be gated behind environment flags to avoid accidental trades.

3. **System Tests**
   - Full WobbleBot runs in sandbox or paper‑trading mode.
   - Longer “soak” tests to observe behavior over hours or days.

4. **Operational Tests**
   - Restart and recovery scenarios: verify that the bot resumes without duplicating actions.
   - Error handling: simulate network failures, API errors, or timeouts and verify proper retries or fallbacks.

## Phase‑by‑Phase Testing Focus

### Phase 1 – Foundation

- **Unit tests** for domain models and port interfaces.
- **Integration tests** for the simulated pipeline using mock exchange and SQLite.

### Phase 2 – Core Trading

- **Unit tests** for the micro-grid engine, grid generation, and safety enforcement.
- **Integration tests** with real Kraken endpoints for price retrieval and minimal order execution (gated by flags).

### Phase 3 – Advisor Integration

- **Unit tests** for JSON schema validation and parsing of advisor recommendations.
- **Integration tests** for the AdvisorPort with dummy LLM responses and end‑to‑end tests verifying suggestions are captured.

### Phase 4 – Harvester

- **Unit tests** for threshold logic, caps, and guardrails.
- **Integration tests** for “dry‑run” transfer proposals; limited live withdrawal tests with small amounts.

### Phase 5 – Hardening

- **System tests** for restart/resume correctness, long‑running stability, and stress scenarios.
- **Recovery tests** for reconciliation logic when database state and exchange state diverge.

## Tooling

- Use **pytest** for unit and integration tests.
- Use **tox** (optional) for running tests across multiple Python versions.
- Use a continuous integration system (e.g., GitHub Actions) to run the test suite on each pull request and enforce coverage thresholds.

## Test Data & Fixtures

- Utilize saved market data snapshots for deterministic tests.
- Provide static JSON fixtures for Advisor recommendations and Harvester proposals.
- Use stub servers or in‑memory mocks for Kraken API (trading + withdrawals per ADR-004) during tests to avoid external side effects.

## Definition of “Tested Enough” per Stage

A stage is considered test‑complete when:

- New logic has unit tests covering both happy paths and anticipated failure modes.
- Integration tests pass for any affected flows.
- For anything that touches real money (Phase 2 onward), there is at least one dry‑run scenario and a documented manual test procedure before enabling active modes.

Testing is not a separate phase—it accompanies implementation.  The project will resist scope creep by requiring tests and documentation for each new feature before merging.
