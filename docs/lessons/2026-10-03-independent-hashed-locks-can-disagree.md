---
id: 2026-10-03-independent-hashed-locks-can-disagree
title: Independently hashed runtime and test locks can still resolve different dependencies
scope: ecosystem:python
tags: [pip, lockfile, ci, docker, verification]
severity: high
status: mechanized
date: 2026-10-03
prevention:
  - Resolve the test environment first and constrain the runtime resolution against it.
  - Assert that every runtime dependency version appears in the tested resolution.
mechanized_by:
  - tests/deployment/test_dependency_locks.py
  - docs/implementation/reproducible-builds.md
related: [2026-07-24-npm-version-skew, 2026-07-24-test-engine-newer-than-prod-floor-gives-false-green]
---

**What happened** — N2 independently generated universal hashed runtime and dev
locks. Both installed, the test suite passed, and the runtime image built, but an
exact comparison found runtime `python-dotenv==1.2.4` versus the dev pin `1.2.3`.
The earlier matching-resolution statement was corrected in the roadmap.

**Why** — Hashes verified each resolution's artifacts; they did not relate the two
resolutions. An exact dev dependency constrained only the dev solve. Runtime had
its own otherwise valid newer transitive version.

**The fix / the rule** — Commit `e947267` constrains runtime compilation against
the dev lock and adds a regression check over runtime/test version pairs. The
[documented commands](../implementation/reproducible-builds.md) reproduce that
order. Existing runtime-image receipts retain their original versions; they do
not become evidence for the corrected image without rebuilding.

**Evidence** — [Regression test](../../tests/deployment/test_dependency_locks.py),
[locks](../../requirements.lock), and the N2 correction receipt in the roadmap.
Both full Linux Python suites used the exact dev pin before the correction.

**Applicability limits** — This checks version inclusion for this repository's
current universal resolution. A future lock with different versions under mutually
exclusive platform markers needs marker-aware comparison and actual target tests.
Matching versions/hashes does not establish bit-reproducible application wheels,
Windows execution, hosted CI or production acceptance.
