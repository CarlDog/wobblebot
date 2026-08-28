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

## Related external assessments

- **[Ollama repository assessment — 2026-08-27](../reference/ollama-repository-assessment-2026-08-27.md)**
  Evidence-backed review of the official Ollama repository, WobbleBot's existing
  integration, recommended hardening work, rejected non-needs, delivery order, and
  acceptance evidence. This is a dated assessment, not an ADR or a replacement for the
  roadmap.

- **[OpenClaw integration assessment — 2026-08-27](../reference/openclaw-integration-assessment-2026-08-27.md)**
  Source-backed boundary analysis for a possible external-agent integration. It records
  deployment and command-lifecycle gaps while keeping any integration demand-gated and
  read-only first.

- **[NemoClaw repository assessment — 2026-08-28](../reference/nemoclaw-repository-assessment-2026-08-28.md)**
  Static review of NVIDIA's agent-sandbox repository as an engineering pattern source.
  It recommends proportional deployment, supply-chain, test-lane, redaction, and readiness
  improvements while explicitly rejecting NemoClaw as a WobbleBot runtime dependency.

These are evidence records, not ratified decisions or roadmap commitments. Adopted changes
still require the normal ADR and roadmap process.

Each document is small, focused, and authoritative for its topic.  
Cross-links are included where needed to keep information DRY.
