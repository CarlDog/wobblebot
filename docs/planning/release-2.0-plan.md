# Closing the current release line and opening the next

**Written 2026-08-28.** Answers the operator's six questions from the same
day: aggregate the 2026-08-27/28 external-repository research against the
already-planned backlog, decide what belongs in the current release, define
"done" so the release can be tagged, lay out the next phase with the ADRs it
needs, plan a documentation audit, and name what was missing.

Nothing here is ratified. This document proposes; `docs/planning/roadmap.md`
stays the status source of truth and receives a pointer only. Where a
recommendation would move a boundary set by an ADR, it says so and names the
ADR rather than assuming.

---

## 0. TL;DR

1. **The version numbers are already settled and are off by one from the
   framing of the question.** v1 closed at the `v1.0.0` tag on 2026-07-31.
   The line currently on `main` is **2.0.0, developed but not tagged**.
   "Pin our final v1.\* release" = *run the 2.0.0 tag ceremony*. "v2.\* work"
   = the **2.1.0** line that follows it.
2. **P4's buildable scope is complete.** The only unshipped P4 item — P4.6
   Historian — is externally data-gated and **crosses the tag**; it must not
   block it.
3. **The research produced two verified defects in WobbleBot's own code**
   (Docker credential co-location; non-atomic pending-command lifecycle) and
   one live adapter bug (`ollama_assistant.py` reads the wrong thinking
   field). None of the three is new *feature* work; all three are hardening.
4. **Recommendation on scope: tag first, then harden.** The compose split and
   the atomic-claim work change deployment and runtime behavior and have zero
   soak hours. A tag should name what actually soaked. Alternative named in §4
   — this is the operator's risk call.
5. **The next phase (2.1) is deployment & lifecycle integrity**, in the
   dependency order both assessments independently converged on. Three new
   ADRs; Phase 9 (equities) stays the committed track behind it.
6. **The documentation audit cannot stamp `docs/release/v1.1/` as "v1"** —
   that directory's work released as 2.0.0, and stamping it v1 would recreate
   the exact confusion the CHANGELOG note exists to resolve. §7 proposes a
   per-document disposition instead of a wholesale move.

---

## 1. The versioning state — read this before anything else

Ground truth, verified 2026-08-28:

| Fact | Value | Source |
|---|---|---|
| Only git tag in the repo | `v1.0.0` | `git tag --list` |
| `v1.0.0` tag date | 2026-07-31 | `CHANGELOG.md` `[1.0.0]` |
| `pyproject.toml` version | `2.0.0` | `pyproject.toml:7` |
| CHANGELOG sections | `[Unreleased]`, `[2.0.0] — Unreleased`, `[1.0.0] - 2026-07-31` | `CHANGELOG.md:23, 660, 777` |
| `v1.1` branch vs `main` | 0 commits on `v1.1` not in `main` (fully merged) | `git rev-list --left-right --count` |
| Production deployment | untagged `sha-…` pin | Portainer stack env |

**The repo already made this decision and wrote down the reasoning.**
`CHANGELOG.md`'s preamble and `roadmap.md`'s "v1.1 Track" heading both say the
`v1.1` branch work merges to `main` as **`2.0.0`, not `1.1.0`**, because it
carries a breaking config-schema change (`EmergencyStopConfig` removed,
ADR-032) and a full replacement of the advisor's decision architecture
(ADR-022). The branch keeps its `v1.1` name for history; only the released
number changed.

So the operator's framing maps like this:

| The question said | It actually is |
|---|---|
| "pin our final v1.\* release" | v1's final release is **already pinned** — `v1.0.0`, 2026-07-31, immutable |
| "what constitutes done in our current phase" | done for **2.0.0**, which is developed on `main` and awaiting a tag |
| "move onto v2.\* work" | the **2.1.0** line (or 3.0.0 if something breaking lands) |

### 1a. The one real ruling the tag forces

Two `Unreleased` sections coexist in `CHANGELOG.md`. That is **not** a
doc-discipline bug — the preamble documents the split deliberately: `[2.0.0]`
holds the branch work, `[Unreleased]` holds direct-to-`main` changes made
after the merge. But the split has no defined resolution at tag time, and it
needs one:

- **Option A (recommended): fold `[Unreleased]` into `[2.0.0]` and tag once.**
  Everything currently on `main` ships as 2.0.0. Simplest; matches the fact
  that nothing on `main` has ever been released.
- **Option B: tag 2.0.0 at the merge point and let the post-merge work become
  2.0.1.** Only worth it if the operator wants a tag whose contents exactly
  match a specific soaked commit. Costs a second ceremony immediately.

Option A is recommended because the post-merge commits are the same
un-released development line, not a patch on top of a shipped artifact.

### 1b. The deployment pin must move with the tag

Production runs an untagged `sha-<short>` image. The tag ceremony should
include repointing `IMAGE_TAG` at the tagged build — otherwise "we released
2.0.0" and "what's running" stay different things, which is exactly the drift
class §5c's startup receipt exists to surface.

