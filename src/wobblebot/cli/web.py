"""Web UI daemon + create-user subcommand (Stage 7.1.D).

Two subcommands::

    python -m wobblebot.cli.web                # serve (default)
    python -m wobblebot.cli.web serve          # explicit
    python -m wobblebot.cli.web create-user    # seed a password

``serve`` boots uvicorn against the FastAPI app built by
:func:`wobblebot.web.app.create_app`. It opens ``operator.db`` (always
required) plus the four optional cross-DB paths (live / advise /
harvest / observe / news) when configured, then hands the app to
``uvicorn.run``.

``create-user`` prompts on stdin for a username, then on the
terminal (via :func:`getpass.getpass`) for a password — twice for
confirmation. It hashes via :func:`wobblebot.web.auth.hash_password`
at the configured bcrypt cost, then inserts a row via
:meth:`StoragePort.create_user`. Duplicate usernames + EOF on the
password prompt + DB-open failures all exit with code 2 and a clean
error message — no raw tracebacks.

Per ADR-016 the daemon is operator-managed (the operator runs it
behind their own reverse proxy for TLS / LAN exposure); per ADR-017
the session-signing key MUST live in an env var named by
``WebConfig.session_secret_env_var`` (default
``WOBBLEBOT_WEB_SESSION_SECRET``) — refusing to start without it is
load-bearing.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    collect_overrides,
    identity,
    load_operator_env,
    run_with_clean_exit,
    safe_shutdown,
)
from wobblebot.config.cli import WebConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.daemon_health import derive_thresholds_from_config
from wobblebot.services.kraken_health import KrakenHealthProbe
from wobblebot.web.app import create_app
from wobblebot.web.auth import hash_password

_LOGGER = logging.getLogger("wobblebot.cli.web")


# --------------------------------------------------------------------- #
# Shared helpers                                                        #
# --------------------------------------------------------------------- #


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return collect_overrides(
        args,
        "web",
        {
            "log_format": ("log_format", identity),
            "bind_host": ("bind_host", identity),
            "bind_port": ("bind_port", identity),
        },
    )


def _require_web_config(config: WobbleBotConfig) -> WebConfig | None:
    """Return the ``web:`` block or log+return ``None`` if missing."""
    if config.web is None:
        _LOGGER.error(
            "settings.yml is missing the `web:` section; the web UI "
            "requires it. See config/settings.example.yml for the "
            "template."
        )
        return None
    return config.web


def _resolve_session_secret(web_config: WebConfig) -> str | None:
    """Pull the cookie-signing key from the env var named in config.

    Returns ``None`` (with a logged error including the mint command)
    if the env var is unset or empty; ``cli/web serve`` then exits 2.
    """
    env_var = web_config.session_secret_env_var
    secret = os.environ.get(env_var, "")
    if not secret:
        _LOGGER.error(
            "session secret env var is unset; refusing to start. "
            "Mint one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))" '
            "and export it as %s in your environment.",
            env_var,
        )
        return None
    return secret


async def _open_storage(path: str) -> SQLiteStorageAdapter | None:
    """Open a SQLite adapter at ``path``; return ``None`` on failure.

    Parent directory is created if missing — matches the
    ``SQLiteStorageAdapter.connect`` posture used by other CLIs.
    """
    parent = Path(path).parent
    if parent and not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _LOGGER.error(
                "failed to create parent dir for db",
                extra={"path": path, "parent": str(parent), "error": str(exc)},
            )
            return None
    adapter = SQLiteStorageAdapter(path)
    try:
        await adapter.connect()
    except StorageError as exc:
        _LOGGER.error(
            "failed to open sqlite db",
            extra={"path": path, "error": str(exc)},
        )
        return None
    return adapter


# --------------------------------------------------------------------- #
# serve subcommand                                                      #
# --------------------------------------------------------------------- #


async def _open_optional_dbs(
    web_config: WebConfig,
) -> dict[str, SQLiteStorageAdapter | None]:
    """Open each configured optional DB; return ``None`` per path that
    isn't set or failed to open."""
    out: dict[str, SQLiteStorageAdapter | None] = {
        "live": None,
        "advise": None,
        "harvest": None,
        "observe": None,
        "news": None,
    }
    paths = {
        "live": web_config.live_db,
        "advise": web_config.advise_db,
        "harvest": web_config.harvest_db,
        "observe": web_config.observe_db,
        "news": web_config.news_db,
    }
    for name, p in paths.items():
        if p is None:
            continue
        adapter = await _open_storage(p)
        if adapter is None:
            _LOGGER.warning(
                "optional db failed to open; the dashboard will "
                "gracefully degrade cards that need it",
                extra={"name": name, "path": p},
            )
        out[name] = adapter
    return out


