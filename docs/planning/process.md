# Development Process & Workflow

This document defines **how** we work on WobbleBot.  It covers the overall approach, branching strategy, code review expectations, and how each stage in the roadmap is executed.

## Overall Approach

- **Phase-based development:** the roadmap defines the current sequential phases and their gates. A subsequent phase does not begin until its predecessor is formally closed.
- **Incremental and test‑first mindset:**  Each stage leaves the system in a working, testable state with unit or integration tests covering the new logic.  We avoid long‑lived feature branches without merging.

## Branching Strategy

- **`main`** — accepted integration line. Tags identify reviewed release commits.
- **`codex/<work-item>`** — short branches from the accepted `main` commit, merged through review. Preserve existing WIP explicitly when starting a batch.
- Historical `v1.1` and `develop` references describe earlier workflows; they do not create another current integration branch.

Pull requests should reference relevant phases/stages and requirement IDs.  Every change that affects behavior should include appropriate tests and documentation updates.

## Phase/Stage Workflow

For each **stage**:

1. Clarify the scope using `requirements.md` and `roadmap.md`.
2. Design or adjust architecture if needed (update `/docs/architecture`).
3. Implement the accepted scope in a short `codex/<work-item>` branch.
4. Write or extend tests (unit, integration, or system as appropriate).
5. Update any relevant documentation (planning, architecture, implementation).
6. Prepare a pull request to `main`; obtain the required review and merge authorization.
7. Verify the integrated commit and applicable CI jobs. Release, publication, deployment and live acceptance are separate evidence gates.

At **phase end**:

- Run broader integration checks across the entire system.
- Prepare the milestone demo defined in `milestones.md`.
- Run the Fleet Kit phase-end procedure plus the project-specific AGENTS.md checks; classify every finding and retain gated work in the backlog index.
- Complete independent plan/diff review and the required non-destructive checks before release. Record the environment, skips and limitations.
- Obtain the concrete release/deployment/close decisions required by the current plan. A source push or green boot does not close a phase.

## Code Review & Quality

- All non‑trivial changes require code review via pull requests.
- Reviews should check for adherence to coding guidelines, correct usage of ports and adapters, proper logging, and adequate tests.
- Reviewers should ensure that changes align with the roadmap and requirements.  Unexpected scope changes should be captured via ADRs and reflected in planning documents.

## Issue Tracking & Tasks

- GitHub is authoritative for issue/PR state. A named local work item may hold the bounded checklist until tracker writes are authorized; never invent an issue number.
- OpenChronicle mirrors the same stable repository/work-item key on a best-effort basis. Keep issue status separate from implementation status.
- Commits and pull requests should reference the stage’s issue to maintain traceability.

## Documentation Discipline

Documentation is part of the definition of done.  When code changes alter the system’s architecture, planning, or usage:

- Update architecture docs in `/docs/architecture` if the change affects system design.
- Update planning docs in `/docs/planning` if the change alters the roadmap, requirements, or milestones.
- Update implementation docs in `/docs/implementation` if the change modifies module specifications, deployment steps, or operational procedures.

Keeping documentation current prevents knowledge drift and reduces onboarding friction.
