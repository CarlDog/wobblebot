# Production advisor forensics — 2026-08-11

First direct read of the **production** advise corpus (the NAS Docker
deployment), obtained via the UNC share
`\\carldog-nas\docker\wobblebot\data`. Every prior analysis of advisor
behaviour used the *local* corpus — 109 suggestions from May, all
`phi4:14b-q8_0` — and was therefore inference, not measurement.

Method: the live DB files were copied (`.db` + `-wal` + `-shm`, so the
WAL replays) to a scratch directory and read there. Nothing was written
to production; nothing was deployed or changed.

**Corpus:** 2628 rows in `advisor_suggestions`, 2026-05-27 → 2026-08-12.
The LLM cost ledger is **not** in the advise DB — `llm_calls` lives in
`wobblebot-operator.db` (1598 calls, $0.7981 lifetime).

The investigation was commissioned to answer a narrow question — *what
does reducing the advisor sweep cadence cost in signal?* — but three
findings outrank the answer.

---

## Finding 1 — the advisor was dead for 3.5 days and nothing said so

**387 consecutive OpenAI failures** (386 `LLMRetryExhausted`, 1
`ReadError`), 2026-08-05 → 2026-08-08 19:12, zero successes. The
underlying error, from `data/logs/advise.log.2026-08-08`:

```
cascade: LLM escalation failed; using heuristic fallback
(error: OpenAI retries exhausted: LLM call failed after 4 attempts;
 last error: Client error '429 Too Many Requests' ...)
```

| day | calls | ok | failed |
|---|---|---|---|
| 2026-08-05 | 279 | 0 | 279 |
| 2026-08-06 | 36 | 0 | 36 |
| 2026-08-07 | 36 | 0 | 36 |
| 2026-08-08 | 42 | 6 | 36 |
| 2026-08-09 onward | — | 100% | 0 |

`CascadingAdvisorAdapter` caught each failure and fell back to the
heuristic verdict — which, with no guard firing, is **HOLD**. From
every outward surface the daemon looked healthy:

- row rate unchanged at exactly 36/day, the same as the preceding 73 days
- rows written with plausible rationale text
- `role` recorded as `heuristic`, which is a legitimate value
- the fallback verdict (HOLD) is indistinguishable from a quiet advisor

**The health surface would not have caught it either, and still won't.**
`services/llm_health.py` probes each provider's *models-list* endpoint
(`GET /v1/models`) — deliberately free and non-billable. A key that is
quota-exhausted still returns 200 there. The `/health` page would have
read **OpenAI: OK** for the entire outage.

This gap is **open**. The two candidate fixes, neither yet built:

1. Surface the `llm_calls` failure streak — the data is already
   recorded, with `error_kind`, and a "N consecutive failures for role
   R" query is cheap. This is the honest signal: it observes the path
   that actually matters instead of a proxy for it.
2. Have the cascade emit a `notifications` row when the fallback fires
   more than K times consecutively.

(1) is preferred: it needs no new state, and it measures the real
request path rather than a reachability proxy. This is the same shape
as the standing CI rule — *trust the run, not a local proxy for it*.

---

## Finding 2 — escalation went 0% → 100%, and that is the config, not a bug

Daily escalation rate (fraction of ticks handed to the LLM):

| window | escalation |
|---|---|
| 2026-05-30 → 2026-08-04 (67 days) | **0.0%** on all but 3 days |
| 2026-08-09 → present | **100.0%**, every day |

The pre-08-05 container was running the **retired** vol→spacing
heuristic — its rationales read `"Volatility 0.04%/tick wants ~0.65%
spacing"`, the first-order logic ADR-022 removed. It resolved every
tick locally: 2263 heuristic rows, ~0 LLM calls, $0.

The ADR-022 image replaced it with the four guards. At the deployed
3.0% spacing in a flat market, **all four are structurally
unreachable**:

