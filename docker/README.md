# Docker deployment

This directory ships a multi-stage `Dockerfile` and a `docker-compose.yml`
that brings up WobbleBot as 8 long-running daemons against a shared
SQLite-backed data directory. Designed primarily for Portainer-managed
deployment on the operator's Synology NAS; works fine for local
docker-compose too.

## Image distribution

The runtime image is published to **GitHub Container Registry** at:

```
ghcr.io/carldog/wobblebot
```

Tags published by the `Publish Docker image to GHCR` workflow on every
main commit that touches the runtime surface (`src/`, `config/`,
`tools/`, `docker/Dockerfile`, `pyproject.toml`):

| Tag           | Lifetime | Use for                                     |
| ------------- | -------- | ------------------------------------------- |
| `:main`       | Mutable  | Follow latest main                          |
| `:latest`     | Mutable  | Alias for `:main` (default in compose)      |
| `:sha-<7hex>` | Pinned   | Freeze a soak / pin a specific commit       |

**Make the package public after first push** so Portainer can pull
without auth: GitHub → your profile → Packages → `wobblebot` → Package
settings → Change visibility → Public. Until that's done, Portainer
needs a GitHub PAT with `read:packages` configured under Settings →
Registries.

## Prerequisites

- Docker 20.10+ on whatever host runs the daemons (Synology Container
  Manager / Docker Desktop / native Docker).
- A `config/settings.yml` populated from `config/settings.example.yml`
  with the operator-specific bits (Discord IDs, harvester thresholds,
  grid sizing). Mounted into the container; lives on the host filesystem.
- All required env vars set per the deployment method (see below).
- An Ollama instance reachable at `host.docker.internal:11434` if you
  want the `cpu-only` profile to work. On the NAS that's the Ollama
  container running with `network_mode: host`. See
  `~/.claude/rules/nas-ollama-optimization.md` for the recommended
  model set + env vars.

## Services

`docker-compose.yml` defines 8 daemon services plus one profile-gated
`tools` service for operator-invoked one-shot CLIs:

| Service        | Entry point                       | Restart on crash |
| -------------- | --------------------------------- | ---------------- |
| `live`         | `python -m wobblebot.cli.live`    | **No** (loss-cap trip means stop) |
| `observe`      | `python -m wobblebot.cli.observe` | Yes              |
| `news`         | `python -m wobblebot.cli.news`    | Yes              |
| `advise`       | `python -m wobblebot.cli.advise`  | Yes              |
| `harvest`      | `python -m wobblebot.cli.harvest` | **No** (touches money) |
| `operator`     | `python -m wobblebot.cli.operator`| Yes              |
| `web`          | `python -m wobblebot.cli.web serve` | Yes            |
| `maintenance`  | `python -m wobblebot.cli.maintenance` | Yes          |
| `tools`        | (no default)                      | No (one-shot)    |

`cli/shadow` is intentionally omitted; operators who want continued
paper-trading on the NAS add it back as a separate service.

## NAS / Portainer deployment (primary path)

This is the deployment pattern the compose is designed for.

### One-time setup

1. **Wait for the first CI build** to publish `ghcr.io/carldog/wobblebot:latest`.
   The "Publish Docker image to GHCR" workflow runs on push to main.
2. **Make the package public** (see Image Distribution above).
3. **Pre-create the host directories** for the bind mounts and chown
   them to UID 1001 (the in-container `wobblebot` user):
   ```bash
   sudo mkdir -p /volume1/docker/wobblebot/{data,data/archive,data/backups,config,logs}
   sudo chown -R 1001:1001 /volume1/docker/wobblebot/data /volume1/docker/wobblebot/logs
   ```
4. **Place `config/settings.yml`** at `/volume1/docker/wobblebot/config/settings.yml`.
   Edit operator-specific values per `config/settings.example.yml`.

### Create the stack

Add a new stack in Portainer (Stacks → Add Stack → Repository) pointing
at `https://github.com/CarlDog/wobblebot`, compose path
`docker/docker-compose.yml`, branch `main`.

Set the environment variables in the Portainer stack-env section. Every
`${VAR}` reference in the compose file is substituted from this section
at deploy time:

| Variable                       | Required for                  | Notes |
| ------------------------------ | ----------------------------- | ----- |
| `KRAKEN_API_KEY`               | observe, status, web          | Read-only key |
| `KRAKEN_API_SECRET`            |                               |       |
| `KRAKEN_TRADE_API_KEY`         | live, preflight               | Trade scope; Withdraw OFF |
| `KRAKEN_TRADE_API_SECRET`      |                               |       |
| `KRAKEN_HARVESTER_API_KEY`     | harvest                       | Withdraw scope ON (ADR-003) |
| `KRAKEN_HARVESTER_API_SECRET`  |                               |       |
| `DISCORD_BOT_TOKEN`            | operator                      |       |
| `WOBBLEBOT_WEB_SESSION_SECRET` | web                           | Mint via `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `CRYPTOCOMPARE_API_KEY`        | news (if CryptoCompare on)    | Optional |
| `ANTHROPIC_API_KEY`            | advise / operator if cloud    | Optional |
| `OPENAI_API_KEY`               | "                             | Optional |
| `GOOGLE_API_KEY`               | "                             | Optional |

Click **Deploy the stack**. Portainer pulls the GHCR image, applies
the env, mounts the host paths, and starts all 8 daemons.

### Verifying after deploy

```bash
# Inside DSM SSH (or via Portainer's container console)
docker inspect wobblebot-live --format '{{json .HostConfig.ExtraHosts}}'
docker inspect wobblebot-live --format '{{.Config.Image}} (started {{.State.StartedAt}})'
docker logs wobblebot-operator --tail 50      # check Discord gateway connect
docker logs wobblebot-web --tail 50           # check uvicorn bind
```

Then open `https://wobblebot.carldog-nas` (DSM reverse proxy fronts
`127.0.0.1:8000` on the NAS). Seed the first web user with:

