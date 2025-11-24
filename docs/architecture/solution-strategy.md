# Solution Strategy

## Architectural Pattern

WobbleBot uses **Hexagonal Architecture (Ports & Adapters)** to ensure:

- Separation between business logic and I/O  
- Replaceable adapters (e.g., Kraken → Coinbase someday)  
- Deterministic unit testing with fake ports  
- Safe isolation of money-moving components

## Design Rationale

1. **Determinism:**  
   All behavior must be testable and reproducible.  
   Core engine logic never calls external systems directly.

2. **Safety:**  
   No single module has full control over funds or logic.  
   LLM is advisory-only by design.

3. **Extensibility:**  
   New modules can be added without cascading refactors.  
   Strategy Advisor and Harvester are isolated domains.

4. **Observability:**  
   Every action is logged, traceable, and inspectable.  
   Architecture supports Grafana or custom dashboards.

## Core Decisions (brief)

- Use **Python** for all modules.  
- Use **SQLite** for persistence (Phase 1–2).  
- Use **Docker Compose** for deployment.  
- Strategy Advisor uses **LLM through a Port Interface**, not direct coupling.  
- Harvester uses dedicated **banking adapter** with strict guards.