> **Correction while you are in here.** Guardrail 1 in
> [`docs/release/v1.1/README.md`](../release/v1.1/README.md) (line 622) says
> the NAS stack is explicitly pinned by `IMAGE_TAG` "so a push is not a
> redeploy." That is **stale**: Portainer stack 158 is git-managed and
> auto-deploys on push — PR #101's DMS-alert fix was running in production
> roughly five minutes after CI published the image, about a day before any
> explicit `IMAGE_TAG` bump, confirmed via the container's
> `org.opencontainers.image.revision` label. The pin is still worth setting
> deliberately at the tag (and a manual bump is still how a docs-only compose
> change advances `ConfigHash`), but it should not be described as what
> *prevents* a redeploy. Fix the guardrail text as part of the documentation
> audit.

---

## 2. What "done" means for 2.0.0 — the tag gate

The roadmap documents the *intent* to ship as 2.0.0 but carries **no tag
ceremony checklist**. This is that checklist. Everything on it is either
already true or is cheap, test-only, or documentation.

### 2a. Already satisfied

- [x] P1 safety hardening complete (2026-07-31 → 2026-08-01).
- [x] P2 data-infrastructure spine complete (2026-08-07 → 08-08).
- [x] P3 ops/observability/UX complete (2026-08-08 → 08-10).
- [x] P4 buildable scope complete (2026-08-16 → 08-17), P4.6 excepted (§2c).
- [x] `pyproject.toml` already reads `2.0.0`.
- [x] `v1.1` fully merged into `main`; no unmerged release content.

### 2b. Required before the tag

- [ ] **CHANGELOG reconciliation** — resolve §1a (recommend Option A), then
      stamp `[2.0.0]` with the tag date.
- [ ] **v1.0 → 2.0 upgrade-survivor test.** *Both* the OpenClaw and NemoClaw
      assessments independently flag this as a 2.0 release gate, and the
      repo has no lane that starts from the published v1.0 state and proves
      the 2.0 artifact upgrades it. Shape: take a sanitized v1.0 fixture DB +
      config, migrate with the 2.0 candidate, `PRAGMA integrity_check`,
      migrate a *second* time to prove idempotence, boot with fake adapters,
      and assert that (a) deprecated config (`safety.emergency_stop`, removed
      by ADR-032) fails actionably rather than silently, and (b) no pending
      command or transfer proposal executes during migration or startup.
      This is the single highest-value tag-gate item — it is the only check
      that exercises the *breaking* part of the breaking change.
- [ ] **Deprived-environment walkthrough** for all 22 operator entry points
      (the CLAUDE.md phase-end item; the last full baseline was 2026-05-15 for
      the original 7).
- [ ] **Schema-drift tests clean**, including `WOBBLEBOT_STRICT_CONFIG_DRIFT=1`.
- [ ] **`IMAGE_TAG` repointed** at the tagged build (§1b).
- [ ] **Documentation audit** (§7) — at minimum the disposition decisions; the
      file moves themselves can follow the tag.

### 2c. Explicitly NOT gating the tag

- **P4.6 Historian.** Gated on the Q2 OHLCVT dump landing and a canonical
  scoring run, then its own design document. That trigger is a third party's
  release cadence. It carries across the tag as a standing item and is
  re-homed into the 2.1 register in §7.
- **ADR-040 Stage 2** (writable POLICY tier). Design ratified, Stage 1
  (Capital Reporter) shipped; Stage 2 is its own work with its own trigger
  (§5d).
- **The regime detector / Oracle track.** Still parked behind a 60–90d
  shadow-run that has not started.
- **Anything from §5** — the whole 2.1 phase is post-tag by recommendation.

### 2d. Cheap, tag-compatible extras (test/CI-only, no runtime change)

These are safe to land before a tag because they cannot change deployed
behavior. They come from the NemoClaw assessment's Slice A:

- [ ] **CI integration-lane collection gate.** `.github/workflows/docker-publish.yml`
      runs a bare `pytest`, and `addopts` deselects `integration`. The roadmap
      already records that `tests/integration/test_phase5_operator_e2e.py`
      rotted into a **collection error** and went undetected for exactly this
      reason — the file was not skipped-but-passing, it was never collected.
      A `pytest -m integration --collect-only` step costs seconds, needs no
      credentials or network, and would have caught it.
- [ ] **A failing Compose contract test** for the credential matrix (§5a) —
      written *before* the compose change, asserting the intended end state.
      Committing a red test is deliberate: it pins the requirement and makes
      the eventual fix provably complete.

---

## 3. The research aggregation — five buckets, not 65 findings