```bash
docker exec -it wobblebot-tools python -m wobblebot.cli.web create-user
# Or, if the tools service isn't running (it's Compose-profile-gated):
docker compose --profile tools run --rm tools python -m wobblebot.cli.web create-user
```

### Three independent config stores

When something doesn't update, which is it?

1. **This `docker-compose.yml`** in the repo. Defines stack shape.
   Updates require git push *and* a Portainer "Pull and redeploy"
   (Re-pull image + Force redeploy).
2. **Portainer stack-level env vars.** Substituted into `${VAR}`
   references at deploy time. NOT read from any file in the repo.
   Edit in Portainer UI → Stacks → wobblebot → Environment variables.
3. **The host's `config/settings.yml`** at the bind-mount path.
   Affects daemon behavior independent of compose / env. Edit on the
   NAS host directly; mount makes it live in the container.

If you update the compose YAML but values still look stale, it's
almost always #2 — the stored stack env in Portainer didn't change.
See `~/.claude/rules/docker-deployments.md` for the full pattern.

## Local docker-compose (development)

For local work on the laptop:

```bash
# 1. Pull the latest image (or build locally with `docker build`)
docker pull ghcr.io/carldog/wobblebot:latest

# 2. Provide env vars somehow. Simplest: a .env at the project root
#    that compose loads via `--env-file`. Example structure in
#    .env.example at the repo root.

# 3. Validate the compose (silent if valid).
(cd docker && docker compose --env-file ../.env config --quiet)

# 4. Bring up the daemons.
(cd docker && docker compose --env-file ../.env up -d)
```

**Never run `docker compose config` without `--quiet` against a real
`.env`** — the verbose form interpolates env_file content into stdout,
exposing every secret.

### Running one-shot CLIs (local or NAS)

The `tools` service is profile-gated; invoke via `docker compose run`:

```bash
docker compose --profile tools run --rm tools python -m wobblebot.cli.preflight
docker compose --profile tools run --rm tools python -m wobblebot.cli.status
docker compose --profile tools run --rm tools python -m wobblebot.cli.recalibrate --target-balance 100
docker compose --profile tools run --rm tools python -m wobblebot.cli.apply --commit
```

## The `cpu-only` profile

`docker-compose.yml` passes `--profile cpu-only` to every WobbleBot
daemon. That profile (defined in `config/settings.example.yml`) swaps
the local-desktop model selections for CPU-friendly q4_K_M variants:

| Role            | Local desktop default | `cpu-only` (NAS Docker)         |
| --------------- | --------------------- | ------------------------------- |
| operator        | phi4:14b-q8_0         | qwen2.5:3b-instruct-q4_K_M      |
| advisor         | deepseek-r1:7b        | llama3.1:8b-instruct-q4_K_M     |
| advisor type    | single (default)      | single (MoE on CPU is too slow) |
| ollama base_url | localhost:11434       | host.docker.internal:11434      |

Measured token rates on the operator's DS1823xs+ (Ryzen V1780B,
2026-05-27, hot cache):

- operator (qwen2.5:3b-q4): **12.69 tok/sec** → 2-8s per response
- advisor (llama3.1:8b-q4): **5.63 tok/sec** → ~35s per 200-token JSON

See `~/.claude/rules/nas-ollama-optimization.md` for the underlying
quant-selection principles + RAM budget guidance.

## Persistence model

The compose bind-mounts three host paths into every container:

| Host path     | Container path    | Notes                                      |
| ------------- | ----------------- | ------------------------------------------ |
| `../data`     | `/app/data`       | SQLite DBs + archive/backups (RW)          |
| `../config`   | `/app/config`     | settings.yml + prompt files (RW for `cli/apply`) |
| `../logs`     | `/app/logs`       | Rotated log files (RW)                     |

The SQLite databases live on the host and survive container rebuilds.
Operator-side backups (Synology Hyper Backup, rsync, etc.) only need
to capture `data/` and `config/` — the image is rebuildable from CI.

## Image security

- Runs as non-root user `wobblebot` (UID 1001). Mounted host volumes
  may need `chown 1001:1001` on the host side if DSM's Container
  Manager doesn't auto-remap.
- No build deps (gcc) in the runtime layer — confined to the builder
  stage.
- No source tree, no tests, no docs in the runtime image — only
  installed wheels under `site-packages` plus `/app/config` and
  `/app/tools`.
- `.dockerignore` excludes `.env`, `data/`, `tests/`, `docs/`, and
  `.git/` from the build context.

## References

- Global Docker deployment rule: `~/.claude/rules/docker-deployments.md`
- NAS Ollama optimization rule: `~/.claude/rules/nas-ollama-optimization.md`
- Architecture: `../docs/architecture/`
- Roadmap: `../docs/planning/roadmap.md`