| guard | condition | production reality |
|---|---|---|
| `directional_runaway` | `cycles == 0` **and** `dd ≤ -0.05` | `cycles == 0` ✓ but `dd` ranges only -0.0024 … -0.0154 |
| `defensive_drawdown` | `dd ≤ -0.05` | same — never reached |
| `dont_fix_working` | `cycles ≥ 8` | `quant.yml` already documents this as *"DORMANT AT WIDE (~3%) SPACING"* |
| `fee_floor_calm` | `spacing ≤ 0.68%` | deployed spacing is 3.0% |

So 100% escalation is not a regression to hunt — it is the guaranteed
consequence of running the guard set at 3.0% spacing. Going from $0/mo
to ~$3.48/mo was a **deploy**, not a cadence problem. Reducing cadence
treats the symptom; the lever is the guard set (or the spacing).

---

## Finding 3 — the incumbent is a constant, and the engine agrees with it

> **Scope note.** "Constant" is a property of **gpt-5-mini on this input
> class**, not of the input class itself. The challenger replay below
> shows grok-4.5 holding on 17% of the same cases — so the uniformity
> here is the model's, not the market's alone.


Across all 216 successful quant calls (2026-08-09 → 08-12, 6 symbols):

- **216 of 216 recommend TIGHTEN.** Zero HOLDs.
- **Zero calls recommended anything at or above the deployed 3.0% grid.**
- Full value set: `0.66, 0.75, 0.80, 0.90, 1.00, 1.10, 1.20, 1.40, 1.50`, median **1.00**
- Confidence: 189 `high`, 25 `medium`, 2 `low`

The market inputs are uniform: flatness 0.97–0.998 on every symbol,
every sweep; ATR₁₄ ≈ 0.7% of price against a 3.0% grid.

The engine's own ledger says the same thing. Trades by month:

| month | trades |
|---|---|
| 2026-05 | 9 |
| 2026-06 | 44 |
| 2026-07 | 16 |
| **2026-08** | **1** (a single $5 ETH buy on 08-01) |

August orders: **640 canceled, 0 closed, 9 open** — the grid laying out
and re-laying out without ever getting filled. Lifetime the book is 52
buys to 18 sells, which is the inventory-accumulation risk showing up
in the ledger rather than in a design doc.

For scale: `quant.yml` already records that *"a 3% grid completes only
~0.2–0.4 round-trips/day (measured on 2013–2025 BTC)"*. Over 11 days ×
6 symbols that predicts on the order of 13–26 round-trips. Observed: one
buy, no round-trips. August is running well below even the wide-grid
baseline the config was designed around — so this is a flat *regime* on
top of a deliberately slow grid, not the grid alone.

So: 216 consecutive high-confidence "tighten to ~1%" calls, against a
grid that has printed one fill in eleven days. The advisor is not
malfunctioning; it is reporting a real and persistent condition.

**This is exactly the failure ADR-022 retired the vol→spacing curve
for** — *"its ceiling sat below the deployed grid, so it mechanically
recommended TIGHTEN on ~every non-guard tick and drowned out the LLM's
trackable signal."* The LLM free judge has reproduced that shape from
the other direction: not because a curve pinned it, but because the
6h window genuinely contains nothing else.

### The counterweight — do not read this as "apply it"

The advisor sees `metrics_lookback_hours: 6`. The reason 3.0% is
deployed is a **full-cycle** argument: a 1% grid works in chop and
bleeds when a trend arrives, and six hours of lookback structurally
cannot see that. `quant.yml` explicitly rejects widening the window
(a 24h lookback dips ≥5% about 13% of the time vs ~2% at 6h, which
would misfire the drawdown guards).

"The advisor is right about the last six hours" is not "the advisor is
right." Per the standing no-false-absolutes rule: 3% is the least-bad
*static* default, not the answer, and a tight grid chosen in chop and
**pulled before the trend** is a different strategy from a tight grid
left running.

---

## The cadence question (what was actually asked)

Churn between consecutive 4h sweeps, measured on the 216 calls:

