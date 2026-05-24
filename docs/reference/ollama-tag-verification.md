# Ollama LLM tag verification -- workflow + gotchas

*Portable reference. Applicable to any project that pulls Ollama
models from `ollama.com/library/<model>` and needs to confirm
the tag is the instruct/chat variant rather than the base
(pretrained, completion-only) variant.*

## Why this matters

`ollama pull <model>` (or `<model>:<size>`) does NOT consistently
fetch the instruct-tuned variant. Some model families default
the plain tag to the instruct version; others default to the
base version. Pulling a base model when you wanted instruct
produces gibberish output that looks like the model is broken
when it's actually just predicting next-token without following
your prompt.

The classic surface for this bug: you run your routing-accuracy
test, score 3/14, conclude "this 1B model is too small" -- but
actually you pulled the base completion model and an instruct
variant of the same size would have scored 12/14.

## The verification workflow

### Step 1 -- confirm the model still exists on Ollama

Ollama has removed models from the library over time. Examples
we've found removed:

- `qwen1.5` (entire family) -- replaced by `qwen2`, `qwen2.5`.
- `granite3` (entire family) -- renamed to `granite3-dense` and
  `granite3-moe`.

Quick check:

```python
import httpx
r = httpx.get(f"https://ollama.com/library/{model}/tags",
              timeout=15, follow_redirects=True)
if r.status_code != 200:
    print(f"{model}: HTTP {r.status_code} -- likely removed")
```

### Step 2 -- enumerate every tag for the model family

The tags-list page (`/library/<model>/tags`) renders every
variant as a clickable link. Extract them with a single regex:

```python
import re, httpx
r = httpx.get(f"https://ollama.com/library/{model}/tags",
              timeout=15, follow_redirects=True)
tags = sorted(set(re.findall(
    rf'href="/library/{re.escape(model)}:([^"]+)"',
    r.text,
)))
```

You'll get every tag the library publishes (typically 30-60
per family covering all size + quantization combinations).

### Step 3 -- identify the instruct variants

Instruct-tuned tags usually contain one of these substrings
case-insensitively:

- `instruct` -- most common (Llama, Qwen, Gemma, Phi recent)
- `chat` -- common for older series (Llama 2, Qwen 1.0, Yi,
  InternLM, DeepSeek-LLM, TinyLlama, original Gemma)
- `-it` -- Google's Gemma family sometimes
- `-mini-instruct` -- Microsoft Phi-3.5/Phi-3 (mini = small variant)

Base variants are typically tagged with `-text` or `-base`
(skip these for chat / intent-parsing use cases).

### Step 4 -- check whether the plain `<size>` tag points to
instruct or base

For each model family, the plain `:<size>` tag (no instruct
suffix) usually aliases to ONE of the variants. WHICH one
varies per family:

```python
plain = [t for t in tags if re.fullmatch(r'[0-9.]+[bm]', t)]
print(f"plain size tags: {plain}")
```

If the family ALSO publishes `<size>-instruct` or `<size>-chat`
variants, the plain `:<size>` tag often points to the BASE
model. The only way to know for sure is to either:

1. Run `ollama show <model>:<size> --modelfile` after pulling
   and look at the `FROM` line for "instruct" / "chat" markers
   in the source.
2. Cross-reference the model's announcement / model-card to
   confirm which variant the library aliases.
3. Trust the empirical conventions in the matrix below.

### Step 5 -- pick the explicit instruct tag + quantization

If in doubt, use the explicit tag with quantization:

- `<size>-instruct-q4_K_M` -- balanced size/quality
- `<size>-instruct-q8_0` -- higher quality, ~2x the VRAM
- `<size>-instruct-fp16` -- unquantized, large

Pick quantization based on hardware vs quality preference;
defaults are project-specific.

## Default-tag convention matrix (as of 2026-05-25)

Empirically observed when scoring all variants of each family
against the WobbleBot operator-assistant prompt:

