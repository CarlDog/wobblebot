# Ollama Cloud advisor setup

Use `provider: ollama_cloud` for authenticated direct Cloud access. Local
`provider: ollama` continues to use `OLLAMA_BASE_URL`. The new provider works for
single-advisor, MoE expert/arbitrator and either fallback slot. It does not enable
the Discord operator assistant or gremlin.

Create an [Ollama API key](https://ollama.com/settings/keys) and set
`OLLAMA_API_KEY` in the deployment environment. Compose forwards it only to
`advise` and `tools`. A configured Cloud target needs the existing `llm:` cost/retry
section and operator ledger, even if it is only a backup. Missing credentials or
ledger configuration fail setup with a named error. Restart the advisor after
changing settings or environment; this is static configuration, not a database
setting or live UI control.

For an **evaluated** target, set its provider/model and per-model inference values:

```yaml
provider: ollama_cloud
model: gpt-oss:120b
inference_params:
  temperature: 0.5
  max_tokens: 4000
  timeout_seconds: 300
```

Keep the role's existing `prompt_file`. The same target shape fits a `fallbacks`
list item. Model IDs come from the direct API catalog; do not append `-cloud`,
which is used by the local Ollama proxy. These example values are starting points
for evaluation, not a qualified seat assignment.

## Pricing and limits

Initial supported prices, USD per million tokens, verified 2026-09-08 against
[Ollama's pricing page](https://ollama.com/pricing) and
[direct API model IDs](https://ollama.com/api/tags):

| Model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| `gpt-oss:120b` | 0.15 | 0.014 | 0.60 |
| `gpt-oss:20b` | 0.07 | 0.035 | 0.30 |
| `qwen3.5:397b` | 0.60 | Full input rate if reported | 3.60 |
| `gemma4:31b` | 0.14 | 0.05 | 0.40 |

Other models fail closed before a request until their prices are registered.
DeepSeek's peak/off-peak prices need time-aware support; its existing Atlas price
must not be reused for Ollama Cloud. Pricing is verified code under ADR-014.

Calls consume the existing shared daily/session and role caps, including tokens
covered by plan credits. The ledger records model-rate usage, not monthly fees or
credit purchases. Provider quota/credit exhaustion follows the existing failure
chain; no purchase or account change is automatic. Unknown 429 errors use bounded
transient retries; recognized quota/billing codes do not. HTTP 402 fails over
without retries. Inspect the account UI for remaining provider credits.

The native API reports total prompt, cached prompt and generated tokens. Cached
tokens are subtracted from total input; generated tokens already include thinking.
Missing cache counts use full input cost. Missing/invalid usage is a clean failure,
not a successful free call. The shared ledger cannot reconstruct missing usage or
charges from timed-out requests; their failed rows carry zero recorded cost.
Budget estimates do not guarantee exact invoice ceilings.

## Response and fallback behavior

[Ollama Cloud lacks structured-output enforcement](https://docs.ollama.com/capabilities/structured-outputs).
The adapter sends the existing JSON instructions without `format`, reads assistant
content separately from thinking, and validates it through the existing schema.
`max_tokens` maps to `options.num_predict`; allow room for thinking and a complete
answer. Invalid/truncated answers retain confirmed token charges and raise a clean
advisor error. They go to the heuristic under `engine: cascade` and do not trigger
another model substitution. The call-outcome ledger measures provider-call success,
so a billed 200 response may be marked successful even if its answer is rejected.

Availability errors use the configured primary → equivalent cloud route when
available → previous cloud backup sequence. Exhaustion uses the heuristic with
`engine: cascade`; the next scheduled non-guard evaluation starts at primary.
`engine: llm` has no heuristic. See [fallback workflow](llm-fallbacks.md).

No current primary has an exact Ollama Cloud equivalent in the checked catalog.
Generic example lists remain disabled. The NAS's enabled fallback routes are
recorded in the [seat register](../reference/advisor-seats.md#fallback-candidates);
none uses Ollama Cloud. The 2026-09-08 GPT-OSS 120B quant evaluation initially
encountered `authentication_error` despite verified Portainer-to-advisor key wiring.
Replacing the key and recreating the advisor resolved authentication; all nine
subsequent provider calls succeeded, with usage recorded under `ollama_cloud`.
Role scoring was 6 OK / 2 UNSAFE / 1 SUBOPTIMAL, so the candidate was not enabled.
See the seat register for fixture-specific findings and the qualification limit.
Catalog access alone does not validate credentials: `/api/tags` is public
([official Cloud API examples](https://docs.ollama.com/cloud)).

## Verification and upgrade

Offline tests use `httpx.MockTransport` and disposable SQLite databases. To perform
an operator-authorized, **billable** connectivity check later:

```powershell
python tools/run_cloud_check.py --provider ollama_cloud --role quant --model gpt-oss:20b --max-tokens 4000
```

This is a connectivity check, not a role battery. The tool's `--dry-run` flag still
sends a paid request and disables cap enforcement; omit it. No such call is part
of this implementation's local verification.

On writable database open, the ledger provider CHECK is widened transactionally.
Rows, column order, indexes and triggers survive, and repeat/concurrent opens are
tested. Read-only opens do not migrate. Back up the database before deployment.
Old writers can still insert their providers, but older application readers reject
`ollama_cloud` rows. After Cloud activation, rollback requires a compatible reader
or a reviewed recovery plan preserving those new rows; do not delete history or
restore an older backup over subsequent activity.

Design: [ADR-045](../architecture/adr-045-ollama-cloud-provider.md).
