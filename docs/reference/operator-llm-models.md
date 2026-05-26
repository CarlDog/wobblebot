# Operator-Assistant LLM Compatibility Matrix

Empirical comparison of Ollama-served local models against the
operator-assistant routing battery. Driven by `tools/probe_assistant.py`
+ a one-off multi-model harness run on **2026-05-24** against
`config/prompts/operator.md` (the post-Stage-5.3 prompt with the
"trust the catalog" + counts-block tightening).

Use this when picking `operator.assistant.model` in `settings.yml`,
or when adding a new model to the operator's Ollama install.

**Methodology caveats:**

- **Language**: every battery message is English. Non-English
  routing accuracy is untested. Most modern multilingual
  models (llama3.2, qwen2.5, gemma2, mistral) should handle
  non-English input via the LLM's own translation step, but
  we have no data. See the "Foreign-language operator support
  -- audit + test coverage" v1.1 entry in
  `docs/release/v1.1/operator-ux.md` for the planned audit.
- **Quantization**: the top-of-page "Results" table is at
  `q8_0` — the apples-to-apples capability ceiling on the
  operator's high-RAM workstation. A separate **Low-end
  hardware recommendations** section below carries `q4_K_M`
  scores from the 2026-05-25 audit, suitable for operators
  on consumer laptops or older GPUs. The q8→q4 quality drop
  is real but smaller than expected — 7-10B-class models
  hold their routing accuracy through quantization; the 1B
  class collapses regardless of quantization.
- **Multi-turn drift**: results from 2026-05-25 onward are
  scored out of **15** because the conversation-drift
  follow-up (BTC → ETH → BTC fills history, then "what about
  the past 6 hours") was added to ``EXPECTED``. Pre-2026-05-25
  results stay at /14. The follow-up is the main
  differentiator between models that hold context across
  turns and those that don't.

## Battery

14 messages exercising every routing surface:

- Simple queries: `status`, `show recent fills`
- Phrasing variants: `how are things?`, `show me what's available`,
  `any news?`, `what's the harvester doing`
- Brief variants: `give me a brief`, `status report for the past 4 hours`
- Commands: `pause BTC`, `stop the bot`
- Edge cases: `buy more bitcoin` (unparseable), `pause XRP`
  (unparseable — XRP not in active symbols), `what's the weather`
  (conversational), `news from the past 12 hours` (lookback extraction)

## Results

Ranked by accuracy then speed:

| Rank | Model | Acc | Errors | Avg/call | Notes |
|---|---|---|---|---|---|
| 1 | `phi4:14b-q8_0` | **14/14** | 0 | **5.1s** | Current default — perfect + fastest |
| 2 | `mistral-nemo:12b-instruct-2407-q8_0` | **14/14** | 0 | 5.9s | Smallest perfect; 12GB |
| 3 | `phi4-reasoning:14b-plus-q8_0` | **14/14** | 0 | 6.2s | Reasoning that works |
| 4 | `granite4.1:30b-q5_K_M` | **14/14** | 0 | 10.4s | IBM, large + perfect |
| 5 | `qwq:32b-q8_0` | 13/14 | 0 | 11.3s | Missed `what's the weather` |
| 6 | `nemotron3:33b` | 13/14 | 0 | 11.7s | Missed `what's the weather` |
| 7 | `deepseek-r1:14b-qwen-distill-q8_0` | 13/14 | 0 | **44s** | Works but too slow for interactive use |
| 8 | `gemma4:e4b-it-q8_0` | 12/14 | 0 | 6.7s | Missed `pause BTC` + `what's the weather` |
| 9 | `qwen3.6:35b-a3b-q8_0` | 11/14 | 3 | 16.3s | **Degraded** — 3 silent empty-content failures |
| 10 | `phi4-mini-reasoning:3.8b-fp16` | **0/14** | 14 | 25s | **Incompatible** — math specialist; pattern-matches every prompt to a quadratic equation |

First-call latency on every model is 25-60s (Ollama loading the model
into VRAM). Subsequent calls are the avg shown above.

## Recommendations

### Best overall

`phi4:14b-q8_0` — perfect routing, fastest, modest 14GB footprint.
This is the bundled default in `config/settings.example.yml` and
remains the recommendation.

### Best efficiency

`mistral-nemo:12b-instruct-2407-q8_0` — perfect routing at 12GB (the
smallest perfect-scoring model). Slightly slower than phi4 but a
genuine alternative if you're VRAM-constrained.

### Best reasoning visibility

`phi4-reasoning:14b-plus-q8_0` — perfect routing AND emits chain-of-
thought reasoning, useful for debugging parse decisions during
prompt iteration.

### Models to avoid

- `phi4-mini-reasoning:3.8b-fp16` — **incompatible against the
  current operator.md prompt** (8706 chars). Direct-probe
  diagnostic on 2026-05-25 (`tools/diagnose_phi4_mini_reasoning.py`)
  showed the failure is prompt-length saturation at 3.8B params,
  not a fundamental "math specialist can't do JSON" problem. When
  the operator.md prompt is replaced with a stripped 175-char
  routing prompt + Ollama's ``format=json`` constraint, the same
  model produces exactly correct JSON (`{"kind": "query",
  "query": {"kind": "status"}}`) in 46 characters with zero
  reasoning preamble. The 14/14 fail across the operator battery
  was specifically against the full operator.md prompt — the
  model is recoverable for the routing role with a compact
  prompt redesign. Adapter still refuses to construct under the
  current prompt; see v1.1 entry below for the prompt-redesign
  follow-up.
- `llava:13b` — **incompatible**. Vision model, not text-instruct-
  tuned for JSON-schema output. Refused by the adapter.
- `qwen3.6:35b-a3b-q8_0` — **degraded**. 3/14 silent empty-content
  failures. The adapter logs a startup WARNING but doesn't block.
- `deepseek-r1:14b-qwen-distill-q8_0` — functional but **44s/call**
  makes operator interactions feel sluggish. Acceptable for batch
  use, not chat.

**Note on dual-role candidates:** several models in the table
above (notably `granite4.1:30b-q5_K_M` at 14/14 operator) score
PERFECTLY here but are wrong-direction outliers on the **advisor
role** (granite4.1: 5/18 with 3 WR). If you're choosing one model
to serve BOTH roles, see the "Cross-role contrast (vs advisor
sweep)" section below.

