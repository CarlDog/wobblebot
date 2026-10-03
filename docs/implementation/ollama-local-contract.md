# Local Ollama boundary and measurement

`provider: ollama` means local execution under ADR-014. It cannot inherit the
separate, costed `ollama_cloud` provider's remote authority. Adapters reject
explicit cloud tags and Ollama cloud hosts before requests, inspect `/api/show`
before each prompt for local provenance and completion capability, disable
redirects, and reject remote provenance in completion responses. Missing metadata
is an error; older servers without completion capability metadata need review.
No failed preflight sends operator/financial context and no provider is silently
substituted by this boundary. Existing explicitly configured fallback policy remains.

On the **Ollama server**, set `OLLAMA_NO_CLOUD=1` or `disable_ollama_cloud: true`
and restart under the normal operator deployment procedure. Verify its disabled
cloud log and host network boundary. A remote-configured server can lie or change
between metadata and inference; client response checks are defense in depth, not
host attestation. Setting the variable only on WobbleBot cannot configure another
machine's Ollama. No server was changed by this implementation.

Advisor instructions use generate's `system` field; engine/news data uses `prompt`.
Assistant chat reads `message.thinking`, with a legacy top-level fallback, and
prefers final content over drafts. Malformed/error/remote/incomplete envelopes and
summary timeouts produce the documented port exception. An explicit length stop
is never admitted as a valid answer. The existing one-retry parse policy remains;
summary does not leak that internal retry marker. Error bodies, raw intent payloads
and reasoning are not added to diagnostics.

```bash
python tools/inspect_ollama.py --base-url http://localhost:11434 --model MODEL
python tools/probe_advisor.py --model MODEL --json
```

The metadata-only tool reads show/version/tags, returns installed digest/version,
and performs no inference, pull or database write. Exit 1 means missing identity;
2 means failed/refused inspection. Keep its JSON with assistant probe receipts.
Advisor JSON receipts additionally contain identity and per-scenario allowlisted
native duration/token metrics; absent metrics remain null, not zero. These are
server observations, not assertions that a mutable model tag cannot change during
a battery. Re-measure the digest after a battery before promotion. No local calls
were added to `llm_calls`; expanding persistence/cost semantics still needs ADR-014
review and measured NAS evidence. Synthetic metadata is not model qualification.

Official contracts reviewed 2026-10-03:
[chat](https://docs.ollama.com/api/chat),
[generate](https://docs.ollama.com/api/generate),
[model list](https://docs.ollama.com/api/tags),
[local-only setup](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).
The older repository assessment remains historical evidence; native-think/schema,
context sizing and keepalive tuning pilots are not silently adopted here.