| symbol | sweeps | value changed | range |
|---|---|---|---|
| BTC | 36 | 57% | 0.66–1.20 |
| ETH | 36 | 66% | 0.80–1.20 |
| SOL | 36 | 54% | 0.80–1.20 |
| XRP | 36 | 57% | 0.75–1.20 |
| DOGE | 36 | 63% | 0.66–1.20 |
| ADA | 36 | 69% | 0.90–1.50 |

~61% churn looks decisive until you ask what changes. The direction
never does. Subsampling the real record:

| cadence | calls | distinct values kept | median | direction changes |
|---|---|---|---|---|
| 4h (current) | 216 | 9/9 | 1.00 | 0 |
| 8h | 108 | 8/9 | 1.00 | 0 |
| 12h | 72 | 7/9 | 1.00 | 0 |
| 24h | 36 | 5/9 | 1.00 | 0 |

Dropping to 12h loses two values off the tail of a jitter band and no
decision at all. **Cadence reduction is nearly free — because the
signal is a constant.** Which is also the reason not to do it: at
$3.48/mo the saving is $1–2/mo, and sampling a degenerate signal less
often makes it *harder* to notice when it stops being degenerate.

### Cost, on measured production tokens

Successful gpt-5-mini calls average **in 3120 / out 153 /
reasoning 1045** tokens (`cache_read` = 0 — no prompt-cache hits).
Reasoning bills as output, so billed output is ~1198 tok. Measured
unit cost: **$0.00318/call** ($0.7053 over 222 calls).

| | 4h (36/day) | 6h (24/day) | 12h (12/day) |
|---|---|---|---|
| gpt-5-mini | $3.48/mo | $2.32/mo | $1.16/mo |
| grok-4.5 | $14.70/mo | $9.80/mo | $4.90/mo |

---

## Challenger replay — grok-4.5 on the same 24 production inputs

Rather than infer whether a better model would answer production
differently, the exact `input_summary` blobs gpt-5-mini answered were
replayed through `xai/grok-4.5` at the deployed escalation params
(temperature 1.0, `max_tokens` 4000). 4 sweeps × 6 symbols = 24 cases,
24/24 no errors, $0.35 into the isolated probe ledger.

**This refuted the prediction.** The expectation written before the run
was that production serves one repeated easy read, so both models would
answer identically and grok's fixture edge would buy nothing. Wrong:

| | gpt-5-mini (recorded) | grok-4.5 (replay) |
|---|---|---|
| **HOLD** | **0** | **4 / 24 (17%)** |
| ≥ deployed 3.0% | 0 | 0 |
| modal value | 1.00 | **1.20** |
| lowest value | **0.66** | 0.90 |
| value spread | 0.66–1.20 | 0.90–1.50 |

Mean absolute difference 0.231pp. On the 20 non-HOLD cases both models
agree on direction (TIGHTEN), every time.

**The difference is the willingness to say "no change."** gpt-5-mini has
not emitted a single HOLD in **216** production calls; grok emitted 4 in
24 on the same class of input. Fisher exact: **p = 7.9e-05** against
the full production record, **p = 0.055** on the strictly matched 24
(suggestive, just short of 0.05 — a 24-case sample is thin).

Two secondary observations, both consistent with the gen3 calibration
result (tau +0.35 vs +0.26):

- **all four grok HOLDs carry `medium` confidence**, not `high` — it
  flags its own uncertainty rather than asserting. Production
  gpt-5-mini is 189/216 `high`.
- **grok is systematically less aggressive**: modal 1.20 vs 1.00, and
  it never went below 0.90 where gpt-5-mini twice recommended 0.66.
  Given the standing concern that a tight grid bleeds when a trend
  arrives, that is directionally the more conservative advisor — though
  "less aggressive" is a judgment about which is *better*, not a
  measurement.

So the seat question **is** decidable on production data, and the answer
moved: the incumbent is a pure constant on this input class and the
challenger is not.

---

## Finding 4 — zero prompt-cache hits in 222 production calls