## Cross-role contrast (vs advisor sweep)

Updated **2026-05-25** after the advisor sweep at
`docs/reference/advisor-llm-models.md` produced results for 9
models that ALSO appear in the operator-assistant table above.
The contrast is informative: **a perfect operator-assistant score
does NOT imply a competent advisor**, and the failure modes
differ in surprising ways.

| Model | Operator | Advisor | Cross-role read |
|---|---|---|---|
| `phi4:14b-q8_0` | **14/14** | 9/18 (1 WR) | Perfect router; mediocre advisor with one wrong-direction call. The operator's currently-deployed choice — fine for routing, NOT a top advisor pick. |
| `mistral-nemo:12b-instruct-2407-q8_0` | **14/14** | 8/18 (1 WR, 3 OVER) | Perfect router; weak advisor with three magnitude overshoots. Role-specialized to routing. |
| `phi4-reasoning:14b-plus-q8_0` | **14/14** (6.2s) | **TIMED OUT** | Perfect, fast router; can't complete the advisor probe within timeout. Reasoning-tuned models burn their latency budget on internal chain-of-thought; tolerable for short routing prompts, fatal for longer-context advisor calls. |
| `granite4.1:30b-q5_K_M` | **14/14** | **5/18, 3 WR** | **Perfect router; wrong-direction-outlier advisor.** The most striking cross-role split in the data — same model, perfect at intent classification, actively recommends the OPPOSITE direction on half the advisor fixtures. Strong evidence that intent-classification skill and numerical-reasoning skill are independent. |
| `qwq:32b-q8_0` | 13/14 | 11/18 | Strong router; ties the "always slight widen" advisor lazy-baseline. |
| `nemotron3:33b` | 13/14 | 10/18 (4 ADJ, 0 WR) | Strong router; "calibrated" advisor (hold-biased, never wrong-direction). Distinctive — the only model with both decent routing AND zero wrong-direction advisor calls. |
| `deepseek-r1:14b-qwen-distill-q8_0` | 13/14 (44s) | 10/18 (611s) | Functional in both roles but **prohibitively slow in both** — 44s/call for operator routing, 611s/call for advisor. Reasoning-tuned latency dominates regardless of role. |
| `gemma4:e4b-it-q8_0` | 12/14 | 11/18 | Decent router; ties the advisor lazy-baseline. Unremarkable in both roles. |
| `qwen3.6:35b-a3b-q8_0` | 11/14 (3 errors) | 8/18 (1 WR, 3 OVER) | **Degraded** in both roles. 3 silent empty-content failures on routing + 3 magnitude overshoots on advisor. The least-recommended model that's still installable. |

