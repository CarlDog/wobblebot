# N1 capability inventory and implementation seam

Work item: `CarlDog/wobblebot:product-completion`. Read-only implementation
inventory against source `9a42a79`; this is preparation for N1, not an accepted
ADR amendment, production acceptance or deployment instruction. Status and
verification belong in the [roadmap](roadmap.md#cloud-product-completion-verification).
The binding requirement is [closeout plan §5](2.0-closeout-and-2.1-entry-plan.md#5-reconciled-21-proposal),
with accepted ADR-041's current grants and explicitly deferred mount split.

## Current consumers

Database names below are logical roles, not a mandate to rename existing files.
`RW` describes required application effects; `R` describes read-only consumers
whose current startup code often nevertheless opens a writable adapter.
All paths come from resolved settings, including profiles and CLI overrides.

| Process | Required database access | Source seams and other authority |
| --- | --- | --- |
| live | live RW; operator RW; optional observe R | `cli/live.py`: `_main_async`, `_open_observe_storage`; operator commands, notifications and heartbeat require writes; trader key only |
| observe | observe RW; optional operator RW | `cli/observe.py`: startup and auth-failure notification wiring; reader key |
| news | news RW; optional operator RW | `cli/news.py`: `_main_async`, poll loop/notifications; optional news-provider credential, no Kraken key |
| advise | advise RW; observe/news R; optional orders/live R; operator RW when cloud ledger wired | `cli/advise.py`: `_main_async`, `_open_orders_storage`, `_CloudWiring`; selected advisor-provider credentials |
| harvest | harvest RW; optional operator RW | `cli/harvest.py`: persistence, notification and approved-command wiring; withdrawal key is exclusive to this process |
| operator | operator RW; live/observe/advise/news/harvest R | `cli/operator.py`: `_main_async`, `OperatorService`, confirmation callback; Discord and selected assistant-provider credentials |
| web | operator RW; live/observe/advise/news/harvest R | `cli/web.py`: `_open_storage`, `_open_optional_dbs`, `_bootstrap_app`; users/sessions/queued approvals require operator writes; web session secret |
| maintenance | configured maintenance targets RW; backup/archive destinations RW | `cli/maintenance.py` and maintenance services: VACUUM, prune, ledger sync, verification, reporting/heartbeat; reader key only |
| tools | Task-dependent explicit grants; settings RW only for authorized one-shot writers | `cli/apply`, `cli/recalibrate`, history importer/Auditor/diagnostics; existing ephemeral service never receives withdrawal key |

This is a minimum starting inventory, not proof that an arbitrary configured
path belongs to the listed role. Before narrowing grants, trace each actual
writer/consumer and test custom/profile paths. In particular:

- All daemons currently receive the whole `/app/data` tree from the shared Compose
  anchor. Read-only config is already implemented for daemons; `tools` is the
  authorized settings writer. Do not reintroduce write access to fix startup.
- Cross-database readers in `cli/advise`, `cli/operator`, `cli/live` and `cli/web`
  frequently construct `SQLiteStorageAdapter(path)` without `read_only=True`.
  The adapter's read-only path already uses SQLite `mode=ro`, skips migrations
  and refuses missing files. Narrow filesystem grants and startup adapters together.
- SQLite WAL/SHM files live beside each database. A bind mount of only the main
  database file is not a sufficient isolation design. Directory-level mounts
  must be rehearsed with a concurrent writer and real read-only filesystem mounts.
  Never use SQLite `immutable=1` for databases that are changing under a writer.
- Every participating daemon still legitimately writes `operator.db` for some
  combination of heartbeat, notifications, costs or commands. File-level mount
  isolation cannot restrict those writers to particular tables. Preserve this
  explicit residual for N3; do not claim semantic command isolation from mount tests.
- Logs, archive and backups are also under the shared data tree. Narrowing only
  database grants while leaving the root tree writable would defeat the change.
  Resolve logging paths, maintenance target lists and backup ownership together.

## Provider health without web provider keys

The existing web startup builds `LLMHealthChecker` with `build_llm_endpoints`
using Anthropic/OpenAI/Google keys and public Ollama configuration.
`services/llm_health.py` performs non-billable model-list probes and caches
`LLMEndpointHealth` results. Simply deleting the Compose keys would incorrectly
report a configured provider as absent.

N1 therefore needs a producer that already legitimately owns the relevant
credentials, a sanitized persisted result contract, and a web reader that
reports unknown/stale evidence. Do not grant extra provider keys to a new process
just to recreate the web's old health card. Advisor and operator may use different
providers: define producer coverage and aggregation explicitly. A missing producer,
dead daemon, failed probe, never-configured provider and stale successful result
are distinct states. Health data must never contain headers, keys or raw config.
The existing `StoragePort` and domain health types are candidate seams; decide
ownership before allocating an unused ADR number or changing schema.

## Required implementation and verification sequence

1. Resolve whether local N1 development may proceed while formal 2.0 closure
   remains pending. This is distinct from accepting missing production evidence.
2. Reconcile the consumer matrix with runtime configuration and document the
   ADR-041 ownership amendment; retain the shared-operator-store limitation.
3. Design the explicit path/mount layout, operator-controlled migration and
   rollback procedure. Preserve existing files and post-backup effects; no live
   migration is authorized by this inventory.
4. Convert actual foreign readers to read-only opens with defined absent/old-schema
   behavior, then narrow mounts/log/config grants and provider credentials.
5. Implement persisted health production/consumption and unknown/stale rendering.
6. Run contract tests, copied-config tests, upgrade/downgrade tests and an isolated
   Compose rehearsal. Exercise legitimate writes, denied writes, concurrent WAL
   reads, all configured maintenance tasks, settings one-shot writes and restart.
   Static YAML assertions alone do not satisfy the N1 gate.

The current cloud environment can inspect and test source and run downloaded
container images, but the product Dockerfile build is blocked on Debian package
DNS. This limits the final image rehearsal, not read-only design preparation.
