#!/bin/sh
#
# WobbleBot container entrypoint.
#
# Populates /app/config with baked defaults on first boot. Skips
# files that already exist so operator edits + customizations are
# preserved across restarts. Then exec's whatever command was
# passed (per-service `command:` in docker-compose.yml).
#
# Why this exists: the compose bind-mounts the host's config/
# directory over /app/config. Docker bind mounts are a one-way
# overlay — they completely hide the image's baked /app/config
# content, leaving the container with an empty config dir on a
# fresh host bind mount. Without this script the operator has to
# manually copy defaults from somewhere; with it, first boot
# auto-populates from a non-mounted location at /opt/wobblebot/
# defaults/config.

set -e

DEFAULTS=/opt/wobblebot/defaults/config
TARGET=/app/config

if [ -d "$DEFAULTS" ]; then
    # Walk every file in defaults. For each, compute its path
    # relative to the defaults root and copy it to the same relative
    # path under /app/config — but only when the target doesn't
    # already exist. Existing files (operator-edited settings.yml,
    # operator-tweaked prompts, etc.) are NEVER overwritten.
    find "$DEFAULTS" -type f | while read -r src; do
        rel="${src#"$DEFAULTS"/}"
        dest="$TARGET/$rel"
        if [ ! -e "$dest" ]; then
            mkdir -p "$(dirname "$dest")"
            cp "$src" "$dest"
        fi
    done
fi

exec "$@"
