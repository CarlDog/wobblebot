# LLM interruption handling

The health page reports cloud calls, including consecutive failures, failed calls
within the window after a success, and fixed recovery hints. `insufficient_credit`
means check the provider API balance; `quota_exceeded` means check provider usage
and spending limits. `authentication_error`/`permission_denied` point to key/access
settings. Rate limits, transport errors and outages use the existing bounded retries.
Generic `http_400` still means an unrecognized bad request: do not assume billing.
Old ledger rows retain their original labels because the provider body was not stored.

## Configure alternatives

For `advisor.type: single`, place `fallbacks` alongside `provider` and `model`.
For MoE, put a separate list inside the expert and/or `arbitrator` entry. A list
contains at most two distinct targets and defaults to empty. Example fragment:

```yaml
# Within an existing advisor.arbitrator or advisor.experts item:
fallbacks:
  - provider: openai
    model: gpt-5-mini
    inference_params:
      temperature: 1.0
      max_tokens: 4000
      timeout_seconds: 120
```

This demonstrates the shape, not a tested seat replacement. Select and evaluate
each backup for its role. Existing providers `anthropic`, `openai`, `google`, `atlas`
and `ollama` are supported; cloud candidates require the corresponding environment
key, the `llm` configuration and cost-ledger wiring. Missing backup credentials fail
setup cleanly. The role's prompt and arbitrator opinions are identical for every
candidate; inference parameters belong to each specific model. For a local backup,
explicitly choose `provider: ollama` and a model available at `OLLAMA_BASE_URL`.

Cloud fallback checks the same daily/session budget before it sends a request.
A local spend-cap denial stops the chain, including local alternatives. Generic
bad requests, invalid output and content refusals do not trigger fallback. The
`cascade` engine retains its heuristic fallback after the configured chain fails.
All new invocations start at the primary, so a recovered provider resumes naturally.

## Verify activation

Keep the current seat register and deployment receipt when selecting alternatives.
Validate configuration and offline role fixtures first, then follow the normal
deployment procedure. Inspect successful news/arbitrator calls in `llm_calls`,
the suggestion's **LLM routing** section, and the substitution logs. Same-provider
backups may share an account outage; a local fallback success is recorded on the
suggestion but does not turn failed cloud requests into successes on the health page.

The new `advisor_suggestions.llm_attempts` column is additive, with an empty-array
default. Old rows and old writers have no attempt history. Old readers ignore the
extra column. Normal database backup and deployment verification still apply.
The full policy and implementation boundary are in
[ADR-043](../architecture/adr-043-llm-fallbacks.md).
