# Changelog

All notable changes to WobbleBot will be documented in this file.  This project uses a modified [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format, with versions aligned to semantic versioning.  Each entry documents additions, changes, fixes, and removals.

## [Unreleased]

### Added
- Phase 1.1 — Repository scaffolding, `pyproject.toml`, dev tooling (black/isort/mypy/pytest), VS Code workspace.
- Phase 1.2 — Domain models (`Order`, `Trade`, `Balance`) and value objects (`Symbol`, `Price`, `Amount`, `OrderSide`, `Timestamp`); six abstract ports (`ExchangePort`, `StoragePort`, `AdvisorPort`, `HarvesterPort`, `NotifierPort`, `DataCollectorPort`); ADR-005 alignment with Kraken vocabulary.
- Phase 1.3 (storage half) — `SQLiteStorageAdapter` implementing `StoragePort` via `aiosqlite` with Decimal-as-TEXT precision preservation, transaction rollback on partial-write failure, dual-ID UPSERT on `orders`, and append-only balance-snapshot history.
- Pre-commit hook (`.githooks/pre-commit`) with gitleaks + PII pattern check + author-identity guard.

### Changed
- Domain exception signatures take `Decimal` (was `float`), preventing precision loss when reporting balance violations.
- `Order.mark_closed` replaced by `Order.record_fill(cumulative_amount)` — partial fills now correctly keep `status='open'` until full fill; matches Kraken `vol_exec` semantics.
- `Timestamp` normalizes any tz-aware input to UTC so ISO 8601 string ordering matches chronological ordering (relied on by the SQLite adapter's `ORDER BY`).

### Pending in Phase 1.3
- Logging backbone — stdlib `logging` configuration, structured (JSON) output when deployed, plain-text in dev.

## [v1.0.0] – TBD

### Added
- Implemented deterministic micro-grid trading engine with configurable grids and safety caps.
- Kraken exchange adapter integrated for live trading with support for paper mode and tiny order sizes.
- Multi-asset trading support with per-asset and global exposure limits.
- Strategy Advisor module integrating a local LLM, with JSON output and optional auto-apply under strict bounds.
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
- Restart / reconciliation logic is basic; manual checks are required after restarts until Phase 5 introduces robust reconciliation.
- Advisor JSON schema is draft; future schema versions may be incompatible with earlier ones.
- Automated bank deposits (bank → Kraken) are not supported in v1.0.0.
