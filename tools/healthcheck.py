"""Docker HEALTHCHECK probe (P3 ops slice).

Explicit modes plus the role-configured Dockerfile default:

* ``--daemon cli/live`` — classify the daemon's freshness through the
  SAME machinery the /health page uses (``fetch_daemon_freshness`` +
  ``derive_thresholds_from_config`` — one staleness definition, no new
  hardcoded multipliers). Exit 0 when FRESH; 1 otherwise. A daemon
  that is wedged-but-alive (stuck socket, blocked Ollama, deadlocked
  aiosqlite) stops heartbeating and goes unhealthy in Portainer,
  which is the whole point — a green running container was previously
  no evidence the loop was looping.

* ``--http URL`` — loopback HTTP GET for the web container’s unauthenticated
  ``/healthz``. Accepts localhost, 127.0.0.1 or [::1] with a configurable port.
  No proxies, credentials, query, fragment or redirects. Exit 0 on 2xx; 1 otherwise.

* ``--container`` — use exactly one of ``WOBBLEBOT_HEALTH_DAEMON`` or
  ``WOBBLEBOT_HEALTH_URL``, with optional ``WOBBLEBOT_HEALTH_CONFIG`` and
  ``WOBBLEBOT_HEALTH_PROFILE``. Missing/ambiguous roles are unhealthy.
  Compose overrides this default with its explicit per-service probes.

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
import http.client
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

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
    """Probe only this container's web liveness endpoint, without proxy/redirects."""
    try:
        target = urlsplit(url)
        port = 80 if target.port is None else target.port
        if (
            target.scheme != "http"
            or target.hostname not in {"localhost", "127.0.0.1", "::1"}
            or target.username is not None
            or target.password is not None
            or target.path != "/healthz"
            or "?" in url
            or "#" in url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or not math.isfinite(timeout)
            or timeout <= 0
            or not 1 <= port <= 65535
        ):
            raise ValueError("expected loopback HTTP /healthz and a positive finite timeout")
    except ValueError:
        print("unhealthy: HTTP probe requires a loopback HTTP /healthz URL and valid timeout")
        return 1

    # Map the allowed names to literals, avoiding DNS and environment proxies.
    # HTTPConnection does not follow redirects; only this fixed path is requested.
    host = "::1" if target.hostname == "::1" else "127.0.0.1"
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/healthz")
        with connection.getresponse() as response:
            status = response.status
    except (http.client.HTTPException, OSError, TimeoutError, ValueError) as exc:
        print(f"unhealthy: local HTTP /healthz failed: {type(exc).__name__}")
        return 1
    finally:
        connection.close()
    if 200 <= status < 300:
        print(f"healthy: local HTTP /healthz -> {status}")
        return 0
    print(f"unhealthy: local HTTP /healthz -> {status}")
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
        include_delivery=daemon == "cli/delivery",
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
    mode.add_argument("--container", action="store_true", help="Read explicit container role env")
    parser.add_argument("--config", default=None, help="settings.yml path override")
    parser.add_argument("--profile", default=None, help="Config profile (e.g. cpu-only)")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    if args.container:
        args.daemon = os.environ.get("WOBBLEBOT_HEALTH_DAEMON", "").strip()
        args.http = os.environ.get("WOBBLEBOT_HEALTH_URL", "").strip()
        if bool(args.daemon) == bool(args.http):
            print(
                "unhealthy: configure exactly one WOBBLEBOT_HEALTH_DAEMON or WOBBLEBOT_HEALTH_URL"
            )
            return 1
        args.config = os.environ.get("WOBBLEBOT_HEALTH_CONFIG") or args.config
        args.profile = os.environ.get("WOBBLEBOT_HEALTH_PROFILE") or args.profile
    if args.http:
        return _check_http(args.http, args.timeout)
    return asyncio.run(_check_daemon(args.daemon, args.config, args.profile))


if __name__ == "__main__":
    sys.exit(main())
