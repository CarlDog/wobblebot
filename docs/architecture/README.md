# WobbleBot – Architecture Documentation

This folder contains the high-level system architecture for WobbleBot.  
Each document here is a single source of truth for one architectural aspect.

## Contents

- **architecture-intro.md**  
  High-level goals, purpose, and architectural vision for WobbleBot.

- **constraints.md**  
  Hard limitations, mandatory rules, and non-negotiable decisions that shape the system.

- **context.md**  
  System boundary, external actors, and environment context diagram with descriptions.

- **solution-strategy.md**  
  Justification of chosen patterns: hexagonal architecture, modular isolation, deterministic core, LLM isolation, etc.

- **architecture-components.md**  
  The building blocks of WobbleBot: Bot Core, Kraken Adapter, Strategy Advisor, Harvester, Orchestrator, Storage, etc.

- **runtime-view.md**  
  How components interact during operation — example sequence flows for trading cycles, harvesting, and advisory interaction.

- **deployment.md**  
  Docker architecture, networking layout, volumes, runtime environment, and NAS-specific constraints.

- **cross-cutting.md**  
  Logging, security, API boundaries, data validation, safety constraints, rate limiting, and error handling.

- **decisions.md**  
  Architecture Decision Records (ADR) summarizing major decisions and the rationale behind them.

- **quality-attributes.md**  
  Key quality goals (safety, determinism, modularity, observability) and how the design satisfies them.

- **risks.md**  
  Current architectural risks and mitigations.

- **glossary.md**  
  Domain-specific terms used throughout WobbleBot’s architecture.

Each document is small, focused, and authoritative for its topic.  
Cross-links are included where needed to keep information DRY.