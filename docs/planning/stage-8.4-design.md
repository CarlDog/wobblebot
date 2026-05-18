# Stage 8.4 — Phase 8 / v1.0 Release Check: Design and Slicing

*Drafted 2026-05-18 at Stage 8.4 kickoff. Living document — actual
slicing may adjust during implementation.*

## What Stage 8.4 delivers

The final stage of Phase 8 — and of pre-v1.0 work entirely. Stage 8.4
flips the project from "feature-complete pending soak" to "tagged
v1.0.0".

Three concerns:

1. **Documentation freeze for v1.0.** A known-limitations document
   that captures the boundary we're shipping (what's explicitly NOT
   in v1.0 and why), and a future-improvements document that lists
   v1.1+ candidate work. Both live under `docs/release/` and are
   updated to reflect *the state as of v1.0.0* — they don't track
   ongoing decisions post-tag.
2. **Pre-1.0 audit.** The one-shot items from
   `~/.claude/rules/phase-end-audit.md`: community standards green
   check, license recognized, author-identity sweep across full git
   history. Plus the wobblebot-specific item: Phase 4 Harvester key
   separation verified (Harvester key has Withdraw scope on, Trade key
   has Withdraw scope off).
3. **The soak itself.** Multi-week operator-driven run under low-risk
   configuration (small per-order USD, conservative spacing, single
   coin, harvester enabled with conservative thresholds). Stage 8.4
   ships the runbook for it; the operator runs it; we resume once
   they report results.

Post-soak, the **release ceremony** flips `[Unreleased]` →
`[1.0.0] - YYYY-MM-DD` in CHANGELOG, writes
`docs/planning/phase-8-summary.md` matching the
`phase-{2,3,4,5,6,7}-summary.md` precedent, and tags `v1.0.0`.

**What Stage 8.4 explicitly does NOT do:**

- Add code. v1.0 is the code that's already in `main` after Stage 8.3
  closed. The soak validates that code; it doesn't iterate on it.
  If the soak surfaces a real defect, that's a separate stage (8.5
  or v1.0.1), not "patch it during 8.4."
- Hardcode a soak duration target in days. The operator decides what
  "enough" looks like based on observed behavior; the runbook
  describes *what to watch for* not *how long to watch*.
- Speculative v1.1 work. Future-improvements doc is a *list*, not a
  commitment. Items there are candidates, not promises.
- New ADRs. The pre-1.0 boundary is set; any new architectural
  decision is a v1.1+ concern.

## Why now

Phase 8's earlier stages (8.0 refactor cleanup, 8.1 reconciliation,
8.2 maintenance worker, 8.3 SQLite pragmas + profile harness) all
landed without real-money cost increase. The codebase has been
behavior-stable since 8.1 closed — every commit since has been
performance, hygiene, or polish. That's the right shape for entering
a soak.

The 2026-05-18 shadow-session shutdown bug fixed in Stage 8.1.B was
the last surfaced behavior issue. If the soak finds anything else,
we want to know before v1.0, not after.

## Why no ADR

Stage 8.4 is a release ceremony, not an architectural change. The
known-limitations doc *captures* prior decisions (single-operator web
auth, no separate banking adapter, etc.) — it doesn't introduce new
ones. If a v1.1 decision wants to revisit those, that's an ADR for
that decision, not for the release.

## Proposed slicing

| Slice | Scope | Risk | Est. |
|-------|-------|------|------|
| **8.4.A — Kickoff** | This commit: `stage-8.4-design.md` + roadmap polish + CHANGELOG kickoff entry. No code. | Low | (this commit) |
| **8.4.B — Known-limitations + future-improvements docs** | `docs/release/v1.0-known-limitations.md` (frozen-at-v1.0 boundary) + `docs/release/v1.0-future-improvements.md` (v1.1+ candidate list). No code. | Low | ~1h |
| **8.4.C — Pre-1.0 one-shot audit** | Per `~/.claude/rules/phase-end-audit.md` pre-1.0 list: community standards check, license recognition verified, author-identity sweep across full history. Plus wobblebot-specific Harvester-key separation verified. Findings surface as a punch list; small fixes (e.g. missing CODE_OF_CONDUCT) ship in focused commits per the global rule's process discipline. | Low-Medium | ~1-2h |
| **8.4.D — Soak runbook** | `docs/release/v1.0-soak-runbook.md` — pre-soak checklist, what to monitor, abort conditions, daily-check questions, what counts as "soak passed". No code; operator-facing document only. | Low | ~1h |
| **8.4.E — Soak** | Operator-driven. Multi-week observation under low-risk config. **NOT in this Claude session.** | (operator) | weeks |
| **8.4.F — Release ceremony** | Post-soak (separate session). `docs/planning/phase-8-summary.md` mirroring `phase-{2,3,4,5,6,7}-summary.md`. CHANGELOG `[Unreleased]` → `[1.0.0] - YYYY-MM-DD`. `pyproject.toml` version bump to `1.0.0`. `git tag v1.0.0` (signed if the operator has a key). | Medium | ~1-2h |

