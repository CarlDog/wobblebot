# Security Policy

## Threat model

WobbleBot is a self-hosted micro-trading system that holds Kraken API
credentials and executes real trades against the operator's account.
The two highest-impact failure modes are:

1. **Credential compromise** — a leaked `KRAKEN_READER_API_KEY` can
   expose account data, `KRAKEN_TRADER_API_KEY` can place or cancel
   orders, and `KRAKEN_HARVESTER_API_KEY` can initiate withdrawals.
   Mitigations: separate reader, trader, and Harvester keys (ADR-003);
   Withdraw scope OFF on the trader key and restricted to the Harvester
   key; gitleaks + PII pattern check + author-identity guard in the
   pre-commit hook (`.githooks/pre-commit`); `.env` gitignored.

2. **Bot Core misbehavior with real money** — a logic bug that
   causes runaway placement, missed cancellations, or unexpected
   exposure. Mitigations: hard-capped session loss / runtime / per-coin
   / total / daily-spend limits in `safety:` (enforced in `GridEngine`
   before any `place_order` call). `cli.live` attempts to cancel its
   open orders on handled shutdown, with Kraken's dead-man switch as
   an exchange-side backstop; abnormal exits or exchange failures
   still require operator reconciliation. `cli.preflight` runs one
   engine step against Kraken's `validate=true` endpoint to verify
   config acceptance without spending.

Out of scope: memory-corruption-style attacks against the Python
process itself, supply-chain attacks against `pip` / Kraken (we trust
the ecosystem we depend on), and physical access to the operator's
machine.

## Release and security updates

WobbleBot publishes tagged releases; see
[the latest release](https://github.com/CarlDog/wobblebot/releases/latest).
The `main` branch is active development. This repository has not established a
standing support or backport window for older tags. Each
[security advisory](https://github.com/CarlDog/wobblebot/security/advisories)
identifies its affected and fixed versions; operators should use that advisory
and the corresponding release to determine whether an upgrade is required.

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
- If accepted: fix on `main` as fast as practical and publish a patched
  tagged release when applicable, with a changelog entry citing your
  report (anonymous if you prefer). Publish the Security Advisory after
  the fix is available to affected operators.
- If declined: a written explanation and, if appropriate, a
  suggestion for where the report would be more useful (e.g. an
  upstream Kraken API issue).

Thank you for helping keep WobbleBot operators safe.
