"""Tests for cli/web — serve + create-user subcommands (Stage 7.1.D)."""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures import grid_config as _grid_config
from tests.fixtures import safety_config as _safety_config
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import web as cli_web
from wobblebot.config.cli import WebConfig
from wobblebot.config.loader import WobbleBotConfig

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_wobblebot_logger() -> Iterator[None]:
    """Snapshot + restore the ``wobblebot`` logger config per test.

    ``cli_web.main()`` calls ``configure_logging`` which flips
    ``root.propagate = False`` on the ``wobblebot`` subtree. Without
    restoring after each test, downstream caplog-based tests stop
    seeing wobblebot.* log records.
    """
    root = logging.getLogger("wobblebot")
    snapshot_level = root.level
    snapshot_propagate = root.propagate
    snapshot_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.handlers = snapshot_handlers
        root.propagate = snapshot_propagate
        root.setLevel(snapshot_level)


# --------------------------------------------------------------------- #
# Config builders                                                       #
# --------------------------------------------------------------------- #


def _full_config(*, web_block: WebConfig | None = None) -> WobbleBotConfig:
    return WobbleBotConfig(
        grid=_grid_config(),
        safety=_safety_config(),
        web=web_block,
    )


def _write_settings_yaml(path: Path, *, operator_db: str, with_web: bool = True) -> None:
    """Write a minimal settings.yml that load_resolved_config accepts."""
    # YAML's double-quoted strings interpret backslash escapes; on
    # Windows the operator_db path uses backslashes that would trip
    # the parser. Forward slashes round-trip cleanly on both OSes.
    operator_db = operator_db.replace("\\", "/")
    body = [
        "grid:",
        "  default:",
        '    spacing_percentage: "1.0"',
        "    levels_above: 3",
        "    levels_below: 3",
        '    order_size_usd: "10"',
        "safety:",
        '  max_total_exposure_usd: "100"',
        '  max_daily_spend_usd: "100"',
        '  max_per_coin_exposure_usd: "50"',
        "  max_orders_per_coin: 10",
        "  sell_guard:",
        "    enabled: true",
        '    max_loss_percentage: "5"',
    ]
    if with_web:
        body += [
            "web:",
            f'  operator_db: "{operator_db}"',
        ]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- #
# _require_web_config                                                   #
# --------------------------------------------------------------------- #


