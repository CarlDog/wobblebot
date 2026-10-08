---
id: 2026-10-03-portable-nonmutating-verification
title: A verification target must select the platform interpreter and leave reviewed source unchanged
scope: fleet:build-tools
tags: [make, python, portability, formatting, verification]
severity: medium
status: mechanized
date: 2026-10-03
prevention:
  - Select the checkout virtual-environment interpreter for the host platform and preserve explicit overrides.
  - Invoke pip through that interpreter and use formatter check modes in verification targets.
mechanized_by:
  - Makefile
related: [2026-07-24-verify-against-real-ci, 2026-07-24-platform-skipped-tests-are-a-false-green]
---

**What happened** — The initial Linux `make format-check` failed with exit 127
because it selected `.venv/Scripts/python.exe`. The `check` recipe also depended
on the formatting target that rewrote source rather than only checking it.

**Why** — A Windows-specific development assumption was embedded in the common
entry point. Formatting and verification shared a mutating dependency, so a gate
could test different source than the source that entered it.

**The fix / the rule** — Commit `e7ab18a` selects the Windows or POSIX venv path,
retains a command-line interpreter override, uses `python -m pip`, and makes
`check` depend on non-mutating formatter checks. The separate formatting target
remains available as an explicit editing operation.

**Evidence** — The [Makefile](../../Makefile), contributor documentation and D1
roadmap receipt. A disposable deliberately unformatted source made the gate fail
without changing its bytes. Dry runs confirmed explicit interpreter overrides and
a simulated Windows venv path selection; the actual Linux gate passed afterward.

**Applicability limits** — Simulated path selection is not Windows execution.
Shell utilities and platform-specific dependencies still require the real hosted
Windows matrix. This lesson does not prohibit intentional formatting commands;
it separates their editing purpose from verification of the candidate under review.
