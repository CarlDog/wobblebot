# Operations & Maintenance Guide

> **Status: forward-looking design doc.** Describes the Phase 2+ target
> system. None of the commands, configs, schemas, or endpoints below exist
> yet. Current code (Phase 1.3) runs only via the test suite. Track real
> progress in [docs/planning/roadmap.md](../planning/roadmap.md).

This document explains how to run, monitor, and maintain WobbleBot on a day‑to‑day basis.  Think of it as the operations playbook for keeping the bot healthy and safe in production.

## Starting & Stopping

Use Docker Compose to manage the WobbleBot services:

- **Start**

  ```bash
  docker compose -f docker/docker-compose.yml up -d
  ```

  This will build images if necessary and then run containers in the background.  Use `--build` to force a rebuild.

- **Stop**

  ```bash
  docker compose -f docker/docker-compose.yml down
  ```

  This stops and removes containers but preserves volumes.  Add `--volumes` only if you intentionally want to drop the database and logs.

- **View Logs**

  Tail logs for a specific service (e.g., the core) with:

  ```bash
  docker compose logs -f wobblebot-core
  ```

  Adjust the service name (`wobblebot-core`, `llm-service`, `harvester`, etc.) as needed.  Use `--tail` to limit output.

## Monitoring

Keep an eye on several aspects of the system:

- **Health of the core loop** – Are trading cycles executing on schedule?  Look for abnormal delays or repeated failures.
- **Exchange connectivity** – Check for authentication errors or rate‑limit violations in the Kraken adapter logs.
- **Harvester actions** – Review transfer proposals and executions (if active) and ensure they align with configured thresholds.
- **Advisor output** – Spot‑check LLM suggestions for sanity.  Ensure auto‑applied changes fall within safe bounds.  Investigate any suggestions that seem anomalous.
- **Resource usage** – On a Synology NAS, monitor CPU and memory consumption of the Docker containers.  If the LLM model is large, ensure it does not starve other services.

Optional: forward metrics into Prometheus and visualize them in Grafana.  At minimum, ensure high‑level metrics (cycles per hour, open orders, P&L, harvester transfers) are exposed for dashboards.

## Backups and Data Retention

WobbleBot persists state in a local SQLite database.  Protect this data:

- **Database Backups** – Copy the DB file from the mounted volume on a regular schedule (e.g., daily).  For example:

  ```bash
  cp /volume1/docker/wobblebot/db/wobblebot.sqlite /path/to/backups/wobblebot-$(date +%Y%m%d).sqlite
  ```

  Keep multiple generations of backups (daily for recent days, weekly for older ones) and verify they can be restored.
- **Log Rotation** – Docker will buffer container stdout.  Configure the Docker logging driver with a maximum size and rotation policy or periodically rotate the logs under `/volume1/docker/wobblebot/logs` yourself.
- **Data Retention** – Over time, the database may accumulate high‑frequency market snapshots or historical metrics.  Implement a retention or archival policy to prune old data or move it to cold storage.  Ensure that pruning does not remove data needed for tax or compliance purposes.

## Incidents & Recovery

### Crash or Unexpected Stop

1. Inspect logs for stack traces or fatal error messages.
2. Identify and fix the underlying issue (e.g., misconfiguration, external outage, unhandled exception).
3. Restart services with `docker compose up -d`.
4. **Verify state consistency**:
   - Check that open positions and orders in WobbleBot match what Kraken reports.
   - Make sure no duplicate orders were placed.
   - Confirm that exposures and balances are within safety limits.

**Note:** Until the reconciliation logic in Phase 5 is complete, treat any restart as a high‑risk operation.  Perform manual checks and be prepared to switch to paper mode or abort trading if inconsistencies are found.

### External Service Outages

- **Kraken Issues** – If Kraken is unreachable or returns errors, switch to paper‑trading mode (via config or CLI flag) until connectivity is restored.
- **Kraken Withdrawal Issues** – If bank transfers (withdrawals) fail, disable or set the Harvester to passive mode.  Log the issue and retry later. Per ADR-004, withdrawals are handled via Kraken's API.
- **LLM Advisor Issues** – If the Advisor is down or producing invalid JSON, disable it in the config.  The bot will continue using the last valid settings.

## Mode Management

Changing the mode of any subsystem (Bot Core, Advisor, Harvester) is akin to a production change.  Follow a safe procedure:

1. Record the change: note the date, time, reason, and new mode.
2. Perform mode changes during low‑volatility periods when possible.
3. Monitor the system closely for at least one full cycle after a mode change.
4. If unexpected behavior appears (e.g., sudden increase in order volume, harvester proposals that exceed limits), revert the change and investigate.

## Routine Maintenance

- Review configuration periodically.  Are grid parameters still sensible given recent volatility?  Do safety caps need adjustment?
- Update dependencies (Python packages, CCXT, LLM models) in a controlled manner.  Use a staging environment when possible.
- Keep the LLM model up to date if using an external provider or local file.  Newer models may perform better, but update only after verifying it does not introduce regressions.
- Plan periodic “sanity check” simulations.  Run the core in paper mode against historical data to ensure no unseen bug has crept in.

### Documentation Updates

WobbleBot’s behavior is governed by documentation and configuration.  When you make changes to code, dependencies, or operations, update the relevant documents (this file, `deployment-guide.md`, the operator guide, etc.) and the changelog.  Documentation drift is itself a risk; treat docs as part of the system.
