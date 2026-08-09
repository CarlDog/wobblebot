"""Docker HEALTHCHECK probe (P3 ops slice).

Two modes, one per container flavor:

* ``--daemon cli/live`` — classify the daemon's freshness through the
  SAME machinery the /health page uses (``fetch_daemon_freshness`` +
  ``derive_thresholds_from_config`` — one staleness definition, no new
  hardcoded multipliers). Exit 0 when FRESH; 1 otherwise. A daemon
  that is wedged-but-alive (stuck socket, blocked Ollama, deadlocked
  aiosqlite) stops heartbeating and goes unhealthy in Portainer,
  which is the whole point — a green running container was previously
  no evidence the loop was looping.

* ``--http URL`` — liveness GET for the web container (target the
  unauthenticated ``/healthz``; the real ``/health`` page requires a
  session). Exit 0 on any 2xx; 1 otherwise.

Docker reserves exit code 2, so this tool exits strictly 0 or 1 —
including on config errors (an unreadable config is an unhealthy
container, not a usage error). Output goes to stdout: the healthcheck
log IS this tool's consumer (``docker inspect`` captures it).

Threshold note: UNKNOWN (no heartbeat row / no content rows yet)
counts as unhealthy — the compose ``start_period`` grace covers boot;
past it, a daemon that has never heartbeated deserves the red.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from wobblebot.config.runtime import load_resolved_config
from wobblebot.services.daemon_health import (
    DaemonStatus,
    derive_thresholds_from_config,
    fetch_daemon_freshness,
)

# Conventional data-dir fallbacks for the two Approach-B DBs when the
# web: section doesn't name them (their WebConfig fields default to
# None because cli/web treats them as optional wiring; the deployed
# layout has used these exact filenames since Phase 5).
_DEFAULT_OBSERVE_DB = "data/wobblebot-observe.db"
_DEFAULT_ADVISE_DB = "data/wobblebot-advise.db"


def _check_http(url: str, timeout: float) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"unhealthy: GET {url} failed: {exc}")
        return 1
    if 200 <= status < 300:
        print(f"healthy: GET {url} -> {status}")
        return 0
    print(f"unhealthy: GET {url} -> {status}")
    return 1


async def _check_daemon(daemon: str, config_path: str | None, profile: str | None) -> int:
    try:
        config = load_resolved_config(
            config_path=Path(config_path) if config_path else None,
            profile_name=profile,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Any config failure = unhealthy (exit 1, never 2 — Docker
        # reserves 2). The daemon itself would be failing on the same
        # config, so the red is honest.
        print(f"unhealthy: config load failed: {exc}")
        return 1
    web = config.web
    observe_db = web.observe_db or _DEFAULT_OBSERVE_DB
    advise_db = web.advise_db or _DEFAULT_ADVISE_DB
    daemons = await fetch_daemon_freshness(
        observe_db=Path(observe_db),
        advise_db=Path(advise_db),
        operator_db=Path(web.operator_db),
        thresholds=derive_thresholds_from_config(config),
    )
    match = next((d for d in daemons if d.name == daemon), None)
    if match is None:
        known = ", ".join(d.name for d in daemons)
        print(f"unhealthy: unknown daemon {daemon!r} (known: {known})")
        return 1
    if match.last_seen is not None:
        age = f"{(datetime.now(UTC) - match.last_seen).total_seconds():.0f}s ago"
    else:
        age = "never"
    if match.status == DaemonStatus.FRESH:
        print(f"healthy: {daemon} fresh (last signal {age})")
        return 0
    detail = f"; {match.detail}" if match.detail else ""
    print(f"unhealthy: {daemon} {match.status} (last signal {age}{detail})")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Docker healthcheck probe (exit 0/1 only)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daemon", help="Daemon name to classify, e.g. cli/live")
    mode.add_argument("--http", metavar="URL", help="Liveness GET (2xx = healthy)")
    parser.add_argument("--config", default=None, help="settings.yml path override")
    parser.add_argument("--profile", default=None, help="Config profile (e.g. cpu-only)")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    args = parser.parse_args(argv)
    if args.http:
        return _check_http(args.http, args.timeout)
    return asyncio.run(_check_daemon(args.daemon, args.config, args.profile))


if __name__ == "__main__":
    sys.exit(main())
