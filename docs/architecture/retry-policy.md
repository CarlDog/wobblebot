# Connectivity Retry Policy — Audit & Reference

*An honest sweep of how every external call site handles **timeouts, retries, and
backoff** today, plus the gaps worth closing in v1.1. This is a documentation/consistency
audit (v1.1 P0.5) — it describes the as-is posture and **tickets** the gaps; it does not
change behavior.*

The soak has surfaced no retry-related defect (the one 2026-05-19 connectivity bug was a
missing `try/except` in the shutdown `finally`, not a missing retry). So the items below
are **consistency + robustness gaps, not active bugs.**

## Summary

| Integration | Per-request timeout | Application-level retry | Rate limiting | How a failure is contained today |
|---|---|---|---|---|
| **Cloud LLM** (Anthropic / OpenAI / Google, advisor + assistant) | yes (60s default, per-adapter) | ✅ **Yes — ADR-015** (`retry_with_backoff`) | n/a | retry 1+3 attempts → `LLMRetryExhausted` → caller degrades (heuristic fallback / skip tick) |
| **Kraken REST** | yes (10s, `KrakenConfig` default) | ❌ **No — single attempt** | ❌ none enforced | per-symbol error swallowed at the CLI (Stage 2.4); retried next tick. One-shot CLIs surface the error to the operator |
| **Ollama** (advisor + operator assistant) | yes (60s default; up to 180s for slow CPU models via config) | ❌ No — single attempt | n/a | MoE is fail-open (one expert timeout → proceed); `cli/advise`/`cli/operator` isolate per-cycle failures |
| **RSS feeds** | yes (30s) + `follow_redirects` | ❌ No — single attempt | n/a | per-source fault isolation in `cli/news` (one feed fails → log + continue) |
| **CryptoCompare** | yes (30s) | ❌ No — single attempt | n/a | same per-source isolation in `cli/news` |

**One-line takeaway:** only the **cloud LLM** path retries. Everything else is
single-attempt-with-timeout, contained by *fault isolation + next-cycle re-poll* on the
daemon paths — which is acceptable by design for polled work, but leaves the **one-shot**
Kraken paths (preflight / status / first-real-trade / harvest `--execute`) with no
cushion against a transient blip, and leaves the Kraken **config knobs dead**.

---

## Per-integration detail

### Cloud LLM — full retry (the reference implementation)

`services/llm_cloud_call.py` wraps every cloud HTTP call in
`services/llm_retry.retry_with_backoff` (ADR-015). Policy:

- **Attempts:** `1 + max_retries` (default `max_retries=3` → 4 attempts).
- **Backoff:** exponential `initial * multiplier ** i` → with defaults **1s, 2s, 4s**.
- **Transient (retry):** httpx `ConnectError`/`ConnectTimeout`/`ReadTimeout`/`WriteTimeout`/
  `PoolTimeout`/`RemoteProtocolError`, plus HTTP **429** and **5xx**.
