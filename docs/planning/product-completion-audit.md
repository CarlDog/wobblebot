# Product completion audit work item

Work item: `CarlDog/wobblebot:product-completion-audit`. Outcomes and command
receipts belong in [roadmap.md](roadmap.md#cloud-product-completion-verification).
This is the punch list, recorded before audit-driven repairs; no issue is filed.

## Source and scope

Read-only Fleet Kit source: `CarlDog/claude-fleet-kit`, commit
`990747556e73d874d76d4ab6d2212460eabe7652`, plugin 0.21.0, standards 3.1.
Applied `plugins/fleet-kit/skills/{phase-end-audit,repo-standards-audit}/SKILL.md`
and the bundled phase-end and hexagonal rules. Register confirms a full-tier
Python service. User authorization permits local fixes without routine checkpoints;
it does not authorize issue publication, OpenChronicle changes or deployment.

## Findings before repairs

- PY-01: no Ruff configuration. Existing exact-pinned Black/isort/pylint/mypy
  gates are real and retained until an explicit N2 tooling decision. The old
  stabilization exception does not make the new standard pass.
- PY-03: manually inspect exact dev pins and lock consumption; a new untracked
  lock on disk is not committed reproducibility evidence.
- PY-05/PY-06: manually inspect Docker and CI; the machine output does not
  establish these requirements. Runtime currently floats and CI lacks the
  required separate quality/platform matrix. N2 owns the changes.
- PY-07: AST dependency-boundary tests and capability/firewall tests exist.
  Review new provider-health and subsequent lifecycle wiring against them.
- UNI-09: stamp describes older standards. Update only with a fresh truthful
  audit receipt; never stamp blanket conformance over failures or unknowns.
- UNI-11/UNI-19: remote default branch and visibility cannot be established by
  the checker. GitHub API connectivity is blocked; historical receipts differ
  from current remote evidence.
- UNI-21: verify via authorized OpenChronicle MCP if exposed. Do not use a CLI,
  create a project, or alter metadata to make the audit pass.
- UNI-12/UNI-17/UNI-18: machine reports unassessed, not verified conformance.
- N1 docs must describe staged configuration, permission setup, WAL directory
  mounts, migration/rollback and shared operator-table authority.
- Existing cohesive large SQLite/operator/engine modules merit future extraction
  review; do not turn this audit into speculative rewrites.
- Re-run phase-end config/deprived-env checks and review README, CLAUDE, AGENTS,
  changelog, dependency decisions, hook/identity/history evidence at final gate.

Hand checks: `git ls-files pyproject.toml .github/workflows docker/Dockerfile`
confirms tracked sources; pyproject uses Black/isort/pylint, the image base is
unpinned before N2, and installed hooks retain all four security properties.
