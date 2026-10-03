# Independent notification delivery

`cli.delivery` drains persisted alerts without running the operator's Gateway or
LLM. It uses `delivery.operator_db`, which must resolve to `operator.operator_db`,
and `operator.auth.outbound_channel_id`, which must be authorized by the operator's
channel allowlist. It accepts only the Discord bot token. The `delivery` Compose
profile is opt-in; starting it requires the usual separate deployment authorization.
`schedules.delivery_poll` controls the daemon cadence. The isolation generator
includes its operator-only database grant and separate log directory when configured.

Both operator and delivery use durable outbox claims. A message receipt ID and the
forwarded flag are saved together. Retries are bounded and apply only to known
pre-send connection failures or explicit rate-limit rejection. Discord's
[Retry-After contract](https://docs.discord.com/developers/topics/rate-limits)
is preserved in persistent shared backoff; there is no hard-coded vendor quota.

A crash or lost receipt leaves an uncertain outcome after the send lease expires.
No automatic resend occurs because Discord may already have received it. Inspect
the channel and daemon evidence before requesting another send. `/notifications`
shows attempts, delivery state and receipt IDs independently of human read status.
A failed/uncertain row is visible rather than retried indefinitely.

Stop old pre-outbox sender binaries during migration. They do not know the shared
claim protocol. No exactly-once guarantee is made. The outbox protects saved alerts;
a producer that crashes before saving an event still needs its own event-specific
reconciliation. Independent health observation builds on this sender in N4.
