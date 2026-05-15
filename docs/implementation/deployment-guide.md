# Deployment Guide

> **Status: forward-looking design doc.** Describes the Phase 2+ target
> system. None of the commands, configs, schemas, or endpoints below exist
> yet. Current code (Phase 1.3) runs only via the test suite. Track real
> progress in [docs/planning/roadmap.md](../planning/roadmap.md).

This document describes how to deploy WobbleBot using Docker Compose, both locally and on a Synology NAS.  Deployment should be reproducible and require minimal manual steps.

## Prerequisites

- Docker and Docker Compose are installed on the host.
- For NAS deployment: access to a Synology NAS with sufficient CPU and memory for the WobbleBot core and the LLM container.
- Kraken API keys: a read‑only key for the trading core and, later, a withdrawal key for the Harvester.  Keep these keys secure and out of version control.
- Optional: A local LLM runtime (e.g., an Ollama container) if the Advisor is enabled.

## Folder Layout

A typical WobbleBot repository contains:

- `docker/`
  - `docker-compose.yml` – defines the services (core, LLM, dashboard, etc.).
  - `env.example` – example environment variables (API keys, model names).  Copy this to `.env` and fill in actual values.
- `config/`
  - `settings.yml` – the main configuration file (which coins, grid settings, safety caps, etc.).
  - `logging.yml` – optional logging configuration.

## Basic Flow

1. **Clone the Repository & Configure**

   - Clone the repo onto your local machine or NAS.
   - Copy `docker/env.example` to `.env` and fill in:
     - `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` (read‑only for trading core).
     - `KRAKEN_WITHDRAW_KEY` and `KRAKEN_WITHDRAW_SECRET` (for the Harvester when active withdrawals are enabled).
     - `LLM_ENDPOINT` if using a local LLM service.
   - Adjust `config/settings.yml` for your coins, grid settings, safety caps, and enabled modules.

2. **Run Locally (Development)**

   From the repository root, run:

   ```bash
   docker compose -f docker/docker-compose.yml up --build
   ```

   This builds the images (if necessary) and starts the containers.  Verify that the core logs appear and that no real trades are sent if you are in paper mode.

3. **Deploy to Synology NAS**

   - Copy or clone the repo to the NAS.
   - Use the Synology Docker GUI or SSH into the NAS and run the same `docker compose` commands.  On Synology, you may need to specify an absolute path to the `docker-compose.yml` file.
   - Mount persistent volumes for the SQLite database and logs, for example:

     | Volume | Host Path | Container Path |
     | --- | --- | --- |
     | DB | `/volume1/docker/wobblebot/db` | `/data/wobblebot/db` |
     | Logs | `/volume1/docker/wobblebot/logs` | `/data/wobblebot/logs` |

   - Start the services.  If the Advisor is enabled, ensure the LLM container is up and accessible to the core.

4. **Environment Modes**

   WobbleBot can run in different modes controlled by config and environment variables:

   - **Sandbox / Dev** \u2013 Paper trading; Harvester disabled; Advisor on with verbose logging.  Safe for local testing.
   - **Live / Low\u2011Risk** \u2013 Real trading on Kraken with tiny order sizes; Harvester in passive mode (no transfers); Advisor restricted to read\u2011only or safe bounds.
   - **Production** \u2013 Real trading with configured caps; Harvester active for withdrawals (within strict limits per ADR-004); Advisor optionally auto\u2011applying safe suggestions.

   Always verify your mode before starting WobbleBot to avoid unintended trades or withdrawals.

## Configuration Notes

- Secrets (API keys, tokens) must never be committed to the repository.  Use environment variables or Synology’s secret store.
- Keep the LLM container internal to your network.  Do not expose it to the internet.
- Avoid binding services directly to 0.0.0.0 unless absolutely necessary.  Use port mappings judiciously.

## Upgrades

When a new version of WobbleBot is released:

1. Pull the latest tag or branch.
2. Run `docker compose pull` to fetch updated images.
3. Run `docker compose up -d --build` to rebuild and restart services.  The `-d` flag runs containers in detached mode.
4. If database migrations are introduced in future versions, run the provided migration scripts (TBD).

Record any version‑specific upgrade steps in `changelog.md`.
