# Command claims and uncertain effects

Commands still require an authorized human decision. A payload cannot be changed
under an existing command ID, an approval cannot have its TTL or confirmer
rewritten, and terminal decisions cannot be reset to approved. Concurrent decisions
race atomically; a losing write returns a conflict instead of replacing the winner.

Live and harvest claim an exact, unexpired approval durably before dispatch.
A claimed command disappears from the approved work queue. If the daemon dies
or fails to save its result, the claim remains visible as `claimed` on the command
watch/history surfaces; restart never silently retries it. The command may have
had no effect, a partial effect, or a completed effect whose receipt was lost.

Before requesting another command, inspect the original command ID, daemon logs,
current engine/open-order state, exchange history and (for a withdrawal) the
proposal's durable transfer reservation. Resolve the observed effect through the
existing operator workflow. Do not clear the claim or reuse its UUID to force a
retry. A new command requires a new human approval. No exactly-once exchange
promise is made, and no automatic reconciliation invents an outcome.

Back up/rehearse upgrades as usual. The schema is additive, but pre-claim binaries
cannot interpret unresolved claims. Do not downgrade active command consumers
until all such claims are reconciled and consumers are stopped. The SQL guards
do not grant table-level security against a process with unrestricted schema access.
