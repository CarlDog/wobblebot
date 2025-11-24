# Development Process & Workflow

This document defines **how** we work on WobbleBot.  It covers the overall approach, branching strategy, code review expectations, and how each stage in the roadmap is executed.

## Overall Approach

- **Phase‑based development:**  Five major phases, each broken into five stages.  A subsequent phase does not begin until its predecessor is stabilized.
- **Incremental and test‑first mindset:**  Each stage leaves the system in a working, testable state with unit or integration tests covering the new logic.  We avoid long‑lived feature branches without merging.

## Branching Strategy

- **`main`** – Always stable.  Tagged releases (e.g., `v1.0.0`) are merged here.
- **`develop`** – Integration branch for the current phase.  Can be slightly ahead of `main` but should never be broken.
- **`feature/phaseX-stageY-*`** – Short‑lived branches for a specific stage or task.  These branch off `develop` and merge back into `develop` via pull request.

Pull requests should reference relevant phases/stages and requirement IDs.  Every change that affects behavior should include appropriate tests and documentation updates.

## Phase/Stage Workflow

For each **stage**:

1. Clarify the scope using `requirements.md` and `roadmap.md`.
2. Design or adjust architecture if needed (update `/docs/architecture`).
3. Implement the feature or refactoring in a `feature/phaseX-stageY-*` branch.
4. Write or extend tests (unit, integration, or system as appropriate).
5. Update any relevant documentation (planning, architecture, implementation).
6. Submit a pull request to `develop`; undergo code review.
7. Once merged, run a small integration test (manual or automated) to verify that the system still works end‑to‑end.

At **phase end**:

- Run broader integration checks across the entire system.
- Prepare the milestone demo defined in `milestones.md`.
- Merge `develop` into `main` and tag the release if appropriate.

## Code Review & Quality

- All non‑trivial changes require code review via pull requests.
- Reviews should check for adherence to coding guidelines, correct usage of ports and adapters, proper logging, and adequate tests.
- Reviewers should ensure that changes align with the roadmap and requirements.  Unexpected scope changes should be captured via ADRs and reflected in planning documents.

## Issue Tracking & Tasks

- Each stage is tracked as an issue (or epic) in the project management tool of choice.  Subtasks may include implementation, testing, documentation updates, and demo preparation.
- Commits and pull requests should reference the stage’s issue to maintain traceability.

## Documentation Discipline

Documentation is part of the definition of done.  When code changes alter the system’s architecture, planning, or usage:

- Update architecture docs in `/docs/architecture` if the change affects system design.
- Update planning docs in `/docs/planning` if the change alters the roadmap, requirements, or milestones.
- Update implementation docs in `/docs/implementation` if the change modifies module specifications, deployment steps, or operational procedures.

Keeping documentation current prevents knowledge drift and reduces onboarding friction.