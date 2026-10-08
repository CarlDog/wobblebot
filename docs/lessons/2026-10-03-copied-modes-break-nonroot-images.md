---
id: 2026-10-03-copied-modes-break-nonroot-images
title: A restrictive checkout can produce an image whose non-root user cannot read its entrypoint
scope: fleet:docker
tags: [docker, permissions, non-root, verification]
severity: high
status: mechanized
date: 2026-10-03
prevention:
  - Set intentional runtime readability and entrypoint modes in the Dockerfile.
  - Run the actual entrypoint and installed application as the declared image user.
mechanized_by:
  - docker/Dockerfile
related: [2026-08-13-verify-docker-base-image-claims-with-a-real-build]
---

**What happened** — The locked product image built successfully, but its first
normal non-root sandbox invocation failed with `cannot open ...wobblebot-entrypoint:
Permission denied`. A separate base-image wheel smoke had passed earlier.

**Why** — The source checkout had restrictive file modes. `COPY` retained them,
and `chmod +x` added execute permission without giving the non-root shell read
permission. Baked defaults/tools also needed explicit readability. Build steps ran
as root and therefore did not exercise that restriction.

**The fix / the rule** — Commit `5237e2c` sets the entrypoint to 755, the public
runtime lock to 644, and baked defaults/tools readable with directory traversal.
The actual image then ran its normal entrypoint and sandbox under its declared
non-root user with network disabled. Separate generated Compose runs tested grants
under UID 1001. The [Dockerfile](../../docker/Dockerfile) preserves the fix.

**Evidence** — N2 image failure/rebuild/sandbox receipts in the roadmap, with exact
image IDs and ignored logs. The successful smoke persisted two synthetic trades;
it did not send any real order.

**Applicability limits** — Apply explicit permissions only to intended public
image content. Never make mounted operator secrets or financial data broadly
readable/writable to fix an image defect. Read-only bind mounts and host UID/GID
permissions still need their own verification; a base-image install smoke cannot
validate the final image's user, entrypoint, defaults or mount layout.
