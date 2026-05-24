# Operator-Assistant LLM Compatibility Matrix

Empirical comparison of Ollama-served local models against the
operator-assistant routing battery. Driven by `tools/probe_assistant.py`
+ a one-off multi-model harness run on **2026-05-24** against
`config/prompts/operator.md` (the post-Stage-5.3 prompt with the
"trust the catalog" + counts-block tightening).

Use this when picking `operator.assistant.model` in `settings.yml`,
or when adding a new model to the operator's Ollama install.

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

- `phi4-mini-reasoning:3.8b-fp16` — **incompatible**. The 3.8B
  math-specialist treats every operator message as a math problem
  and never emits valid JSON. The adapter refuses to construct
  with this model.
- `llava:13b` — **incompatible**. Vision model, not text-instruct-
  tuned for JSON-schema output. Refused by the adapter.
- `qwen3.6:35b-a3b-q8_0` — **degraded**. 3/14 silent empty-content
  failures. The adapter logs a startup WARNING but doesn't block.
- `deepseek-r1:14b-qwen-distill-q8_0` — functional but **44s/call**
  makes operator interactions feel sluggish. Acceptable for batch
  use, not chat.

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