- **Permanent (re-raise immediately):** HTTP 4xx (non-429) and every non-httpx exception
  (don't retry bugs).
- **No failover, fail-loud:** exhaustion raises `LLMRetryExhausted`; the caller decides
  (the cascade advisor falls back to the heuristic; `cli/advise` skips the tick).
- **Configurable:** `LLMRetryConfig` (`config/llm.py` → `llm.retry`), per the schema —
  operator-tunable.

This is the gold standard the other integrations are measured against.

### Kraken REST — single attempt, dead config knobs

- The adapter builds `httpx.AsyncClient(timeout=config.request_timeout_seconds)` and
  `_public_get` / `_private_post` make **one** request, mapping `httpx.HTTPError` →
  `ExchangeError`. **No retry loop, no backoff.**
- `KrakenConfig` (`config/kraken.py`) is built via `from_env(...)` — it carries only
  credentials, `base_url`, and a **default 10s** `request_timeout_seconds`. It is **not**
  populated from the YAML.
- **Dead config (now removed — see G1):** `settings.example.yml`'s `exchange:` block used
  to document `rate_limit_rps`, `max_retries`, `retry_delay_seconds`, and
  `request_timeout_seconds` — **none were referenced by any code** (`WobbleBotConfig` has
  no `exchange` field). The adapter neither retries nor rate-limits, and used the env-built
  `KrakenConfig` 10s timeout default. The dead section was removed 2026-06-02.
- **Why it's been fine:** on the engine path, a per-symbol Kraken error is swallowed at
  the CLI layer (Stage 2.4 design) and the symbol is retried on the **next 5s tick** —
  an implicit retry with a generous interval. The one-shot CLIs (`preflight`, `status`,
  `first_real_trade`, `harvest --execute`) surface the failure to the operator, who
  re-runs. So a transient blip is recoverable, just not automatically on the one-shots.

### Ollama — single attempt, fail-open above it

- `httpx.AsyncClient(timeout=timeout_seconds)` (default **60s**; the cpu-only advisor
  profile raises it toward 180s for slow local models). Single attempt; `httpx.HTTPError`
  → wrapped error.
- Containment is at a higher layer: the **MoE advisor is fail-open** (one expert
  timing out → WARNING + proceed; only all-fail raises), and `cli/advise` / `cli/operator`
  isolate a failed cycle (log + continue to the next scheduled run). A single Ollama
  hiccup costs at most one advisor/operator cycle, retried on cadence.
- *(Companion: the parked "Ollama hang detection audit" verifies the timeout actually
  cancels a hung request — orthogonal to retry.)*

### RSS feeds — single attempt, per-source isolation

- `httpx.AsyncClient(timeout=30.0, follow_redirects=True)`, one instance per feed. Single
  attempt; failure wrapped.
- `cli/news` polls each source independently with per-source fault isolation — one feed
  erroring logs and continues; the feed is retried on the next news-poll cadence.

### CryptoCompare — single attempt, per-source isolation

- `httpx.AsyncClient(timeout=30.0)`. Single attempt. Same per-source fault isolation in
  `cli/news`. CryptoCompare is redundant with RSS by design (ADR-010), so a transient
  failure is doubly cushioned.

---

## Findings / gaps (ticketed for v1.1 — not fixed here)

- **G1 — Kraken connection knobs were dead config. ✅ RESOLVED 2026-06-02 (removed).**
  The whole `exchange:` block (`name`, `rate_limit_rps`, `request_timeout_seconds`,
  `max_retries`, `retry_delay_seconds`) was consumed by **no** code — `WobbleBotConfig`
  has no `exchange` field at all, so Pydantic silently dropped it on load; the adapter is
  built from env-sourced `KrakenConfig`. Took the honest first step (option b): **removed
  the entire dead `exchange:` section** from `settings.example.yml` + the deploy `settings.yml`
  (replaced with a comment explaining Kraken is env-configured). The real robustness
  improvement (option a — wire an actual transient-retry wrapper + a client-side rate
  limiter) stays as future work; it would reintroduce these knobs *with code behind them*.

- **G2 — retry asymmetry on one-shot Kraken paths.** The daemon paths get an implicit
  next-cycle retry; the **one-shot** Kraken calls (`preflight`, `status`,
  `first_real_trade`, `harvest --execute`) get none — a transient timeout is a hard
  failure the operator must notice and re-run. A *small* bounded transient retry
  (timeouts / 5xx / connection only; never on a business rejection or a `validate` result)
  on these paths would smooth real-world flakiness. **Money-path caution:** any retry on
  `AddOrder`/`Withdraw` must guard against double-submission (Kraken's nonce + idempotency
  semantics) — this is exactly why it's *not* done blindly today.

- **G3 — Kraken timeout isn't operator-tunable.** The 10s timeout is the `KrakenConfig`
  default; an operator on a slow link can't raise it without editing env/code. Low
  priority; folds into G1 if the YAML knobs get wired.

- **G4 — no client-side Kraken rate limiting.** Relies on Kraken's generous hobby-tier
  limits + the 5s tick spacing. Fine at single-symbol; revisit at multi-symbol scale
  (already tracked as a parked perf item — "WebSocket / async parallelism" group). The
  `rate_limit_rps` knob from G1 would be its home if implemented.

**Posture going forward:** the cloud-LLM `retry_with_backoff` is the template. When a
Kraken/Ollama/news retry is actually warranted (G1/G2), reuse the same
classifier-driven, transient-only, exponential-backoff shape rather than inventing a
second retry idiom — with the money-path double-submit guard called out in G2.

---

*Companion: [`decisions.md`](decisions.md) ADR-015 (cloud LLM failover/retry policy);
the v1.1 plan's parked "performance" cluster (caching / async / WebSocket) for the
rate-limit + latency side.*