### Cross-role takeaways

1. **Roles exercise different skills.** A perfect operator-
   assistant score (intent classification) gives no signal about
   advisor quality (numerical reasoning over engineering metrics).
   `granite4.1:30b` is the smoking gun: 14/14 routing, 5/18 with
   3 wrong-direction calls on advisor.
2. **Reasoning-tuned models hit a latency wall on advisor before
   routing.** `phi4-reasoning:14b-plus` answers operator messages
   in 6.2s but times out on a 6-fixture advisor battery. The
   advisor prompt injects a longer `PerformanceSummary` JSON
   context that pushes the model into longer chain-of-thought
   episodes. Tolerable latency in one role does NOT imply
   tolerable latency in another.
3. **Don't dual-role from this matrix without checking both.**
   Operators tempted to repurpose their working operator-assistant
   model as the advisor (or vice versa) should verify against
   the sibling sweep's data first. The matrix above is the
   shortcut; running both probes against a new candidate is the
   rigorous path.
4. **`phi4:14b-q8_0` (currently bundled default) is the right
   choice for operator-assistant but is a 9/18 advisor.** The
   v1.1 MoE advisor design (Phase 3.4a) splitting quant / risk
   / news into separate expert seats is reinforced by this data:
   the "one model for everything" approach undersells both roles.

## Falcon3 family (added 2026-05-25 follow-up sweep)

TII's Falcon3 family was missed in the original 2026-05-24 sweep
because the earlier `falcon` family was documented-rejected as
older-generation. Falcon3 is a separate, newer line; sweeping it
2026-05-25 surfaced one strong pick.

| Model | Score | Errors | Time | Notes |
|---|---|---|---|---|
| `falcon3:3b-instruct-q8_0` | **13/15** | 0 | 80s | **Surprise: ties top-tier at 3B.** Matches granite3-dense:8b's score at one-third the disk size. Strong candidate for low-end-hardware operator-assistant. |
| `falcon3:7b-instruct-q8_0` | 11/15 | 1 | 148s | No scaling benefit vs 3b — produced one routing error the 3b didn't. |
| `falcon3:10b-instruct-q8_0` | 11/15 | 1 | 192s | Same pattern as 7b — no improvement, one error. |
| `falcon3:1b-instruct-q8_0` | **2/15** | n/a | 56s | Below the routing-schema threshold (same pattern as llama3.2:1b). |

**Why falcon3:3b is unusual:** in the rest of the sweep, scaling
generally helps until the 7-10B mid-tier where most models plateau
at 12-13/15. Falcon3 inverts this — the 3B is the family's best
performer, and the 7B + 10B both regress with one routing error
each. The Falcon3 instruction-tuning may have been optimized
specifically for the smaller sizes; or there's a quirk in how the
larger sizes handle the multi-variant intent schema. Worth noting
for operators considering the family.

