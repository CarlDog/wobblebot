# FreeToken repository assessment for WobbleBot

**Recorded:** 2026-09-09 UTC. **Status:** completed external-reference review;
recommendations unratified and unscheduled. This is not an accepted ADR, an
implementation plan, a security certification, or a production acceptance
receipt. [The roadmap](../planning/roadmap.md) remains authoritative for current
phase, release, and operational acceptance state.

**Work item:** `CarlDog/wobblebot#freetoken-review-2026-09-09`.

## Baselines and verdict

Source: [`FlashML-org/FreeToken` at `e05cff83`](https://github.com/FlashML-org/FreeToken/tree/e05cff83a04b322fc7823678aa2d05c826aad26c).
Target: WobbleBot `0481f82b524b2ab01056ccee7e800724c3640759`, clean at the
review baseline. Pinned target links refer to that commit; this documentation
receipt does not alter the ongoing v2.0.11 acceptance record.

FreeToken is a plausible **optional local advisory backend**, subject to a
bounded workload evaluation and service-contract fixes. WobbleBot already has
the provider separation needed to contain such an experiment. There is no
evidence here to replace the advisor architecture, expand model authority, or
change the trade/withdrawal boundary.

An explicit local provider is preferable to pretending FreeToken is a hosted
OpenAI SKU or an Ollama daemon. The current OpenAI HTTP adapter can inform its
request/response handling, but application configuration, accounting, identity,
and failure semantics need a deliberate design. This is not a base-URL-only
configuration change at the application boundary.

## Recommendation register

| ID | Recommendation | Disposition | Acceptance proof if ratified |
| --- | --- | --- | --- |
| WB-FT-01 | Explicit local OpenAI-compatible advisory provider | Open candidate, unscheduled | End-to-end config/dispatch to a verified local service, truthful model identity, existing typed recommendation validation, and no new trading authority |
| WB-FT-02 | Separate provider charges from local resource accounting | Prerequisite to WB-FT-01 | No cloud-SKU impersonation or fake free-price row; missing usage stays unknown; cost/retry guards remain meaningful |
| WB-FT-03 | Prove admission, cancellation, output budgets, and process ownership | Prerequisite to a shared-service pilot | Bounded concurrency, queue and deadlines; overload/timeout/engine-death tests; no duplicate blind retry after unknown outcomes |
| WB-FT-04 | Stable-prefix prompt experiment | Parked until baseline quality/latency is established | Identical stable prefix demonstrably reduces prefill cost without stale market inputs or changed recommendation quality |
| WB-FT-05 | Accounting outbox / protocol-neutral generation core | Corroboration; no automatic refactor | Only adapt at an evidenced gap after comparing existing ledgers, retries, and ports |
| WB-FT-06 | Semantic recommendation caching, provider-policy bypass, or autonomous trade execution | Rejected | None follows from inference acceleration or API compatibility |

## 1. Integration seam: reuse the wire shape, retain explicit identity

The [OpenAI adapter][wb-http] constructs chat messages, sends an explicit
`max_completion_tokens` and temperature, and appends `/v1/chat/completions`
to its API base. The adapter accepts a base URL, but the [application factory][wb-factory]
uses the ordinary hosted default for `openai` and a dedicated Atlas route for
`atlas`. The [provider configuration literal][wb-config] has no general local
OpenAI-compatible provider.

The native [Ollama adapter][wb-ollama] calls `/api/generate` and requests a JSON
schema. FreeToken's OpenAI chat endpoint does not implement that native API,
and its [unsupported-feature handling][ft-unsupported] explicitly rejects
JSON-schema/JSON-object constrained decoding. Pointing Ollama configuration at
FreeToken would not preserve the contract.

**Proposed narrow slice:** if selected later, add an explicit local-provider
identity with a validated endpoint, model/revision/alias policy, explicit
budgets and timeout, and the existing advisor result validation. Reuse the
shared HTTP/protocol logic only where its hosted-provider assumptions do not
leak. Repeated `/v1` path segments must be covered by the endpoint contract.
Do not add a fake API key or hosted model name to bypass current wiring.

**Proof:** configure through the real application factory, exercise a valid
recommendation and malformed/incomplete responses, and verify the recorded
provider/model actually identifies the served checkpoint. An adapter-only
probe is insufficient if application configuration cannot express the route.

## 2. Preserve truthful costs and operational limits

[Pricing lookup][wb-pricing] deliberately raises for unknown provider/model
pairs. FreeToken does not make a local checkpoint a known hosted price row.
An unpriced request must not evade the existing accounting boundary by using
an unrelated SKU, a fake zero cost, or missing usage interpreted as zero.

A later design should explicitly distinguish provider-billed charges from
local GPU/RAM/time consumption. Local operation can have zero provider charge
without zero resource cost; unknown measurements remain unknown. Model IDs,
token counts, cached-token counts, and finish reasons must carry their actual
meaning. Preserve the current advisor's retry limits, outcome classification,
budget enforcement, and deterministic result validation as applicable to the
new provider, rather than inheriting accidental cloud assumptions.

**Proof:** unknown model, missing usage, truncated output, provider rejection,
timeout, retry, and cancellation fixtures leave truthful records and do not
generate unbounded or duplicate work. Record an explicit policy for resource
budget exhaustion before trying shared production hardware.

## 3. FreeToken defects that constrain a pilot

These are upstream findings at the pinned source, not WobbleBot regressions.
The detailed source report is in the local FreeToken fork at
`docs/fleet-review-2026-09-09.md`.

| Source ID | Verified source/probe finding | WobbleBot consequence |
| --- | --- | --- |
| FT-01 | [Benchmark handler][ft-benchmark] allows a serving start while a stubbed benchmark child remains active | Reserve hardware across full lifecycle operations before sharing the service |
| FT-02 | [Profile helper][ft-profile-import] imports model dependencies from the supposedly lightweight daemon | Readiness/diagnostics must be verified without inference dependencies when promised |
| FT-03 | [Request conversion][ft-spec] silently ignores some accepted controls; required/parallel tool semantics are not fully enforced | Do not rely on compatibility labels for schema, tool, seed, or penalty guarantees; keep deterministic recommendation validation |
| FT-04 | [Chat response][ft-chat] can report the requested model instead of the loaded model | Reject unknown IDs or validate explicit aliases; preserve accurate advisor/evaluation attribution |
| FT-05 | [Sampling fallback][ft-sampling] uses 32,768 when chat omits the budget despite a configured server default | WobbleBot already sends an explicit budget; keep that invariant and test it through dispatch |
| FT-06 | Nonstreaming chat lacks the streaming disconnect wrapper; pending admission is not globally bounded by the running limit | A caller timeout may leave remote work; verify cancellation and finite queue ownership before enabling retries/concurrency |
| FT-07 | [Release workflow][ft-release] builds/publishes without the Python test-suite gate | Independently validate the pinned runtime/artifact and hardware before relying on it |

The reviewed generation application is separate from the daemon's optional
token authentication. A later service boundary must protect exposed generation
and mutation routes intentionally. The [checkpoint encoder loader][ft-encoder]
can execute model-supplied Python, so the inference process must not inherit
WobbleBot's exchange credentials or trading authority. No such configuration
change or exploit test was performed here.

## 4. Workload acceptance before any provider switch

An operator-approved pilot should use fixed, sanitized historical/synthetic
advisor inputs. It should not submit trades or change live advisor routing.
Set limits and pass/fail criteria before comparing against the current provider:

1. **Output correctness:** complete recommendations satisfy the existing schema,
   enum/range checks, numeric consistency, and deterministic business validation.
   Score malformed, empty, truncated, and refusal outputs explicitly.
2. **Identity and accounting:** effective model/revision is known, usage has
   documented semantics, and missing values and local costs are represented
   truthfully. Reasoning tokens and output budget interpretation are included.
3. **Performance:** cold and warm latency distributions, useful completed
   recommendation rate, GPU/host RAM, and concurrency fit the declared budget.
   Token decode speed alone is not sufficient.
4. **Failure ownership:** timeout/disconnect, overload, engine death, model
   switch, and shutdown leave bounded work and deliberate recovery/fallback.
   Do not retry an unknown outcome merely because the HTTP caller timed out.
5. **Authority:** existing advisory-only constraints remain. Generated text
   cannot authorize order execution, change risk limits, acquire exchange
   secrets, or exercise withdrawal authority.

No hardware or checkpoint has been selected and no pilot has been run. A
failure to meet the gate is a reason to retain the current provider, not to
relax the gate or bypass deterministic validation.

## 5. Prompt caching is exact reuse, not stale-advice reuse

FreeToken's [prefix cache][ft-prefix] matches token prefixes at supported
boundaries. A future experiment could keep stable policy/schema text before
dynamic market observations, but only after measuring the effect on output
quality and prefill latency. Tool-call anchors are not a general semantic cache.

Never reuse a recommendation merely because two market contexts are similar.
Fresh market data, balances, risk limits, and policy changes must reach every
request that requires them. WobbleBot's existing caching and state contracts
remain authoritative; this is a parked prompt-layout experiment.

## Verification and limits

The comparison used the current provider/config/pricing code and the pinned
FreeToken source. The source review obtained 29 passing daemon tests and two
POSIX-only skips across successful selections, parsed 463 Python files, and
used two ASGI diagnostics plus source-isolated API probes with inference
collaborators replaced. Initial temporary-directory fixture failures were
recovered explicitly. Those checks do not prove GPU behavior or advisory
quality. No WobbleBot runtime suite was rerun for this documentation-only pass.

No model download, provider switch, dependency addition, live service change,
new price row, trade, deployment, or operational acceptance change is included.
The recommendations remain unscheduled until selected through WobbleBot's
normal design/decision process. Existing architecture decisions and the roadmap
retain their authority.

[wb-http]: https://github.com/CarlDog/wobblebot/blob/0481f82b524b2ab01056ccee7e800724c3640759/src/wobblebot/adapters/openai.py#L192-L234
[wb-factory]: https://github.com/CarlDog/wobblebot/blob/0481f82b524b2ab01056ccee7e800724c3640759/src/wobblebot/cli/advise.py#L247-L287
[wb-config]: https://github.com/CarlDog/wobblebot/blob/0481f82b524b2ab01056ccee7e800724c3640759/src/wobblebot/config/advisor.py#L29-L36
[wb-ollama]: https://github.com/CarlDog/wobblebot/blob/0481f82b524b2ab01056ccee7e800724c3640759/src/wobblebot/adapters/ollama.py#L220-L244
[wb-pricing]: https://github.com/CarlDog/wobblebot/blob/0481f82b524b2ab01056ccee7e800724c3640759/src/wobblebot/services/llm_pricing.py#L634-L646
[ft-unsupported]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/server/openai_api.py#L627-L640
[ft-benchmark]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/daemon/app.py#L338-L381
[ft-profile-import]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/daemon/app.py#L61-L69
[ft-spec]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/server/openai_api.py#L58-L83
[ft-chat]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/server/openai_api.py#L147-L228
[ft-sampling]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/server/generation.py#L151-L187
[ft-release]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/.github/workflows/release.yml
[ft-encoder]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/tokenizer/tokenize.py#L172-L188
[ft-prefix]: https://github.com/FlashML-org/FreeToken/blob/e05cff83a04b322fc7823678aa2d05c826aad26c/python/freetoken/kvcache/hybrid_radix_cache.py#L75