**This session: A + B + C + D.** E is operator-driven; F resumes
when the operator reports soak results.

## Design decisions to ratify

### 1. Soak duration is operator-decided, not Claude-mandated

The runbook describes *what to watch for* (engine reconciliation
results on each startup, no SQLite corruption, no orphaned orders
on Kraken, harvester proposals match expected band semantics,
notifications flow end-to-end via Discord and web UI, etc.). It
deliberately doesn't name a number of days. A two-week soak under
real conditions catches more than a four-week soak under perfect
conditions; the operator is best-positioned to call the gate.

### 2. Low-risk soak configuration ratified in the runbook

The runbook recommends:
- Single coin (BTC/USD) to limit blast radius.
- Conservative `order_size_usd` (low end of the operator's risk
  tolerance — e.g. $1-2 per order).
- Wide spacing (`spacing_percentage` ≥ 1.0%) so fills are
  infrequent.
- Hard caps tuned to the soak balance via `cli/recalibrate`.
- Harvester *enabled* with conservative thresholds so the harvester
  loop is part of the soak, not a Phase 4.5 throwback.

The operator can deviate. The runbook explains the reasoning so
deviations are informed.

### 3. Documentation freeze, not codebase freeze

Code can still change post-soak if the soak surfaces a defect. What
*can't* change post-soak without a separate stage is the v1.0
documentation boundary — known-limitations + future-improvements
describe v1.0 as it exists at tag time. If 8.4.E surfaces a real
defect we fix it as a focused commit, update the v1.0 docs to
reflect the corrected behavior, and *then* tag. The docs are a
snapshot, not a forward commitment.

### 4. Known-limitations doc covers each ADR-deferred decision

Each ADR-rejected or ADR-deferred decision gets one paragraph in
known-limitations:
- ADR-002 — LLM is advisory only (no auto-execute path); v1.1 may
  revisit if the auto-apply gate's bounds prove restrictive.
- ADR-003 — Harvester is the sole transfer authority; no other
  module can move money.
- ADR-004 — No separate banking adapter; Harvester uses Kraken's
  withdrawal API.
- ADR-013 — Confirm-before-execute gate; web UI mutations cross
  `pending_commands` like Discord.
- ADR-016 — Web UI is server-rendered Jinja2 + HTMX (no SPA, no
  Node).
- ADR-017 — Single-operator v1 web auth (bcrypt + session cookie;
  no SSO, no multi-user roles).
- ADR-018 — Engine reconciliation at startup only; no mid-session
  reconciliation; harvester reconciliation deferred to v1.1.

Plus v1.0-specific:
- CryptoCompare 90-day evaluation still due 2026-08-13 per ADR-010
  (deferred from Stage 6.5 close).
- No CI perf regression check (Stage 8.3 decision 8); operator's
  deployment is the canonical measurement surface.
- No remote backup destinations in v1.0 (Stage 8.2 decision; the
  `BackupDestination` Protocol is declared but no S3/rclone variant
  shipped).
- `tools/profile_storage.py` is operator-runnable only; the soak
  doesn't automate latency regression detection.

### 5. Future-improvements doc is grouped by motivation

Three groups:
- **Earned by soak data**: caching layers, async query parallelism,
  batch APIs — speculative until the soak's profile harness output
  justifies them.
- **Earned by operator feedback**: multi-coin defaults, web UI
  read-views the operator wishes existed, Discord command shortcuts
  for repeated workflows.
- **Earned by code review**: Phase 4.5 audit's seven-defense-layer
  Harvester gate could grow an eighth (e.g. cumulative daily total
  visible to operator pre-approve); harvester reconciler;
  remote backup destinations.

Each item gets one paragraph: *what*, *why deferred*, *what would
trigger picking it up*.

### 6. Pre-1.0 audit findings ship in focused commits

Per the global phase-end-audit rule's process discipline. If
community-standards check reveals no CODE_OF_CONDUCT and no
CONTRIBUTING.md, those get separate commits with focused messages
("Add CODE_OF_CONDUCT.md", "Add CONTRIBUTING.md") not a "Pre-1.0
audit cleanup" omnibus.

### 7. Author-identity audit is across all branches + history

