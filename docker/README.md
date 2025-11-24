# Docker Configuration

**Status:** Placeholder for Phase 2+

This directory will contain Docker and Docker Compose configurations for deploying WobbleBot on Synology NAS or other containerized environments.

## Planned Contents

- `Dockerfile` – Multi-stage build for production deployment
- `docker-compose.yml` – Service orchestration (app, database, optional LLM)
- `docker-compose.dev.yml` – Development override with volume mounts
- `.env.example` – Template for environment variables and secrets

## Phase Dependencies

Docker deployment is planned for **Phase 2** (Core Trading Engine) and beyond. During Phase 1 (Foundation & Sandbox), development occurs locally with `pip` and virtual environments.

## References

- See [Architecture - Deployment](../docs/architecture/deployment.md) for deployment strategy
- See [Planning - Roadmap](../docs/planning/roadmap.md) for phase details
