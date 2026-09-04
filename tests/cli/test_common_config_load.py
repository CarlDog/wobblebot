"""The config-load half of the deprived-env contract, pinned across the family.

WHY THIS EXISTS. The 2026-09-04 release-close audit found that all sixteen
entry points shared a byte-identical handler::

    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\\n")
        return 2

and that two classes escape it, one line apart inside
``config/runtime.py::_load_yaml``:

* ``path.open()`` raises ``IsADirectoryError`` / ``PermissionError`` — both
  ``OSError``, **neither** ``FileNotFoundError`` — so ``--config config``
  (the natural typo, since every doc says ``config/settings.yml``) produced
  a raw traceback.
* ``yaml.safe_load`` raises ``yaml.YAMLError``, whose MRO is
  ``(YAMLError, Exception, BaseException, object)`` — it is **not** a
  ``ValueError`` — so one bad character in the operator's 1000-line
  ``settings.yml`` did the same. That one is the operationally nasty case:
  ``docker/docker-compose.yml`` runs seven daemons under
  ``restart: unless-stopped``, which turns an uncaught traceback into a
  crash-loop rather than a clean stop.

This is the SAME defect class as ``test_deprived_env_profile.py`` — a missing
exception class in one tuple — except that this time the tuple was wrong in
all sixteen copies rather than one. That is why the fix centralized it as
``_common.CONFIG_LOAD_ERRORS`` and why these tests drive each ``main()`` for
real: a grep test would assert source text, and the thing that broke was
behavior.

Deliberately in-process. Both scenarios fail at config resolution, before any
adapter, network call, or credential read, so no CLI can reach a side effect.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from tests.cli.test_deprived_env_profile import _EXTRA_ARGV, CLI_MODULES

pytestmark = pytest.mark.unit


def _drive(name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> object:
    """Run one CLI's ``main()`` with ``argv``, normalizing argparse's exit."""
    module: Any = importlib.import_module(f"wobblebot.cli.{name}")
    monkeypatch.setattr("sys.argv", ["wobblebot", *_EXTRA_ARGV.get(name, []), *argv])
    try:
        return module.main()
    except SystemExit as exc:  # argparse-style exit
        return exc.code


@pytest.mark.parametrize("name", CLI_MODULES)
def test_a_directory_config_path_exits_2_and_names_the_mistake(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--config <a directory>`` is a clean exit 2, not errno 13.

    The message must name the directory mistake. Before the fix a Windows
    operator got ``PermissionError: [Errno 13] Permission denied: 'config'``,
    which reads as a filesystem-ACL problem rather than "you named the folder,
    not the file" — an actionable-message failure even once the exit code is
    right, which is why this asserts the text and not only the code.
    """
    a_directory = tmp_path / "config"
    a_directory.mkdir()

    try:
        result = _drive(name, ["--config", str(a_directory)], monkeypatch)
    except Exception as exc:  # noqa: BLE001 - the failure this test exists to catch
        pytest.fail(
            f"cli/{name} raised {type(exc).__name__} for a directory --config instead of "
            f"returning exit 2. IsADirectoryError/PermissionError are OSError, NOT "
            f"FileNotFoundError — catch _common.CONFIG_LOAD_ERRORS: {exc}"
        )

    assert result == 2, (
        f"cli/{name} returned exit {result!r} for a directory --config; the "
        "deprived-env contract is a clean exit 2"
    )
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" not in err
    # Assert the phrases ONLY our message can produce. A substring like
    # "directory" is not safe here: the error echoes the config path, and
    # pytest builds tmp_path from the test's own name — which contains
    # "directory" — so that assertion passed even with the is_dir() check
    # deleted. Caught by mutation testing, 2026-09-04; it is the exact
    # "position-independent substring assertion" shape the pre-deploy rule
    # names, and it pinned the test's own name rather than the behavior.
    assert "not a file" in err and "did you mean" in err.lower(), (
        f"cli/{name} exited 2 but did not name the directory mistake. The OS "
        f"gives 'Permission denied' (errno 13) on Windows, which reads as an "
        f"ACL problem; _discover_config_path must say so itself. Got: {err!r}"
    )


