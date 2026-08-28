# Ollama repository assessment for Wobblebot

- **Assessment date:** 2026-08-27
- **Official upstream:** <https://github.com/ollama/ollama>
- **Local checkout:** `D:\GitHub\ollama`
- **Reviewed commit:** `f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a`
  (`main`, 2026-08-27)
- **Latest stable release checked:**
  [`v0.33.1`](https://github.com/ollama/ollama/releases/tag/v0.33.1)
- **License:** Ollama source is MIT; model weights and bundled/native dependencies have
  separate licenses.

The local checkout is a clean, source-complete shallow clone of the official repository.
It was refreshed from `origin/main`, and the latest stable tag was fetched. Wobblebot's
existing dirty worktree was not altered except for adding this assessment.

## Executive conclusion

Wobblebot should continue treating Ollama as an external inference runtime behind its
existing ports and adapters. There is no evidence that copying Ollama's Go/C++ runtime,
scheduler, model store, agent loop, or compatibility APIs into Wobblebot would improve the
system. It would create a second runtime to maintain without solving an observed problem.

The review did find several concrete improvements worth making:

1. Enforce that `provider: ollama` really means local inference. Current Ollama can
   transparently send a request made to the local server to Ollama Cloud, while Wobblebot
   assumes every Ollama call is local, free, and exempt from ADR-014 accounting.
2. Fix two adapter correctness defects: `/api/chat` returns reasoning in
   `message.thinking`, but Wobblebot reads a top-level field; and the advisor's configured
   system prompt is currently sent as ordinary prompt text rather than as Ollama's
   `system` field.
3. Use Ollama's capability, version, digest, and response metadata to make model selection,
   failure diagnosis, and P4 scoring reproducible.
4. Finish the already-planned migration from model-name guessing and bare JSON mode to
   version-gated native `think` plus JSON Schema, while retaining Pydantic validation and
   every Wobblebot safety gate.
5. Make context size and truncation behavior explicit on the CPU NAS, and verify the
   unauthenticated Ollama port is reachable only from intended hosts/containers.

These are integration-hardening changes. They do not justify a new advisor role, increased
LLM authority, an all-local MoE, or a vector/RAG subsystem.

## Stage alignment and scope

At assessment time, the roadmap was ahead of the root status paragraph; the 2026-08-28
documentation reconciliation corrected that pointer. P4 began on 2026-08-17, and its
buildable scope is complete except for the data-gated Historian
([roadmap](../planning/roadmap.md)). The suggestions here fit maintenance and adapter hardening.
They do not start Phase 9 or bypass the P4.6 gate.

This assessment preserves the load-bearing decisions:

- ADR-002: LLM output is advisory and cannot execute trades.
- ADR-003: only the Harvester can initiate transfers.
- ADR-013/ADR-034: operator mutations pass through typed intents, `pending_commands`, and
  explicit confirmation; an assistant cannot originate a withdrawal execution.
- ADR-014: remote inference is cost-accounted and bounded.
- ADR-015: a provider or model is not silently substituted.

## What Wobblebot already does well

This is not a greenfield integration. Wobblebot already has:

- A native `/api/generate` advisor adapter with schema-constrained output for
  non-thinking models and independent Pydantic/domain validation
  ([adapter](../../src/wobblebot/adapters/ollama.py)).
- A native `/api/chat` operator assistant with role-separated conversation messages,
  typed `OperatorIntent` validation, bounded timeout retry, and deterministic report
  fallbacks ([adapter](../../src/wobblebot/adapters/ollama_assistant.py)).
- Mixed-provider MoE composition, with local Ollama experts isolated behind `AdvisorPort`.
- A non-billable `/api/tags` reachability check in `/health`.
- Operator-controlled pull, probe, resume, and cull tooling. There is no need for another
  model-management framework.
- Role-specific evaluation batteries and an advisor-seat register. Ollama's capability
  flags can establish compatibility, but must not replace those empirical quality tests.
- A measured decision not to run the safety-sensitive main advisor seats locally on the
  current CPU NAS. The deployed local role is narrow: the Gremlin and selected operator
  prose/intent work. Current evidence does not support moving quant, risk, news, or
  arbitrator seats back to local Ollama.

Keeping the native API and the current `httpx.MockTransport` test seam is preferable to
adding the official Python client or routing everything through Ollama's partial OpenAI
compatibility layer.

## Relevant upstream architecture

Ollama is a model-serving system, not an application policy layer. Its relevant pieces are:

- `api/`: native request/response types and client behavior.
- `server/`: HTTP routes, prompt construction, model metadata, cloud proxying, and the
  resource scheduler.
- `llm/`, `ml/`, and `discover/`: runner lifecycle and hardware-specific inference.
- `manifest/` and model storage: content-addressed manifests and verified layers.
- `model/`, renderers, parsers, and tool normalization: model-family compatibility.

The useful ideas to adopt are capability negotiation, digest-keyed provenance, structured
error handling, explicit load/sampling controls, and constrained output followed by
application validation. The runtime itself belongs outside Wobblebot's Python hexagon.

Current native API features directly relevant to Wobblebot include:

- `/api/version`, `/api/tags`, `/api/show`, and `/api/ps` for server/model identity,
  capabilities, quantization, context, load state, and remote provenance.
- Full JSON Schema in `format` for chat and generate requests.
- Explicit `think`, `truncate`, `keep_alive`, `num_ctx`, `seed`, and `num_predict` controls.
- `done_reason`, token counts, per-phase timing (prompt eval and generation), and load
  duration on responses.
- `remote_model` and `remote_host` fields when local requests use remote inference.

Important contract caveat: Ollama's native `/api/*` path is unversioned, and the checked-in
OpenAPI document does not describe every live field. Wobblebot should probe the server
version and contract-test the exact response fields it consumes during an Ollama upgrade.

## Prioritized findings

| Order | Finding | Recommendation | Value | Effort |
|---:|---|---|---|---|
| 1 | Local calls can transparently become cloud calls | Fail closed on remote provenance and disable Ollama Cloud | Critical boundary | Small |
| 2 | Chat thinking is parsed from the wrong location | Read `message.thinking`; add an upstream-shaped fixture | High | Small |
| 3 | Advisor policy is not sent as a system prompt | Use the native `system` field; keep engine state in `prompt` | High | Small |
| 4 | Outer-envelope and report errors can escape port contracts | Normalize JSON/timeout/HTTP errors to port exceptions | High | Small |
| 5 | Mutable tags obscure which weights produced a result | Preflight and persist version/digest/capabilities | High | Medium |
| 6 | Local inference failures and truncation are weakly observable | Consume metrics/`done_reason`; add provider-neutral invocation telemetry | High | Medium |
| 7 | Assistant output is JSON-only, not schema-constrained | Pilot native `think` plus an actual intent schema | High | Medium |
| 8 | CPU defaults can silently truncate conversational context | Configure and verify context/truncation by role | Medium | Medium |
| 9 | Ollama's local API has no authentication | Audit binding/firewall; keep port 11434 private | High operational | Small |
| 10 | Probe results can vary for reasons unrelated to a candidate | Add fixed `seed` to probe tools only | Medium diagnostic | Small |

### 1. Make `provider: ollama` verifiably local

Current Ollama supports cloud models that are automatically offloaded while callers keep
using the local API. The current response contract exposes upstream identity as
`remote_model` and `remote_host` for both
[`ChatResponse`](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/api/types.go#L518-L548)
and
[`GenerateResponse`](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/api/types.go#L900-L940).
The upstream cloud documentation explicitly describes automatic offload
([source](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/docs/cloud.mdx#L6-L16)).

Wobblebot currently encodes `ollama` as intrinsically local and free:

- ADR-014 says Ollama bypasses the cost gate and needs no instrumentation
  ([decision](../architecture/decisions.md)).
- `LLMProvider` excludes Ollama and `LLMCallRecord` is cloud-only
  ([model](../../src/wobblebot/domain/llm_cost.py)).
- The `llm_calls` database constraint excludes Ollama, and a pure-Ollama advisor does not
  build the cloud ledger/cost wiring.

Therefore, a cloud-backed Ollama tag could send market/operator context off the NAS and
incur remote usage while Wobblebot records it as local and bypasses its cost/provenance
controls. Calling `localhost` is no longer proof of local execution.

Recommended defense in depth:

1. Set `OLLAMA_NO_CLOUD=1` or `disable_ollama_cloud: true` on the Wobblebot-serving Ollama
   instance ([upstream local-only configuration](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/docs/faq.mdx#L165-L181)).
2. Reject configured `:cloud` tags during startup preflight.
3. Reject any `/api/show`, chat, or generate response with non-empty `remote_host` or
   `remote_model` when provider semantics are `ollama`.
4. Emit an explicit operator-visible configuration error; never silently substitute a
   different model/provider.

If Ollama Cloud is ever desired, it should enter Wobblebot as an explicitly remote,
costed provider through a follow-on ADR. It must not inherit the local/free exception.
Current Ollama Cloud also does not support structured output, but that compatibility
failure is not an adequate security boundary.

### 2. Correct native response and prompt semantics

#### Chat reasoning location

Current native chat responses put reasoning at `message.thinking`
([message type](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/api/types.go#L197-L207)).
Generate responses instead use a top-level `thinking` field. Wobblebot's advisor handles
the generate shape correctly, but the assistant reads `envelope.get("thinking")` after
reading `message.content`
([current parser](../../src/wobblebot/adapters/ollama_assistant.py)). Its test fixture
codifies that outdated top-level chat shape.

Impact: a reasoning response with empty final content can be classified as an empty
response, retried, and then failed even though Ollama returned reasoning content.

Change the assistant decoder to use `message.get("thinking")`, optionally accepting the
old top-level field only as a documented compatibility fallback. Add fixtures matching
current upstream chat and generate envelopes so the two shapes cannot be conflated again.

#### Advisor system role

`OllamaAdapter` says the configured prompt body becomes the system prompt, but it currently
concatenates those instructions and the engine summary into the ordinary `prompt` field.
It does not populate `GenerateRequest.system`. When the request system field is empty,
Ollama uses the model's embedded Modelfile system prompt.

Send `self._prompt.body` as `system` and keep the serialized engine state plus task request
in `prompt`. This restores the intended instruction hierarchy and reduces the chance that
model-embedded policy or untrusted content in a summary competes at the same role level.

During model preflight, inspect `/api/show` for embedded `system` and `messages`. Record and
explicitly approve unexpected overlays by digest rather than letting a mutable tag change
the advisor's instruction stack unnoticed. Moving the advisor to `/api/chat` could make
role separation even clearer, but that behavior change should be a later battery-tested
pilot, not part of the small correction.

### 3. Restore the adapters' port-level failure guarantees

Two additional defects were confirmed in the current integration:

- Both adapters call `response.json()` inside blocks that catch only `httpx.HTTPError`.
  A malformed outer HTTP body raises a JSON-decoding exception and can escape
  `AdvisorError`/`AssistantError`, defeating MoE isolation and normal daemon degradation.
- `OllamaAssistantAdapter.summarize()` converts a read timeout into the private
  `_OllamaReadTimeoutRetry` marker. That marker is translated only by intent parsing;
  report builders catch `AssistantError`, so a status/weather timeout can bypass the
  deterministic fallback promised by `AssistantPort`.

Normalize malformed envelope JSON, timeouts, and upstream HTTP errors to the appropriate
port exception. Preserve the existing single retry for the intent path. Parse Ollama's
sanitized JSON error body when available and classify at least:

- `400`: unsupported capability or invalid request; do not retry unchanged.
- `404`: model missing; surface provisioning/configuration failure.
- `503`: queue full; bounded retry with jitter is defensible.
- Read/connect timeout and selected `5xx`: bounded transient handling.
- Malformed envelope or `done_reason: length`: failed inference, not a valid HOLD.

Tests should cover malformed outer JSON, every public adapter method's promised exception,
and deterministic report fallback on a summary timeout.

### 4. Make model identity and compatibility forensic

An Ollama tag such as `latest` is mutable. Wobblebot currently persists the configured
`model_name`, but not the content digest. P4's counterfactual rank/hit-rate work could
therefore aggregate outcomes from different model weights under one apparent identity.

Before admitting a configured model, query:

- `/api/version` for server compatibility.
- `/api/tags` and `/api/show` for existence, digest, quantization, context length,
  capabilities, license, embedded system/messages, and remote provenance.
- `/api/ps` after warmup when actual allocated context/load placement matters.

Record at least `{configured_tag, digest, ollama_version, quantization, capabilities,
prompt_version}` with probe results and advisor suggestions. Warn if the digest changes
mid-deployment. Do not auto-pull or auto-replace it; use the existing operator-controlled
pull/probe/promotion workflow.

The existing `/health` check should retain its cheap reachability signal but distinguish it
from configured-model readiness. An empty `models` list currently counts as healthy. A
more useful status is:

1. server reachable;
2. configured model installed;
3. required completion/thinking capability present;
4. expected digest observed;
5. local provenance confirmed;
6. loaded/unloaded state reported as detail, not health (an unloaded four-hour Gremlin is
   normal).

### 5. Consume completion metadata and add local invocation telemetry

Ollama returns `done_reason`, load/prompt/evaluation durations, and prompt/evaluation token
counts
([metrics type](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/api/types.go#L557-L564)).
Wobblebot currently discards them.

Immediate use:

- Reject `done_reason == "length"` with a precise truncation error. This is already a HIGH
  item in the probe-battery backlog; it should not be relabeled as a new discovery.
- Emit structured, prompt-free logs for load duration, prompt tokens/duration, output
  tokens/duration, model digest, and server version.
- Distinguish cold loading, prompt evaluation, generation, queue pressure, and malformed
  model output when diagnosing NAS latency.

Follow-on architecture decision:

The inference failure streak reads `llm_calls`, but Ollama never writes that table.
`/health` can therefore show `/api/tags` green while the configured local model fails every
inference. Either broaden `llm_calls` into a provider-neutral invocation ledger or add a
separate inference ledger shared by local and cloud adapters. A genuinely local Ollama
row has `cost_usd = 0`, while remote provenance must never be recorded as local/free.

Because the present schema and ADR explicitly define `llm_calls` as a cloud cost ledger,
this needs a small ADR-014 amendment and migration rather than an incidental database
constraint edit. Do not persist prompts, chain-of-thought, or raw operator/financial
context in this telemetry.

### 6. Version-gate native thinking and full assistant schemas

Wobblebot's advisor already sends a curated JSON Schema on the constrained path. The
assistant sends only `format: "json"` and then validates the decoded object with the
discriminated `OperatorIntent` union. Current Ollama supports:

- a native `think` field, including model-specific reasoning levels;
- a thinking capability reported by model metadata; and
- schema-constrained final output after a thinking pass.

This provides a route to retire model-tag substring guessing and reduce the malformed-key
failures already observed in operator-model sweeps:

1. Probe `/api/version` and `/api/show` capabilities.
2. Generate a schema from the LLM-emittable `OperatorIntent` adapter, not from a broader
   command set. `ExecuteProposalCommand` must remain structurally absent.
3. Validate that Ollama accepts Pydantic's `$defs`/discriminated-union schema shape; if not,
   derive a curated wire schema as the advisor does.
4. Send an explicit `think` policy rather than inferring it from a model tag.
5. Keep Pydantic validation, the pending-command firewall, range checks, and confirmation
   as final authorities.
6. Run the existing assistant/advisor batteries on the deployed NAS version and promoted
   digest before enabling the modern path.

This modernization is partly existing backlog. The new repository review confirms the
upstream mechanism and the version/capability gate; it does not establish that every
current local model will become better or faster. Thinking plus constrained output can
require a second generation pass, so latency must be measured.

### 7. Make context and truncation explicit

Ollama's current automatic context default is hardware-dependent and falls to 4096 tokens
below the higher VRAM thresholds. Chat truncation is enabled by default and removes older
messages while preserving the system and last message
([prompt truncation](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/server/prompt.go#L21-L91)).

Wobblebot's operator prompt has already approached the 4K range before engine snapshot and
conversation history. Silent removal of prior turns can change the meaning of a follow-up
operator command.

Add per-role `num_ctx` and truncation policy only after measuring NAS memory and latency.
At startup or after warmup, compare `/api/ps`'s allocated context with the configured
minimum. For operator intent, failing visibly when required context cannot fit is safer
than silently discarding conversational premises. For free-form status prose, bounded
truncation may be acceptable. Keep those policies separate.

`keep_alive` should also be measured rather than set to `-1`. Ollama defaults to five
minutes. The local Gremlin runs on a much longer cadence, so permanent retention would
waste memory. A bounded operator keepalive is worth testing only if observed cold-load
latency remains material after the existing retry path.

### 8. Constrain the unauthenticated service boundary

Ollama's local API has no built-in authentication, and the same server exposes inference
plus model pull/create/copy/delete routes. It binds to loopback by default, but Wobblebot's
Docker deployment reaches a host-managed service at `host.docker.internal:11434`
([compose](../../docker/docker-compose.yml)). That topology often requires a listener
beyond loopback.

This review did not inspect the live NAS firewall, so it does not claim the port is
currently exposed. It establishes that the deployment should verify:

- port 11434 is not publicly or broadly LAN-routed;
- only the Docker bridge/intended administration host can reach it;
- a cross-host deployment uses firewalling and an authenticated TLS reverse proxy;
- CORS/`OLLAMA_ORIGINS` is not mistaken for authentication;
- `OLLAMA_DEBUG_LOG_REQUESTS` remains off in production because it stores full prompts and
  replay commands in temporary storage.

Where practical, an authenticated proxy can allow inference/status paths to daemons while
keeping model mutation endpoints operator-only. Do not add this complexity if a private
host/container network already provides the boundary; verify first.

### 9. Improve probe reproducibility without changing production behavior

Ollama exposes a sampling `seed`. Add a fixed seed option to model probe and battery tools
so repeated compatibility runs can separate model/config changes from sampling noise.
Production should remain governed by its evaluated role configuration; a fixed seed is
not automatically desirable for live recommendations.

Contract tests should pin the upstream shapes Wobblebot depends on:

- chat `message.thinking` versus generate top-level `thinking`;
- local/remote identity fields;
- JSON-schema output;
- `done_reason` and metrics;
- structured error envelopes; and
- `/api/show`/`/api/tags` metadata used by preflight.

## Confirmed documentation/configuration drift

The 2026-08-28 documentation reconciliation closed the documentation-only items found by this
assessment: the advisor-seat register now reflects the NAS panel/Gremlin deployment;
`docker/README.md` describes the 1.5B operator, external host Ollama, and operator-specific seat
overrides; the retry policy records the assistant's one narrow retry; and the deployment page no
longer invents a Compose-managed LLM container or points to a user-home rule.

One configuration issue remains intentionally separate from this documentation pass: the
example's top-level `deepseek-r1:7b` advisor retains `max_tokens: 512` and a 60-second timeout
beside comments saying thinking models need 2048+ tokens and 180+ seconds. The production profile
overrides it, but copying the generic example can fail. Correcting that default changes operator
configuration behavior and should land with validation/tests rather than masquerading as prose.

## Ideas that should not be incorporated now

| Upstream feature/pattern | Decision | Evidence threshold for reconsideration |
|---|---|---|
| Ollama runtime/scheduler/model store | Do not vendor or port | A requirement to manage inference without Ollama itself, plus an ADR and maintenance budget |
| Native tool/agent loops | Do not expose mutations | A read-only retrieval use case that cannot be met by deterministic context assembly; never direct trade/transfer/settings authority |
| Ollama web search | Do not add | A measured coverage gap in the provenance-aware news pipeline and an explicit egress/cost design |
| Embeddings/vector storage/RAG | Defer | P4.6 Historian or news benchmarks showing retrieval/duplicate-context failure |
| OpenAI/Anthropic compatibility proxy | Do not use for Wobblebot adapters | A demonstrated reduction in complexity that retains provider-native pricing, metrics, errors, and provenance |
| Prompts embedded in Modelfiles | Do not use | None expected; committed prompt files are more auditable and less prone to tag drift |
| Automatic pulls, upgrades, or deletes | Do not add to daemons | Operator-approved maintenance workflow with digest pinning and rollback |
| All-local MoE / more parallelism | Do not add on current NAS | New hardware plus fresh role batteries showing quality and latency wins |
| Infinite keepalive | Do not default | Measured latency benefit without memory pressure or model eviction |
| Streaming schema-bound responses | Defer | A demonstrated timeout/latency problem not solved by metrics, context, and bounded retry |
| Logprobs as confidence | Research only | Calibration evidence that they improve ADR-035 rank/hit-rate beyond outcome scoring |
| Ollama model recommendations | Do not substitute for seat batteries | Independent evidence that recommendations predict Wobblebot role quality |

Ollama's tool calls are output data, not authorization. Even a future read-only tool must
be allowlisted, argument-validated, time/iteration-bounded, credential-free, and unable to
mutate engine, exchange, settings, approvals, or transfers.

## Suggested delivery sequence

No code change is required to benefit from this review immediately. The smallest safe
sequence is:

### Slice A — boundary and correctness

- Set and document Ollama local-only mode.
- Add remote-provenance rejection.
- Fix `message.thinking` extraction and upstream-shaped tests.
- Send the advisor prompt through the native system field.
- Normalize malformed-envelope and `summarize()` errors to port exceptions.

### Slice B — identity and diagnostics

- Add version/model/capability/digest preflight.
- Capture response metrics, `done_reason`, and useful sanitized error bodies.
- Record structured model provenance with probes/suggestions.
- Amend ADR-014 before generalizing persistent inference telemetry to local calls.

### Slice C — schema and context pilot

- Pilot native `think` plus the real assistant schema behind version/capability checks.
- Add explicit per-role context/truncation settings and verify them on the NAS.
- Add a fixed probe seed and run the existing batteries against the exact digest.

### Slice D — operations cleanup

- Verify the NAS port/firewall boundary.
- Correct the confirmed documentation and example-config drift.
- Tune bounded keepalive only if captured metrics show an actual cold-load problem.

## Acceptance evidence for a future implementation

A follow-up change should demonstrate:

1. A local model succeeds with empty remote-provenance fields; a cloud tag or remote response
   fails before its output is admitted.
2. Current upstream-shaped chat thinking is parsed once without an empty-content retry.
3. Advisor instructions occupy the system field and engine/news data remains user content.
4. Malformed outer JSON and summary timeouts surface only as the documented port errors.
5. Missing model, unsupported capability, queue-full, truncation, and transport failure are
   distinguishable in tests and operator-visible diagnostics.
6. A suggestion/probe receipt contains the immutable model digest and Ollama version.
7. No prompt, chain-of-thought, API credential, or raw financial/operator context is added
   to logs or telemetry.
8. The current safety tests still prove that Ollama cannot execute trades, approve commands,
   rewrite settings, or initiate transfers.
9. The assistant/advisor probe batteries show no regression on the exact promoted NAS model
   digest and context allocation.

## Licensing conclusion

Ollama's repository source is MIT licensed
([license](https://github.com/ollama/ollama/blob/f96e7aa0513b9973a0ccc71be414c2ecb9d65b1a/LICENSE)).
Direct reuse is permitted if the copyright and permission notice is retained in copied or
substantial portions. Conceptual reuse is more appropriate here because the runtime and
language boundaries differ.

The repository license does not grant rights to every model weight. `/api/show` exposes
model license text; any future bundling or redistribution must inventory each promoted
model separately. Native/vendor dependencies also retain their own licenses.
