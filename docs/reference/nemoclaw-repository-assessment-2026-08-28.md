# NemoClaw repository assessment — 2026-08-28

**What this is.** A source-backed assessment of the canonical
[`NVIDIA/NemoClaw`](https://github.com/NVIDIA/NemoClaw) repository and the patterns that could
materially improve WobbleBot. The purpose is to find useful engineering evidence without
turning NemoClaw's feature list into a speculative WobbleBot backlog.

**Status.** External reference assessment only. It is not an ADR, implementation commitment,
security certification, or project-status document. Binding decisions live in
[`decisions.md`](../architecture/decisions.md) and
[`ratified-decisions.md`](../architecture/ratified-decisions.md); sequencing and status live only
in [`roadmap.md`](../planning/roadmap.md).

**Reviewed baselines.**

- NemoClaw clean `main` at
  [`b7261ff7cc73c76a15deb3e95291c24b1624534e`](https://github.com/NVIDIA/NemoClaw/tree/b7261ff7cc73c76a15deb3e95291c24b1624534e),
  package version `0.1.0`, described by Git as `v0.0.115-2-gb7261ff7c`.
- Local NemoClaw copy: `D:\GitHub\NemoClaw`, origin
  `https://github.com/NVIDIA/NemoClaw.git`.
- WobbleBot working tree rooted at `d589216e73e895d383a10cc211c3708c4e4635e5`, including the
  existing user-owned uncommitted documentation/vendor work. The checkout was 14 commits behind
  its locally recorded `origin/main`; the relevant deployment, command-lifecycle, notification,
  advisor, and test-lane source findings were checked against that recorded tip and remained
  present. Prior untracked assessments were used only to avoid duplicating their conclusions.
- The existing
  [`OpenClaw integration assessment`](openclaw-integration-assessment-2026-08-27.md) and
  [`Ollama repository assessment`](ollama-repository-assessment-2026-08-27.md) were treated as
  prior work. Corroborating evidence below does not create a duplicate recommendation.

**Method.** Read-only review of NemoClaw's source, schemas, security guidance, lifecycle code,
CI, tests, release metadata, and official documentation, followed by comparison with WobbleBot's
source, Compose deployment, ADRs, current roadmap, and recorded operational incidents. The
NemoClaw test suite was not run; this is a static architecture and source assessment, not a
runtime qualification or penetration test.

**Current-stage check.** During the review, the root guidance still said P4 was unstarted while
the authoritative roadmap recorded P4.1–P4.5 and P4.7 as shipped. The 2026-08-28 documentation
reconciliation corrected that pointer. P4's buildable scope remains complete except the
deliberately gated P4.6 Historian ([roadmap](../planning/roadmap.md)); this assessment does not
pull P4.6 or any later phase forward.

---

## Executive verdict

Do **not** run WobbleBot inside NemoClaw or adopt OpenShell, NemoClaw's agent lifecycle, model
router, plugin surface, or policy engine as a WobbleBot dependency.

NemoClaw is designed to contain an autonomous, potentially tool-using agent. WobbleBot is a
deterministic financial system whose narrow ports, typed commands, explicit approval boundary,
and fragmentation of trading and transfer authority are deliberate safety properties. Importing
an agent control plane would increase availability risk and the trusted computing base without a
demonstrated trading or operator use case.

NemoClaw is nevertheless a strong pattern source. Six proportional actions are supported by
concrete WobbleBot evidence; two corroborate prior assessments rather than create new work:

1. **Enforce WobbleBot's authority boundaries in deployment**, not only in Python. This is the
   highest-value result and independently corroborates the OpenClaw assessment.
2. **Make dependency resolution deterministic and deploy images by digest.** This is the clearest
   new finding from the NemoClaw review.
3. **Record a bounded deployment/startup receipt** for the image, settings, and prompts. WobbleBot
   has already experienced mounted-config and prompt drift.
4. **Add a CI lane-completeness guard.** An integration test previously rotted into a collection
   error because the default CI lane never collected it.
5. **Add centralized log redaction.** This is a small defense around existing logging, not a new
   secret-management system.
6. **Consolidate existing health facts into a read-only structured readiness report.** This is the
   existing OpenClaw Doctor candidate, strengthened rather than duplicated here.

NemoClaw also strengthens the case for the already-documented atomic command claim and durable
notification outbox work. Those should remain one backlog item each, not be relabelled as new
NemoClaw discoveries.

---

## The architectural mismatch matters

NemoClaw combines a host CLI, versioned blueprint, OpenShell gateway, sandboxed agent, routed
inference, and declarative egress policy. Its
[`architecture`](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/architecture.mdx#L14-L18)
explicitly distinguishes OpenShell's general runtime from NemoClaw's opinionated reference stack.
The gateway stores credentials and injects them at an L7 boundary; the untrusted sandbox does not
receive raw provider keys
([architecture](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/architecture.mdx#L161-L190)).

WobbleBot has a different trust shape:

| Boundary | NemoClaw | WobbleBot consequence |
|---|---|---|
| Workload | Arbitrary always-on agent and tools | Trusted, deterministic daemons plus advisory-only LLM output |
| Primary containment | OpenShell sandbox, gateway, policy, seccomp, Landlock, netns | Ports/adapters, typed commands, process separation, Docker |
| Credentials | Gateway-side placeholder injection | Adapters sign/call providers directly using process credentials |
| Inference | Host-routed and intercepted | Explicit provider adapters, cost gate, retry policy, seat register |
| Mutation | Versioned lifecycle transactions and operator policy approvals | SQLite command firewall, engine/Harvester ownership, operator confirmation |
| State | Blueprint plus managed sandbox lifecycle | YAML configuration, SQLite databases, backups, reconciliation |

This is why the transferable unit is normally a **principle or contract**, not a NemoClaw module.
In particular, Kraken HMAC signing requires the trusted exchange adapter to use the secret; adding
a signing proxy merely to imitate gateway injection would create another financial authority and
failure mode.

---

## Disposition summary

| Area | WobbleBot evidence | Disposition |
|---|---|---|
| Per-service credentials, mounts, and container controls | Confirmed deployment gap; violates the intent of ADR-003 at the container boundary | Adopt; highest urgency; already identified by OpenClaw review |
| Locked dependencies, base-image digest, full deployment digest | Runtime dependencies and image bases can float; deployment uses tags | Adopt proportionally; clearest new finding |
| Image/config/prompt deployment receipt | Actual NAS mount and stale-prompt drift | Adopt a bounded receipt; a persistent joinable manifest is ADR-gated |
| Test-lane membership and integration collection | Actual integration collection failure escaped CI | Adopt a small guard; do not copy NemoClaw's CI scale |
| Central log redaction | Logger serializes arbitrary extras and exception text; no central filter located | Small defense-in-depth candidate; no leak was found |
| Structured read-only readiness report | Existing checks are fragmented; multiple deployment incidents were diagnosable only after the fact | Merge into the existing OpenClaw Doctor candidate |
| Atomic command claim and delivery outbox | Confirmed by prior OpenClaw review | Merge NemoClaw transaction lessons into existing findings |
| High-authority daemon egress restriction | Material blast-radius reduction, but NAS enforcement/availability cost is unknown | Conditional pilot after service isolation |
| Ollama authentication/bind boundary | Existing Ollama review; live NAS boundary not inspected | Verify first; add a proxy only if network isolation is insufficient |
| Semantic configuration validation | WobbleBot already has extensive Pydantic and drift validation | Use only for concrete missing cross-field invariants; no parallel blueprint system |
| P4.6 provenance/Historian | Existing roadmap item with an explicit data/design gate | Design input only when that gate opens |
| NemoClaw/OpenShell runtime, agent plugins, memory, model router, snapshots | No WobbleBot use case; expands trusted computing base | Decline |

---

## Candidate 1 — enforce financial-power fragmentation in Compose

**Classification:** confirmed and highest impact, but not a new backlog item. NemoClaw provides
independent evidence for the finding already made during the OpenClaw review.

The common anchor in [`docker/docker-compose.yml`](../../docker/docker-compose.yml) injects every
Kraken credential—including the withdrawal-enabled Harvester key—every cloud LLM key, the Discord
token, and the web-session secret into every inheriting service. It also mounts the entire data and
configuration trees read-write. Every daemon and the generic tools container inherit that anchor.

That means a compromise in a nominally read-only or low-authority daemon can read a transfer
credential or modify another daemon's database/configuration. The Python code respects ADR-002 and
ADR-003, but the production container boundary does not enforce their intent.

NemoClaw's relevant lesson is explicit capability declaration and deny-by-default containment:

- provider credentials remain in the gateway's store and are substituted only at egress
  ([credential storage](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/credential-storage.mdx#L12-L16));
- its baseline policy scopes writable paths
  ([filesystem policy](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/nemoclaw-blueprint/policies/openclaw-sandbox.yaml#L18-L43))
  and network endpoints
  ([network policy](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/nemoclaw-blueprint/policies/openclaw-sandbox.yaml#L75-L116)); and
- its process guidance combines capability removal
  ([capabilities](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/process-controls.mdx#L17-L55)),
  no-new-privileges and resource limits
  ([runtime controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/process-controls.mdx#L110-L169)),
  and non-root users
  ([runtime users](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/process-controls.mdx#L219-L261)).

### Proportional WobbleBot adaptation

1. Keep a small common anchor only for non-secret runtime defaults.
2. Define and test a per-service **deployment** capability matrix for:
   - credentials present and forbidden;
   - database/config paths and read/write mode;
   - outbound destinations, if later enforced; and
   - container process controls.
   Keep semantic powers—creating an order, approving/claiming a command, rewriting settings, or
   initiating a transfer—in application and database contract tests. Compose alone cannot prove
   them.
3. Give the Harvester credential only to `harvest`; trader credentials only to live/preflight
   paths; provider keys only to actual LLM callers; Discord only to `operator`; the web secret only
   to `web`. Keep the generic `tools` service credential-free by default; split command profiles
   or use explicit invocation-time injection so a one-shot receives only its required capability.
4. Mount configuration read-only for long-running daemons. Preserve a separately invoked,
   explicitly authorized writer path for `apply`/recalibration or deployment bootstrapping.
5. After validating daemon behavior, add `cap_drop: [ALL]`,
   `security_opt: [no-new-privileges:true]`, a read-only root filesystem, explicit temporary
   storage, and PID/file/process limits.

WobbleBot already uses a multi-stage image and a non-root runtime
([Dockerfile](../../docker/Dockerfile)), so this extends rather than replaces the existing model.
Two implementation constraints need deliberate design:

- [`docker/entrypoint.sh`](../../docker/entrypoint.sh) currently creates/copies configuration on
  startup, so a read-only config mount requires separating bootstrap from steady-state execution.
- SQLite WAL and cross-database reads make individual file mounts awkward. Split directories or
  database ownership only after tracing each port's actual read/write graph; do not guess a mount
  matrix from daemon names.

**Protecting evidence.** A Compose contract test should parse a resolved configuration built only
from synthetic/canary env values and fail when a service receives a forbidden canary, forbidden
writable mount, missing hardening control, or mutable production image reference. Never render,
capture, or log a live expanded configuration; live validation is limited to
`docker compose config --quiet`. Runtime self-audit can later compare non-secret container facts
to the same declared matrix.

---

## Candidate 2 — deterministic dependencies and content-addressed deployment

**Classification:** new, high-confidence reliability and supply-chain improvement.

WobbleBot currently has three distinct identity gaps:

1. [`docker/Dockerfile`](../../docker/Dockerfile) uses the mutable base reference
   `python:3.14-slim` for both build and runtime stages.
2. [`pyproject.toml`](../../pyproject.toml) pins development tooling but leaves many runtime
   dependencies as lower bounds. No runtime lockfile was located, so the same source commit can
   resolve a different transitive graph on a later build.
3. [`docker/docker-compose.yml`](../../docker/docker-compose.yml) defaults to mutable `:main` and
   documents `:sha-<short>` as the production pin. A short-SHA tag is operationally useful but is
   still registry metadata, not a content-addressed deployment reference.

NemoClaw's checked-in blueprint pins the sandbox image by exact OCI digest and constrains the
compatible OpenShell cohort
([blueprint](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/nemoclaw-blueprint/blueprint.yaml#L4-L15)).
It also verifies reviewed lock hashes and package integrity
([lock verification](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/scripts/audit-reviewed-npm-graph.mts#L227-L255),
[installed graph verification](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/scripts/audit-reviewed-npm-graph.mts#L316-L327))
and checks base-image pin freshness
([Docker pin workflow](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/.github/workflows/docker-pin-check.yaml#L1-L32)).

### Proportional WobbleBot adaptation

- Select one Python lock mechanism that captures the complete runtime graph and hashes. The choice
  (`uv.lock`, a hashed requirements lock, or equivalent) is less important than making the Docker
  build consume the committed resolution.
- Pin both Python base stages by digest and let an existing dependency-update path propose digest
  refreshes.
- Have CI publish and retain the full GHCR digest for each successful build. Portainer should deploy
  `ghcr.io/carldog/wobblebot@sha256:...`, while human-friendly tags remain discovery aliases.
- Record source revision, dependency-lock hash, base digest, and resulting image digest in the
  deployment receipt.
- Pin third-party GitHub Actions to full commit SHAs as a later small hardening step.

Do **not** copy NemoClaw's custom reviewed npm archive machinery, large native-package audit graph,
or organization-scale release workflows. SBOM or signing/attestation can be evaluated after the
basic lock-and-digest contract has a consumer; they are not prerequisites to obtain most of the
benefit. This does **not** claim byte-for-byte reproducible images: unpinned OS packages, build
timestamps, wheel toolchains, and registry inputs can still vary. The bounded goal is deterministic
Python dependency resolution plus deployment of one identified image digest.

---

## Candidate 3 — a bounded deployment/startup receipt

**Classification:** narrow new response to demonstrated config/prompt drift. A persistent,
joinable runtime-manifest schema is optional and ADR-gated.

The roadmap records that the NAS configuration mount overrides the image and therefore required a
manual prompt sync; the deployed `risk.md` had drifted from the repository
([roadmap](../planning/roadmap.md)). A running process currently has no single bounded receipt that
identifies the image, settings, prompt bodies, and profile it loaded.

NemoClaw treats blueprint and image identity as explicit lifecycle inputs
([blueprint lifecycle](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/architecture.mdx#L237-L296)),
validates immutable image cohorts
([image validation](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/architecture.mdx#L311-L339)),
and exposes producer identity in its machine-readable readiness contract
([producer provenance](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/system-readiness.mdx#L55-L80)).

The smallest justified WobbleBot change is a deployment/startup receipt, not a new database table
or foreign-key graph. Record at least:

- source revision and deployed OCI digest;
- runtime dependency-lock hash;
- resolved profile and a SHA-256 for `settings.yml`;
- SHA-256 for every loaded prompt/heuristic file;
- daemon name, boot ID, and observation timestamp.

Each long-running daemon can emit the bounded receipt once at startup. An approved settings rewrite
should record before/after content identities, and a Doctor/status surface can flag disagreement
among daemons or a change from the operator-declared deployment bundle.

This does not mean baking operator-specific settings into the image or forbidding intentional
edits. It makes the effective deployment identifiable. Model digest/provenance and advisor
resolution lineage remain findings of the Ollama and OpenClaw assessments respectively. They may
contribute fields later, but this config-drift finding does not by itself justify a persistent
manifest ID referenced by suggestions or LLM calls. That joinable schema needs an ADR and a real
forensic query as its consumer.

---

## Candidate 4 — prove that every test belongs to a CI lane

**Classification:** new, small, and supported by an actual detection failure.

WobbleBot's default pytest configuration applies `-m "not integration"`, and the publish workflow
runs only that default command
([pyproject](../../pyproject.toml), [workflow](../../.github/workflows/docker-publish.yml)). The
roadmap records that `tests/integration/test_phase5_operator_e2e.py` rotted into a collection error
and went unnoticed because the default run deselected it.

NemoClaw keeps its Vitest project globs disjoint and exhaustive. Its membership audit rejects a
test with zero, overlapping, unexpected, or incorrect lane membership
([test-lane audit](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/scripts/checks/vitest-project-overlap.mts#L9-L34),
[membership checks](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/scripts/checks/vitest-project-overlap.mts#L174-L194)).

The proportional first step for WobbleBot is not a new test framework:

1. Add a CI command that collects the integration lane even when it cannot safely execute it.
2. Assert that expected integration files collect at least one test and that no test file silently
   falls outside the intended roster.
3. Split deterministic/offline integration tests from genuinely live external tests only if that
   enables the former to run in CI.
4. Keep live financial/external tests opt-in.

A `pytest --collect-only` gate would have caught the recorded failure at very low runtime and zero
network cost. More elaborate lane tooling should wait for a second concrete need.

---

## Candidate 5 — centralize redaction at the logging boundary

**Classification:** small defense in depth; no credential leak was found during this review.

[`src/wobblebot/config/logging.py`](../../src/wobblebot/config/logging.py) emits arbitrary
structured extras and formatted exception text. Individual adapters generally avoid logging raw
keys and several targeted tests protect specific call sites, but no central logging filter was
located. One future exception, nested object, authorization header, cookie, or credential-bearing
URL could bypass call-site discipline.

NemoClaw routes logger values through recursive redaction helpers before presentation
([redaction](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/security/redact.ts#L298-L329),
[logger serialization](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/cli/logger.ts#L108-L126),
[logger emission](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/cli/logger.ts#L191-L219)).

A WobbleBot filter should sanitize messages, nested `extra` data, exception strings, headers,
cookies, URL userinfo/query secrets, and known credential-shaped keys before either the plain or
JSON formatter sees them. Tests should use synthetic canary secrets across nested dictionaries,
lists, exceptions, and both handlers. Redaction must not replace least-secret deployment; it only
limits accidental forensic leakage after a value has entered a process. Pattern/key-based filters
cannot guarantee removal of arbitrary encoded, split, or previously unknown secret formats; even
NemoClaw documents scanner limits
([known limitation](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/best-practices.mdx#L405-L413)).

---

## Existing candidate — a semantic, read-only readiness contract

**Classification:** the existing OpenClaw Doctor recommendation, corroborated and refined here.
It creates no separate backlog item and remains lower urgency than the deployment fixes.

WobbleBot already has health checks, status/preflight commands, schema-drift tests, database
integrity work, reconciliation, and web health cards. The problem is fragmentation and weak
machine semantics, not absence of checks.

NemoClaw's `host probe` returns the same bounded model for human and JSON presentation, including
schema version, stable finding/capability IDs, evidence, provenance, redaction, and deterministic
exit codes. Its JSON path is explicitly read-only, and mutation is reserved for a separate mode
([system readiness](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/system-readiness.mdx#L25-L53),
[read-only boundary](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/system-readiness.mdx#L329-L349)).

Rather than importing a Doctor subsystem, evolve an existing WobbleBot status/health surface to
produce a schema-versioned report with stable finding IDs and evidence. Useful checks are those
that prove real invariants:

- expected daemon boot/heartbeat freshness;
- database reachability, schema version, integrity, and required writeability by role;
- latest reconciliation status and unresolved ambiguity;
- effective image/config/prompt receipt agreement;
- credential presence by role without returning the credential;
- optional, explicitly requested, single-attempt authenticated/API scope checks; and
- disk/retention checks only when their existing roadmap gates permit them.

JSON mode must never repair, rewrite, pull, restart, vacuum, migrate, or rotate anything. Live API
checks should be opt-in, single-attempt, and bounded so readiness does not introduce the repeated
authenticated probing that drove the reader-key lockout/DMS incident. Human output and JSON
should derive from the same model, and secret-shaped evidence must be redacted before either
representation receives it.

---

## Existing findings strengthened by NemoClaw—not new work

### Atomic command ownership and `unknown_after_effect`

The [`OpenClaw assessment`](openclaw-integration-assessment-2026-08-27.md) already documents that
pending-command confirmation is read/check/write, approved payloads are mutable, execution is not
atomically claimed, and a crash after an external effect can leave redispatch ambiguity. The same
shape exists on the Harvester path.

NemoClaw's monotonic recreate journal is useful design evidence. Its declared sequence deletes the
old workload before creating the replacement
([phases](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/onboard/sandbox-recreate-transaction.ts#L24-L32));
it reuses a matching in-flight transaction, rejects target changes, and forbids phase regression
([transaction controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/onboard/sandbox-recreate-transaction.ts#L500-L615)).
It proves source identity before deletion
([source proof](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/onboard/sandbox-recreate-transaction.ts#L955-L976)),
proves the created replacement identity before completion, and cannot abandon a transaction after
effects are recorded
([replacement/terminal controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/onboard/sandbox-recreate-transaction.ts#L617-L705)).

Apply only the needed primitive to WobbleBot: immutable approved payload/version/digest, atomic
`approved -> executing` claim with owner/token/deadline, reconciliation before retry, and an
explicit ambiguous terminal/intermediate state. Do not create a generic lifecycle framework.

### Durable notification delivery

Discord notification forwarding still sends first and marks the row forwarded second. NemoClaw's
state-safety discipline reinforces the prior recommendation for a small outbox with attempt state,
backoff, platform receipt when available, and `unknown_after_send`. This belongs in the existing
typed notification storage, not in Kafka, a general message broker, or NemoClaw messaging.

### Artifact-level 1.0-to-2.0 survivor test

NemoClaw's upgrade classification and lifecycle verification reinforce the existing recommendation
for a test that starts from an actual v1.0 artifact/database/config and proves the 2.0 artifact can
upgrade, reconcile, and restart without losing safety state. Keep this as a 2.0 release gate if it
is still absent; do not build a NemoClaw-style updater.

### P4.6 Historian provenance

NemoClaw's distinction between source identity, observation, bounded evidence, and producer
provenance is useful input to the Historian design. P4.6 remains gated behind canonical Q2-dump
scoring and its own design document. This assessment supplies design evidence only and does not
authorize early implementation.

---

## Conditional improvements that need evidence first

### Outbound network restrictions

NemoClaw's baseline is deny-by-default
([security posture](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/best-practices.mdx#L90-L108))
and can scope allowed egress by host, protocol, method, path, and executable
([policy example](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/nemoclaw-blueprint/policies/openclaw-sandbox.yaml#L75-L116)).
This is appropriate for an untrusted agent.

For WobbleBot, first split credentials and filesystem authority. Then evaluate a narrow allowlist
for the two highest-authority daemons—`live` and `harvest`—against the actual Synology/Docker
networking model and Kraken availability requirements. `news` has a changing feed set, and cloud
provider endpoints can evolve; a brittle policy that stops reconciliation or cleanup is not an
automatic safety win. Prefer host/container controls natural to the deployment over importing
OpenShell.

### Ollama boundary

The Compose deployment reaches `host.docker.internal:11434`. Ollama has no built-in API
authentication, but this review did not inspect the live NAS listener or firewall. Follow the
existing Ollama assessment: prove whether the port is restricted to the intended bridge/admin
path. Add an authenticated proxy that permits inference/status but blocks model mutation only if
network isolation is insufficient. Do not build it speculatively.

### Additional semantic configuration checks

NemoClaw separates structural schemas from semantic invariants and tests that every discovered
configuration artifact is validated. WobbleBot already has strong Pydantic validation and schema
drift tests. Borrow the technique for concrete gaps—such as the proposed Compose capability
matrix or test-lane roster—but do not add a parallel versioned blueprint or validation framework
without an invalid configuration that current checks admit.

---

## Capabilities already present—do not rebuild them

NemoClaw does not reveal a need to replace or generalize these WobbleBot capabilities:

- LLM output remains advisory-only under ADR-002.
- Harvester remains the only transfer authority under ADR-003/ADR-004.
- The LLM-emittable command union excludes withdrawal execution.
- Human confirmation and daemon-kind ownership remain the mutation firewall.
- Engine exposure, spend, order, inventory, and loss controls remain deterministic and inside Bot
  Core.
- Advisor auto-apply remains default-off, bounded, and role-restricted.
- Cloud provider retry, cost gates, seat evaluation, and no-silent-failover policy remain explicit.
- SQLite remains the authoritative financial/audit store; NemoClaw's best-effort JSONL audit is not
  a replacement.
- Maintenance backups, retention, trade reconciliation, outcome scoring, trace IDs, weather
  reporting, and cost-honesty work already exist.
- P4.6 remains the sole planned Historian/memory direction until its evidence gate opens.

---

## Ideas that should not be incorporated

| NemoClaw feature or pattern | Decision | Why |
|---|---|---|
| OpenShell/NemoClaw as WobbleBot's runtime | Decline | Adds a control plane and availability dependency around deterministic daemons |
| General shell/browser/tool execution for the advisor | Decline | Conflicts with advisory-only design and creates no trading-quality evidence |
| Model router or silent provider failover | Decline | Duplicates provider adapters and conflicts with ADR-015 |
| Agent plugins, skills, MCP tool discovery, messaging catalog | Decline | No demonstrated operator workflow; existing MCP policy is demand-gated and read-only first |
| Agent memory, self-learning, workspace snapshots | Decline/defer | P4.6 owns the provenance/scoring gate; database backups already cover operational state |
| Dynamic endpoint approval by unattended daemons | Decline | Endpoint drift should fail and alert; a daemon should not widen its own policy |
| Credential proxy for Kraken signing | Decline | Adds a new financial authority and request-signing failure mode |
| NemoClaw's full CI/release/native-package audit machinery | Decline | Organization-scale overhead; adopt only locks, digests, and small contract checks |
| Raw JSONL audit as financial truth | Decline | NemoClaw itself treats the audit as non-authoritative/best-effort |
| Kubernetes/GPU/fleet/HA/OTLP machinery | Decline | No present consumer or demonstrated NAS problem |
| Automatic updates, model pulls, or fetched-script execution | Decline | WobbleBot's operator-controlled promotion and rollback are safer for financial software |
| Landlock or same-UID process proofs as a claimed guarantee | Decline for now | Kernel/platform support varies; Compose hardening provides a smaller verified step |

No new NVIDIA inference provider should be added merely because NemoClaw supports it. A provider or
model becomes a candidate only if WobbleBot's existing role battery demonstrates better quality,
latency, privacy, or cost at the exact promoted model identity.

---

## A proportional delivery sequence if these findings are accepted

This is ordering evidence, not a roadmap amendment.

### Slice A — close low-cost detection gaps

- Add integration-lane collection/roster coverage to CI.
- Add centralized log redaction with canary-secret tests.
- Define the per-service capability matrix and a failing Compose contract test before changing
  runtime privileges.

### Slice B — make deployment identity real

- Commit and consume a complete hashed runtime dependency lock.
- Pin the two Python base images by digest.
- Publish, capture, and deploy the full GHCR digest.
- Record the first bounded deployment/startup receipt with image/config/prompt identity.

### Slice C — enforce least privilege

- Split secrets by service.
- Separate steady-state read-only config from the authorized writer/bootstrap path.
- Narrow data ownership based on traced port access.
- Add verified capability, no-new-privileges, root-filesystem, temporary-storage, and resource
  controls.

### Slice D — consolidate operations

- Expose the existing health/readiness facts through one redacted, schema-versioned, read-only
  report.
- Merge transaction-journal lessons into the already-open atomic command/outbox work.
- Evaluate high-authority egress and Ollama proxying only after measuring the live NAS boundary.

An ADR is appropriate before changing command lifecycle states, deployment/config ownership, or
persistent provenance schemas. A test-only integration collection gate, central redaction filter,
and pin/lock maintenance may fit existing decisions if their implementation does not alter those
boundaries.

---

## Acceptance evidence for future implementation

A follow-up should demonstrate, as applicable:

1. No service receives a credential outside its declared role; only `harvest` sees the withdrawal
   credential.
2. A read-only daemon cannot rewrite configuration or another daemon's authoritative state.
3. Normal startup, SQLite WAL behavior, backups, reconciliation, and graceful shutdown still work
   under the narrowed mounts and process controls.
4. The Docker build consumes the committed locked dependency graph, and production deploys the
   exact recorded OCI digest; no byte-for-byte reproducible-build claim is implied.
5. Every daemon emits a bounded startup receipt, and a mounted prompt/settings change is visible
   without recording secret content or requiring a new persistent join table.
6. Every integration test is collected by some CI lane; a deliberately broken integration import
   makes CI fail.
7. Every secret in the declared canary corpus is removed from messages, extras, exceptions,
   headers, cookies, and URLs in plain, JSON, and rotating-file logs; the test does not claim to
   detect arbitrary encoded or unknown formats.
8. Readiness JSON is schema-valid and redacted for success, warning, inconclusive, and failure; it
   performs no mutation.
9. Concurrent confirmation/execution tests prove one claimant, immutable approved payload, safe
   reconciliation, and explicit ambiguity after an unconfirmed external effect.
10. Existing ADR-002/003/004 tests still prove that no LLM, web route, operator assistant, or
    low-authority daemon can trade, approve itself, rewrite settings, or transfer funds.

---

## Maturity and licensing cautions

NemoClaw is a useful engineering reference, not a security certification:

- Its own contributor guide calls the project active development with interfaces that may change
  without notice
  ([AGENTS.md](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/AGENTS.md#L6-L16));
- its README labels support alpha/best-effort
  ([README](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/README.md#L68-L70));
- Landlock is configured as best-effort and can be skipped on older kernels
  ([filesystem controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/filesystem-controls.mdx#L179-L186));
- its same-UID topology cannot prove provenance against a malicious same-UID agent
  ([trust boundary](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/tcb-boundary.mdx#L147-L152)),
  while writable agent configuration can redirect inference
  ([filesystem controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/filesystem-controls.mdx#L101-L106));
- host/root compromise remains outside the boundary
  ([trust boundary](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/tcb-boundary.mdx#L19-L23));
- its updater constrains transport to HTTPS but still pipes a freshly fetched script to `bash`
  without a separately verified content digest or publisher signature
  ([updater](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/actions/update.ts#L12-L22)); and
- this checkout contains a concrete documentation/test drift: the contributor guide says the
  blueprint test rejects mutable image tags and mismatched digests
  ([guidance](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/AGENTS.md#L272-L277)),
  but the named test currently contains policy assertions rather than that image-pin assertion
  ([test](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/test/onboarding/validate-blueprint.test.ts#L1-L282)),
  while the structural schema accepts an arbitrary string for top-level `digest`
  ([schema](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/schemas/blueprint.schema.json#L42-L49)).

The repository is Apache-2.0 licensed
([license](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/LICENSE)).
Conceptual adaptation is the expected path because the languages and architectures differ. If any
source is copied, preserve the applicable copyright and license notices and review whether a
NOTICE obligation applies. No NemoClaw code was copied into WobbleBot by this assessment.

---

## Primary NemoClaw source register

- [Repository and README](https://github.com/NVIDIA/NemoClaw/tree/b7261ff7cc73c76a15deb3e95291c24b1624534e)
- [Architecture details](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/architecture.mdx)
- [Security posture](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/best-practices.mdx)
- [Filesystem controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/filesystem-controls.mdx)
- [Process controls](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/process-controls.mdx)
- [Trust boundary](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/security/tcb-boundary.mdx)
- [System readiness contract](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/docs/reference/system-readiness.mdx)
- [Versioned blueprint](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/nemoclaw-blueprint/blueprint.yaml)
- [Baseline network/filesystem policy](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/nemoclaw-blueprint/policies/openclaw-sandbox.yaml)
- [Lifecycle transaction](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/onboard/sandbox-recreate-transaction.ts)
- [Test-lane membership audit](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/scripts/checks/vitest-project-overlap.mts)
- [Central redaction helper](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/src/lib/security/redact.ts)
- [Docker pin freshness workflow](https://github.com/NVIDIA/NemoClaw/blob/b7261ff7cc73c76a15deb3e95291c24b1624534e/.github/workflows/docker-pin-check.yaml)