@pytest.mark.parametrize("name", CLI_MODULES)
def test_a_malformed_settings_yml_exits_2_without_a_traceback(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A YAML syntax error is a clean exit 2, not a crash-loop.

    ``yaml.YAMLError`` is not a ``ValueError``; the old tuple let it out. Under
    ``restart: unless-stopped`` an uncaught traceback here is not a stop, it is
    an indefinite restart loop on a config the operator can fix in one edit.
    """
    bad = tmp_path / "settings.yml"
    # Unclosed flow mapping — a YAML syntax error, not a schema error, so it
    # fails inside safe_load rather than in pydantic validation.
    bad.write_text("live:\n  symbols: [BTC/USD\n", encoding="utf-8")

    try:
        result = _drive(name, ["--config", str(bad)], monkeypatch)
    except Exception as exc:  # noqa: BLE001 - the failure this test exists to catch
        pytest.fail(
            f"cli/{name} raised {type(exc).__name__} for a malformed settings.yml instead "
            f"of returning exit 2. yaml.YAMLError is NOT a ValueError — catch "
            f"_common.CONFIG_LOAD_ERRORS: {exc}"
        )

    assert result == 2, (
        f"cli/{name} returned exit {result!r} for a malformed settings.yml; under "
        "restart: unless-stopped anything else is a crash-loop"
    )
    assert "Traceback (most recent call last)" not in capsys.readouterr().err


class TestPreconditionRaisedFromTheAsyncBody:
    """A config precondition too deep to reach ``main()``'s handler.

    ``_require_cloud_key`` runs inside ``_main_async``, four frames below the
    config-load ``except``. It used to raise a bare ``ValueError`` there, and
    ``run_with_clean_exit`` only ever caught ``KeyboardInterrupt`` — so a
    missing ``ATLASCLOUD_API_KEY`` reached the operator as a traceback and
    then crash-looped under ``restart: unless-stopped``.

    That was live: ``.env.example`` told operators the Atlas key was inert
    while the deployed ``cpu-only`` profile set ``provider: atlas``, so this
    was the failure mode for anyone provisioning a host from the documented
    example file.
    """

    def _run(self, exc: BaseException, monkeypatch: pytest.MonkeyPatch) -> int:
        """Drive run_with_clean_exit with a coro that raises ``exc``."""
        import logging
        import os as os_mod

        from wobblebot.cli import _common

        captured: dict[str, int] = {}

        def fake_exit(code: int) -> None:
            captured["rc"] = code
            raise _Stop

        monkeypatch.setattr(os_mod, "_exit", fake_exit)

        async def body() -> int:
            raise exc

        with pytest.raises(_Stop):
            _common.run_with_clean_exit(body(), logger=logging.getLogger("test"))
        return captured["rc"]

    def test_operator_config_error_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from wobblebot.cli._common import OperatorConfigError

        rc = self._run(OperatorConfigError("ATLASCLOUD_API_KEY missing"), monkeypatch)
        assert rc == 2, (
            "an operator-fixable precondition must exit 2 like the config-load "
            f"path, not {rc}; anything else crash-loops under unless-stopped"
        )

    def test_the_message_reaches_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 2 alone is not the contract — the operator has to be told
        which variable to set, in the same shape main()'s handler uses."""
        from wobblebot.cli._common import OperatorConfigError

        self._run(OperatorConfigError("ATLASCLOUD_API_KEY missing from environment"), monkeypatch)
        err = capsys.readouterr().err
        assert "ATLASCLOUD_API_KEY" in err and err.startswith("error: ")

    def test_an_unrelated_value_error_is_still_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The narrowing half. Catching bare ValueError here would swallow
        genuine bugs in every daemon's async body and report them as a tidy
        config problem — which is why OperatorConfigError is a dedicated
        subclass rather than a widened except."""
        with pytest.raises(ValueError, match="a real bug"):
            self._run(ValueError("a real bug"), monkeypatch)


class _Stop(BaseException):
    """Sentinel standing in for os._exit, which does not unwind."""
