# Configuration Files

**Status:** Placeholder for Phase 1.4+

This directory contains runtime configuration files for WobbleBot. Configuration is loaded from YAML files with environment variable overrides.

## Structure

```
config/
  settings.example.yml   # Template configuration with documentation
  settings.yml           # Actual configuration (gitignored, created by operator)
```

## Configuration Approach

- **YAML files** define grid parameters, safety caps, coin whitelists, etc.
- **Environment variables** (`.env`) provide secrets (API keys) and deployment settings
- **Pydantic models** (`src/wobblebot/config/`) validate and provide type-safe access

## Usage

1. Copy `settings.example.yml` to `settings.yml`
2. Adjust grid parameters, safety caps, and coin settings
3. Never commit `settings.yml` (it's gitignored)

## Phase Dependencies

Configuration loading is implemented in **Phase 1, Stage 1.3** (Storage & Logging Backbone).

## References

- See [Architecture - Components](../docs/architecture/architecture-components.md) for config module design
- See [Implementation - Module Specs](../docs/implementation/module-specs.md) for config schemas
