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

- Daily exposure caps  
- Trade-size caps  
- Withdrawal/deposit caps  
- Advisor suggestions validated before use  
- Any **auto-applied Advisor change** must:
  - Pass JSON schema validation
  - Pass range checks against configured min/max bounds
  - Be fully logged with before/after config snapshots

## Rate Limiting

- Kraken Adapter respects exchange rate limits  
- LLM Advisor requests throttled  

## Validation

- Schema validation for Advisor JSON  
- Input sanitization at every port boundary  
- Strict type and range validation for config values affecting money or risk