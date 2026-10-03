# Project lessons for fleet harvest

These records follow Fleet Kit's lesson format from source commit
`990747556e73d874d76d4ab6d2212460eabe7652`. They are project evidence, not
unpublished edits to the separate Fleet Kit checkout. The parent coordinates
fleet incorporation. Canonical verification receipts remain in the
[roadmap](../planning/roadmap.md#cloud-product-completion-verification).

| Record | Proposed fleet handling |
| --- | --- |
| [Independent hashed locks can disagree](2026-10-03-independent-hashed-locks-can-disagree.md) | New Python-specific lesson, related to existing npm-version-skew and test-engine-newer-than-prod-floor lessons. |
| [Copied file modes can break non-root images](2026-10-03-copied-modes-break-nonroot-images.md) | Specific Docker lesson or concrete extension of verify-docker-base-image-claims-with-a-real-build. |
| [Verification must be portable and non-mutating](2026-10-03-portable-nonmutating-verification.md) | New build-tool lesson; keep actual Windows verification limits explicit. |

## Evidence to extend existing lessons, not duplicate them

- **Retry only idempotent operations / honor Retry-After.** N3's synthetic
  crash-boundary tests show that a sent Discord message with a lost durable
  receipt is indistinguishable from an abandoned pre-send claim. Durable claims,
  visible uncertainty and no blind resend prevent duplicate attempts; safe
  connection/429 rejection retries are bounded, and provider backoff persists
  across sender restart. See [tests](../../tests/services/test_delivery.py),
  [REST fixtures](../../tests/adapters/test_discord_delivery.py), ADR-049/050 and
  commit `017fca4`. This is verified synthetic behavior, not a newly observed
  production Discord incident or an exactly-once guarantee.
- **Verify against real CI / platform-skipped tests.** Linux Python 3.13/3.14,
  a built image and synthetic Compose grants do not establish Windows, hosted CI,
  deployed NAS configuration or real exchange access. Those limits remain in the
  roadmap. Git transport working while API calls fail does not verify remote
  repository metadata or workflow runs.
- **Audit unknowns remain unknown.** The actual Fleet audit exited 1 because
  PY-01 failed and also reported remote metadata UNKNOWN and unassessed NA rows.
  There was no observed successful-exit false-green incident here. Preserve the
  existing audit skill's rule to read per-check states and manual coverage instead
  of inventing a new incident or treating exit status as complete conformance.

No global memory, OpenChronicle project, remote issue or Fleet Kit source record
was modified by this capture. Future harvest should check for intervening lessons
before adding a new fleet file.
