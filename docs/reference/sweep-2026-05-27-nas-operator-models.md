# NAS operator-model sweep — 2026-05-27/28

Permanent record of the Ollama model evaluation for the
`cli/operator` assistant role on the operator's Synology
DS1823xs+ (Ryzen V1780B 4C/8T, 64 GB RAM, no GPU, no AVX-512).

## Why

Discord assistant errors surfaced 2026-05-27 during Day 10 of
the NAS Docker deployment: `qwen2.5:3b-instruct-q4_K_M` (the
existing cpu-only profile pick) returned schema-invalid JSON
on some routing decisions and hit a first-message timeout on
every fresh container start. Per the operator priority that
"speed and reliability are paramount for cli/operator," ran a
bracket sweep to find a better model.

## Methodology

- Tool: `tools/sweep_assistant_nas.py` (added 2026-05-27).
- Battery: 8 messages covering query / command / brief / edge
  routing surfaces. Smaller than `tools/probe_assistant.py`'s
  27-message default — keeps a 16-model sweep under an hour.
- Endpoint: `http://carldog-nas:11434`.
- Prompt: `config/prompts/operator.md` (production).
- Per-call timeout: 120s.
- Each model goes through `warmup()` (fires `/api/generate`
  with empty prompt + `num_predict=1`) then 8 sequential
  `parse_intent` calls. Sequential per the no-parallel-Ollama
  rule.
- Suitability blocklist bypassed (`bypass_suitability_check=True`)
  so models on `KNOWN_INCOMPATIBLE_FOR_ASSISTANT` can be
  re-evaluated.

## Sweep 1 — 16 installed candidates (2026-05-27)

```
model                                       pass     mean    warmup
qwen2.5:1.5b-instruct-q4_K_M                8/8    3.04s     1.4s   ← winner
qwen2.5:3b-instruct-q4_K_M (prior pick)     7/8   24.26s     1.4s
qwen2.5:3b-instruct-q8_0                    7/8   28.10s    11.3s
falcon3:3b-instruct-q8_0                    6/8   35.57s    10.4s
qwen2.5:7b-instruct-q4_K_M                  5/8   52.51s    13.4s
qwen2:7b-instruct-q4_K_M                    5/8   54.16s    14.1s
llama3.2:1b                                 3/8   16.24s     6.2s
llama3:8b-instruct-q4_K_M                   3/8   79.37s    14.8s
granite3-dense:8b-instruct-q4_K_M           3/8   84.41s    15.3s
llama3.1:8b-instruct-q4_K_M (advisor)       3/8   86.04s    15.9s
mistral-nemo:12b-instruct-2407-q4_K_M       2/8   96.92s    19.9s
zephyr:7b                                   2/8  100.05s    12.7s
neural-chat:7b                              1/8  108.91s    11.3s
starling-lm:7b                              1/8  118.37s    13.3s
phi4:14b-q4_K_M                             0/8  120.75s    18.2s
solar:10.7b-instruct-v1-q4_K_M              0/8  120.87s    17.6s
```

## Sweep 2 — 7 new small candidates + qwen2.5:1.5b control (2026-05-28)

```
model                                       pass     mean    warmup
qwen2.5:1.5b-instruct-q4_K_M (control)      8/8   16.10s     6.2s   ← winner
smollm2:1.7b-instruct-q4_K_M                6/8   35.33s     2.2s
phi4-mini:3.8b-q4_K_M                       6/8   49.01s     4.1s
gemma3:1b-it-q4_K_M                         4/8   61.51s     3.8s
smollm2:135m-instruct-q4_K_M                3/8    5.96s     1.8s
smollm2:360m-instruct-q4_K_M                3/8   17.58s     1.9s
tinydolphin:1.1b-v2.8-q4_K_M                0/8   49.50s     1.9s
phi3:3.8b-mini-4k-instruct-q4_K_M           0/8   69.06s     2.8s
```

## Key findings

1. **qwen2.5:1.5b is the runaway winner for the operator role.**
   8/8 pass across both sweeps; mean call time 3-16s depending
   on system cache contention. Production target is 3-10s.

2. **The first-message cache-warm tax is the dominant failure
   mode for models ≥3B on this hardware.** Every 3B+ candidate
   hit 120s timeout on the FIRST call after a fresh model load,
   then succeeded at 2-10s on subsequent calls. The `warmup()`
   method's `/api/generate` ping is not sufficient to fully
   cache-prime the model for the next real call.

3. **Everything ≥7B is non-viable for interactive Discord use
   on this NAS.** Best 7B was 5/8 with 52s mean. The Phi-14B
   and Solar-10.7B got 0/8 — never recovered enough to respond
   within the 120s cap across all 8 attempts.

4. **smollm2:135m (90 MB) successfully routes some messages**
   at 3/8 pass. Not production-viable but a remarkable data
   point: a 135M-parameter model CAN handle simple intent
   routing when the schema is well-anchored and `format=json`
   is enforced.

5. **qwen architecture out-performs llama at the 1-2B tier**
   for structured-output routing. llama3.2:1b scored 3/8;
   qwen2.5:1.5b scored 8/8. Worth noting for future small-model
   architecture decisions.

6. **The Phi family is split:** `phi4-mini` (non-reasoning)
   scored 6/8 and is viable. `phi3:mini` scored 0/8 — model
   produces invalid JSON syntax (unquoted keys) despite
   `format=json` request, suggesting Phi3 architecture
   genuinely can't honor JSON-mode constraints reliably. The
   `phi4-mini-reasoning` variant (separately) remains on the
   `KNOWN_INCOMPATIBLE_FOR_ASSISTANT` blocklist for a different
   reason: math-specialization treats every prompt as a math
   problem.

## Decision

Switched the cpu-only profile's `operator.assistant.model` from
`qwen2.5:3b-instruct-q4_K_M` to `qwen2.5:1.5b-instruct-q4_K_M`
in `config/settings.example.yml`. Operator's own
`settings.yml` on the NAS needs the matching edit + container
restart.

## When to re-run

- New Ollama model release that looks promising for structured
  output (e.g. a Qwen3 small variant, a new Gemma small).
- After hardware change (different CPU, RAM, GPU).
- If qwen2.5:1.5b's pass rate drops below 6/8 over a few weeks
  of real-world Discord traffic — could indicate prompt drift
  or a model degradation across the 24-hour resident period.

Use `tools/sweep_assistant_nas.py` with `--models` to constrain
the candidate set or `--base-url` to target a different Ollama
host.

## Related

- Stage 5.3 (OllamaAssistantAdapter) introduced the assistant
  port + the `is_thinking_model` / `force_json` machinery.
- 2026-05-26 reasoning-model sweep
  (`docs/reference/sweep-2026-05-25-reasoning-models.md`) reached
  a different conclusion about phi4-mini-reasoning via a
  different methodology (compact prompt + force_json escape
  hatch). That sweep's finding stands: reasoning fine-tunes
  remain "not recommended" for the operator role.
- The NAS Ollama optimization global rule
  (`~/.claude/rules/nas-ollama-optimization.md`) still holds
  for advisor + general selection; this sweep only changes the
  operator-role default within that framework.
