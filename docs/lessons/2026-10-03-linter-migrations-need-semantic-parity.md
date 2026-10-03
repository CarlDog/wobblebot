---
id: 2026-10-03-linter-migrations-need-semantic-parity
title: Linter migration requires semantic coverage and runtime-type review
scope: fleet:python
tags: [ruff, pylint, sqlite, lint, migration, verification]
severity: high
status: documented
date: 2026-10-03
prevention:
  - Compare load-bearing checks with negative controls before removing a linter.
  - Review auto-fixes against actual runtime protocols, not names that resemble dictionaries.
mechanized_by:
  - pyproject.toml retained pylint gate and SQLite Row exclusions
related: [2026-10-03-portable-nonmutating-verification]
---

**What happened** — A Ruff migration dry run showed two material differences.
A used cross-module cycle failed existing pylint R0401 but passed the prescribed
Ruff rules. A reviewed unsafe SIM118 fix changed SQLite Row `.keys()` checks to
membership on the row, which searches values, not column names. Applying it would
silently drop legacy-schema provenance/delivery fields and claimed-command state.
The change was caught in review and restored before delivery; no production
incident or delivered data loss is claimed.

**Why** — Rule counts do not establish equivalent semantic coverage, and SQLite
rows resemble mappings without implementing the same membership protocol.

**The fix / the rule** — ADR-054 records the operator's explicit decision to keep
pylint alongside Ruff and mypy. Existing pylint checks are unchanged. Ruff owns
formatting/imports; precise SQLite Row exclusions preserve `.keys()` semantics.
Tests assert actual Pydantic/SQLite/port exceptions instead of blanket Exception.
Do not add suppressions just to make a migration pass or infer a fleet-wide waiver
from this repository-specific decision.

**Evidence** — The roadmap migration receipt, the retained Make/CI pylint gate,
`ruff-cycle-parity.log` and `ruff-row-membership.log` in the ignored verification
evidence directory, and the existing SQLite command/delivery/provenance tests.
The positive membership control uses a real sqlite3.Row, not a dictionary mock.

**Applicability limits** — This does not require pylint in every Ruff project or
forbid supported fixes. Establish the project's actual contract and preserve it.
No Fleet Kit source was edited; this lesson is available for the parent's separate
harvest process.
