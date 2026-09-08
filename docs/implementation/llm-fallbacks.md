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
contains at most two distinct targets and defaults to empty. Example arbitrator
fragment for the current Haiku arbitrator (enabled on the NAS after the recorded checks):

```yaml
# Within an existing advisor.arbitrator or advisor.experts item:
fallbacks:
  - provider: atlas
    model: anthropic/claude-haiku-4.5-20251001
    inference_params:
      temperature: 1.0
      max_tokens: 4000
      timeout_seconds: 300
  - provider: openai
    model: gpt-5-mini
    inference_params:
      temperature: 1.0
      max_tokens: 4000
      timeout_seconds: 300
```

The example settings file includes explicit `fallbacks: []` entries at the base
advisor and each profile's advisor targets. For `--profile NAME`, edit the matching
entries under `profiles.NAME.advisor`; MoE backups belong inside each expert or
arbitrator there. Replace the empty list with the desired targets and restart the
advisor. Existing `config/settings.yml` files are gitignored, so a source update
does not insert new settings into an operator's copy automatically.

Role-specific, preselected candidate blocks are commented beside the empty lists;
their [seat-register entries](../reference/advisor-seats.md#fallback-candidates)
explain why each model was chosen and its remaining limitations. The preferred
order is primary → equivalent model on another cloud route → the previously
selected cloud fallback → heuristic. Skip the equivalent slot when no verified
match exists; a different model family is a backup, not an equivalent. The Atlas
Haiku/Sonnet matches are listed and priced but report `is_ready: false`; that flag
did not prevent successful Haiku role calls. NAS news/arbitrator now use Atlas
Haiku first; the [seat register](../reference/advisor-seats.md#fallback-candidates)
records the activated quant/risk/final backups and qualification limits. Sonnet
remains an unactivated example. Local Ollama candidates were removed from these
presets. Generic example lists stay disabled until the operator replaces
the active `[]` with the selected list. When using a
MoE profile, leave the base `advisor.fallbacks` empty and edit the chosen profile's
expert/arbitrator lists. Operator chat and the gremlin have no fallback support in
this change.

This demonstrates the shape, not a tested seat replacement. Select and evaluate
each backup for its role. Providers `anthropic`, `openai`, `google`, `atlas`,
`ollama_cloud` and `ollama` are supported; cloud candidates require the corresponding environment
key, the `llm` configuration and cost-ledger wiring. Missing backup credentials fail
setup cleanly. The role's prompt and arbitrator opinions are identical for every
candidate; inference parameters belong to each specific model. For a local backup,
explicitly choose `provider: ollama` and a model available at `OLLAMA_BASE_URL`;
that remains a generic capability, outside the current cloud-only fallback policy.
For direct cloud access use the distinct `ollama_cloud` provider with `OLLAMA_API_KEY`,
shared cost/retry accounting and local output validation; see [Ollama Cloud setup](ollama-cloud.md).
No exact current primary was found in its catalog, so no Ollama Cloud route is preset.

## Workflow and failure boundaries

1. With `engine: cascade`, a clear heuristic guard resolves the tick immediately.
   A non-guard tick enters the configured LLM route. `engine: llm` enters it directly;
   `engine: heuristic` never constructs or calls the candidates.
2. Each role tries its primary, then fallback list item 1, then item 2. The first
   successful, parsed recommendation stops that role's chain. All new invocations
   start at the primary; there is no sticky fallback or persistent cooldown.
3. Before switching, a cloud candidate exhausts `llm.retry` for transient failures
   (rate limiting, server/transport interruption). `max_retries: 3` means up to four
   HTTP attempts for that candidate. Account credit/quota/access denials switch
   without those retries. Ollama currently makes one HTTP attempt per candidate;
   it does not use the cloud retry loop.
4. Every cloud candidate checks the same daily/session budget before its logical
   call. A **provider** credit/quota denial can reach the Ollama candidate after the
   cloud backup fails. A **bot spend-cap** denial (`LLMCostCapExceeded`) stops that
   role's chain immediately, including the free Ollama alternative. It does not
   cancel other MoE role calls already in flight. A route that reaches Ollama does
   no separate local spend check or cost-ledger write.
5. Generic bad requests, unrecognized 404s, invalid/schema output and content
   refusals stop substitution. Ollama malformed envelopes and non-object answers
   raise `AdvisorError`, so the surrounding engine handles them normally. Missing
   pricing, storage/programming errors and cancellation are not failover signals.
6. MoE expert chains run concurrently. An expert whose chain raises `AdvisorError`
   is omitted; the arbitrator receives the successful opinions after the experts
   finish, then runs its own chain. A failed arbitrator or no surviving experts
   fails the MoE call. There is no automatic switch to voting. `cascade` catches
   `AdvisorError`/spend-cap denial and returns its heuristic result (HOLD on the
   tested non-guard summaries). `engine: llm` has no final heuristic; a failed call
   yields no suggestion. Both repository settings files now select `cascade` at
   the base, inherited by every supplied profile; `cpu-only` already selected it.
   Exhaustion ends that evaluation; the next scheduled non-guard evaluation retries
   the primary. There is no busy-wait or extra health-polling loop.
   News attribution and auto-apply exclusion still apply to
   recommendations produced by a substituted model.

The limit is **three logical candidates per role**, not three HTTP requests per
cycle. With three cloud candidates, the default retry count permits up to twelve
HTTP requests per role for retryable outages. Per-model HTTP timeouts and retry counts
are bounded, but there is no total cycle deadline. The HTTP timeout is not a
guaranteed wall-clock ceiling. Parallel experts and a subsequent arbitrator can
make an outage cycle much slower than a normal one.

## Offline workflow verification

`tests/cli/test_advisor_cloud_equivalents.py` covers the current cloud-only selection:
native success, equivalent Atlas success before the previous backup, terminal cloud
success, preserved prompts/arbitrator context and role, exact catalog billing,
shared cap denial, fully exhausted clouds returning the heuristic, and the next
evaluation recovering on the primary alone. All HTTP is synthetic; the local
Ollama constructor is forbidden in these tests. Catalog listing and these protocol
tests do not prove live inference availability or unchanged model judgment.

The earlier `tests/cli/test_advisor_ollama_fallbacks.py` retains generic local-adapter
regressions; it no longer describes the recommended presets. It uses CLI builders, Anthropic,
OpenAI/Atlas and Ollama adapters, and SQLite with `httpx.MockTransport`:

| Path | Check |
|---|---|
| Primary / cloud backup / Ollama success, all four MoE roles | Stop at first success; actual route persisted; next evaluation retries primary |
| Backup cloud outage | Retry count exhausted before one Ollama attempt; one ledger row per logical cloud call |
| Arbitrator context and local protocol | Same summary/opinions and role prompt; `/api/generate`, exact tag, schema and per-model parameters |
| Local news and arbitration | Configured roles override claimed roles; forged routing metadata ignored; news-driven auto-apply blocked |
| Bot cap before primary or cloud backup | No request to subsequent cloud or Ollama candidates |
| Ollama timeout, 503, missing-model 404, malformed envelope/answer/schema | Clean `AdvisorError`; final heuristic only with `cascade` |
| Failed expert / failed arbitrator / all experts failed | Remaining opinions used, heuristic used, or arbitrator skipped, respectively |

The malformed-response cases initially exposed raw JSON/type errors bypassing the
cascade. The Ollama adapter now validates both response envelope and answer shape
and wraps invalid envelope JSON at the port boundary. This preserves the existing
no-substitution policy for invalid output while restoring clean engine degradation.
The existing fallback/retry/config suites also cover eligibility, cancellation,
duplicate/oversized chains, missing cloud credentials and default-off behavior.
These are offline protocol and workflow checks; no live model answers or NAS timing
were measured. The verification receipt is in the [roadmap](../planning/roadmap.md).

## Verify activation

Keep the current seat register and deployment receipt when selecting alternatives.
Validate configuration and offline role fixtures first, then follow the normal
deployment procedure. Confirm the exact tags/digests are installed at the endpoint
the advisor container resolves as `OLLAMA_BASE_URL`; startup constructs clients but
does not check or pull local models. All local roles share that endpoint. Validate
complete outputs and loaded/queued latency against the current prompts before
enabling a list. Inspect successful cloud news/arbitrator calls in `llm_calls`,
the suggestion's **LLM routing** section, and the substitution logs. Same-provider
backups may share an account outage; a local fallback success is recorded on the
suggestion but does not turn failed cloud requests into successes on the health page.
Retries are collapsed into one cloud ledger row. Successful suggestions retain the
winning role's candidate attempts; fully failed role chains have no successful
opinion to attach, and heuristic fallbacks currently have no complete failed-chain
routing trail. Use substitution/failure logs and the cloud ledger for those cases.

The new `advisor_suggestions.llm_attempts` column is additive, with an empty-array
default. Old rows and old writers have no attempt history. Old readers ignore the
extra column. Normal database backup and deployment verification still apply.
The full policy and implementation boundary are in
[ADR-043](../architecture/adr-043-llm-fallbacks.md).
