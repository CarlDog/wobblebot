# Security Policy

## Threat model

WobbleBot is a self-hosted micro-trading system that holds Kraken API
credentials and executes real trades against the operator's account.
The two highest-impact failure modes are:

1. **Credential compromise** — a leaked `KRAKEN_READER_API_KEY` or
   `KRAKEN_TRADER_API_KEY` lets an attacker read balances or place
   orders on the operator's behalf. Mitigations: separate read-only
   and trade keys (per ADR-003), withdraw scope OFF on the trade key
   (the future Phase 4 Harvester key gets that scope alone), gitleaks
   + PII pattern check + author-identity guard in the pre-commit
   hook (`.githooks/pre-commit`), `.env` always gitignored.

2. **Bot Core misbehavior with real money** — a logic bug that
   causes runaway placement, missed cancellations, or unexpected
   exposure. Mitigations: hard-capped session loss / runtime / per-coin
   / total / daily-spend limits in `safety:` (enforced in `GridEngine`
   before any `place_order` call), cleanup discipline (every CLI
   cancels open orders in a `finally` block), `cli/preflight` runs
   ONE engine step against Kraken's `validate=true` endpoint to
   verify config acceptance without spending.

Out of scope: memory-corruption-style attacks against the Python
process itself, supply-chain attacks against `pip` / Kraken (we trust
the ecosystem we depend on), and physical access to the operator's
machine.

## Supported versions

WobbleBot is pre-v1.0.0 (see the [roadmap](docs/planning/roadmap.md)).
Until a tagged release exists, the **`main` branch** is the only
supported version. Security fixes will land on `main` immediately;
the changelog will note them prominently.

| Version | Supported           |
| ------- | ------------------- |
| `main`  | ✅ active development |
| tagged releases | not yet (planned for Phase 5 / v1.0.0 per the roadmap) |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security
vulnerabilities.** Public disclosure before a fix is in place can
expose other operators running the same code.

Preferred channel: **[GitHub Security Advisories](https://github.com/CarlDog/wobblebot/security/advisories/new)**.
This creates a private discussion visible only to the maintainer
and lets you collaborate on a fix before public disclosure.

If for some reason you cannot use GitHub Security Advisories, open
an issue titled "Security: please contact me privately" without
disclosing details, and the maintainer will reach out via the
contact information on your GitHub profile.

### What to include

- A clear description of the vulnerability and its impact.
- Steps to reproduce, including the affected commit SHA or branch.
- If known, your suggested fix or mitigation.

### What to expect

- **Acknowledgement** within 7 days. (This is a hobby project — the
  maintainer is solo. Faster response is likely but not guaranteed.)
- **Triage decision** (accept / decline) within 14 days, with
  reasoning either way.
- If accepted: fix on `main` as fast as practical, with a changelog
  entry citing your report (anonymous if you prefer). The Security
  Advisory becomes public after the fix is merged.
- If declined: a written explanation and, if appropriate, a
  suggestion for where the report would be more useful (e.g. an
  upstream Kraken API issue).

Thank you for helping keep WobbleBot operators safe.
