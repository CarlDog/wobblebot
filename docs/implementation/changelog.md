# Changelog

All notable changes to WobbleBot will be documented in this file.  This project uses a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format, with versions aligned to semantic versioning.  Each entry documents additions, changes, fixes, and removals.

## [Unreleased]

### Added
- New documentation scaffolding under `/docs/` covering architecture, planning, and implementation.
- Initial repository scaffolding including domain models and port interfaces (Phase 1).
- Basic persistence layer using SQLite and logging setup.
- Kraken mock adapter for paper‑trading simulations.

### Changed
- N/A — initial version, nothing changed yet.

### Removed
- N/A — initial version, nothing removed.

## [v1.0.0] – TBD

### Added
- Implemented deterministic micro-grid trading engine with configurable grids and safety caps.
- Kraken exchange adapter integrated for live trading with support for paper mode and tiny order sizes.
- Multi‑asset trading support with per‑asset and global exposure limits.
- Strategy Advisor module integrating a local LLM, with JSON output and optional auto‑apply under strict bounds.
- Harvester module for guarded withdrawals from Kraken to bank accounts, with passive and active modes (uses Kraken withdrawal API per ADR-004).
- Centralized Orchestrator coordinating Bot Core, Advisor, and Harvester modules.
- Data Collector for live market data and volatility metrics.
- Observability layer: structured logging, metrics, and dashboard integration.
- Deployment via Docker Compose on Synology NAS or locally.
- Comprehensive documentation: architecture, planning, implementation, operator and operations guides.

### Changed
- N/A — first release; any changes from unreleased will be noted here.

### Removed
- N/A — first release.

### Known Issues
- Restart / reconciliation logic is basic; manual checks are required after restarts until Phase 5 introduces robust reconciliation.
- Advisor JSON schema is draft; future schema versions may be incompatible with earlier ones.
- Automated bank deposits (bank → Kraken) are not supported in v1.0.0.