Three assessments were produced 2026-08-27/28 (Ollama 19 findings, OpenClaw
17, NemoClaw 29 — with NemoClaw explicitly marking overlaps as "corroborates,
not new"). All three reached the same executive verdict: **do not adopt any of
the three as a dependency or subsystem.** What follows sorts everything they
produced into what actually happens to it.

### Bucket 1 — Verified defects in WobbleBot's own code

Each of these was re-checked directly against source during this session, not
taken on the assessment's word.

| # | Finding | Verified how | Severity |
|---|---|---|---|
| B1-1 | **`docker-compose.yml`'s `x-wobblebot-defaults` anchor gives every service every credential** — including the withdrawal-enabled `KRAKEN_HARVESTER_API_KEY` — plus read-write mounts of the whole `data/` and `config/` trees. | Read the anchor (line 23) and confirmed all **nine** services inherit it: live (95), observe (110), news (125), advise (139), harvest (154), operator (171), web (189), maintenance (210), tools (229). | **High.** ADR-003's financial-power fragmentation is enforced in Python and *assumed* at the container boundary. It is not enforced there. |
| B1-2 | **The pending-command lifecycle has no atomic ownership.** | `services/confirm_decision.py` is read → check status → `save_pending_command` (no compare-and-set). `sqlite_storage.py:1314` upserts `command_json` *and* `status`, so an approved payload is mutable. `cli/live.py` selects `status='approved'`, dispatches, *then* marks — its own docstring notes a failed final write leaves the row approved for redispatch. | **Medium — lower than the assessments rank it** (see the nuance below). |
| B1-3 | **`adapters/ollama_assistant.py:484` reads `envelope.get("thinking")`** — the top-level `/api/generate` location — on the `/api/chat` path, where Ollama puts reasoning at `message.thinking`. The test fixture codifies the wrong shape, masking it. | Read lines 462–502. | **Medium.** A reasoning response with empty final content misclassifies as empty → retry → fail. Not the cause of the 2026-08-27 operator failure (that was a genuine `ReadTimeout`, and the deployed 1.5B operator model is not a thinking model), so this is latent today. |

> **Nuance that changes B1-2's ranking, and must survive into whatever gets
> scheduled.** Both assessments frame the atomicity gap around double-execution
> of money-moving commands. **That specific instance is already closed** —
> ADR-026 put a UNIQUE index on `transfer_results.proposal_id` (DB-enforced,
> concurrency-proof), and ADR-034's kind-scoped SELECT stops `cli/live` from
> touching an `execute_proposal` row at all; `cli/live.py` additionally keeps a
> hard refusal for it as defense in depth. What genuinely remains is narrower:
> (a) **within-daemon redispatch** after a crash between the engine action and
> the `dispatched` write — mostly benign for pause/resume/cancel, **not benign
> for `reanchor`**, which would cancel and re-lay a second time; (b) an
> approve-vs-reject **decision race** between the Discord button and the web
> UI (single operator, low likelihood); (c) **payload mutability** on an
> approved row — latent, since no current writer changes `command_json` after
> approval. Real work, worth doing; not the emergency the framing implies.

### Bucket 2 — Belongs to the 2.0.0 tag gate

- The **v1.0 → 2.0 upgrade-survivor test** (§2b). Flagged independently by
  both assessments as a 2.0 release gate.
- The **CI integration-collection gate** and the **red Compose contract test**
  (§2d) — test-only.

### Bucket 3 — The 2.1 phase (see §5 for sequencing)

Deployment & lifecycle integrity: per-service credential/mount split;
supply-chain identity (lockfile, base-image digest pin, deploy-by-digest);
bounded startup receipt; atomic command claim + immutable approved payload;
notification delivery outbox; central log redaction; a read-only
`wobblebot doctor --json`; and the Ollama adapter hardening slices.

### Bucket 4 — Parked with a named trigger

Recorded so they are not re-derived, and **not** scheduled:

| Item | Trigger |
|---|---|
| Egress allowlist for `live` / `harvest` | *After* the credential split lands, and only evaluated against the real Synology/Docker networking model. The assessment flags a brittle allowlist as itself a safety problem — `news` has a changing feed set and provider endpoints move. |
| Authenticated proxy in front of NAS Ollama | Only if verification shows port 11434 is reachable beyond the intended bridge/admin path. **Verify first; do not build speculatively.** |
| Advisor resolution lineage (`trace_id` + `heuristic_guard \| llm \| heuristic_fallback \| moe_partial` on `AdvisorSuggestion`) | The next compatible advisor schema change. Provenance only — explicitly does **not** justify cross-provider failover, which ADR-015 rejects. |
| Read-only MCP surface for WobbleBot | An operator demonstrating a real OpenClaw workflow. Writing the assessment does not itself establish demand. |
| P4.6 Historian provenance rules (source table/row IDs, content hashes, observation times, source classes; deterministic eligibility *before* synthesis; findings never recursively become evidence) | Design input for P4.6 when its own gate opens. Does **not** authorize early implementation. |
| Ollama `num_ctx` / truncation policy per role; `keep_alive` tuning | After measuring real NAS memory and latency — not speculatively. Note the correctness edge: silent chat truncation can change the meaning of a follow-up operator command, so `cli/operator` should fail visibly rather than truncate. |
| Broadening `llm_calls` into a provider-neutral invocation ledger | Requires an **ADR-014 amendment** first — ADR-014 currently exempts Ollama from cost accounting entirely. |

### Bucket 5 — Declined

Both assessments produced explicit decline tables (OpenClaw: 10 items;
NemoClaw: 11; Ollama: 5). The common shape is that each declined item would
either enlarge the trusted computing base around deterministic financial
daemons or hand the LLM authority ADR-002 removes. Notable declines worth
recording so they are not re-proposed:

- OpenShell / the NemoClaw or OpenClaw gateway as a runtime.
- A model router or silent cross-provider failover (ADR-015 already rejects it).
- Agent plugins, tool loops, shell/browser gateways, conversational memory,
  self-learning, production subagent swarms.
- A **credential-signing proxy** imitating NemoClaw's gateway injection —
  Kraken HMAC signing requires the trusted exchange adapter to hold the
  secret, so a proxy would create a *second* financial-authority surface, not
  remove one.
- Vendoring Ollama's runtime, scheduler, or model store; Ollama web search;
  embeddings/RAG (deferred behind a measured retrieval failure).
- Adding an NVIDIA inference provider merely because NemoClaw supports one — a
  provider becomes a candidate only when the role battery shows it beats the
  incumbent at the exact promoted model identity.

---

## 4. Does any of this belong in the current release? — the scope fork

This is the operator's call. Both branches are defensible; the recommendation
is stated plainly and the alternative is not strawmanned.

**Recommended — tag first, harden after.**
The compose credential split changes what every container can reach, and the
atomic-claim work changes the lifecycle of the firewall that gates every
mutation. Neither has a single hour of soak. The 2.0.0 line, by contrast, has
been running in production since mid-August across eight daemons. A tag should
name an artifact that has actually run. Tagging first also means the
upgrade-survivor test (§2b) validates a *stable* target rather than a moving
one, and it gives the hardening work a clean `2.1.0` boundary to be described
under.

**The alternative — fix B1-1 and B1-2 before tagging.**
The argument for it is real: B1-1 means a compromise of any low-authority
daemon (`web`, `news`, `advise`) can read the withdrawal credential, and
shipping a tagged release with that property known-and-unfixed is a
deliberate choice rather than an oversight. If the operator weighs "the tag is
a security statement" above "the tag is a soak statement," this is the right
call — and it makes 2.1's scope much smaller.

**Not on the fork either way:** B1-3 (the Ollama thinking-field bug) and the
rest of Ollama Slice A are ordinary adapter bug fixes with an existing test
seam. The Ollama assessment's own stage-alignment note classes all four of its
slices as *current-phase maintenance*, explicitly not phase-gate work. They
can land before or after the tag on their own merits.

---

## 5. The 2.1 plan — deployment & lifecycle integrity

**Why this phase, in this order.** The OpenClaw and NemoClaw assessments were
written independently and produced the same dependency order: isolate
credentials and filesystem authority *first*, because every later control
(egress rules, receipts, claims) is either weakened or untestable while every
service holds every key. Within that, detection-only work comes before
privilege narrowing, so a mistake in the narrowing is caught by a test rather
than by an outage.

Effort key: **S** = hours · **M** = 1–2 days · **L** = several days · **XL** = a week+.

### 5a. Slice 1 — Deployment capability isolation (L) · **ADR-041**

Replace the shared `x-wobblebot-defaults` credential bundle with per-service
capability manifests. Keep one shared anchor for genuinely common *non-secret*
runtime settings.

Acceptance evidence, from the assessments' own criteria:

- `docker compose config` rendered with **synthetic canary** credentials shows
  the Harvester canary reaching **only** `harvest`. (Render live-stack config
  with `--quiet` only — never print expanded real secrets.)
- No long-running service holds both the trader and the Harvester credential.
- Reader / LLM / Discord / web-session secrets each reach only the services
  that use them.
- Per-service DB and config mounts are scoped, and read-only where the service
  is a reader. Steady-state read-only config is separated from the authorized
  writer/bootstrap path.
- Startup, SQLite WAL behavior, backups, reconciliation, and graceful shutdown
  all still work under the narrowed mounts.

**Honest limit, stated in the ADR:** Compose can prove *credential presence*
and *mount scope*. It cannot prove *semantic* powers — placing an order,
approving a command, rewriting settings, initiating a transfer. Those stay
enforced by the application and DB contract tests. This slice closes the gap
between "the Python respects ADR-003" and "the deployment respects ADR-003";
it does not replace either.

**Sequencing note:** the red Compose contract test (§2d) is written first.

### 5b. Slice 2 — Command lifecycle atomicity (M–L) · **ADR-042**

Narrow fix, not a generic lifecycle framework. Proposed state machine:

```
awaiting_confirmation
  → {approved | rejected | expired}        (atomic decision — one SQL statement,
                                            conditional on status AND unexpired)
approved
  → executing(claim_token, deadline)       (atomic claim by the owning daemon)
executing
  → {dispatched | failed}
  → unknown_after_effect                   (crash/commit ambiguity — explicit,
                                            never silently retried)
```

Plus: bind approval to an **immutable** typed payload (a `command_json`
digest, refused on change), and reconcile before any retry.

Scope discipline: this **amends** ADR-034's model (web queues typed commands;
approval and execution stay independently owned) — it does not reopen
ADR-002's firewall. `pending_commands WHERE status='approved'` remains the only
path to the engine; the change is that a row can be *claimed* rather than
merely observed. Record in the ADR that the double-withdrawal instance is
already closed by ADR-026, so this work is not re-solving it — and that
**`reanchor` is the concrete non-idempotent redispatch case** the tests must
cover.

