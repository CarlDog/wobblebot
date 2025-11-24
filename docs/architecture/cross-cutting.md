# Cross-Cutting Concerns

## Logging

- Structured logs (JSON Lines)
- Unified logging at Orchestrator level
- Each module logs through its own namespace

## Security

- Minimal API permissions
- Separate API keys per module where applicable
- Banking adapter locked down behind thresholds
- No module may trigger external calls without Orchestrator gating

## Error Handling

- All external calls wrapped in retry logic
- Timeouts applied consistently
- Fatal errors escalate to Orchestrator

## Safety Enforcement

- **Dual-layer safety model:**
  - **Bot Core (local):** Enforces exposure caps, daily limits, order size constraints before generating trade intents
  - **Orchestrator (global):** Gate-keeping layer that can veto trades even if Bot Core approved them (defense in depth)
- Trade-size caps
- Withdrawal/deposit caps (Harvester-specific)
- Advisor suggestions validated before use
- Any **auto-applied Advisor change** must:
  - Pass JSON schema validation
  - Pass range checks against configured min/max bounds
  - Be fully logged with before/after config snapshots

## Rate Limiting

- Kraken Adapter respects exchange rate limits
- LLM Advisor requests throttled

## Validation

- Schema validation for Advisor JSON (AdvisorPort contract)
- Input sanitization at every port boundary
- Strict type and range validation for config values affecting money or risk
- **Port contract enforcement:** All ports defined in `src/wobblebot/ports/` with explicit type hints and docstrings
