# syntax=docker/dockerfile:1.7
#
# WobbleBot Dockerfile — multi-stage build.
#
# Stage 1 (builder): installs build deps and produces wheels for
# wobblebot + all runtime dependencies into /wheels.
#
# Stage 2 (runtime): minimal python:3.13-slim image with only the
# wheels installed. No compilers, no source tree, no test deps.
#
# Single image serves all 8 daemons (cli/live, cli/observe,
# cli/news, cli/advise, cli/harvest, cli/operator, cli/web,
# cli/maintenance). docker-compose.yml provides the per-daemon
# `command:` to pick which entry point runs.
#
# Build from the project root:
#   docker build -f docker/Dockerfile -t wobblebot:dev .

# ---------- Stage 1: builder ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build deps for any C-extension wheels that pip falls back to
# building from source. bcrypt + pydantic-core ship wheels for
# python:3.13-slim, so this is belt-and-suspenders, not strictly
# required today.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only what `pip wheel` needs to resolve and build the project.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Produce wheels for wobblebot + all transitive runtime deps.
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim AS runtime

# Streaming-friendly output (Docker captures stderr per daemon)
# and no .pyc cruft in the layered image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user. UID 1001 matches the convention used elsewhere in
# the operator's NAS Docker setups; mounted volumes can chown to
# this UID from the host side if needed.
RUN groupadd -r -g 1001 wobblebot \
 && useradd -r -u 1001 -g wobblebot -d /app -s /sbin/nologin wobblebot

WORKDIR /app

# Install from wheels only — no source, no build deps.
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Operator-facing files baked into the image as defaults. The
# compose mounts config/ over /app/config at runtime so the
# operator's settings.yml + prompts override these; the baked
# copies are present so a fresh container without mounts still
# has the example/template files available.
COPY config/ /app/config/
COPY tools/ /app/tools/

# Writable runtime directories. Compose mounts host volumes over
# these so SQLite DBs + log files persist across container
# rebuilds. mkdir + chown happen here so the non-root user can
# write before any mount is established.
RUN mkdir -p /app/data /app/data/archive /app/data/backups /app/logs \
 && chown -R wobblebot:wobblebot /app

USER wobblebot

# No long-running default command — each compose service supplies
# its own `command:`. If someone runs the image directly without
# a command, surface a helpful hint instead of starting a daemon
# that wasn't asked for.
CMD ["python", "-c", "import sys; sys.stderr.write('wobblebot image: specify a CLI via docker compose or `docker run wobblebot:dev python -m wobblebot.cli.<entry>`\\n'); sys.exit(2)"]