### 5c. Slice 3 — Supply-chain and runtime identity (M–L) · **ADR-043**

Three verified gaps, one slice:

- `docker/Dockerfile` uses mutable `python:3.14-slim` for both build and
  runtime stages → pin both by digest, with the existing dependency-update
  path proposing refreshes.
- `pyproject.toml` leaves runtime dependencies as lower bounds with no
  lockfile → commit one hashed lock (`uv.lock` or a hashed requirements lock)
  and make the Docker build consume it.
- Production deploys a `:sha-<short>` tag, which is mutable registry metadata
  → CI publishes and retains the full GHCR digest; Portainer deploys
  `ghcr.io/carldog/wobblebot@sha256:…` while the human-friendly tag stays a
  label.

Paired with the **bounded startup receipt**: each long-running daemon emits
once, at boot, a single log line carrying source revision, deployed OCI
digest, dependency-lock hash, resolved profile, SHA-256 of `settings.yml`, and
SHA-256 of every loaded prompt file. This is *deliberately not* a new table or
a joinable runtime-manifest schema — that stays ADR-gated behind a real
forensic-query consumer.

**Why the receipt earns its place:** the roadmap already records a real
incident where the NAS config mount silently overrode the shipped image and
the deployed `risk.md` had drifted from the repository, requiring a manual
prompt sync. A receipt makes that visible at boot instead of at diagnosis.

