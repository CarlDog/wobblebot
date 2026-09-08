# ADR-043 — Explicit advisor fallback targets and safe provider failures

**Status:** Accepted for local implementation; activation requires operator configuration.
**Date:** 2026-09-08
**Deciders:** Operator request to address hidden credit errors and add fallback options.

## Context

News and arbitrator used the same provider and failed continuously when its API
credit became insufficient. HTTP wrapping and the call ledger reduced the account
error to `http_400`. Successful quant/risk calls could not rescue the unavailable
arbitrator; the cascade produced its heuristic HOLD. This is a maintenance follow-up
to the observed interruption, separate from formal 2.0.x acceptance or 2.1 entry.

ADR-015 deferred provider substitution until an observed need justified explicit
opt-in and a provenance trail. This decision supersedes its no-cross-provider
restriction for the single advisor, MoE experts and arbitrator. It retains the
retry policy and the prohibition on implicit provider/local substitutions.

## Decision

1. Classify known billing/quota/authentication/model-access failures using bounded,
   defensive response parsing and fixed labels. The observed Anthropic credit
   message requires its actual host, HTTP status and error type. Unknown errors
   retain their status; never echo or persist the provider's message/body.
   Logs and health rows use fixed actionable hints. Retry exhaustion records the
   underlying failure. Recognized quota/billing denials never waste transient retries.
2. An optional `fallbacks` list on each advisor target allows at most two distinct
   alternatives. Empty or omitted means the existing primary-only behavior. Each
   target declares its provider, model and inference settings; it inherits the
   parent's prompt, role, summary and arbitrator context. Nested chains are invalid.
   The primary is tried first on every invocation, followed by configured alternatives
   only after a classified availability/account failure (and existing transient retries).
3. Failover permits insufficient credit, provider quota/billing, authentication or
   permissions, explicit model-not-found, rate limits, server errors and transport
   interruptions. Generic bad requests, output/schema errors, content refusals,
   missing pricing, storage failures and programming errors do not qualify.
   Cancellation propagates. No alternate can bypass `LLMCostCapExceeded`.
4. Each cloud candidate reuses the existing cost gate, ledger, session tracker and
   trace ID. It gets a fresh gate check before its logical call. Retry timing and
   per-model HTTP timeouts remain existing bounded controls. No extra dependencies,
   spend increases, automatic purchases or hidden model selection are introduced.
5. Structured substitution logs name the from/to provider and model, role, safe
   failure kind and trace. Runtime-owned `llm_attempts` accompany successful
   recommendations, including per-expert and arbitrator attempts. An additive SQLite
   JSON column defaults old rows/writers to `[]` (unknown history, never invented).
   The advisor page renders the trail and the saved model label names actual routes.
   Models cannot supply this metadata through their response parser.
6. Health remains a **cloud-call** view. A successful cloud backup does not hide the
   window's failed calls and latest failure reason. Local Ollama is explicitly
   selectable but remains unbilled/outside this ledger; its success is visible in
   the suggestion's routing trail. The existing cascade is the last resort when all
   configured LLM candidates fail. News-role and auto-apply firewalls remain intact.

## Options considered

| Option | Complexity | Availability | Cost/control |
| --- | --- | --- | --- |
| Retry and heuristic only | Existing | No LLM answer during account/provider outages | Existing budget, no substitution |
| Automatically choose another provider | High | Depends on unknown credentials/model fitness | Unapproved decision-maker and costs; rejected |
| Explicit bounded per-role alternatives | Moderate | Survives an unavailable primary when a backup works | Existing caps and operator-selected models; accepted |

Different models can make different recommendations. A fallback must be validated
for its role before activation. The [seat register](../reference/advisor-seats.md)
has prior gpt-5-mini arbitration evidence and a weaker news result than Haiku;
its current risk assignment alone does not qualify it for another role.
An independent provider is preferable when the
failure is provider/account-wide; two models behind the same provider share that risk.

## Consequences and review

Fallback adds latency and may incur additional calls within the existing caps. Timeouts
can represent provider work whose usage was not returned; current cost accounting
records confirmed usage and does not guarantee a precise invoice ceiling. This change
preserves that accounting policy rather than claiming to resolve it. There is no new
circuit breaker or persistent cooldown; the next scheduled evaluation probes the primary.

Operator chat and the separate gremlin are outside this advisor-role change. Their cloud
errors still benefit from shared classification. Fallback targets remain disabled in
the shipped example and no production seats are changed by implementation.

Adversarial review checks: prompt/context loss at the arbitrator seam; a substituted
news model impersonating quant; forged provenance; bypass of the shared cap; arbitrary
400/refusal errors triggering substitution; cancellation swallowed; same-target loops;
and loss of history during upgrade/rollback. Offline regressions must cover these before
the roadmap records local completion.

Provider references, checked 2026-09-08:
[Anthropic errors](https://platform.claude.com/docs/en/api/errors),
[OpenAI errors](https://developers.openai.com/api/docs/guides/error-codes),
[Gemini troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting).
