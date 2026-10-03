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

## Final local-gap reconciliation (2026-10-03 follow-up)

The earlier PY-05 disposition stopped too early: no authoritative requirement
forbids a meaningful image probe. Fleet `standards/python-service.md` PY-05
requires a Dockerfile `HEALTHCHECK`; roadmap P3 slice 8 requires actual freshness
and HTTP health, and `docker/Dockerfile` intentionally has no default daemon.
These are compatible. The image now uses an explicit role-configured probe,
Compose retains its specific probes, delivery gains its missing heartbeat probe,
and one-shot tools explicitly disable inherited daemon health. The generator
preserves both overrides and disabled probes. There is no PY-05 exception to ask
for; current execution evidence belongs in the roadmap.

Comprehensive reconciliation of the finite baseline and every retained catalog
gate, including the accepted later-phase boundaries, found these dispositions:

- FR/NFR and phases 1-8, P0-P4 implemented portions, N1-N5: exercise local
  configuration/deprived-env, approval/capability, storage upgrade/recovery,
  provider contracts, packaged image and offline integration gates. No unfinished
  runtime stub was found in the added configuration, delivery, health or doctor
  paths. The new health wiring and usage-error exit normalization are the local
  defects addressed by this follow-up. No speculative module rewrite is needed.
- G4/PB1-PB13 and the triggered NW04 defect: offline fixes remain implemented;
  rerun their tests with the full suite. A paid provider campaign is not authorized.
- G1/G3/P4.6: readiness records exist; actual private history/canonical scoring
  and the data-dependent design gates remain unavailable. Synthetic fixtures
  cannot substantiate those acceptance criteria or justify invented conclusions.
- G2/G5-G8/G10: each retained row still requires its original consumer, adoption,
  policy, observed trigger or outcome evidence. No new qualifying evidence arose
  in this reconciliation; catalog membership alone is not authorization to build
  every proposal. The equity flag is implemented; dependent Phase 9 work remains
  explicitly deferred by the operator, not represented as completed.
- Documentation/setup/versioning: README and AGENTS/CLAUDE point to the current
  roadmap rather than duplicate status; build/deployment guides now describe
  health-role configuration, one-shot behavior and the alpha.2 checkpoint. Config
  examples, hashed dependencies, least-privilege grants and migration contracts
  remain test-enforced. Cohesive large modules stay outside optional refactoring.
- True external gates remain: six actual private config checks; hosted Windows,
  CI/CodeQL and remote metadata; authorized real-provider and NAS qualification,
  production observations; OpenChronicle access. No local mock, image or passed
  machine audit substitutes for them. Retained pylint is the only newly accepted
  standards deviation; no broader waiver is inferred.

Hand checks: `git ls-files pyproject.toml .github/workflows docker/Dockerfile`
confirms tracked sources; pyproject uses Black/isort/pylint, the image base is
unpinned before N2, and installed hooks retain all four security properties.

## Pre-migration manual reconciliation (historical)

- PY-03: tracked hashed runtime/build/dev locks and exact dev pins are consumed
  by Make/CI/Docker. The cross-lock test prevents untested runtime versions.
- PY-05: actual image is multi-stage/non-root and digest-pinned. Health checks
  are per-service in Compose, not a generic Dockerfile HEALTHCHECK: one image
  also serves one-shot tools. This remains a literal standard deviation, not a
  false claim that the machine script checked it.
- PY-06: tracked separate quality and Linux/Windows Python matrix both gate
  publication. Existing Black/isort/pylint/mypy are retained under ADR-048;
  Ruff migration remains a standards gap. Replacing the whole lint/format stack
  is a separately scoped tooling migration, not a product defect repair.
- PY-07: architecture AST, withdrawal authority, approval and capability tests
  cover new lifecycle/delivery/health/provider wiring. No framework types entered
  the domain or service API; explicit legacy adapter/helper exceptions stay bounded.
- UNI-09: current stamp names the audited standard and links these gaps; it does
  not claim blanket conformance or accepted exceptions.
- UNI-12: manifest/runtime/installed version synchronization is test-enforced.
  The authorized local prerelease is explicitly unpublished; no existing tag is
  moved. The existing release checker deliberately does not compare prerelease
  suffixes; it still displays current/latest identity without asserting an update.
- UNI-17: SECURITY.md is tracked and describes real financial/key threat boundaries.
- UNI-18: missing DEVELOPER-TOOLS.md was a verified documentation gap; it now names
  required/recommended tools and scoped Azure applicability without installing any.
- UNI-11/19 and CodeQL: current remote/hosted evidence remains unavailable; no
  local config or historical receipt is substituted for it.
- UNI-21: authorized OpenChronicle MCP is unavailable; no CLI, project creation
  or metadata mutation was used. Machine NA is not verification.

Required private config drift checks remain skipped because the operator files
are absent; no exception has been accepted. Config example parity, deprived CLI
behavior, local quality, upgrade gates and offline integration are exercised.
Large cohesive SQLite/engine/operator modules are retained, with future extraction
only on a concrete ownership/change need. No speculative rewrite was performed.
No remote issue/PR, access change, publish, deployment or real money action occurred.

## Historical Ruff follow-up: demonstrated coverage conflict

The previous blanket statement that migration was unrelated work was too broad.
The operator explicitly requested assessment and routine remediation. Read Fleet
`standards/python-service.md` PY-01 and PY-06 at the pinned audit source: PY-01
requires replacing Black, isort **and pylint** with Ruff, explicit rules
`E,W,F,I,UP,B,C4,PIE,SIM,RET`; PY-06 requires Ruff lint/format and mypy in CI.
The accepted closeout plan §3 says not to add a second lint stack merely to flip
the machine check. ADR-048 retained the old stack during its build change; that
temporary local decision does not by itself prohibit a later migration.

A disposable two-module negative control imports each module from the other and
uses the imported value. Pylint's existing `R0401/cyclic-import` check rejects it;
Ruff's prescribed rule set accepts it. The project's AST test verifies forbidden
layer edges, not all same-layer cycles. This is a concrete coverage difference,
consistent with [Ruff's official FAQ](https://docs.astral.sh/ruff/faq/#how-does-ruffs-linter-compare-to-pylint).
Thus blindly deleting pylint would conflict with the user's instruction not to
weaken checks; adding a duplicate stack would conflict with the accepted patch
rule and would not satisfy PY-01's literal replacement requirement. No exception
is accepted by recording this conflict. The operator has been asked whether to
retain semantic pylint checks during a migration or adopt the Fleet replacement
coverage intentionally. Current disposition/commands belong in the roadmap.


## Approved Ruff disposition (ADR-054)

The operator explicitly accepted retaining pylint's semantic checks during Ruff
migration. Ruff now owns formatting/imports and the canonical rule selection;
Make/CI/editor retain mypy and unchanged pylint checks. The prior pending choice
is resolved. This is an accepted repository-specific deviation from PY-01's
literal pylint removal, not blanket Fleet conformance. No Fleet standards changed.
PY-06's Ruff quality steps are implemented. Verification receipts belong in the
roadmap; hosted/remote/private checks remain independently visible. The later
healthcheck follow-up above resolves PY-05 through implementation, without waiver.
