# Advisor budget sessions and recovery (#184)

ADR-056 defines an advisor session as one scheduled sweep across every configured
symbol. The existing session amount is shared by all model seats, fallbacks and
the optional gremlin. The tracker resets before the next sweep, after all expert
calls from the previous evaluation have settled. No amount is increased.

The independent daily amount remains a sliding 24-hour limit over the persisted
operator cost ledger, shared across roles and providers. Sweep resets and process
restarts do not clear it. Keep production's existing daily limit and enforcement
setting. A session or daily denial still prevents the outbound request.

## Evidence and monitoring

Denied attempts now write `llm_calls` rows with `success=0`, zero tokens/cost and
`error_kind=cost_cap_session` or `cost_cap_daily`. These rows represent requests
that were never sent. Their role, model, provider and trace identify the affected
route. They cannot inflate the spend total.

Three consecutive cost denials for one advisor role within the last 24 hours
mark a fresh advisor `degraded`. The Docker daemon probe exits 1; the dashboard
and legacy operator heartbeat alerts use the same classification. The mainline
doctor and independent observer also report it. A success in that role clears
its streak; operator chat and another seat's success cannot clear it. Genuine
heuristic guard decisions do not manufacture denial records. A configured cloud
advisor with an unreadable ledger has unknown health, not a healthy budget claim.

The health signal does not raise a limit, restart the daemon or affect trading.
Docker `unhealthy` alone does not trigger `restart: unless-stopped`; the alert
path must be running to deliver notifications. Existing suggestion-freshness
checks continue to detect a stale or stopped daemon.

## Bounded production rollout

1. Read the actual running advisor image ID, immutable revision label, container
   command/profile, mounts and private configuration. The October 28 incident
   receipt identifies `sha-c9c6404` / 2.0.13; that is a backport starting point,
   not proof of today's deployed revision. Mainline contains unrelated 2.1 alpha
   changes and is not an automatic production upgrade candidate.
2. Qualify the narrow hotfix against the observed production source. Retain
   existing private/NAS acceptance gates and independent review. Verify the
   published image digest/revision; successful PR tests alone do not prove image
   publication or deployment. No dependency or schema migration is needed by
   this correction.
3. Record existing image/configuration references and take SQLite-aware backups
   of affected stores. Preserve the operator's daily/session amounts, model
   choices, schedules, risk limits and enforcement switch.
4. Update only the approved advisor/monitor service images. The advisor needs the
   corrected session loop, denial writer and Docker probe. Operator/dashboard
   images need the shared health classifier to display and alert on degradation.
   Do not recreate the live trader or harvester as part of this repair.
5. Read back each updated container's image revision and health result. Confirm
   a complete real scheduled sweep writes LLM recommendations, then confirm the
   next sweep resumes with fresh session headroom. Observe the existing cost
   ledger across the boundary. Do not raise limits or make extra paid probes to
   manufacture a cap test; use the offline denial/recovery fixtures.
6. If rollback is necessary, restore the previous approved image references and
   configuration. Preserve the current ledgers; do not replace databases with
   older copies over subsequent trading/financial activity. A rollback also
   restores the old uptime-cap defect and therefore is not a permanent repair.

Until container identity and post-rollout evidence are captured, report the fix
as code-validated and pending deployment. A container restart alone does not
establish that the recurring defect is fixed.
