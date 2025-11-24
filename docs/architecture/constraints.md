# System Constraints

These constraints shape and limit the design.  
They are non-negotiable unless explicitly overridden by an ADR.

## Technical Constraints

- **Python-only runtime**  
  All backend modules must be Python 3.11+.

- **SQLite primary database**  
  No external DB dependencies unless added by ADR.

- **Docker-based execution**  
  System must run isolated in containers on Synology NAS.

- **Kraken exchange only (Phase 1–3)**  
  Multi-exchange support deferred to later phases.

## Safety Constraints

- **No module except Harvester may initiate fund transfers.**

- **LLM Strategy Advisor cannot send executable commands**  
  Only produces JSON suggestions.

- **Kraken API keys must exclude withdrawal permissions**  
  (Except Harvester’s dedicated key.)

- **Max balance exposure and daily spend caps mandatory**  
  Bot Core must enforce locally.

## Architectural Constraints

- **Hexagonal architecture is required.**  
- **All modules communicate through ports.**  
- **No module should depend on another’s internal implementation.**

## Operational Constraints

- System must support:
  - Logging every action
  - Replayable market/history data
  - Offline mode for testing