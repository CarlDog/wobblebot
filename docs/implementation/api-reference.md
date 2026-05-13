# API & Interface Reference

> **Status: forward-looking design doc.** Describes the Phase 2+ target
> system. None of the commands, configs, schemas, or endpoints below exist
> yet. Current code (Phase 1.3) runs only via the test suite. Track real
> progress in [docs/planning/roadmap.md](../planning/roadmap.md).

This document describes WobbleBot’s public interfaces.  These include command‑line entry points, any optional HTTP endpoints, and the JSON schema expected from the LLM Advisor.  External APIs (Kraken, banking, LLM servers) are out of scope and should be referenced from vendor documentation.

## Command‑Line Interface (Planned)

### `wobblebot run`

Run the main trading loop using the configured environment.

**Options:**

| Flag | Description |
| --- | --- |
| `--config <path>` | Path to the configuration file. |
| `--once` | Run a single cycle and exit.  Useful for debugging. |
| `--paper` | Force paper‑trading mode regardless of config. |
| `--dry-harvest` | Log harvester proposals without executing transfers. |

### `wobblebot status`

Display a high‑level status including active coins, open orders, recent cycles, and the current modes of the Bot Core, Advisor, and Harvester.

### `wobblebot advise`

Trigger a one‑off Advisor run and print recommendations to the console.  Does not apply changes, even if auto‑apply is enabled.

## HTTP Interface (Optional / Future)

If a web API or UI is added in Phase 5, endpoints will be documented here.  For example:

| Method & Path | Purpose |
| --- | --- |
| `GET /status` | Returns a JSON summary of the bot’s current state. |
| `POST /control/pause` | Pause trading for a specific asset. |
| `GET /advisor/recommendations` | Returns the latest Advisor suggestions. |

Each endpoint specification should include request parameters, response schema, and any authentication or rate limiting requirements.

## Advisor JSON Schema (Draft)

The Strategy Advisor must return JSON conforming to a strict schema.  A draft example is shown below:

```json
{
  "version": "1.0",
  "generated_at": "2025-01-01T12:00:00Z",
  "asset_recommendations": [
    {
      "symbol": "DOGE",
      "actions": [
        {
          "type": "adjust_grid",
          "params": {
            "new_step": 0.002,
            "new_min_price": 0.12,
            "new_max_price": 0.18
          },
          "confidence": 0.82
        }
      ]
    }
  ],
  "notes": "Optional explanatory text for the human operator."
}
```

**Rules:**

- `version` identifies the schema version.
- `generated_at` is an ISO 8601 timestamp of when the advice was produced.
- `asset_recommendations` is an array of objects, one per asset.
- Each asset object contains a `symbol` field and an array of `actions`.
- Each action has a `type` and a `params` object specifying the parameters for that action.  Only **whitelisted actions** (e.g., `adjust_grid`) will be considered for auto‑apply.  Unknown or unsupported actions must be ignored safely.
- Numeric parameters must pass range checks defined in the configuration before being applied.

If additional fields are needed in the future, they should be added in a backward‑compatible manner and documented here.  A formal JSON schema (e.g., using JSON Schema Draft 7) can be added as the interface stabilizes.

## Stability Contract

Any interfaces documented here are part of the “public surface” of WobbleBot.  Backwards‑incompatible changes should be deliberate and accompanied by a version bump and changelog entry.  Use semantic versioning (e.g., `v1.0.0`) to communicate breaking changes.