async def _bootstrap_app(
    config: WobbleBotConfig,
) -> tuple[Any, list[SQLiteStorageAdapter], httpx.AsyncClient] | int:
    """Open every storage adapter the web app needs and build the FastAPI
    instance. Returns ``(app, [adapters], kraken_http_client)`` on success
    or an int exit code on failure (so the caller can return it from
    ``serve``).

    The ``kraken_http_client`` is the lifetime owner of the connection
    pool the :class:`KrakenHealthProbe` uses for ``/0/public/SystemStatus``
    polls; it must be closed in the ``finally`` of the serve loop so the
    underlying transport sockets release on shutdown.
    """
    web_config = _require_web_config(config)
    if web_config is None:
        return 2

    session_secret = _resolve_session_secret(web_config)
    if session_secret is None:
        return 2

    operator_storage = await _open_storage(web_config.operator_db)
    if operator_storage is None:
        return 2

    optionals = await _open_optional_dbs(web_config)
    opened: list[SQLiteStorageAdapter] = [operator_storage]
    for adapter in optionals.values():
        if adapter is not None:
            opened.append(adapter)

    # Stage 8.4.E: shared httpx client + cached probe singleton. Sized
    # for the cli/web process — one probe behind the in-process TTL
    # cache so dashboard refreshes don't multiply Kraken's read load.
    kraken_http = httpx.AsyncClient(timeout=10.0)
    kraken_probe = KrakenHealthProbe(kraken_http)

    # Derive per-daemon staleness thresholds from the full operator
    # config so an operator who tunes ANY interval — schedules.*,
    # live.tick_seconds, operator.forwarder_poll_seconds — gets a
    # proportionally adjusted health threshold for free. Solves the
    # v1.0 magic-numbers problem operator caught 2026-05-22.
    daemon_thresholds = derive_thresholds_from_config(config)

    app = create_app(
        config=web_config,
        operator_storage=operator_storage,
        session_secret=session_secret,
        live_storage=optionals["live"],
        advise_storage=optionals["advise"],
        harvest_storage=optionals["harvest"],
        observe_storage=optionals["observe"],
        news_storage=optionals["news"],
        kraken_health_probe=kraken_probe,
        daemon_health_thresholds=daemon_thresholds,
    )
    return app, opened, kraken_http


async def _close_storages(adapters: list[SQLiteStorageAdapter]) -> None:
    for adapter in adapters:
        try:
            await adapter.close()
        except StorageError:
            # Best-effort cleanup; shutdown is happening regardless.
            pass


async def _serve_async(config: WobbleBotConfig) -> int:
    bootstrap = await _bootstrap_app(config)
    if isinstance(bootstrap, int):
        return bootstrap
    app, adapters, kraken_http = bootstrap
    assert config.web is not None  # _bootstrap_app guarantees this

    uv_config = uvicorn.Config(
        app,
        host=config.web.bind_host,
        port=config.web.bind_port,
        log_config=None,  # let our configure_logging stand
        access_log=False,
        # Cap uvicorn's own "wait for in-flight requests" at 5s. Without
        # this, a long-polling client or a stuck background task can
        # hold ``server.serve()`` past SIGINT for minutes; the soak's
        # Day-3 cli/web 3-minute hang pattern was exactly this. Combined
        # with safe_shutdown(timeout_seconds=10) on the post-serve
        # cleanup, total worst-case shutdown is ~15s.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(uv_config)
    # Startup banner so operators see the daemon is alive. uvicorn's
    # own banner is suppressed by log_config=None + access_log=False;
    # without this line cli/web serves silently and looks dead even
    # when it's healthy. Surfaced 2026-05-20 post-bounce when operator
    # noticed Terminal 7 was silent and HAD to curl the login page to
    # verify the daemon was actually up.
    _LOGGER.info(
        "cli/web listening",
        extra={
            "bind_host": config.web.bind_host,
            "bind_port": config.web.bind_port,
            "url": f"http://{config.web.bind_host}:{config.web.bind_port}",
        },
    )
    # ADR-016 binds to 127.0.0.1 by default. Operators who change
    # this to 0.0.0.0 (LAN exposure) or any non-loopback address
    # need an HTTPS-terminating reverse proxy in front — the
    # login form + session cookie + trade-control buttons should
    # never traverse a network in cleartext. Warn loudly so the
    # exposure isn't accidental.
    if config.web.bind_host not in ("127.0.0.1", "localhost", "::1"):
        _LOGGER.warning(
            "cli/web bound to non-loopback address; HTTPS reverse proxy "
            "strongly recommended (see docs/deploy/reverse-proxy.md)",
            extra={"bind_host": config.web.bind_host},
        )
    try:
        await server.serve()
    finally:
        await safe_shutdown(
            [
                ("close_web_storages", lambda: _close_storages(adapters)),
                ("close_kraken_http", kraken_http.aclose),
            ],
            logger=_LOGGER,
        )
    return 0


def _serve_command(args: argparse.Namespace) -> int:
    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = (
        args.log_format
        if args.log_format is not None
        else (config.web.log_format if config.web else "plain")
    )
    log_file_path = config.web.log_file_path if config.web else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)

    run_with_clean_exit(_serve_async(config), logger=_LOGGER)


