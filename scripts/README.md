# Utility Scripts

## Current

- `install-hooks.sh` / `install-hooks.ps1` — point `core.hooksPath` at
  `.githooks/` so the repo-specific pre-commit hook runs (gitleaks +
  PII pattern check + author-identity guard). Run once per fresh clone.

## Planned (Phase 2+)

- `setup.sh` / `setup.ps1` – First-time setup automation (venv, deps, config)
- `deploy.sh` / `deploy.ps1` – Deployment helpers for Synology NAS
- `backup.sh` / `backup.ps1` – Database and config backup utilities
- `reconcile.py` – Manual reconciliation of exchange state vs. database
- `emergency_stop.py` – Force-stop trading and cancel all open orders

## Usage

Scripts will be documented individually as they are created during later phases.

## Phase Dependencies

Most scripts are planned for Phase 2+ as operational tooling becomes necessary.

## References

- See [Implementation - Operations](../docs/implementation/operations.md) for operational procedures