**Recommended pick from Falcon3:** `falcon3:3b-instruct-q8_0` for
low-end hardware (it's the only Falcon3 worth using); skip the
larger variants — they're slower without being better.

Updated low-end hardware tier (mid-tier) recommendations: the
audited q4_K_M sweep below still names `qwen2.5:3b-instruct-q4_K_M`
as the recommended mid-tier pick (13/15, zero errors, 1.9GB).
`falcon3:3b-instruct-q8_0` (13/15, zero errors, ~3.2GB) is the
operator's pick if they have the extra ~1.3GB of RAM available and
prefer Falcon's tuning style.

## Low-end hardware recommendations (q4_K_M)

Audit run 2026-05-25 against 12 `q4_K_M` candidates spanning
the three hardware tiers an operator with weaker-than-the-
maintainer's hardware would reach for. The maintainer runs
phi4 at q8_0 on a 64GB workstation; these recommendations
are for everyone else.

Scored on the same routing battery as the top table, plus
the multi-turn drift follow-up (total /15).

### Recommended pick per tier

| Hardware budget | Recommended | Score | Disk | Why |
|---|---|---|---|---|
| **Bottom — 8GB RAM, no GPU** | `qwen2.5:1.5b-instruct-q4_K_M` | 11/15 (1 err) | ~1GB | The only sub-2B model that scores above floor on this prompt. Beats every 2B candidate in the audit. 39s probe time was the fastest in the sweep. |
| **Mid — 16GB RAM, no GPU** | `qwen2.5:3b-instruct-q4_K_M` | **13/15 (0 err)** | ~1.9GB | The standout — zero parse errors at 3B, matches the 7B and 10B class. Strongest "value pick" in the audit. |
| **Upper-mid — 16GB + 4-6GB VRAM** | `solar:10.7b-instruct-v1-q4_K_M` | **14/15 (0 err)** | ~6.5GB | Highest score in the entire low-end sweep — beat granite3-dense:8b's q4 result by one point. Upstage's SOLAR 10.7B has a noticeable affinity for structured-output prompts. |

### Full audit data

Ranked by accuracy then speed. Speed numbers reflect first-load latency on the operator's hardware on 2026-05-25 (models hosted on memory-card storage — typical disks will be faster).

| Model | Score | Errors | Time | Notes |
|---|---|---|---|---|
| `solar:10.7b-instruct-v1-q4_K_M` | **14/15** | 0 | 136s | Sweep winner |
| `qwen2.5:3b-instruct-q4_K_M` | 13/15 | 0 | 89s | Best efficiency |
| `qwen2.5:7b-instruct-q4_K_M` | 13/15 | 0 | 104s | Diminishing returns vs 3b |
| `granite3-dense:8b-instruct-q4_K_M` | 13/15 | 0 | 115s | q8 was 13/14; q4 holds |
| `mistral:7b-instruct-v0.3-q4_K_M` | 12/15 | 1 | 114s | Solid baseline |
| `nemotron-mini:4b-instruct-q4_K_M` | 12/15 | 1 | 121s | NVIDIA's mid-class |
| `qwen2.5:1.5b-instruct-q4_K_M` | 11/15 | 1 | 39s | Bottom-tier winner |
| `phi3.5:3.8b-mini-instruct-q4_K_M` | 11/15 | 0 | 80s | Zero errors but lower score |
| `gemma2:2b-instruct-q4_K_M` | 10/15 | 2 | 91s | Marginal |
| `llama3:8b-instruct-q4_K_M` | 10/15 | 1 | 114s | q8 was 12/14; quantization hurt this one |
| `granite3-dense:2b-instruct-q4_K_M` | 9/15 | 3 | 66s | Underperforms qwen2.5:1.5b |
| `llama3.2:1b-instruct-q4_K_M` | **1/15** | 11 | 129s | **Incompatible — pattern matches q8 1b failure** |

### Avoid at low-end

- **`llama3.2:1b-*`** at any quantization. 1/15 at q4, 4/14 at
  q8 (yesterday's audit). The 1B parameter count is below the
  threshold for following this routing schema regardless of
  quantization — operators reaching for a sub-2B model should
  pick `qwen2.5:1.5b-instruct-q4_K_M` instead.
- **`granite3-dense:2b`** — underperforms qwen2.5:1.5b at
  roughly the same disk size. Stick to qwen2.5 below 4B.

### q4 vs q8 reality check

Three models were tested at both quantizations:

| Model | q8 score (2026-05-24) | q4 score (2026-05-25) | Delta |
|---|---|---|---|
| `granite3-dense:8b` | 13/14 | 13/15 (+1 multi-turn) | flat |
| `qwen2.5:7b` | 12/14 | 13/15 (+1 multi-turn) | flat |
| `qwen2.5:3b` | 12/14 | 13/15 (+1 multi-turn) | flat |
| `llama3:8b` | 12/14 | 10/15 | **−2 effective** |
| `mistral:7b-instruct-v0.3` | 11/14 | 12/15 (+1 multi-turn) | flat |

The 7-10B `qwen2.5` and `granite3-dense` families lose
essentially nothing at q4_K_M for this prompt — operators on
mid-tier hardware can use q4 without quality concerns.
`llama3:8b` is the exception; its quantization sensitivity is
unusual and bears watching if operators report routing
regressions.

## Untested candidates by VRAM tier

The lightest validated model is `mistral-nemo:12b-instruct-2407-q8_0`
at 12GB. That excludes operators on consumer GPUs with 8GB or
less VRAM, and gives no data for users with mid-range hardware
(8GB) or older generation hardware that can only run pre-Llama-3
models. The candidates below span every hardware tier from
mobile-class to high-end consumer plus a "legacy / superseded"
section for users specifically running older models on dated
hardware.

All sizes are q4_K_M or q4_0 quantized unless noted -- match
Ollama's default pull. q8 doubles the footprint but improves
output quality; preferable when the hardware can hold it.

### Tier A — sub-1GB (mobile-class / SBC / Raspberry Pi 5)

For operators running on embedded boards or who want to ship
the operator-assistant role on a model that fits in spare RAM
on a laptop's iGPU.

| Candidate | Approx q4 size | Notes |
|---|---|---|
| `tinyllama:1.1b` | 0.7GB | The classic tiny model; useful as a baseline floor |
| `qwen2.5:0.5b` | 0.4GB | Qwen's smallest 2.5 variant |
| `qwen2:0.5b` | 0.4GB | Earlier Qwen 2 (pre-2.5) |
| `smollm2:360m` | 0.3GB | HuggingFace's small instruct model |
| `smollm2:1.7b` | 1GB | SmolLM2 larger variant -- still tier-A |

Expect most to fail on the routing battery -- under 1B params
struggles with our complex multi-variant schema. Worth testing
to confirm + document the floor.

### Tier B — 1-2GB (entry consumer GPU, 3-4GB total)

| Candidate | Approx q4 size | Notes |
|---|---|---|
| `llama3.2:1b` | 1.3GB | Meta's smallest current; instruction-tuned |
| `qwen2.5:1.5b` | 1GB | Qwen 2.5 small |
| `qwen1.5:1.8b` | 1.1GB | Qwen 1.5 small (older gen) |
| `gemma2:2b` | 1.6GB | Google's smaller gemma2 |
| `gemma:2b` | 1.4GB | First-gen Gemma (legacy) |
| `granite3:2b` | 1.5GB | IBM Granite small |

### Tier C — 2-4GB (low-end consumer GPU, 6GB total)

| Candidate | Approx q4 size | Notes |
|---|---|---|
| `llama3.2:3b` | 2GB | Meta 3B current gen |
| `qwen2.5:3b` | 2GB | Qwen 2.5 mid-small |
| `qwen1.5:4b` | 2.4GB | Qwen 1.5 (older gen, larger than 1.5:1.8b) |
| `phi3.5:3.8b` | 2.2GB | Pre-phi4 phi3.5 |
| `phi3:3.8b` | 2.3GB | Original phi3 (legacy) |
| `phi2:2.7b` | 1.6GB | Microsoft Phi-2 (legacy; pre-phi3) |
| `nemotron-mini:4b` | 2.5GB | NVIDIA's small model |
| `stablelm-zephyr:3b` | 1.9GB | Stability AI's zephyr fine-tune |
| `stablelm2:1.6b` | 1GB | Stability AI's smaller (legacy) |
| `orca-mini:3b` | 1.9GB | Microsoft Orca-Mini (legacy) |
| `dolphin-phi:2.7b` | 1.6GB | Dolphin fine-tune of Phi-2; may be uncensored-flavored |

### Tier D — 4-8GB (mid-range consumer GPU, 8GB total)

This is the bracket where most operators on prosumer hardware
land. Most promising tier for a "small but functional" pick.

| Candidate | Approx q4 size | Notes |
|---|---|---|
| `llama3.1:8b` | 5GB | Llama 3.1 8B instruct (current) |
| `llama3:8b` | 4.7GB | Llama 3 8B instruct (earlier, legacy) |
| `llama2:7b` | 4GB | Llama 2 (truly legacy, pre-Llama-3) |
| `llama2:13b` | 7.5GB | Llama 2 larger variant (legacy) |
| `mistral:7b` | 4.4GB | Mistral 7B instruct (current v0.3) |
| `mistral:7b-instruct-v0.2` | 4.4GB | Mistral v0.2 (legacy) |
| `mistral:7b-instruct-v0.1` | 4.4GB | Mistral v0.1 (legacy) |
| `qwen2.5:7b` | 4.7GB | Qwen 2.5 7B |
| `qwen2:7b` | 4.7GB | Qwen 2 7B (earlier gen) |
| `qwen:7b` | 4.5GB | Original Qwen 1.0 (legacy) |
| `qwen1.5:7b` | 4.5GB | Qwen 1.5 (legacy) |
| `gemma2:9b` | 5.5GB | Google Gemma 2 9B |
| `gemma:7b` | 5GB | First-gen Gemma 7B (legacy) |
| `granite3:8b` | 4.9GB | IBM Granite 8B |
| `granite3-dense:8b` | 4.9GB | Granite 3 dense variant |
| `yi:6b` | 3.5GB | 01.AI Yi 6B |
| `yi:9b` | 5.2GB | 01.AI Yi 9B |
| `internlm2:7b` | 4.4GB | Shanghai AI Lab InternLM 2 |
| `openchat:7b` | 4.4GB | OpenChat fine-tuned chat model |
| `starling-lm:7b` | 4.4GB | Starling RLHF |
| `neural-chat:7b` | 4.4GB | Intel Neural Chat |
| `zephyr:7b` | 4.4GB | Mistral-tuned zephyr |
| `solar:10.7b` | 6.5GB | Upstage SOLAR (larger 7B-class) |
| `nous-hermes:7b` | 4GB | Nous Research fine-tune (legacy) |
| `nous-hermes2:10.7b` | 6.6GB | Nous Hermes 2 (newer) |
| `deepseek-llm:7b` | 4GB | DeepSeek 7B (older, before R1) |

### Tier E — 8-16GB (high-end consumer, our existing tested band)

Already covered by validated models; listed here for completeness
when matching candidates by VRAM:

| Candidate | Approx q4 size | Notes |
|---|---|---|
| `mistral-nemo:12b-instruct-2407-q8_0` | 12GB (q8) | ✅ tested 14/14 |
| `phi4:14b-q8_0` | 14GB (q8) | ✅ tested 14/14 (current default) |
| `phi4-reasoning:14b-plus-q8_0` | 16GB (q8) | ✅ tested 14/14 |
| `command-r:35b` | 19GB | Cohere Command R (haven't tested) |
| `nous-hermes2-mixtral:8x7b` | 26GB | Mixtral-based hermes2 |

### Rejected from testing (and why)

Models considered during the 2026-05-24 curation and the
2026-05-25 deep-list expansion that were intentionally excluded
from the candidate tables above. Documenting the rationale so
future contributors don't re-litigate the same decisions.

**Roleplay / uncensored / creative-writing variants** —
fine-tuned for content-policy-free chat, character play, or
fiction. Not optimized for schema-conforming structured output;
likely to "stay in character" rather than emit clean JSON.

- `vanilj/mistral-nemo-12b-celeste-v1.9:Q8_0` — Celeste
  roleplay variant. The base `mistral-nemo:12b-instruct-2407`
  is in the validated set (14/14).
- `LESSTHANSUPER/RP-INK-Qwen2.5-32b:Q5_K_S` — RP-INK
  roleplay variant.
- `nchapman/mn-12b-mag-mell-r1:latest` — community RP fine-tune.
- `dolphin-mistral`, `dolphin-llama3`, `dolphin2.5-mixtral` —
  Eric Hartford's Dolphin series uncensored fine-tunes. The
  smaller `dolphin-phi:2.7b` is in Tier C for completeness but
  expect lower routing accuracy than the underlying phi3.
- `wizard-vicuna-uncensored`, `wizardlm-uncensored` —
  uncensored chat-only fine-tunes.

**Coding-specialized models** — fine-tuned on code corpora;
strong at code completion but the JSON-schema-conforming chat
format is outside their training distribution.

- `deepseek-coder-v2:16b-lite-instruct-q8_0` (installed locally
  but excluded from the comparison). Use `deepseek-llm:7b` or
  `deepseek-r1:14b-qwen-distill` for chat-style intent parsing.
- `codellama:7b`, `codellama:13b`, `codellama:34b` — Meta's
  CodeLlama family.
- `starcoder:7b`, `starcoder2:15b` — BigCode's StarCoder series.
- `codegemma:7b` — Google's coding-focused Gemma variant.
- `granite-code:8b` — IBM's coding variant; the general
  `granite3` series is in Tier D instead.

**Vision / multimodal models** — fundamentally can't reliably
produce text JSON to our schema; cannot route a text-only
operator message into a typed intent.

- `llava:13b`, `llava:7b`, `llava:34b` — installed `llava:13b`
  is hard-blocked in `KNOWN_INCOMPATIBLE_FOR_ASSISTANT`.
- `bakllava:7b` — alternative llava fork.
- `moondream:1.8b` — small vision-language model.
- `llama3.2-vision:11b`, `llama3.2-vision:90b` — Meta's
  multimodal Llama 3.2.

**Pre-instruction-tuned base models** — base completion-only
weights without instruct fine-tuning. Can't follow complex
prompts; predict-next-token only.

- `llama3:text`, `llama2:text` — base completion variants of
  the chat models. Use the instruct variants (`llama3:8b`,
  `llama2:7b-chat-q8_0`) instead.
- `mistral:7b-text-v0.2` — base completion Mistral; use
  `mistral:7b` (instruct) instead.
- Anything tagged `:base` or `:text` on Ollama's library.

**Important pitfall**: many model families publish a plain
`<size>` tag (no `-instruct` / `-chat` suffix) that maps to the
BASE model, not the instruct variant. `ollama pull qwen2.5:1.5b`
fetches a base completion model that can't follow our prompt;
`ollama pull qwen2.5:1.5b-instruct-q8_0` fetches the instruct
variant. See `docs/reference/ollama-tag-verification.md` for
the per-family convention matrix (which families default the
plain tag to instruct vs base) and a reusable verification
script.

**Math / reasoning specialists with too-narrow training** —
trained on math problems exclusively; pattern-match every
prompt to a math problem (see phi4-mini-reasoning's 0/14).
**Scope note:** these rejections apply to the OPERATOR-ASSISTANT
role only. WobbleBot is fundamentally a numerical-reasoning
application -- prices, percentages, ratios, fee accounting,
volatility, position sizing -- so math specialists have several
plausible high-value homes in the codebase. We have NOT tested
any math-specialist model in any of them yet; the rejection
here only documents operator-assistant unsuitability. Keep
these installed for the candidate roles below.

Candidate roles for math-specialist LLMs in WobbleBot:

1. **MoE quant-expert** (Phase 3.4's `config/prompts/quant.md`
   advisor slot). Already designed as the "numerical analysis
   of performance summaries" expert; a math specialist is the
   on-paper-correct fit. Output is still JSON-schema-bound
   (AdvisorRecommendation), so the rejection mechanism would
   need similar evaluation but against a different prompt.
2. **Backtest / cycle analysis prose** (future feature). Free-
   form prose describing Sharpe / Sortino / drawdown /
   win-loss-ratio for a window. No schema constraint;
   math-specialist's narrative-step-by-step style is well-
   matched.
3. **Anomaly detector scoring** (v1.1 entry in
   `observability.md`). Spec'd as deterministic Z-score / IQR
   today, but a math specialist could ALSO explain WHY a value
   is anomalous in operator-friendly prose ("the cancel rate
   spiked because the engine is churning under the new
   tighter spacing -- expect ~Nx more cancels per fill").
4. **Recalibration recommendations** (extension of
   `cli/recalibrate`). The current scaler does linear math; a
   math specialist could reason about non-linear effects like
   Kraken's tiered fee schedule, slippage characteristics at
   different order sizes, optimal grid density for a given
   realized volatility regime.
5. **`weather_report` query** (v1.1 entry in `operator-ux.md`).
   Market-trend math: sentiment + price + volume aggregation.
   The narrative-from-numbers pattern fits.
6. **Cost-honesty dashboard math** (v1.1 entry in
   `operator-ux.md`). Compute infrastructure cost per cycle,
   electricity per cycle, fees per cycle. Heavy on arithmetic
   + explanation.

If you're considering wiring any of these, the
`KNOWN_INCOMPATIBLE_FOR_ASSISTANT` blocklist does NOT apply --
the gate is in `adapters/ollama_assistant.py`, which only
constructs the operator-assistant role. The advisor adapter
(`adapters/ollama.py`) and any future math-role adapter live
on separate paths.

- `phi4-mini-reasoning:3.8b-fp16` — 0/14 against the current
  operator.md prompt, BUT the 2026-05-25 direct-probe diagnostic
  (`tools/diagnose_phi4_mini_reasoning.py`) confirmed this is
  prompt-length saturation, not model-fundamental incompatibility.
  Same model + 175-char stripped prompt + `format=json` →
  exactly correct routing JSON in 46 chars. Still hard-blocked
  for the operator role under the current prompt. **Tested at
  0/18 in the quant role under the same prompt-saturation
  failure mode** — see advisor-llm-models.md.
- `mathstral:7b` — Mistral's math-specialized variant.
  Probably exhibits the same operator-assistant pattern but
  untested. **Untested in the quant role.**
- `wizardmath:7b`, `wizardmath:13b` — WizardLM math-tuned
  variants. **Untested in the quant role.**

**Domain specialists outside operator-assistant scope** —
trained for medical, legal, finance, biology, etc. The general
instruct models in the candidate tiers will outperform on
WobbleBot's catalog routing task.

- `meditron:7b` (medical), `medllama2:7b`
- `everythinglm:13b` (story generation)
- `nexusraven:13b` (function calling — could theoretically work
  but the JSON schema differs from ours; would need testing).

**Discontinued / orphaned models** — models that pulled from
Ollama's library or whose maintainers stopped updating. Worth
noting so contributors don't waste time chasing them.

- `falcon:7b`, `falcon:40b` — TII Falcon; older generation,
  weak instruction-following vs current candidates.
- `vicuna:7b`, `vicuna:13b`, `vicuna:33b` — LMSYS Vicuna;
  superseded by the underlying Llama-2/3 chat fine-tunes.
- `wizardlm:7b`, `wizardlm:13b`, `wizardlm:70b` — WizardLM
  general chat; superseded.

**Reasoning models that work but were excluded for runtime
cost** — these CAN run our routing battery but the latency
makes them poor operator-assistant picks.

- `deepseek-r1:7b` — likely similar 30-50s/call to the 14B
  variant tested. The 14B is in the validated set.
- `deepseek-r1:32b`, `deepseek-r1:70b` — even slower; only
  worth testing if quality matters more than latency.
- `o1-` Ollama variants — same reasoning-overhead concern.

If you want to test ANY of the above, the
`KNOWN_INCOMPATIBLE_FOR_ASSISTANT` / `KNOWN_DEGRADED_FOR_ASSISTANT`
lists in `src/wobblebot/adapters/ollama_assistant.py` are the
guardrails. The adapter refuses to start with a blocked model,
which means you can pin one in `settings.yml`, run
`probe_assistant.py` against it, and if it surprises us
(produces clean JSON anyway) you can argue for removing it
from the blocklist.

### Notes on legacy candidates

The "older generation" picks (anything tagged legacy or pre-3
above) are mostly here as **a fallback path for operators on
genuinely dated hardware** -- a 2019-era laptop GPU with 4GB
VRAM that can't run Llama 3 cleanly but might handle Llama 2
or Mistral v0.1. They are NOT preferred when newer alternatives
fit in the same VRAM envelope:

- Llama 2 < Llama 3 < Llama 3.1 < Llama 3.2 -- all at ~5GB
  for the 7-8B class.
- Qwen 1.0 < Qwen 1.5 < Qwen 2 < Qwen 2.5 -- successive
  generations consistently outperform on instruction-following.
- Gemma 1 < Gemma 2 -- significant capability jump.
- Phi-2 < Phi-3 < Phi-3.5 < Phi-4 -- Microsoft's small-model
  series; each gen materially better.

Test a legacy model only if the current-gen equivalent in the
same VRAM tier fails or won't run.

### Suggested test sequence

When time permits, validate in this order to get max coverage
per probe-hour spent:

1. **Tier B current-gen (1-2GB)**: `llama3.2:1b`, `qwen2.5:1.5b`,
   `gemma2:2b`, `granite3:2b`. These set the realistic
   "smallest viable" floor.
2. **Tier C current-gen (2-4GB)**: `llama3.2:3b`, `qwen2.5:3b`,
   `phi3.5:3.8b`, `nemotron-mini:4b`. Most-likely sweet spot
   for low-VRAM users.
3. **Tier D current-gen (4-8GB)**: `llama3.1:8b`, `mistral:7b`,
   `qwen2.5:7b`, `gemma2:9b`, `granite3:8b`, `openchat:7b`.
   Most promising tier for "small but actually works."
4. **Tier A (<1GB)** + **legacy variants**: only after the
   current-gen tiers are mapped out -- diminishing returns vs
   testing time.

Workflow per candidate: `ollama pull <model>`, then
`python tools/probe_assistant.py --model <model> --skip-multi-turn`,
then append the result row to the main compatibility table
above. If <11/14 OR persistent silent errors, add to
`KNOWN_INCOMPATIBLE_FOR_ASSISTANT` or `KNOWN_DEGRADED_FOR_ASSISTANT`
in `src/wobblebot/adapters/ollama_assistant.py`.

## How to add a new model to this list

1. Install in Ollama: `ollama pull <model>`
2. Run the comparison harness against it:

   ```pwsh
   .venv/Scripts/python.exe tools/probe_assistant.py --model <model> --skip-multi-turn
   ```

3. Note the routing accuracy + average latency.
4. If 14/14, append to the recommendations table.
5. If <11/14 OR persistent silent errors, add the model tag pattern
   to `KNOWN_INCOMPATIBLE_FOR_ASSISTANT` or
   `KNOWN_DEGRADED_FOR_ASSISTANT` in
   `src/wobblebot/adapters/ollama_assistant.py` so future operators
   get a clear startup error / warning.

## Related

- `tools/probe_assistant.py` — single-model probe (LLM-only, fast).
- `tools/probe_discord_bot.py` — full end-to-end probe via webhook
  (slower but exercises the full daemon path).
- `config/prompts/operator.md` — the system prompt every model is
  scored against. Changes here invalidate prior compatibility data.