### 5d. Slice 4 — Operational consolidation (M) · no new ADR

- **Central log redaction.** `config/logging.py` emits arbitrary structured
  `extra=` fields and formatted exception text with **no central sanitizer**
  (verified: none located). Call-site discipline is exactly what
  `security.md`'s chokepoint rule warns against. Add one recursive filter over
  messages, nested `extra`, exception strings, headers, cookies, and URL
  userinfo/query, tested with synthetic canary secrets through **both** the
  plain and JSON formatters. No leak has been found — this is defense in
  depth, and the ADR-less classification is deliberate: it changes no boundary.
- **Notification delivery outbox.** `cli/operator` sends to Discord and *then*
  marks the row forwarded — a crash between the two resends, and a persistent
  failure retries every poll forever with no attempt count, backoff, receipt,
  or dead-letter state. Evolve the existing typed notification rows into
  states (`pending`, `sending`, `delivered`, `ambiguous`, `dead_letter`) with a
  claim lease, bounded attempts, jittered backoff, the Discord message ID where
  returned, and queue-depth visibility. Explicitly **not** a message broker.
  Recommend recording this as a ratified operational decision rather than a
  full ADR unless it grows a schema the Historian would join against.
- **`wobblebot doctor --json`.** Aggregate — never repair — the checks that
  already exist: per-service credential/mount expectations (Slice 1),
  config/schema/prompt drift and provenance (Slice 3), SQLite version /
  `quick_check` / WAL / backup / disk, daemon freshness, stale approved-or-
  executing commands, notification backlog and ambiguity, deployed image
  identity. One schema-versioned model serving both the human and JSON
  presentations, with stable finding IDs, evidence, redaction, and
  deterministic exit codes. Read-only by construction.

### 5e. Slice 5 — Ollama adapter hardening (M, can run in parallel) · possible **ADR-014 amendment**

Per the Ollama assessment's own A→D ordering, and classed by that assessment
as current-phase maintenance rather than new-phase work:

- **A (correctness — small, ship whenever):** fix the `message.thinking`
  read (B1-3) with fixtures for **both** the chat and generate envelope
  shapes so they cannot be conflated again; send the advisor's prompt body as
  the request's `system` field rather than concatenating it into `prompt`
  (today an empty `system` lets the model's own Modelfile prompt take that
  role); normalize malformed-envelope JSON and `summarize()` read timeouts to
  the correct **port** exception types — today `response.json()` sits inside a
  block catching only `httpx.HTTPError`, so a malformed body escapes
  `AdvisorError`/`AssistantError` entirely and defeats MoE isolation.
- **A (boundary — operational, zero code):** set `OLLAMA_NO_CLOUD=1` on the
  NAS Ollama. Current Ollama transparently offloads to Ollama Cloud, surfacing
  as `remote_model` / `remote_host` on the response — and WobbleBot hardcodes
  `provider: ollama` ⇒ local, free, unaccounted (ADR-014 exempts it from the
  cost gate; the `llm_calls` constraint excludes it). A `:cloud` tag could send
  market and operator context off the NAS silently. Add a startup preflight
  rejecting `:cloud` tags and any response carrying non-empty remote
  provenance.
