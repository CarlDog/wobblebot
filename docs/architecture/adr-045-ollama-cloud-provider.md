# ADR-045 — Distinct Ollama Cloud advisor provider

**Status:** Accepted for local implementation; model activation is separate.
**Date:** 2026-09-08
**Decider:** Operator request to implement Ollama Cloud as a new provider.

## Context

ADR-043 permits an equivalent cloud model ahead of the previously selected cloud
backup. The local `ollama` adapter has no cloud authentication, spend gate or call
ledger. Repointing that adapter would misclassify paid usage as local inference.
Ollama Cloud also lacks the structured-output enforcement used by local Ollama.

## Decision

- Add `ollama_cloud` to advisor primary, expert, arbitrator and fallback targets.
  Use native `POST https://ollama.com/api/chat`, non-streaming, with bearer
  `OLLAMA_API_KEY`. Keep the endpoint fixed and refuse redirects; never send this
  key to `OLLAMA_BASE_URL`. No model alias rewriting or local server is needed.
- Reuse the shared retry policy, cost gate, session tracker, trace and call ledger.
  Record the distinct provider `ollama_cloud`. Token credit consumption counts
  toward existing USD caps even when subscription credits cover the request.
  Monthly subscription charges and purchased credits are not per-call costs.
- Retain role prompts and arbitrator context. Omit unsupported `format` and local
  `keep_alive` controls. Map output cap to `options.num_predict`; generated token
  counts include thinking, so do not double-charge it. Normalize cached prompt
  tokens into disjoint ledger buckets. Validate model text locally; malformed or
  truncated responses are `AdvisorError`, not permission to change models.
- Register four verified constant-rate models initially: `gpt-oss:120b`,
  `gpt-oss:20b`, `qwen3.5:397b`, `gemma4:31b`. Unpriced identifiers fail before HTTP.
  DeepSeek has peak/off-peak pricing and is not registered in this implementation.
  Provider support is not seat qualification; existing primary/backup selections
  and disabled fallback lists remain unchanged.
- Widen the SQLite ledger's provider CHECK via a transactional, serialized table
  rebuild. Retain original columns, values, constraints, indexes and triggers.
  Read-only connections do not migrate. Older writers remain compatible, but old
  application readers cannot deserialize the new provider once its rows exist.
- Inject the optional key only into Compose `advise` and operator-invoked `tools`.
  Operator chat/assistant and gremlin provider support are outside this advisor
  change. The cloud-check tool rejects the unsupported operator role cleanly.

## Options and trade-offs

Reusing local Ollama would bypass accounting. Using an OpenAI-compatible adapter
would obscure provider identity and require different usage/wire semantics.
A small native adapter keeps those rules explicit while reusing cloud plumbing.
The shared ledger still records provider-call success separately from valid
recommendation output; confirmed usage remains billed even for invalid answers.
Missing usage or timeout responses cannot reconstruct provider charges and remain
failed rows with unknown usage, represented by zero under the existing contract.
Local estimates are not invoice reconciliation or atomic concurrent reservations.

## Consequences and verification

Availability failures follow ADR-043's bounded chain, then cascade to the heuristic.
Each subsequent scheduled non-guard evaluation starts at the primary. No extra
polling, credit purchase, quota reset, provider switch or budget increase occurs.

Offline tests cover native auth/payload, redirect refusal, usage/cache billing,
malformed output, retries, cap denial, cancellation, cloud fallback context/order,
heuristic recovery, tagged-schema preservation, concurrent starts and rollback
after a failed rebuild. No paid requests or live database migrations are needed
for local verification. Deployment and role qualification require separate evidence.

References verified 2026-09-08: [Cloud API](https://docs.ollama.com/cloud),
[chat contract](https://docs.ollama.com/api/chat),
[structured-output limitation](https://docs.ollama.com/capabilities/structured-outputs),
[prices](https://ollama.com/pricing), [model identifiers](https://ollama.com/api/tags).
