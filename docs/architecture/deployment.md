# Deployment Architecture

The production topology is a service-per-daemon Docker Compose stack on a Synology NAS. Every
WobbleBot service uses the same application image but starts a different CLI entry point; there is
no monolithic `wobblebot-core` process.

## Compose services

- `live`, `observe`, `news`, `advise`, `harvest`, `operator`, `web`, and `maintenance` are the
  long-running services.
- `tools` is profile-gated for explicitly invoked one-shot commands and does not start with the
  daemon stack.
- SQLite databases, archives/backups, logs, settings, and prompts live in host bind mounts under
  the operator-selected data/config roots. The application image is replaceable; those mounts are
  the durable state.

The exact commands, health checks, ports, mounts, and restart policies live in
[`docker/docker-compose.yml`](../../docker/docker-compose.yml); operational instructions live in
[`docker/README.md`](../../docker/README.md).

## LLM topology

Ollama is host-managed and external to this Compose stack. Containers reach it through
`OLLAMA_BASE_URL=http://host.docker.internal:11434` plus the explicit host-gateway mapping. Cloud
advisor seats call their providers directly through the configured adapters and cost gate. Compose
does not start an Ollama, generic LLM proxy, Grafana, or Prometheus container.

## Network and host boundary

- Daemons share the Compose network; the web service is the only normal HTTP ingress and is meant
  to sit behind the operator's authenticated reverse proxy.
- No service should be exposed to WAN directly. Host firewall/reverse-proxy policy remains outside
  Compose and must be configured deliberately.
- The present Compose file still shares more credentials and writable mounts than the Python
  authority model requires. That confirmed hardening gap is recorded in the OpenClaw and NemoClaw
  assessments; this document does not claim isolation that the deployment does not yet enforce.
- Synology storage, backup, CPU/memory, and restart settings are operator-owned deployment policy;
  verify them alongside WobbleBot's own health/readiness checks.