- **B (identity + diagnostics):** record the model **digest** alongside the
  configured tag — mutable tags mean P4's rank/hit-rate scoring can aggregate
  outcomes from different weights under one apparent model identity. Capture
  `done_reason` (reject `"length"` as truncation, precisely) and the
  load/prompt/eval durations and token counts currently discarded. Persisting
  local-inference telemetry needs the **ADR-014 amendment** first.
- **C/D:** the native `think` parameter pilot (could retire name-pattern
  thinking detection entirely — the P0 Q2 candidate) and `num_ctx` policy.
  Both gated on measurement, both parked in Bucket 4.

### 5f. Then — Phase 9 (Kraken Securities equities)

Phase 9 is **already operator-committed** (2026-05-20) with a seven-slice
sketch in the roadmap. It is not re-planned here. Two things about its
position:

- **Stage 9.0 (kickoff + ADR-019) is design-only and not capital-gated.** The
  PDT-aware risk model, settlement-aware pacing, earnings-pause posture, and
  wash-sale accounting can all be ratified while capital is below the
  activation threshold. That work can start any time after 2.1 without
  spending anything.
- **Activation stays gated at ~$500 account equity.** The roadmap's own
  framing: the design work happens in advance, activation waits for capital.

### 5g. Data- and trigger-gated items that cross into the 2.x line

Not scheduled; listed so the register survives the release boundary:

| Item | Gate |
|---|---|
| **P4.6 LLM Historian** | Q2 OHLCVT dump → canonical scoring run in the NAS tools container → its own design doc. |
| **ADR-040 Stage 2+** (writable POLICY tier) | Ratified design; Stage 1 shipped. **Trigger: a second manual POLICY-tier edit of the kind Stage 1 detected but could not apply.** The SOL/ADA `order_size_usd` fix on 2026-08-28 was the first; a second one is the evidence that the manual loop is the bottleneck rather than an isolated event. See the ADR-040 addendum for why the SOL validation fixture no longer exists and step 4 of its validation plan needs a new vehicle. |
| **Regime detector / Oracle track** | Research producing detection that beats buy-and-hold, *then* a 60–90d shadow-run, before any consumer wires into `cli/live`. |
| **Auto-action cluster** | P4 outcome data + per-item ADRs; auto-pause additionally needs ADR-002 ratified-with-exception. |

### 5h. ADRs this plan requires

Proposed, not written — ratification is the operator's. Numbers follow ADR-040.

| ADR | Scope | Why it needs an ADR |
|---|---|---|
| **ADR-041** | Per-service deployment capability matrix: which credentials, which mounts, which mode, per service; and the explicit limit that Compose proves presence, not semantic authority. | Changes deployment/config ownership and makes a boundary that ADR-003 asserts into one the deployment enforces. |
| **ADR-042** | Pending-command lifecycle: atomic decision, immutable approved payload, `executing` claim with owner/token/deadline, `unknown_after_effect`. | Changes command lifecycle **states** — the firewall ADR-002 and ADR-034 depend on. |
| **ADR-043** | Deployment supply-chain identity: dependency lock, base-image digest pins, deploy-by-digest, and the bounded startup receipt (with persistent manifest explicitly out of scope). | Changes what "the deployed artifact" means and introduces a new provenance surface. |
| **ADR-014 amendment** | Only if local-inference telemetry becomes persistent. ADR-014 currently exempts Ollama from cost accounting; broadening `llm_calls` (or adding an inference ledger) contradicts that as written. | Amends a standing decision. |
| **ADR-019** | Already reserved by the roadmap for the Phase 9 equity-grid risk model. | Pre-existing. |

Explicitly **not** requiring an ADR: the CI collection gate, the central
redaction filter, and pin/lock maintenance — provided their implementation does
not move a boundary.

---

## 6. Branching strategy for the 2.x line

The operator asked for a new development branch. There is a real lesson to
apply first: **the `v1.1` branch's name became misdirection the moment its
release number changed to 2.0.0**, and the CHANGELOG, the roadmap, and the
`docs/release/v1.1/` directory all now carry explanatory notes that exist only
to undo that confusion.

`v1.1` was a long-lived branch for one specific reason: `main` was **frozen**
at the soak commit while the v1.0 gating soak ran. That condition no longer
holds. Everything since — P1 through P4 — actually shipped through short-lived
per-slice branches (`claude/capital-reporter`, `feat/p3-discord-buttons`,
`claude/staking-income`, …) merged to `main` by PR.

**Recommendation:** do not create a long-lived `v2.x` branch. `main` is the
post-tag development line — Guardrail 1 in
[`docs/release/v1.1/README.md`](../release/v1.1/README.md) already says so —
and each slice in §5 gets its own short-lived, **content-named** branch → PR →
`main`, tagged `2.1.0` when the phase closes. If a long-lived branch is wanted
anyway, name it for its content (`hardening/deployment-integrity`), never for a
version number that may change.

**This narrows an explicit ask** — the operator asked for a new development
branch, and the recommendation is that the *long-lived* form of it is the
wrong shape. Flagged rather than silently reinterpreted; the ruling is theirs.

