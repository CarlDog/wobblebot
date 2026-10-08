# Prepare an isolated deployment

This is an opt-in implementation of ADR-047. It prepares reviewable files and
empty directories only; it does not copy databases, deploy, or start daemons.

```sh
python tools/prepare_isolated_deployment.py --config config/settings.example.yml \
  --profile cpu-only --output /tmp/wobblebot-isolated
```

Use an existing operator settings file and its chosen profile for a real plan.
`--host-root` sets the eventual absolute mount source without writing there.
The output must not exist. The generator validates policy and database ownership,
preserves policy values, and rejects unsupported/missing prompt assets. It writes
resolved `config/settings.yml`, `compose.yml`, and `migration-map.json`; database
paths in the map distinguish container destinations from staged host destinations.
Treat the generated settings as private. The staging directory starts owner-only.

Before a separately authorized migration, stop all affected writers, take and
verify consistent backups with the existing backup tooling, and retain the old
layout. Copy each database to its mapped `state/<role>/` directory using a
consistent SQLite backup or clean shutdown; copying a live main file without
its WAL is unsafe. Preserve ownership and grant the runtime UID/GID (1001) the
required access to the staged directories and files. Verify permissions locally
before starting any service. Do not broadly make financial data world-writable.

The resolved Compose mounts owner directories writable and consumer directories
read-only, including WAL/SHM sidecars. Config is read-only except for tools; logs,
archive and backups have separate grants. Generated services disable automatic
config bootstrap because all required assets are staged already. Start owners
before readers; a missing foreign database is not created by a reader. Maintenance
retains its configured backup/retention targets. Live and harvest never restart
automatically. No root data mount remains in the generated plan.

Review with `docker compose --env-file /path/to/approved.env -f compose.yml
config --quiet`; avoid printing resolved secrets. Rehearse with disposable copies
and no financial credentials before deployment. Confirm backups, WAL reads,
settings one-shot writes, denied writes and restart behavior using the final image.
A read-only reader does not migrate an old schema. Provider observations are
additive; an old database produces unknown provider health until an owner upgrades.

Web now shows observations from the operator's model-list probes, with timestamps
and explicit unknown/stale states. This covers only endpoints the operator probes;
it does not verify inference or advisor-only providers. `schedules.provider_health`
controls publication (zero disables); stale publication cannot remain green.

Rollback is a deliberate stop-and-reconcile procedure. Retain newer databases and
any external financial effects; do not restore an old backup over new activity.
Map consistent current databases back to the prior layout after schema and
application compatibility verification. Directory isolation still leaves multiple
writers to operator.db; it is not table-level command isolation.
