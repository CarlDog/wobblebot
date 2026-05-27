# Docker deployment

This directory ships a multi-stage `Dockerfile` and a `docker-compose.yml`
that brings up WobbleBot as 8 long-running daemons against a shared
SQLite-backed data directory. Designed primarily for the operator's
Synology NAS via Portainer; works fine for local docker-compose too.

## Prerequisites

- Docker 20.10+ (Docker Desktop on Windows / macOS; native Docker on
  Linux + Synology).
- A populated `.env` at the repo root. Use `.env.example` as the
  template — every variable listed there is expected to be set.
- A populated `config/settings.yml`. Copy from `config/settings.example.yml`
  and edit the operator-specific bits (Discord IDs, harvester thresholds,
  etc.).
- An Ollama instance reachable at `host.docker.internal:11434` if you
  want the `cpu-only` profile to work. On the operator's NAS that's
  the Ollama container running with `network_mode: host` on the same
  Synology box. See `~/.claude/rules/nas-ollama-optimization.md` for
  the recommended model set + env vars.

## Build

```bash
docker build -f docker/Dockerfile -t wobblebot:dev .
```

Run from the **project root** (not from `docker/`) — the build context
is the repo, and `.dockerignore` controls what gets sent to the daemon.
The resulting image is ~320 MB.

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

## Local docker-compose

From the project root:

```bash
# 1. Validate the compose syntax — exits 0 silently if valid.
(cd docker && docker compose config --quiet)

# 2. Bring up the daemons.
(cd docker && docker compose up -d)

# 3. Tail one service's logs.
(cd docker && docker compose logs -f operator)

# 4. Stop + remove.
(cd docker && docker compose down)
```

**Never run `docker compose config` without `--quiet` against a real
`.env` file.** The verbose form interpolates env_file content into
stdout, exposing every secret. Use `--quiet` for validation; if you
need to inspect the resolved compose, validate against a placeholder
`.env` first.

### Running one-shot CLIs

The `tools` service is profile-gated so it doesn't start with
`docker compose up`. Invoke it via `docker compose run`:

```bash
(cd docker && docker compose run --rm tools python -m wobblebot.cli.preflight)
(cd docker && docker compose run --rm tools python -m wobblebot.cli.status)
(cd docker && docker compose run --rm tools python -m wobblebot.cli.recalibrate --target-balance 100)
(cd docker && docker compose run --rm tools python -m wobblebot.cli.apply --commit)
```

`--rm` removes the one-shot container after exit.

## NAS / Synology Portainer deployment

The operator's NAS runs Portainer on top of Container Manager. The
compose file is designed to be pasted into a Portainer "Stacks → Add
Stack → Build method: Web editor" panel.

**Three independent config stores** — when something doesn't update,
which is it?

1. **This `docker-compose.yml`.** Defines the stack shape. Updates
   require a git push *and* a Portainer "Pull and redeploy" with
   Re-pull image + Force redeploy.
2. **Portainer stack-level env vars.** Substituted into `${VARIABLE}`
   references at deploy time. NOT read from the repo's `.env`. Edit
   in Portainer UI → Stacks → wobblebot → Environment variables.
3. **The host's `.env`.** Only affects manual `docker compose up`
   from the cloned repo. Has no effect on a Portainer deployment.

If you update the compose YAML but values still look stale, it's
almost always #2 — the stored stack env in Portainer didn't change.
See `~/.claude/rules/docker-deployments.md` for the full pattern.

### Verifying a Portainer redeploy

A "stack updated" toast doesn't prove the container came up with the
config you wanted. After a redeploy, verify against the live container:

```bash
docker inspect wobblebot-live --format '{{json .HostConfig.ExtraHosts}}'
docker inspect wobblebot-live --format '{{.Config.Env}}' | tr ' ' '\n' | grep OLLAMA_BASE_URL
docker inspect wobblebot-live --format '{{.Config.Image}} (started {{.State.StartedAt}})'
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
Operator-side backups (Time Machine, rsync, the Synology Hyper Backup
job) only need to capture `data/` and `config/` — the image itself
is rebuildable from source.

## Image security

- Runs as non-root user `wobblebot` (UID 1001). Mounted host volumes
  may need `chown 1001:1001` on the host side if the operator's NAS
  user is a different UID and DSM's Container Manager doesn't auto-
  remap.
- No build deps (gcc) in the runtime layer — they're confined to the
  builder stage.
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
