# OpenClaw integration assessment — 2026-08-27

**What this is.** A source-backed assessment of the canonical
[`openclaw/openclaw`](https://github.com/openclaw/openclaw) repository and the parts of its
architecture that could improve WobbleBot. The purpose is to identify concrete gaps and useful
patterns without turning OpenClaw's feature set into a speculative WobbleBot backlog.

**Status.** External reference assessment only. It is not an ADR, implementation commitment,
security certification, or project-status document. Binding decisions live in
[`decisions.md`](../architecture/decisions.md) and
[`ratified-decisions.md`](../architecture/ratified-decisions.md); implementation status and
sequencing live only in [`roadmap.md`](../planning/roadmap.md).

**Reviewed baselines.**

- OpenClaw `main` at
  [`16eb38cf9725b073bec2cf89842c28d59509231d`](https://github.com/openclaw/openclaw/tree/16eb38cf9725b073bec2cf89842c28d59509231d),
  package version `2026.8.1`, on 2026-08-27.
- WobbleBot checkout `d589216e73e895d383a10cc211c3708c4e4635e5`. The two highest-impact
  findings were also checked against the locally known `origin/main` at
  `7b304ea9acf95f6dbd935d267cb3c2f449730e41` and remained present.

**Method.** Read-only source review of OpenClaw's official repository, documentation, security
policy, and patched advisory history, followed by a comparison with WobbleBot's source, ADRs,
deployment definition, and current roadmap. No OpenClaw installation, runtime benchmark,
penetration test, dependency audit, or production trial was performed. Source-visible crash
windows are labelled as such and are not presented as observed incidents.

---

## Executive verdict

Do **not** embed OpenClaw into WobbleBot or make its Gateway, agent, plugin runtime, scheduler, or
approval system a financial control plane.

OpenClaw is a broad personal-agent Gateway: it connects models, tools, messaging channels,
sessions, memory, plugins, and automations for a primarily single-operator trust model. WobbleBot
is a narrow financial system whose typed commands, hexagonal ports, and separation of trading,
advisory, and transfer authority are safety features. Replacing those boundaries with a general
agent runtime would enlarge the trusted computing base without a demonstrated trading use case.

OpenClaw is useful in two narrower ways:

1. **Borrow implementation patterns** for least-privilege deployment, atomic state transitions,
   immutable approval binding, durable delivery, provenance, and structured diagnostics.
2. **Optionally expose WobbleBot through a constrained MCP boundary** if an operator demonstrates
   a real OpenClaw workflow. The first surface should be read-only. Any later mutation surface may
   request a typed pending command but must never approve or execute it.

The most important result of the review is not an OpenClaw integration feature. It is the
discovery that two existing WobbleBot guarantees stop at the Python boundary: financial-power
fragmentation is not enforced by Docker Compose, and pending-command decisions/execution are not
atomically claimed.

---

## Controlling WobbleBot boundaries

This assessment does not reopen the following decisions.

| Source | Relevance to this assessment |
|---|---|
| [`constraints.md`](../architecture/constraints.md) | Only Harvester transfers funds; the advisor cannot execute; financial credentials are separated. |
| ADR-001 in [`decisions.md`](../architecture/decisions.md) | Ports and adapters remain the architectural boundary. |
| ADR-002 | LLM output is advisory; confirmed pending commands are the execution firewall. |
| ADR-003 / ADR-004 | Harvester alone owns withdrawal authority and uses Kraken through `ExchangePort`. |
| ADR-015 | WobbleBot deliberately avoids silent cross-provider model failover. |
| ADR-034 | Web mutations queue typed commands; approval and money execution remain independently owned. |

Any proposal below that changes one of these boundaries requires a new or superseding ADR. An
external agent is treated as an untrusted requester even when it is operated by the same person
who operates WobbleBot.

---

## What OpenClaw contributes

OpenClaw's official [runtime architecture](https://docs.openclaw.ai/agent-runtime-architecture)
separates its agent loop, session persistence, provider transport, tool policy, plugin-facing
contracts, and Gateway control plane. The pieces most relevant to WobbleBot are:

- **Durable writer claims and queues.** An admitted agent run owns a persisted writer identity;
  stale or superseded writers cannot commit transcript state. Per-session work is serialized and
  global concurrency is bounded.
- **Approval-plan binding.** Its
  [exec-approval design](https://docs.openclaw.ai/tools/exec-approvals) binds an approval to a
  canonical execution plan rather than trusting a later caller to reproduce the approved input.
- **Delivery ambiguity as state.** Its
  [message-lifecycle design](https://docs.openclaw.ai/concepts/message-lifecycle-refactor)
  distinguishes durable intent, send attempt, platform receipt, failure, and
  `unknown_after_send`; it does not pretend every external delivery can be exactly once.
- **Provenance outside model-authored text.** Its
  [memory architecture](https://docs.openclaw.ai/concepts/memory-architecture) stores origin class,
  observation time, and session kind structurally, and gates untrusted or system-derived material
  before model consolidation.
- **Machine-readable diagnostics.** [Doctor](https://docs.openclaw.ai/gateway/doctor) and the
  security audit use stable finding identifiers, severities, evidence, and fix hints, separating
  inspection from repair.
- **MCP client support.** OpenClaw can connect to MCP servers over stdio, SSE, and Streamable HTTP;
  discovered tools still pass through tool policy. Its
  [MCP Doctor](https://docs.openclaw.ai/tools/mcp) can validate configuration and probe a live
  server.

These are transferable principles, not reasons to import OpenClaw's runtime.

---

## Disposition summary

| Area | WobbleBot evidence | Disposition |
|---|---|---|
| Container secret and filesystem isolation | Confirmed deployment gap | Candidate safety correction; highest urgency |
| Pending-command approval and execution ownership | Confirmed concurrency/replay gap | Candidate safety correction |
| Discord notification delivery | Confirmed crash window; no recorded incident located | Candidate reliability correction |
| Advisor resolution lineage | Confirmed forensic join gap and prior hidden provider outage | Fold into the next compatible advisor schema work |
| Read-only deployment Doctor | Existing checks are fragmented; recurring deployment drift exists | Candidate operator capability |
| v1.0-to-2.0 upgrade-survivor test | Individual migrations exist; no artifact-level upgrade lane was located | Candidate 2.0 release gate |
| Historian provenance | P4.6 already planned and design-gated | Design input when its existing gate opens |
| MCP integration | Existing external-demand item; no server or demonstrated workflow | Conditional; read-only first |
| Physical-size/WAL-aware retention | Existing retention/disk-awareness gates have not opened | Conditional on those gates only |
| Non-money daemon run ledger | No missed/duplicate-cycle incident located | Conditional on an operational failure |
| Gateway/runtime, plugins, shell tools, generic memory, production swarms | Conflicts with current safety model or lacks a use case | Decline |
| Cross-provider failover, central scheduler, full OpenTelemetry stack | Existing deliberate alternatives; no demonstrated need | Decline for current scope |

---

## Confirmed finding 1 — deployment collapses credential separation

**Observed fact.** The `x-wobblebot-defaults` anchor in
[`docker/docker-compose.yml`](../../docker/docker-compose.yml) supplies every inheriting service
with:

- reader, trader, and withdrawal-enabled Harvester credentials;
- every cloud-LLM key;
- the Discord bot token and web-session secret; and
- read-write mounts of the complete `/app/data` and `/app/config` trees.

All long-running services and the generic `tools` service inherit that anchor. Therefore a
compromise or arbitrary-code path in `web`, `operator`, `advise`, `news`, or `maintenance` could
read the withdrawal credential or alter another daemon's SQLite/config state, even though normal
Python wiring does not grant those operations.

This conflicts with the intended per-service ownership described in
[`docker/README.md`](../../docker/README.md) and ADR-003. OpenClaw's
[security policy](https://github.com/openclaw/openclaw/blob/16eb38cf9725b073bec2cf89842c28d59509231d/SECURITY.md)
is useful here because it treats process and plugin privileges as part of the trusted computing
base; model instructions and approval prompts are not process isolation.

**Narrow recommendation.** Replace the shared credential/mount bundle with explicit per-service
capability manifests. Preserve one shared anchor only for genuinely common, non-secret runtime
settings.

**Acceptance criteria if ratified:**

- With a synthetic/canary env file, rendered `docker compose config` exposes the Harvester
  canary only to Harvester. Live-stack validation uses `docker compose config --quiet` only;
  expanded live values must never be printed, captured, or logged.
- No long-running service possesses both trader and Harvester credentials.
- Reader, LLM, Discord, news, and web credentials appear only where a traced code path needs them.
- The generic `tools` service does not inherit every credential; one-shot operations receive only
  their required capability through separate profiles or explicit invocation.
- Long-running services mount `/app/config` read-only. A configuration writer is separately and
  explicitly invoked.
- Each daemon has read-write access only to owned SQLite storage; cross-service data is read-only
  where practical. The design accounts for SQLite `-wal` and `-shm` files.
- Containers use a read-only root filesystem, dropped Linux capabilities,
  `no-new-privileges`, and bounded temporary storage unless a tested exception is documented.
- A Compose contract test fails when a service gains an undeclared secret or writable mount.
- Runtime verification proves `web` and `advise` cannot read trader/Harvester credentials or
  mutate live/Harvester state.

An MCP or OpenClaw-facing service must not inherit `x-wobblebot-defaults` in its current form.

---

## Confirmed finding 2 — pending commands lack atomic ownership

**Observed facts.**

- [`confirm_decision.py`](../../src/wobblebot/services/confirm_decision.py) resolves a command by
  reading it, checking status and TTL, and saving a replacement model.
- The [`StoragePort`](../../src/wobblebot/ports/storage.py) explicitly offers no optimistic
  concurrency control for read-modify-write operations.
- [`save_pending_command`](../../src/wobblebot/adapters/sqlite_storage.py) uses an upsert that can
  rewrite `command_json` and status during later lifecycle updates instead of making the approved
  payload immutable.
- [`cli/live.py`](../../src/wobblebot/cli/live.py) selects approved rows, performs the engine
  action, and only then persists `dispatched`. Its source notes that a failed final write leaves
  the row approved and redispatches it on the next poll.
- Harvester uses the same poll/effect/final-write shape. Its independent transfer-result
  uniqueness constraint prevents a straightforward second withdrawal, but it does not make the
  command lifecycle atomic.

Two concurrent decisions can overwrite one another, two consumers can observe the same approved
row, and a crash after an external effect is ambiguous. Confirmation TTL also controls only how
long the operator may decide; it does not limit how long an approved row remains executable.

**Narrow recommendation.** Add database compare-and-set transitions and bind approval to an
immutable typed payload. Do not import a generic shell-approval system.

```text
awaiting_confirmation
  -> approved | rejected | expired       atomic decision

approved
  -> executing(claim_token, deadline)    atomic claim

executing
  -> dispatched | failed
  -> unknown_after_effect                crash/commit ambiguity
```

**Acceptance criteria if ratified:**

- One SQL operation decides a row only while it remains awaiting confirmation and unexpired.
- Concurrent conflicting decisions produce exactly one winner; matching retries are idempotent
  and conflicting retries report the recorded winner.
- The confirmation display is bound to a canonical typed payload, schema version, and digest.
  Later lifecycle updates cannot rewrite the approved command.
- `execute_before` is distinct from the confirmation TTL; stale approvals cannot execute after a
  prolonged outage.
- Dispatch starts only after an atomic `approved -> executing` claim. Only its token/owner may
  terminalize the row, and two processes cannot win the same claim.
- A crash or persistence failure after a possible effect records `unknown_after_effect` and does
  not automatically replay a non-idempotent command.
- Recovery is explicit per command kind. Harvester's DB-enforced transfer uniqueness remains.
- `ExecuteProposalCommand` remains outside the LLM-emittable command union and owned only by
  Harvester.
- Tests cover simultaneous approve/reject, double claim, expiry boundaries, restart, persistence
  failure after effect, and command-kind ownership.

OpenClaw's current design is a useful reference, but its patched historical advisories involving
[approval-display truncation](https://github.com/openclaw/openclaw/security/advisories/GHSA-xww8-gqvh-92x9)
and [inputs not bound to the approved interpreter](https://github.com/openclaw/openclaw/security/advisories/GHSA-xf99-j42q-5w5p)
also show why the exact payload and failure behavior must be pinned. These are historical, patched
issues, not claims about the assessed commit's current vulnerability.

---

## Confirmed finding 3 — Discord delivery has an ambiguity window

**Observed fact.** [`cli/operator.py`](../../src/wobblebot/cli/operator.py) sends each unforwarded
notification to Discord and then marks it forwarded. A successful Discord request followed by a
database failure causes the next poll to send the row again. Persistent failures retry every poll
without a durable attempt count, backoff schedule, remote receipt, or visible terminal state.

No production duplicate-notification incident was located. This is a source-visible crash window,
not a claim that operators have observed the failure.

**Narrow recommendation.** Evolve the existing notification rows into a minimal durable outbox:

- explicit `pending`, `sending`, `delivered`, `ambiguous`, and `dead_letter` states;
- atomic claim token/lease before send;
- bounded attempt count, last error, and `next_attempt_at` with jittered backoff;
- Discord message ID where the API returns one;
- `ambiguous` rather than immediate replay when delivery may have occurred; and
- health/UI visibility for queue depth, oldest age, ambiguous rows, and dead letters.

Exactly-once delivery is not promised. Critical-alert policy must explicitly choose whether an
ambiguous result should favor a possible duplicate or operator reconciliation.

---

## Confirmed finding 4 — advisor suggestions lose resolution lineage

[`LLMCallRecord`](../../src/wobblebot/domain/llm_cost.py) carries the P4 per-cycle `trace_id`, but
[`AdvisorSuggestion`](../../src/wobblebot/ports/advisor.py) does not. The cascade returns the same
heuristic-shaped recommendation both when a deterministic guard resolves normally and when an LLM
escalation fails or trips its cost cap
([`cascading_advisor.py`](../../src/wobblebot/adapters/cascading_advisor.py)).

The distinction is operationally real: the production-forensics record documents 387 consecutive
provider failures hidden behind normal-looking heuristic rows before the later failure-streak
health card closed the detection gap
([`production-advisor-forensics-2026-08-11.md`](production-advisor-forensics-2026-08-11.md)).
Current monitoring now surfaces the outage, but a persisted suggestion still cannot be directly
joined to its attempted LLM calls or classified as a normal guard versus fallback.

**Narrow recommendation.** At the next compatible advisor schema change, persist:

- the suggestion's `trace_id`;
- a bounded resolution enum such as `heuristic_guard`, `llm`, `heuristic_fallback`, or
  `moe_partial`;
- bounded `fallback_reason` / `error_kind`; and
- actual provider/model when relevant.

This is provenance only. It does not justify OpenClaw-style cross-provider fallback, which ADR-015
deliberately rejects.

---

## Candidate operational patterns

### Read-only deployment Doctor

WobbleBot has strong individual checks—schema drift, deprived-environment behavior, preflight,
backup restoration, daemon health, and HTTP health—but no single machine-readable deployment
assessment. OpenClaw's Doctor suggests a small `wobblebot doctor --json` that aggregates existing
evidence without repairing state.

Initial high-signal checks would cover:

- unexpected per-service credentials and writable mounts;
- trader/Harvester credential co-location;
- config/schema/deprecated-key drift and mounted prompt/config provenance;
- SQLite version, `quick_check`, WAL, backup, and disk state;
- daemon freshness and last successful cycle;
- stale approved/executing commands and notification backlog/ambiguity;
- deployed image tag/version and container-hardening flags; and
- web bind, session-secret, API-documentation, cookie, and reverse-proxy assumptions.

Every finding should have a stable check ID, severity, evidence/path, fix hint, equivalent human
and JSON representations, and documented exit codes. Default checks should be read-only and
offline. Network probes must be explicit. No repair or `settings.yml` rewrite should be added
without an ADR resolving ADR-034 ownership.

### v1.0-to-2.0 upgrade-survivor gate

The repository contains realistic individual migration tests but no located lane that starts from
the published v1.0 state, upgrades the exact 2.0 candidate artifact, and proves runtime readiness.
Before a 2.0 release, a sanitized v1.0 fixture should be upgraded using the immutable candidate
image/package, integrity-checked, migrated a second time to prove idempotence, and started with fake
adapters. The gate should prove that deprecated configuration fails actionably and that no pending
command or transfer proposal executes during migration/startup.

This complements existing unit/migration tests; it does not replace them.

### Provenance rules for P4.6 Historian

P4.6 already requires its own design document and remains sequenced behind the canonical scoring
run ([`p4-completion-plan.md`](../planning/p4-completion-plan.md)). OpenClaw's generic
conversational memory should not be imported. Its provenance controls are useful design inputs:

- store exact source table/row IDs, content hashes, observation times, and source classes outside
  model-authored prose;
- distinguish exchange/market, operator, external-news, advisor-derived, and system material;
- make deterministic eligibility decisions before LLM synthesis;
- prevent model-generated findings from recursively becoming primary evidence;
- record model, prompt, evaluator, and supersession versions; and
- keep findings read-only and out of auto-apply.

The existing Historian gate and advisory boundary remain unchanged.

### Conditional patterns with explicit triggers

| Pattern | Trigger before reconsideration |
|---|---|
| Physical DB/WAL high-water retention and verified archive-before-prune | The existing retention and disk-awareness gates open. |
| Non-money daemon run receipts/single-flight claims | A missed, duplicated, or overlapping daemon cycle is observed and current heartbeats/logs are insufficient. |
| Small operational metrics export | An existing dashboard/log query cannot answer an operational question, or an external collector is adopted. |
| Docker secret files / `*_FILE` inputs | Per-service capability isolation lands and the NAS/Portainer path can provision them reliably. |

These triggers prevent OpenClaw parity from becoming the reason to build a feature.

---

## Conditional OpenClaw integration boundary

The existing OpenClaw research trigger in
[`external-triggers.md`](../release/v1.1/external-triggers.md) was met by this assessment. That does
not establish demand for a production integration.

If an operator demonstrates a real OpenClaw workflow, prefer a generic WobbleBot-owned MCP service
over an OpenClaw-specific adapter, a broad Discord webhook identity, or HTML scraping. A portable
MCP surface would also serve other clients.

```text
OpenClaw
   -> authenticated MCP
        -> read-only WobbleBot query services

   -> optional request_command (later, separately ratified)
        -> awaiting_confirmation
             -> authenticated human approves in WobbleBot web/Discord
                  -> owning WobbleBot daemon atomically claims and executes
```

### First surface: read-only

Candidate tools can adapt existing typed query services for:

- overall and per-daemon health;
- engine/status snapshots and open orders;
- notifications;
- recent suggestions, outcomes/scoreboard, and weather report; and
- cost summary.

The service should authenticate its client, expose an explicit tool allowlist, rate-limit calls,
redact sensitive account detail where it is not needed, and possess no Kraken, trader, Harvester,
LLM, Discord, or web-session credentials. Prefer a WobbleBot-owned service/API over mounting raw
database files into the OpenClaw runtime.

### Later surface: request creation only

If read-only use proves valuable and operators request actions, a separate ADR may allow a typed,
rate-limited `request_command` operation. It may create an `awaiting_confirmation` row with origin,
TTL, canonical payload version/digest, and audit identity. It may not:

- approve or reject its own request;
- execute an engine command;
- create or execute a withdrawal command;
- rewrite configuration or prompts;
- access a Kraken key; or
- bypass the owning daemon's kind-scoped claim and safety checks.

The older suggestion of a `confirm-pending-command` MCP tool would collapse the human firewall and
is rejected.

### Existing Discord probe

[`tools/probe_discord_bot.py`](../../tools/probe_discord_bot.py) demonstrates that a webhook can
exercise the live inbound path only after its webhook identity and channel are explicitly
allowlisted. It cannot perform confirmation reactions. That makes it useful as a controlled test
or a zero-code experiment, not an authenticated approval mechanism or the preferred durable
integration contract.

### Genuine external-agent use cases

- Summarize typed health/notification evidence for an operator without changing state.
- Run the already-proposed LLM-provider/model/pricing drift research workflow and produce a report
  or issue while leaving remediation human-controlled.

No trading-strategy, capital-sizing, anomaly-detection, or autonomous-remediation use case was
found that OpenClaw should own.

---

## Declined imports

| OpenClaw capability | Why WobbleBot should decline it for current scope |
|---|---|
| Whole Gateway/runtime replacement | Centralizes a control plane and enlarges the trusted computing base; WobbleBot's ports and authority split already fit the problem. |
| In-process third-party plugins or marketplace | Plugins execute with host-process privilege; reviewed adapters or isolated single-capability sidecars are safer. |
| General shell/browser/computer tool gateway | Replaces a closed typed command vocabulary with an open-ended execution surface. |
| Conversational memory, dreaming, or self-learning | Introduces stale authority, poisoning, and non-reproducible state without a conversational-memory requirement. |
| Production subagents or swarms | WobbleBot's bounded MoE already supplies isolated expert opinions without action tools; swarms add cost, latency, and injection surface. |
| Cross-provider/model fallback and auth rotation | ADR-015 favors transparent failure and deterministic heuristic fallback for reproducibility and cost control. |
| Unified cron/heartbeat replacement | Purpose-specific daemons, maintenance scheduling, heartbeats, and recovery alerts already exist. Borrow a missing primitive only after an incident. |
| Full OpenTelemetry collector stack | Disproportionate for the current single-NAS deployment without an existing collector or unresolved debugging need. |
| Shared Gateway secret store | Centralizing reader, trader, Harvester, LLM, Discord, and web credentials defeats financial-power fragmentation. |
| HTML dashboard scraping | Brittle, loses typed semantics, and couples an integration to presentation markup. |

OpenClaw is MIT-licensed, so selective reuse is possible subject to its
[license](https://github.com/openclaw/openclaw/blob/16eb38cf9725b073bec2cf89842c28d59509231d/LICENSE)
and [third-party notices](https://github.com/openclaw/openclaw/blob/16eb38cf9725b073bec2cf89842c28d59509231d/THIRD_PARTY_NOTICES.md).
Because the upstream is a rapidly changing TypeScript monorepo and WobbleBot is Python with a
different authority model, narrow reimplementation of principles is preferable to vendoring
runtime code. This is an engineering assessment, not legal advice.

---

## Dependency order if recommendations are ratified

This is a dependency statement, not roadmap status.

1. Enforce per-service Docker credential and mount isolation.
2. Add atomic pending-command decision, immutable approval binding, execution deadlines, and
   execution claims.
3. Add explicit notification delivery/ambiguity state.
4. Add the read-only deployment Doctor and artifact-level upgrade-survivor gate.
5. Fold advisor resolution lineage and Historian provenance into their next already-gated schema
   and design work.
6. Only then consider a read-only MCP experiment after an operator confirms a real workflow.

Accepted work must be recorded and sequenced in the roadmap or an owning ADR rather than tracked
by completion marks in this assessment.

---

## Limitations and review triggers

OpenClaw changes quickly. The commit above is authoritative for source claims; mutable documentation
URLs may describe newer behavior. Feature existence is not evidence of fitness for financial
software.

Review this assessment when any of the following happens:

- a controlling WobbleBot ADR is superseded;
- an operator confirms a real OpenClaw deployment and concrete WobbleBot workflow;
- a notification-delivery, duplicate-command, or missed/duplicate-cycle incident occurs;
- WobbleBot becomes multi-host or adopts an external telemetry collector;
- P4.6 Historian design begins;
- source code is about to be copied from OpenClaw; or
- an MCP mutation surface is proposed.

No periodic review is required solely because OpenClaw added another feature.

---

## Primary source register

Accessed 2026-08-27 unless otherwise stated.

**OpenClaw:**

- [Canonical repository](https://github.com/openclaw/openclaw) and
  [assessed commit](https://github.com/openclaw/openclaw/tree/16eb38cf9725b073bec2cf89842c28d59509231d)
- [Runtime architecture](https://docs.openclaw.ai/agent-runtime-architecture) and
  [agent loop](https://docs.openclaw.ai/concepts/agent-loop)
- [Security](https://docs.openclaw.ai/gateway/security) and
  [exec approvals](https://docs.openclaw.ai/tools/exec-approvals)
- [Message lifecycle](https://docs.openclaw.ai/concepts/message-lifecycle-refactor)
- [Memory architecture](https://docs.openclaw.ai/concepts/memory-architecture)
- [Doctor](https://docs.openclaw.ai/gateway/doctor)
- [MCP](https://docs.openclaw.ai/tools/mcp)
- [Model failover](https://docs.openclaw.ai/concepts/model-failover)
- [Cron jobs](https://docs.openclaw.ai/automation/cron-jobs) and
  [heartbeat](https://docs.openclaw.ai/gateway/heartbeat)
- [Subagents](https://docs.openclaw.ai/tools/subagents) and
  [multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent)
- [OpenTelemetry](https://docs.openclaw.ai/gateway/opentelemetry)

**WobbleBot:**

- [`constraints.md`](../architecture/constraints.md),
  [`decisions.md`](../architecture/decisions.md), and
  [`ratified-decisions.md`](../architecture/ratified-decisions.md)
- [`docker/docker-compose.yml`](../../docker/docker-compose.yml) and
  [`docker/README.md`](../../docker/README.md)
- [`pending_commands` storage](../../src/wobblebot/adapters/sqlite_storage.py),
  [`confirm_decision.py`](../../src/wobblebot/services/confirm_decision.py),
  [`cli/live.py`](../../src/wobblebot/cli/live.py), and
  [`cli/harvest_execute.py`](../../src/wobblebot/cli/harvest_execute.py)
- [`cli/operator.py`](../../src/wobblebot/cli/operator.py) and
  [`discord_transport.py`](../../src/wobblebot/adapters/discord_transport.py)
- [`AdvisorSuggestion`](../../src/wobblebot/ports/advisor.py),
  [`LLMCallRecord`](../../src/wobblebot/domain/llm_cost.py), and
  [`cascading_advisor.py`](../../src/wobblebot/adapters/cascading_advisor.py)
- [`external-triggers.md`](../release/v1.1/external-triggers.md),
  [`p4-completion-plan.md`](../planning/p4-completion-plan.md), and
  [`roadmap.md`](../planning/roadmap.md)
