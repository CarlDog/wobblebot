# Four-homes audit — hardcoded mutable facts (v1.1 P0.1)

**What:** a systematic sweep of `src/wobblebot` for facts hardcoded in *code* that are
actually *mutable* (drift on a cadence we don't fully control), classifying each by the
**four-homes test** to decide its right home. The audit gates every later storage-tier
decision (SQLCipher, DB-driving model lists) — see the [v1.1 plan](README.md) P0.

**When:** 2026-06-01 (v1.1 branch). **Method:** a 5-lens discovery sweep (vendor-values,
lists/maps, URLs/endpoints, thresholds/limits, model-patterns) → four-homes classification →
an adversarial safety+completeness review. 64 raw candidates → ~30 distinct facts.

**Headline verdict (review-confirmed):** the classification holds and is **safe**. **Zero
safety-critical facts are moved out of code** — `_PRICING` (LLM token prices → ADR-014 cost
gate) and the Kraken maker/taker fees (→ loss caps + the grid spacing/fee-floor validator)
correctly stay code-resident; the PR + CI + 180-day-freshness-test path is the control. The
genuine move candidates are model-ecosystem drift surfaces (compat lists + name-pattern
classifiers) — **config candidates, queued** as their own slices (each needs a loader +
schema + fail-soft + tests; none were trivial).

## The four-homes test

1. **code constant** — part of our design/logic, OR we *want* the change gated by PR + CI + review.
2. **config file** — operator-tunable, restart to apply.
3. **DB table** — needs runtime mutation (no restart) and/or history/audit of *when* the value was X.
4. **live-fetch + cache** — the vendor exposes it via API; solves *staleness* (a DB table does not).

**Two rules.** (a) *DB is not the default* — for vendor facts the real problem is staleness →
live-fetch+cache solves it (we already do this for Kraken pair metadata via `/AssetPairs`); DB
only buys runtime-mutability + audit. (b) *Safety carve-out* — facts feeding a safety gate stay
**code-resident on purpose**; the reviewed-code path is the control.

---

## Punch list (the actionable outcome)

### ✅ Keep code-resident — SAFETY-CRITICAL (the carve-out held)
| Fact | Location | Why code |
|---|---|---|
| `_PRICING` LLM token-price table (~25 entries) | `services/llm_pricing.py:83-266` | Feeds `estimate_cost_ceiling` → the ADR-014 budget gate; a wrong-low price slips a call past the cap. Module docstring already declares "code, not config"; the 180-day `test_llm_pricing_freshness` CI gate is the drift control. |
| Pricing freshness window (180d) + `verified_date` anchors | `services/llm_pricing.py:77,80` | Provenance + the policy that forces CI re-verification. Moving them out defeats the freshness gate. |
| Kraken maker/taker fees — **validator copy** | `config/grid.py:35-36` (`KRAKEN_MAKER/TAKER_FEE_RATE`) | Feed the minimum-profitable-spacing guard (reject spacing < 2× maker). A stale-low fee lets an unprofitable grid pass validation. |
| Kraken fees — shadow / mock / shadow-CLI defaults | `shadow_exchange.py:48-49`, `mock_exchange.py:43`, `config/cli.py:161-162` | Same vendor fact; synthetic-ledger fee model informs go-live. Keep per the carve-out. |

> **Negative finding (good):** live executed-trade accounting reads the **actual** fee from
> Kraken's receipt (`kraken_exchange.py:685`) — the hardcoded 0.40/0.26 only feed the
> validator/shadow/mock, never the money-path P&L.

### 🟡 Queue — move to **config** (the genuine candidates; each its own slice)
These are ecosystem-drift, **non-safety**, restart-to-apply, with **no vendor API to
live-fetch** (config, not DB, not live-fetch). All are **branch-safe** (no `main`/soak impact)
so they *could* be pulled during the soak.

| # | Fact | Location | Slice |
|---|---|---|---|
| **Q1** | `KNOWN_INCOMPATIBLE_FOR_ASSISTANT` + `KNOWN_DEGRADED_FOR_ASSISTANT` + the embedded recommended-replacement model list | `adapters/ollama_assistant.py:100,112,133-137` | **Model-compat externalization** — one config section, fail-soft loader (empty/malformed override must degrade, not crash), schema-drift test. The prime candidate: MEMORY records a 2026-05-25 sweep *reversing* two prior "broken" verdicts in an hour. |
| **Q2** | `_REASONING_MODEL_PREFIXES` (`("o1","o3")`) + `_THINKING_MODEL_PATTERNS` | `adapters/openai.py:77`, `adapters/ollama.py:57` | **Model-pattern externalization** — config the pattern sets with a safe default + validation (an empty override must not send `temperature` to every model). **Includes fixing the o4 drift gap (below).** |
| **Q3** | `_COIN_PATTERNS` news coin/ticker whitelist (MATIC→POL rebrand is stale in-list) | `adapters/rss_news.py:49` | **News-coin whitelist → config** — ideally *derived from / cross-checked against* the operator's traded symbols so the two lists can't drift apart. |

### ⚪ No action — already in the right home (config / live-fetch) or stable design constants
- **Already config (override path exists):** heuristic `fee_floor`/calm-guard defaults (`config/heuristic.py`), Ollama base URL (`OLLAMA_BASE_URL` env + `config/cli.py:471`), Kraken Pro account URL (`config/cli.py:621`), `AdvisorConfig.engine/type/aggregator` (model/provider names have no baked default — they live in `settings.yml`). *(These carry `verdict=already-right-home`; their in-code Pydantic defaults stay code.)*
- **Already live-fetch+cache (the gold-standard pattern):** Kraken pair metadata — `ordermin`/`costmin`/decimals and the legacy X/Z response codes (`XXBT`/`ZUSD`) are resolved dynamically from `/AssetPairs` + `/Assets` (`kraken_exchange.py:_ensure_asset_metadata`).
- **Stable design constants / domain invariants (dismiss):** asset aliases `BTC→XBT`/`DOGE→XDG` (only the stable colloquial exceptions are hardcoded), `OHLCBar.ALLOWED_INTERVALS`, `LLMProvider`/`LLMRole` enums, the harvester 24h rolling window (the *meaning* of "per day"), Discord embed limits + colors, page-size defaults, the cost quantizer + `len//4` token heuristic, RSS MIME types, the web loopback-allowlist (a security invariant), GitHub/htmx self-URLs, the `status.py` re-anchor/drift/age severity thresholds (info-only UX tuning), `calibrator._QUANTIZE`.
- **Provider base URLs / paths / version headers (keep code):** Kraken, Anthropic, OpenAI, Google, CryptoCompare — vendor contracts that change rarely, are env/constructor-overridable already, and move together (a contract change wants code + tests, not a config flip).

---

## Secondary findings (not four-homes moves — flagged for the operator)

These surfaced during the sweep. They are **not** "wrong home" problems; they're code-health
items to decide separately.

1. **⚠️ Latent bug — `_REASONING_MODEL_PREFIXES` drift gap.** `_PRICING` lists o4-mini-class
   models but the prefix tuple is still only `("o1","o3")`, so a future `o4`/`o5` model would
   be **misclassified** for reasoning-token handling (temperature sent, wrong token param →
   degraded/failed call). Not safety-critical (no budget bypass), but a real defect. *Fix:
   verify which families belong, extend the tuple — fold into **Q2** (the externalization
   that touches this exact code), or a one-line fix-now if Q2 is deferred.*
2. **Duplication smells (consolidate in code, not move out of code):**
   - **Kraken fee rate hardcoded in 4 places** (`grid.py`, `shadow_exchange.py`, `mock_exchange.py`, `config/cli.py`) → centralize on one code constant. *Touches the safety-critical validator → careful, its own slice, not trivial.*
   - Kraken base URL ×3 (`config/kraken.py`, `kraken_health.py`, config); Ollama base URL ×4; Anthropic URL/version ×2; Discord colors ×2 (embed-render + transport); `OHLCBar.ALLOWED_INTERVALS` ×2 (value-objects + the kraken range-check).
   - RSS `User-Agent` carries a stale `0.1` that doesn't track `__version__` `0.1.0` — derive the UA from `__version__`.
3. **Daemon-health fallback cadences** mirror `settings.example.yml` defaults — a lockstep/schema-drift check would catch silent drift (the live thresholds already derive from config).

---

## Method & provenance

- Discovery: 5 parallel lens-agents over `src/wobblebot` (excluding `tests/`, `config/`,
  `docs/`). Classification: one agent applying the four-homes test + safety flag.
- Adversarial review verdict (verbatim gist): *"Classification holds and is safe to ship.
  SAFETY: clean — zero safety-critical facts moved out of code (verified `_PRICING` and the
  Kraken fees against source). MISCLASSIFICATION: none substantive (only a label nit on the
  four already-right-home rows). COMPLETENESS: the sweep missed `status.py` thresholds and
  `calibrator._QUANTIZE`, but both correctly belong in code. Prior expectations all
  confirmed."*
- **Bottom line for storage-tier work:** nothing safety-critical is a DB/config candidate.
  The only externalizations are the three config slices (Q1–Q3). SQLCipher and any
  DB-migration remain gated on their own triggers (see the [plan](README.md) parked register);
  this audit does **not** unblock moving any safety fact into a DB.