| Model family | Plain tag default | Explicit instruct/chat tag pattern |
|---|---|---|
| `llama3.2` | **Instruct** (q8_0) | `<size>-instruct-q4_K_M`, `-q8_0` |
| `llama3.1` | **Instruct** (q4_K_M) | `<size>-instruct-q4_K_M`, `-q8_0` |
| `llama3` | **Instruct** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `llama2` | **Base** | `<size>-chat-q4_K_M`, `-q8_0` |
| `mistral` | **Instruct** (latest v0.3) | `<size>-instruct-v0.3-q4_K_M`, `-q8_0` (also v0.2, v0.1) |
| `qwen2.5` | **Base** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `qwen2` | **Base** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `qwen` (1.0) | **Base** | `<size>-chat-v1.5-q4_K_M`, `-q8_0` |
| `qwen1.5` | NOT FOUND on Ollama anymore | (removed) |
| `gemma2` | **Base** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `gemma` | **Base** | `<size>-instruct-v1.1-q4_K_M`, `-q8_0` (also non-v1.1) |
| `phi4` | **Instruct** (q4_K_M) | `<size>-q8_0` (no -instruct suffix used) |
| `phi4-reasoning` | **Instruct** (q4_K_M) | `<size>-plus-q8_0` |
| `phi3.5` | **Instruct** | `<size>-mini-instruct-q4_K_M`, `-q8_0` |
| `phi3` | Mixed | `<size>-mini-4k-instruct-*` or `<size>-mini-128k-instruct-*` |
| `phi` (2) | **Base** | `<size>-chat-v2-q4_K_M`, `-q8_0` |
| `tinyllama` | **Base** | `<size>-chat-v1-q4_K_M`, `-q8_0` |
| `smollm2` | **Base** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `nemotron-mini` | **Instruct** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `nemotron3` | **Instruct** (q4_K_M) | (no suffix observed) |
| `granite3` | NOT FOUND on Ollama anymore | (renamed to granite3-dense) |
| `granite3-dense` | **Instruct** | `<size>-instruct-q4_K_M`, `-q8_0` |
| `granite4.1` | **Instruct** (q5_K_M) | (no suffix observed) |
| `stablelm-zephyr` | **Chat** (name says it) | (plain tag is the chat variant) |
| `stablelm2` | **Base** | `<size>-chat-q4_K_M`, `-q8_0` |
| `orca-mini` | **Chat** (general-purpose by description) | (plain tag is the chat variant) |
| `dolphin-phi` | **Uncensored chat** | (plain tag is the chat variant) |
| `openchat`, `starling-lm`, `neural-chat`, `zephyr` | **Chat** (purpose by name) | (plain tag is the chat variant) |
| `nous-hermes`, `nous-hermes2` | **Chat** | (plain tag is the chat variant) |
| `solar` | **Base** | `<size>-instruct-v1-q4_K_M`, `-q8_0` |
| `deepseek-llm` | **Base** | `<size>-chat-q4_K_M`, `-q8_0` |
| `deepseek-r1` | **Distill (instruct-equivalent)** | (plain tag works; thinking model) |
| `yi` | **Base** | `<size>-chat-q4_K_M`, `-q8_0` (also `-v1.5` variants) |
| `internlm2` | **Base** | `<size>-chat-v2.5-q4_K_M`, `-q8_0` |
| `qwq` | **Instruct (reasoning)** | (plain tag works; reasoning model) |
| `qwen3.6` | **Instruct** (a3b MoE) | `<size>-a3b-q8_0` |

When in doubt: explicit `-instruct` or `-chat` tag wins.

## Reusable verification script

This script enumerates tags for any list of model families and
flags the plain tag's likely default + the explicit instruct
variants for each size. Copy + adapt:

```python
import re
import httpx


def tag_audit(model: str) -> dict:
    """Return a dict describing the model's tag landscape on Ollama."""
    try:
        r = httpx.get(
            f"https://ollama.com/library/{model}/tags",
            timeout=15,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        return {"model": model, "error": str(exc)}
    if r.status_code != 200:
        return {"model": model, "error": f"HTTP {r.status_code}"}
    tags = sorted(set(re.findall(
        rf'href="/library/{re.escape(model)}:([^"]+)"',
        r.text,
    )))
    plain_sizes = [t for t in tags if re.fullmatch(r'[0-9.]+[bm]', t)]
    instruct_like = [
        t for t in tags
        if any(s in t.lower() for s in ("instruct", "chat", "-it"))
        and "text" not in t.lower()
        and "base" not in t.lower()
    ]
    return {
        "model": model,
        "total_tags": len(tags),
        "plain_size_tags": plain_sizes,
        "instruct_variants_sample": instruct_like[:8],
    }


if __name__ == "__main__":
    for m in ("llama3.2", "qwen2.5", "gemma2"):
        print(tag_audit(m))
```

## Common removed-model substitutions

| Removed | Substitute |
|---|---|
| `qwen1.5` | `qwen2` or `qwen2.5` (newer; better instruction-following) |
| `granite3` | `granite3-dense` (renamed) |

If the script returns `HTTP 404` for a model you expected, check
the model card / vendor announcement for renames. The Ollama
library URL is the source of truth.

## See also

- WobbleBot's operator-assistant LLM compatibility matrix
  (`docs/reference/operator-llm-models.md`) -- empirical accuracy
  scores per validated model against the WobbleBot routing
  battery. Uses the verification workflow above for every
  candidate.
- `tools/probe_assistant.py` -- WobbleBot's per-model probe
  harness. Generalizable to any project that wants per-model
  accuracy scoring against a fixed prompt + battery.