The global rule's one-liner
`git log --all --pretty='%ae' | sort -u` shows the full author email
set. The pre-commit hook's author-identity guard catches *future*
commits; the audit catches *past* commits that may have leaked a
personal-domain email before the hook was installed. If any found,
remediation is `git filter-branch` or `git filter-repo` — a
documented `docs/release/v1.0-history-rewrite-notes.md` if the rewrite
is non-trivial.

### 8. Phase 4 Harvester-key separation verified live

ADR-003's load-bearing invariant. The audit asks the operator to:
- Confirm in Kraken UI: Harvester key has Withdraw scope ON.
- Confirm in Kraken UI: Trade key has Withdraw scope OFF.
- Confirm `.env` references `KRAKEN_HARVESTER_API_KEY` /
  `KRAKEN_HARVESTER_API_SECRET` separately from `KRAKEN_TRADE_*`.
- Confirm `cli/harvest` uses the harvester env vars (audit by
  grep, not by trust).

If the operator hasn't yet minted the Harvester key (because no live
withdrawal has run), the audit asks them to mint it before the soak
so the soak window includes harvester operation.

### 9. v1.0.0 tag is annotated, not lightweight

`git tag -a v1.0.0 -m "..."` per git's standard release practice.
Tag message references `docs/planning/phase-8-summary.md`. If the
operator has signing configured, `git tag -s v1.0.0 -m "..."`.

### 10. pyproject.toml version bump in the same commit as the tag

`pyproject.toml` currently shows whatever pre-1.0 version we've been
shipping under. The bump to `1.0.0` and the tag annotation go in
*one* commit so `git show v1.0.0` includes the version-bump diff.

## What's NOT in scope for Stage 8.4

- **Adding tests.** v1.0's test count is what 8.3 closed with (1785
  unit + 29 integration on opt-in). New tests are v1.1+.
- **Adding features the operator asks for during the soak.** Logged
  as future-improvements candidates; not v1.0 work.
- **Refactoring "while we're here."** Same rule as the phase-end
  audit: queue, don't drift.
- **A v1.0.1 plan.** Patch versions exist; planning them
  preemptively is overreach.
- **CI / GitHub Actions setup.** If the operator wants CI eventually,
  v1.1+ concern. The repo runs on local pre-commit + manual `make
  check` today; that's been sufficient.

## Stage close criteria

For 8.4.A (this commit):
1. `docs/planning/stage-8.4-design.md` exists with the slicing above.
2. `docs/planning/roadmap.md` Stage 8.4 entry expanded with
   sub-slice list.
3. `CHANGELOG.md` gains a "Stage 8.4 kickoff" entry under
   `[Unreleased]`.

For 8.4.B (subsequent commit):
1. `docs/release/v1.0-known-limitations.md` exists, covers every
   ADR-deferred decision + the v1.0-specific items.
2. `docs/release/v1.0-future-improvements.md` exists, grouped by
   motivation per decision 5.
3. Both docs cross-link from each other for items that appear on
   both sides (e.g. harvester reconciler is a limitation AND a
   future-improvement).

For 8.4.C (subsequent commit(s)):
1. Community standards check: README, LICENSE, SECURITY,
   CODE_OF_CONDUCT, CONTRIBUTING checked.
2. License recognized by GitHub (badge renders).
3. Author-identity sweep shows no personal-domain emails.
4. Harvester-key separation verified per decision 8.
5. Any missing items added in focused commits.

For 8.4.D (subsequent commit):
1. `docs/release/v1.0-soak-runbook.md` exists.
2. Operator can read it and start the soak without further
   instruction from this conversation.

For 8.4.E:
1. Operator runs the soak. Out of this session's scope.

For 8.4.F (post-soak, separate session):
1. `docs/planning/phase-8-summary.md` written.
2. `pyproject.toml` version bumped to `1.0.0`.
3. CHANGELOG `[Unreleased]` heading replaced with `[1.0.0] - <date>`.
4. `git tag -a v1.0.0` (or `-s` if signing).
5. Roadmap Phase 8 + Stage 8.4 receipts marked ✅ with date.

## Test plan

No new tests in 8.4.A, 8.4.B, or 8.4.D — these are documentation
deliverables. 8.4.C may add or modify hook-level tests if the audit
finds gaps (e.g. if author-identity guard is missing checks). 8.4.E
is the soak itself. 8.4.F doesn't add tests either.

**Lint gates** (continuous through every sub-slice):
- pylint **10.00/10** maintained.
- mypy clean across all src files.
- black + isort clean across src/ + tests/.
- All currently-passing unit tests stay green.