# --------------------------------------------------------------------- #
# create-user subcommand                                                #
# --------------------------------------------------------------------- #


def _read_username(stream: Any | None = None) -> str | None:
    """Prompt for a username on stdin. Returns ``None`` on EOF or blank.

    Default-resolves to ``sys.stdin`` at call time, not module-load time —
    important for the test harness, which monkeypatches ``sys.stdin``
    after the module has already been imported.
    """
    sys.stderr.write("Username: ")
    sys.stderr.flush()
    src = stream if stream is not None else sys.stdin
    line = src.readline()
    if not line:
        return None
    candidate = line.strip()
    if not candidate:
        return None
    return candidate


def _read_password_twice() -> str | None:
    """Prompt for password (echoless) twice; return the plaintext or
    ``None`` on EOF / mismatch / empty."""
    try:
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        return None
    if not first:
        return None
    if first != second:
        sys.stderr.write("error: passwords do not match\n")
        return None
    return first


async def _create_user_async(  # pylint: disable=too-many-return-statements
    config: WobbleBotConfig, *, stdin: Any | None = None
) -> int:
    web_config = _require_web_config(config)
    if web_config is None:
        return 2

    username = _read_username(stdin)
    if username is None:
        sys.stderr.write("error: username is required\n")
        return 2

    password = _read_password_twice()
    if password is None:
        sys.stderr.write("error: password prompt aborted or passwords did not match\n")
        return 2

    storage = await _open_storage(web_config.operator_db)
    if storage is None:
        return 2

    try:
        password_hash = hash_password(password, cost=web_config.bcrypt_cost)
        existing = await storage.get_user_by_username(username)
        if existing is not None:
            sys.stderr.write(f"error: username '{username}' already exists\n")
            return 2
        user = await storage.create_user(username, password_hash)
        _LOGGER.info(
            "created operator account",
            extra={"username": user.username, "user_id": user.id},
        )
    except StorageError as exc:
        sys.stderr.write(f"error: failed to create user: {exc}\n")
        return 2
    finally:
        await storage.close()
    return 0


def _create_user_command(args: argparse.Namespace) -> int:
    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format = (
        args.log_format
        if args.log_format is not None
        else (config.web.log_format if config.web else "plain")
    )
    log_file_path = config.web.log_file_path if config.web else None
    configure_logging(log_format=log_format, rotating_file_path=log_file_path)
    return asyncio.run(_create_user_async(config))


# --------------------------------------------------------------------- #
# Entry point                                                           #
# --------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command")

    serve = subs.add_parser(
        "serve",
        help="Run the FastAPI dashboard (default if no subcommand given).",
    )
    add_config_args(serve)
    serve.add_argument("--bind-host", default=None)
    serve.add_argument("--bind-port", type=int, default=None)
    serve.add_argument("--log-format", choices=("plain", "json"), default=None)
    serve.set_defaults(func=_serve_command)

    cu = subs.add_parser(
        "create-user",
        help=("Prompt for a username + password and seed an operator account " "in operator.db."),
    )
    add_config_args(cu)
    cu.add_argument("--log-format", choices=("plain", "json"), default=None)
    cu.set_defaults(func=_create_user_command)

    # When invoked with no subcommand, default to `serve` while preserving
    # any --config / --profile parsed at the top level. Easiest path:
    # also add those args to the top-level parser so the user can write
    # `python -m wobblebot.cli.web --config path` without a subcommand.
    add_config_args(parser)
    parser.add_argument("--bind-host", default=None)
    parser.add_argument("--bind-port", type=int, default=None)
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_operator_env()
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        # No subcommand → default to serve.
        return _serve_command(args)
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
