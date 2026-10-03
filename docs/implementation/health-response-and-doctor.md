# Independent health response and diagnostics

The opt-in `delivery` process observes daemon freshness independently of
`operator`. It reads observer/advisor stores and operator heartbeats, queues a
notification when a configured daemon changes state, and sends through the
shared durable outbox. Run it under an external process supervisor on the intended
host; this repository does not install one or start it automatically.

`delivery.observe_db` and `delivery.advise_db` are read-only sources. The isolated
deployment generator supplies their read-only directory grants. Keep
`delivery.operator_db` identical to `operator.operator_db`. `schedules.health_response`
defaults to `30s`; `0` disables observation without disabling delivery. Freshness
thresholds derive from the configured daemon cadences plus the existing slack.
A never-observed daemon receives one threshold of startup grace. A known stale
heartbeat alerts immediately, including when operator is dead or wedged.

Transitions and notifications commit together. Persistent transition state
suppresses duplicate alerts across restarts and simultaneous observers. First
healthy observations do not page; recovery after a bad state does. Future-dated
observations are unknown, not proof of health. A health-task failure terminates
the delivery process with exit 1 for external supervision. Observation and sends
have separate tasks and bounded waits.

There is no restart authority, Docker socket, command execution, or financial key.
All daemons, especially live, harvest and tools, are page-only. A failed delivery
process, shared host/database failure, revoked Discord credentials, or unavailable
network can still silence this path: external host monitoring remains necessary.
No test fixture is evidence of a real Discord or NAS acceptance run.

## Read-only doctor

```bash
python -m wobblebot.cli.doctor --config config/settings.yml --profile cpu-only
python -m wobblebot.cli.doctor --config config/settings.yml --profile cpu-only --json
```

Doctor loads configuration and opens existing databases read-only. It does not
create/migrate stores, load credentials, probe providers, repair claims, change
prices/settings, or send messages. JSON schema version 1 contains sanitized build,
lock and resolved-config fingerprints plus findings with stable codes, status,
summary and evidence. Boot and doctor hash the same validated configuration.
Missing/old/unreadable stores, absent provider observations and future/stale
heartbeats remain unknown or warning. Unresolved command claims and delivery
outcomes are bounded to the oldest matching records rather than hidden behind
recent successful rows. Disk free bytes are observed without inventing a G1
threshold or historical baseline.

Exit 0 means all returned findings are healthy; 1 means warning/unknown; 2 means
invalid configuration. A healthy result describes observed evidence, not a
financial safety certificate. Reconcile uncertain external effects using the
command/delivery guides; doctor never retries them.