This plan document is on **`claude/release-2.0-plan`** — content-named,
short-lived, to be merged with the tag-ceremony decisions once ratified.

---

## 7. The documentation audit

The operator's proposal: collect the v1-pertaining documents, stamp them "v1,"
and move them to an archive so the live tree holds only active work and
cross-version material. The goal is right. Two traps make a wholesale move the
wrong mechanism.

**Trap 1 — `docs/release/v1.1/` must not be stamped "v1."** Its work released
as **2.0.0**. Stamping it v1 recreates precisely the confusion that the
CHANGELOG preamble, the roadmap heading, and the directory's own historical
note exist to resolve. If it is archived, it archives as **2.0.0**.

**Trap 2 — `docs/release/v1.1/README.md` is simultaneously a historical record
and the live backlog register.** It holds the parked register (with active
triggers), the open questions, P4.6's gate, and every unshipped row. Archiving
it wholesale would archive the live backlog. The live content must be
*extracted* first.

**Trap 3 — the `v1.0-` filename prefix names when a doc was written, not what
it covers.** `v1.0-incident-runbook.md` is an operator runbook for the system
running *today*. `v1.0-soak-runbook.md` describes the soak methodology, which
is reusable. `v1.0-future-improvements.md` is the backlog catalog index — live.
Only `v1.0-known-limitations.md` is genuinely a snapshot of a tagged artifact.

### 7a. Proposed disposition

Four dispositions, applied per document:

- **SSOT** — never archived, never moved.
- **CROSS-VERSION** — applies regardless of release; drop the version prefix and
  move to a version-neutral home.
- **EXTRACT-THEN-ARCHIVE** — carries live content that must be re-homed first.
- **ARCHIVE** — a record of a closed release.

| Document | Disposition | Note |
|---|---|---|
| `planning/roadmap.md` | **SSOT** | The status source of truth. Never archived. Gains a pointer to this plan. |
| `architecture/decisions.md`, `ratified-decisions.md`, `constraints.md`, and the rest of `architecture/` | **CROSS-VERSION** | ADRs span versions by definition. No change. |
| `implementation/*` | **CROSS-VERSION** | Coding guidelines, logging conventions, module specs. No change. |
| `reference/*` | **CROSS-VERSION** | Kraken API reference, advisor-seat register, the three 2026-08 assessments. No change. |
| `release/v1.0-incident-runbook.md` | **CROSS-VERSION** | Rename to `operations/incident-runbook.md`. It is a runbook for the live system, not a v1.0 artifact. |
| `release/v1.0-soak-runbook.md` | **CROSS-VERSION** | Rename to `operations/soak-runbook.md`. The methodology is reusable for any future soak, including a 2.1 one. |
| `release/v1.0-known-limitations.md` | **ARCHIVE** as 1.0.0 | A genuine snapshot of the tagged artifact. **Then write a 2.0.0 known-limitations doc** — the tag needs one, and much of the v1.0 list has since been closed. |
| `release/v1.0-future-improvements.md` | **EXTRACT-THEN-ARCHIVE** | The backlog catalog index. Merge with the live half of `v1.1/README.md` into one active backlog. |
| `release/v1.1/README.md` | **EXTRACT-THEN-ARCHIVE** | Extract: the parked register, open questions, P4.6's gate, unshipped rows, and every guardrail. Archive the rest as the 2.0.0 development record. |
| `release/v1.1/standing-rules.md` | **CROSS-VERSION** | Its own first line says these rules survive every version boundary (margin/futures gates, Kraken-UI declines). Move to a version-neutral home — this is the clearest miscategorized file in the tree. |
| `release/v1.1/external-triggers.md` | **EXTRACT-THEN-ARCHIVE** | Live third-party triggers. Also carries two known staleness bugs (§8). |
| `release/v1.1/{adaptive-grid,engine,harvester,infrastructure,news-pipeline,observability,operator-ux,trading-scope}.md` | **EXTRACT-THEN-ARCHIVE** | Each is mostly a shipped-work record but each also carries un-shipped candidate detail and, in three cases, newly-filed 2026-08-26/28 findings. Extract the open items; archive the receipts. |
| `release/v1.1/four-homes-audit.md` | **ARCHIVE** as 2.0.0 | A completed audit with its verdict recorded. Its queued Q1–Q3 items extract with the rest. |
| `planning/phase-{2..8}-summary.md` | **ARCHIVE** as 1.0.0 | Closing summaries for phases inside the v1.0 line. |
| `planning/stage-*.md` (10 files) | **ARCHIVE** as 1.0.0 / 2.0.0 by stage | Per-stage design docs for closed stages. |
| `planning/p4-completion-plan.md` | **EXTRACT-THEN-ARCHIVE** | P4.6 is still open in it. |
| `planning/p4-outcome-ledger-design.md`, `experiment-4h-strategy-selection.md`, `help-directory-design.md` | **ARCHIVE** as 2.0.0, except live proposals | `help-directory-design.md` is an unbuilt proposal — keep active. |
| `planning/{requirements,testing-plan,risks-plan}.md` | **CROSS-VERSION** | Project-level, not release-scoped. |
| `planning/future-ideas.md` | **CROSS-VERSION** | Explicitly a scratchpad that graduates into the roadmap. |
| `planning/milestones.md`, `planning/process.md`, `planning/README.md` | **CROSS-VERSION, needs correction** | See §8 — `process.md` still says "Five major phases" (there are nine) and describes a branching strategy that predates current practice. |