class TestRequireWebConfig:
    def test_returns_none_when_missing(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = _full_config(web_block=None)
        result = cli_web._require_web_config(cfg)
        assert result is None
        assert any("missing the `web:`" in r.message for r in caplog.records)

    def test_returns_web_when_present(self) -> None:
        web = WebConfig()
        cfg = _full_config(web_block=web)
        assert cli_web._require_web_config(cfg) is web


# --------------------------------------------------------------------- #
# _resolve_session_secret                                               #
# --------------------------------------------------------------------- #


class TestResolveSessionSecret:
    def test_missing_env_var_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("WOBBLEBOT_WEB_SESSION_SECRET", raising=False)
        result = cli_web._resolve_session_secret(WebConfig())
        assert result is None
        assert any("session secret env var" in r.message for r in caplog.records)

    def test_empty_env_var_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WOBBLEBOT_WEB_SESSION_SECRET", "")
        result = cli_web._resolve_session_secret(WebConfig())
        assert result is None

    def test_present_env_var_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WOBBLEBOT_WEB_SESSION_SECRET", "x" * 64)
        assert cli_web._resolve_session_secret(WebConfig()) == "x" * 64

    def test_custom_env_var_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CUSTOM_KEY", "custom-secret-value-32-bytes-min")
        web = WebConfig(session_secret_env_var="CUSTOM_KEY")
        assert cli_web._resolve_session_secret(web) == "custom-secret-value-32-bytes-min"


# --------------------------------------------------------------------- #
# _open_storage                                                         #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestOpenStorage:
    async def test_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "data" / "deep" / "operator.db"
        adapter = await cli_web._open_storage(str(nested))
        assert adapter is not None
        assert nested.parent.exists()
        await adapter.close()

    async def test_returns_adapter_for_existing_dir(self, tmp_path: Path) -> None:
        path = tmp_path / "operator.db"
        adapter = await cli_web._open_storage(str(path))
        assert adapter is not None
        await adapter.close()


# --------------------------------------------------------------------- #
# _open_optional_dbs                                                    #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestOpenOptionalDbs:
    """2026-08-05 outage regression: a failed optional DB must degrade
    gracefully (log a WARNING, return None for that key), never crash
    the whole dashboard. The original bug was in the warning call
    itself -- extra={"name": ...} collides with the reserved
    LogRecord.name attribute and raises KeyError from inside logging,
    independent of *why* the DB failed to open."""

    async def test_failed_db_degrades_instead_of_crashing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A path that IS a directory can never open as a SQLite file --
        # connect() raises StorageError, _open_storage returns None,
        # and _open_optional_dbs must handle that without raising.
        bad_path = tmp_path / "advise_is_a_dir"
        bad_path.mkdir()
        good_path = tmp_path / "harvest.db"

        web = WebConfig(advise_db=str(bad_path), harvest_db=str(good_path))
        with caplog.at_level(logging.WARNING, logger="wobblebot.cli.web"):
            result = await cli_web._open_optional_dbs(web)

        assert result["advise"] is None
        assert result["harvest"] is not None
        assert "optional db failed to open" in caplog.text
        await result["harvest"].close()

    async def test_all_paths_unset_returns_all_none(self) -> None:
        result = await cli_web._open_optional_dbs(WebConfig())
        assert result == {
            "live": None,
            "advise": None,
            "harvest": None,
            "observe": None,
            "news": None,
        }


# --------------------------------------------------------------------- #
# create-user subcommand                                                #
# --------------------------------------------------------------------- #


def _stdin_with(*lines: str) -> io.StringIO:
    return io.StringIO("".join(line + "\n" for line in lines))


@pytest.mark.asyncio
class TestCreateUserAsync:
    async def test_seeds_a_user(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = str(tmp_path / "operator.db")
        cfg = _full_config(web_block=WebConfig(operator_db=db_path, bcrypt_cost=10))
        # getpass.getpass + stdin both stubbed.
        monkeypatch.setattr(cli_web.getpass, "getpass", lambda _prompt: "hunter2")
        rc = await cli_web._create_user_async(cfg, stdin=_stdin_with("operator"))
        assert rc == 0
        # Round-trip: user should now exist in the DB.
        storage = SQLiteStorageAdapter(db_path)
        await storage.connect()
        try:
            user = await storage.get_user_by_username("operator")
            assert user is not None
            assert user.password_hash.startswith("$2b$")
        finally:
            await storage.close()

    async def test_no_web_block_exits_2(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = _full_config(web_block=None)
        rc = await cli_web._create_user_async(cfg, stdin=_stdin_with("operator"))
        assert rc == 2

    async def test_blank_username_exits_2(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = _full_config(web_block=WebConfig(operator_db=str(tmp_path / "x.db")))
        rc = await cli_web._create_user_async(cfg, stdin=_stdin_with("   "))
        assert rc == 2

    async def test_eof_on_username_exits_2(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = _full_config(web_block=WebConfig(operator_db=str(tmp_path / "x.db")))
        rc = await cli_web._create_user_async(cfg, stdin=io.StringIO(""))
        assert rc == 2

    async def test_mismatched_password_exits_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _full_config(web_block=WebConfig(operator_db=str(tmp_path / "x.db"), bcrypt_cost=10))
        prompts: list[str] = []

        def _getpass(prompt: str) -> str:
            prompts.append(prompt)
            return "first" if len(prompts) == 1 else "second"

        monkeypatch.setattr(cli_web.getpass, "getpass", _getpass)
        rc = await cli_web._create_user_async(cfg, stdin=_stdin_with("operator"))
        assert rc == 2

    async def test_eof_on_password_exits_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _full_config(web_block=WebConfig(operator_db=str(tmp_path / "x.db"), bcrypt_cost=10))

        def _getpass(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr(cli_web.getpass, "getpass", _getpass)
        rc = await cli_web._create_user_async(cfg, stdin=_stdin_with("operator"))
        assert rc == 2

    async def test_duplicate_username_exits_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = str(tmp_path / "operator.db")
        cfg = _full_config(web_block=WebConfig(operator_db=db_path, bcrypt_cost=10))
        monkeypatch.setattr(cli_web.getpass, "getpass", lambda _p: "hunter2")
        # First call succeeds
        rc1 = await cli_web._create_user_async(cfg, stdin=_stdin_with("operator"))
        assert rc1 == 0
        # Second call with the same username fails
        rc2 = await cli_web._create_user_async(cfg, stdin=_stdin_with("operator"))
        assert rc2 == 2


# --------------------------------------------------------------------- #
# _serve_async deprived-env paths                                       #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestServeDeprivedEnv:
    async def test_no_web_block_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WOBBLEBOT_WEB_SESSION_SECRET", "x" * 64)
        cfg = _full_config(web_block=None)
        rc = await cli_web._serve_async(cfg)
        assert rc == 2

    async def test_missing_session_secret_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("WOBBLEBOT_WEB_SESSION_SECRET", raising=False)
        cfg = _full_config(web_block=WebConfig())
        rc = await cli_web._serve_async(cfg)
        assert rc == 2

    async def test_bootstrap_returns_app_and_adapters(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WOBBLEBOT_WEB_SESSION_SECRET", "x" * 64)
        db = str(tmp_path / "operator.db")
        cfg = _full_config(web_block=WebConfig(operator_db=db))
        result = await cli_web._bootstrap_app(cfg)
        assert not isinstance(result, int)
        app, adapters, kraken_http = result
        try:
            assert app is not None
            assert len(adapters) >= 1  # operator.db at minimum
            # Stage 8.4.E health-icon work — every bootstrap now creates
            # a Kraken probe + httpx client; ensure the client is real
            # and the probe was wired onto app.state.
            assert kraken_http is not None
            assert app.state.kraken_health_probe is not None
        finally:
            await cli_web._close_storages(adapters)
            await kraken_http.aclose()


# --------------------------------------------------------------------- #
# _release_check_loop (v1.1 footer "update available" background poll) #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestReleaseCheckLoop:
    async def test_runs_immediately_and_updates_app_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_poll_loop runs a cycle BEFORE its first sleep, so the
        footer has a real result within the first request rather than
        waiting a full interval. The fake check sets stop_event itself
        so this test exercises exactly one cycle, deterministically,
        with no real sleeping."""
        import asyncio
        from types import SimpleNamespace

        from wobblebot.services.release_checker import ReleaseCheckResult

        calls = {"n": 0}
        stop_event = asyncio.Event()

        async def fake_check_for_update(version: str) -> ReleaseCheckResult:
            calls["n"] += 1
            stop_event.set()
            return ReleaseCheckResult(
                update_available=True,
                current_version=version,
                latest_version="9.9.9",
                release_url="https://example.com/releases/9.9.9",
            )

        monkeypatch.setattr(cli_web, "check_for_update", fake_check_for_update)
        app = SimpleNamespace(state=SimpleNamespace())

        await cli_web._release_check_loop(app, 6.0, stop_event)  # type: ignore[arg-type]

        assert calls["n"] == 1
        result = app.state.release_check_result
        assert result.update_available is True
        assert result.latest_version == "9.9.9"

    async def test_no_update_result_stored_when_none_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        from types import SimpleNamespace

        from wobblebot.services.release_checker import ReleaseCheckResult

        stop_event = asyncio.Event()

        async def fake_check_for_update(version: str) -> ReleaseCheckResult:
            stop_event.set()
            return ReleaseCheckResult(
                update_available=False,
                current_version=version,
                latest_version=version,
                release_url=None,
            )

        monkeypatch.setattr(cli_web, "check_for_update", fake_check_for_update)
        app = SimpleNamespace(state=SimpleNamespace())

        await cli_web._release_check_loop(app, 6.0, stop_event)  # type: ignore[arg-type]

        assert app.state.release_check_result.update_available is False


# --------------------------------------------------------------------- #
# Parser surface — argparse dispatch                                    #
# --------------------------------------------------------------------- #


class TestParser:
    def test_no_args_defaults_to_serve(self) -> None:
        parser = cli_web._build_parser()
        args = parser.parse_args([])
        assert getattr(args, "command", None) is None  # no subcommand
        # main() falls through to _serve_command when func is missing

    def test_serve_subcommand_sets_func(self) -> None:
        parser = cli_web._build_parser()
        args = parser.parse_args(["serve"])
        assert args.func is cli_web._serve_command

    def test_create_user_subcommand_sets_func(self) -> None:
        parser = cli_web._build_parser()
        args = parser.parse_args(["create-user"])
        assert args.func is cli_web._create_user_command

    def test_bind_port_parses_to_int(self) -> None:
        parser = cli_web._build_parser()
        args = parser.parse_args(["serve", "--bind-port", "9000"])
        assert args.bind_port == 9000

    def test_config_path_parses(self, tmp_path: Path) -> None:
        parser = cli_web._build_parser()
        target = tmp_path / "settings.yml"
        args = parser.parse_args(["serve", "--config", str(target)])
        assert args.config == target

    def test_invalid_subcommand_rejected(self) -> None:
        parser = cli_web._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["nonexistent-subcommand"])


class TestBuildOverrides:
    def test_collects_passed_flags(self) -> None:
        import argparse

        ns = argparse.Namespace(
            bind_host="0.0.0.0",
            bind_port=9000,
            log_format="json",
        )
        result = cli_web._build_overrides(ns)
        assert result == {
            "web": {
                "bind_host": "0.0.0.0",
                "bind_port": 9000,
                "log_format": "json",
            }
        }

    def test_skips_unset_flags(self) -> None:
        import argparse

        ns = argparse.Namespace(bind_host=None, bind_port=None, log_format=None)
        result = cli_web._build_overrides(ns)
        assert result == {}
