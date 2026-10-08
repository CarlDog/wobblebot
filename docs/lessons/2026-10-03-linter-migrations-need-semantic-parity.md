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
  - tests/adapters/test_sqlite_row_membership.py
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
The committed `tests/adapters/test_sqlite_row_membership.py` regression uses real
in-memory `sqlite3.Row` values and the production `row_to_transfer_result` mapper
in `src/wobblebot/adapters/sqlite_storage_rowmap.py`. Modern rows must preserve
reserved/rejected state even though the column name is absent from their values;
a legacy row deliberately stores `submission_state` as a value in another column
and must still take the missing-column fallback. Explicit opposing key/value
membership assertions preserve both negative controls. Replacing the production
key check with value membership fails all three cases. The SIM118 exclusions in
`pyproject.toml` cover only the two SQLite adapter files with these row checks;
they do not disable the rule across ordinary dictionaries or other modules.
The regression's intentional key-membership assertion has one explanatory
`noqa: SIM118`; its value-membership assertion remains unsuppressed. An isolated
copy of the production mapper with only that guard mutated failed all three
tests (`sqlite-row-mutation.log`), without changing production source.

**Applicability limits** — This does not require pylint in every Ruff project or
forbid supported fixes. Establish the project's actual contract and preserve it.
No Fleet Kit source was edited; this lesson is available for the parent's separate
harvest process.