### 7b. Proposed target shape

```
docs/
  architecture/          # unchanged — cross-version
  implementation/        # unchanged — cross-version
  reference/             # unchanged — cross-version
  operations/            # NEW — runbooks for the running system
    incident-runbook.md
    soak-runbook.md
  planning/
    roadmap.md           # SSOT
    backlog.md           # NEW — the live register, extracted
    standing-rules.md    # moved from release/v1.1/
    …
  release/
    2.0.0/               # release notes + known limitations for the tag
  archive/
    1.0.0/               # phase summaries, v1.0 known-limitations, closed stage designs
    2.0.0/               # the v1.1-branch development record, named by RELEASE
      README.md          # index, and one paragraph on why "v1.1 work" archives as 2.0.0
```

### 7c. Sequencing

The **dispositions** are a tag-gate item (§2b) because they are decisions; the
**file moves** are not, and should follow the tag so that link churn does not
land in the same commit as the release. Do the extraction — building
`planning/backlog.md` — before any archive move, so nothing live is ever only
in an archived file. One commit per disposition category, per the phase-end
audit's process discipline.

---

## 8. "Anything else I'm missing?"

Flagged, not scheduled. Each is a one-liner because each deserves the
operator's call on whether it is worth a slot.

- **~31 local branches, most merged.** `git branch` lists 31 local and 24
  remote; the `v1.1` branch is fully merged (0 commits not in `main`) and so
  are most of the `claude/*` and `feat/p3-*` lines. A prune before the tag
  makes the repo's history legible for the first archaeologist who comes back
  to it. Low effort, purely hygienic.
- **`settings.example.yml`'s `deepseek-r1:7b` entry is a live footgun.** Line
  564 sets `max_tokens: 512` with `timeout_seconds: 60` directly beside
  comments saying thinking models need 2048 and 180+. The production profile
  overrides it, so nobody is bitten today — but an operator copying the
  example straight across hits a mid-answer truncation that reads as "the model
  went stupid," not as a config error. Fix it as a real change with a test, not
  a prose edit.
- **`external-triggers.md` carries stale numbers.** It still quotes 0.40/0.26
  fee rates; Kraken doubled the schedule to **0.80 taker / 0.40 maker** on
  2026-07-09 (caught five weeks late by the phase-end audit, now ADR-038). Its
  CryptoCompare entry has also been overtaken — the feed was retired
  2026-07-31.
- **The Discord fill card vs. the web fill toast.** Filed 2026-08-26 in
  `operator-ux.md`: both draw from the same `fill` event payload, but the
  operator's read after weeks of living with both is that the web toast is
  *orders of magnitude* more informative. Not investigated. A side-by-side
  review is a natural 2.1 UX slice.
- **The fee-drift dust false-positive is filed but unfixed.** ADR-038's
  tripwire pages on Kraken dust fragments because the guard only skips
  `trade.cost <= 0`, not sub-materiality costs. No accounting impact — the
  check is advisory — but it costs operator attention every time it fires. The
  proposed materiality floor is recorded in `engine.md`.
- **`process.md` describes a project that no longer exists.** "Five major
  phases, each broken into five stages" — there are nine phases plus the
  P0–P4 track, and the branching section predates how P1–P4 actually shipped.
  It is a cross-version doc, so it should be corrected rather than archived.
- **No 2.0.0 known-limitations document exists.** v1.0 got one; the tag should
  have its own, and writing it is a good forcing function for confirming which
  v1.0 limitations the 2.0 line actually closed.
- **The capital-utilization item outranks its current position.** The SOL 0/6
  finding — a symbol with a configured grid placing nothing because
  `order_size_usd` fell under the pair's `ordermin` — was caught by the
  Capital Reporter four days running before a human noticed. The stopgap
  (per-coin overrides) is the same workaround DOGE needed in June. The
  balance- and ordermin-aware sizing candidate in `engine.md` is the actual
  fix, and this is now its second occurrence.

---

## 9. What happens next

Ratification order, if this plan is accepted:

1. Rule on the CHANGELOG split (§1a) and the scope fork (§4).
2. Land the tag-gate items (§2b, §2d) — the upgrade-survivor test is the one
   that matters.
3. Tag `2.0.0`, repoint `IMAGE_TAG`, write the 2.0.0 known-limitations doc.
4. Do the documentation extraction (`planning/backlog.md`), then the archive
   moves (§7c).
5. Write ADR-041 → open 2.1 Slice 1.

The roadmap gets a one-line pointer to this document; it does not get a second
status ledger.