`tokens_cache_read` is **0 on every one of the 222 successful
gpt-5-mini calls** (min 0, max 0, count-with-hit 0). This is not a
recording gap: `adapters/openai.py` parses
`usage.prompt_tokens_details.cached_tokens` correctly, the column is
persisted, and the *challenger* replay through Atlas/grok returns
non-zero cache reads (~384 tokens/call) through the same code path.

The conditions for OpenAI's automatic caching look satisfied:

- the system prompt is `config/prompts/quant.md`, **~1270 tokens**, sent
  first and byte-identical on every call (above the 1024-token minimum)
- within one sweep the six symbol calls fire **9–14 seconds apart**, far
  inside any plausible cache TTL — so calls 2–6 should hit a prefix
  warmed by call 1

Observed: 0/222. **Mechanism undiagnosed** — do not assume a cause. The
candidates worth testing are whether the `gpt-5` reasoning family caches
on `chat/completions` at all, and whether a `prompt_cache_key` is
required.

**Size the prize before spending time on it.** gpt-5-mini's cached rate
is $0.025/M vs $0.25/M input:

| scenario | saving |
|---|---|
| gpt-5-mini, 1270-tok prefix cached | $0.31/mo (off $3.48) |
| gpt-5-mini, 2500-tok prefix cached | $0.62/mo |
| grok-4.5 at Atlas's $0.50/M cached, 1270 tok | $2.08/mo (off $14.70) |
| grok-4.5, 2500 tok | $4.10/mo |

So it is worth ~9% on the incumbent and ~14–28% on grok. Real, modest,
and **not** on its own a reason to change models. The larger prefix
figure assumes moving the shared news block (identical across all six
symbols — `news_match_coin: false`) ahead of the per-symbol metrics,
which is a prompt restructure, not a config flip.

### Direct-from-xAI vs the Atlas aggregator

Checked at the operator's request against xAI's own docs
(<https://docs.x.ai/developers/models/grok-4.5>), not from recall:

| | input /M | output /M | cached input /M |
|---|---|---|---|
| xAI direct | $2.00 | $6.00 | **$0.30** |
| Atlas (recorded, verified 2026-08-10) | $2.00 | $6.00 | **$0.50** |

**Headline rates are identical.** The only difference is cached input,
and at measured cache volumes that is worth **$0.08/month** — $0.28/mo
even if the whole prefix cached. Going direct is not a cost play. It
would have to be justified on other grounds (removing a hop from the
failure path, xAI's published 150 rps / 50M tpm limits), and at this
call volume none of those bind either.

---

## Open items this produced

1. **Silent-outage detection** (Finding 1) — still open. Prefer the
   `llm_calls` failure-streak surface over a reachability probe.
2. **The escalation lever is the guard set, not the cadence**
   (Finding 2). At 3.0% spacing the guards cannot fire; that is a
   design question, not a tuning one.
3. **The advisor seat decision IS decidable on production data** — see
   the challenger replay above. The pre-run expectation (that both
   models would answer this input class identically) was measured and
   refuted: gpt-5-mini never holds, grok holds 17% of the time and
   flags those calls `medium`. Cost remains the counterweight
   ($3.48 vs $14.70/mo at 4h). Operator's call; see `advisor-seats.md`.
4. **Zero prompt-cache hits** (Finding 4) — mechanism undiagnosed,
   prize sized at ~9% of incumbent spend. Investigate only if the seat
   moves to a model where caching is worth more.
5. **Stale comment**, `tools/probe_advisor.py` `_build_cloud_advisor`:
   it claims "llm_pricing has no entries for Atlas-hosted models, so
   the cost gate falls back to its heuristic." Both halves are now
   wrong — Atlas models *are* priced (keyed under provider `openai`,
   e.g. `xai/grok-4.5`), and the cost gate **raises** rather than
   falling back. Fix in its own commit.

## Related

- `advisor-seats.md` — the seat register this feeds
- `docs/architecture/decisions.md` — ADR-014 (cost gate), ADR-022
  (guards + LLM free judge), ADR-035 (counterfactual scoring)
- `~/.claude/rules/ci-verification.md` — "a CI-wait monitor must emit
  on failure too" is the same lesson as Finding 1, in a different system
