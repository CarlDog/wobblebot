"""Every CLI must refuse an unknown ``--profile`` with exit 2, not a traceback.

WHY THIS EXISTS. The 2026-08-28 pre-2.0-tag deprived-environment sweep ran all
16 entry points through three deprived scenarios (bad ``--config`` path, no
``config/`` directory, unknown ``--profile``). 47 of 48 were clean.
``cli/operator`` was the one outlier: its config-load handler caught
``(FileNotFoundError, ValueError)`` while ``load_resolved_config`` raises
**KeyError** for an unknown profile — documented in that function's own
docstring — so the operator got a raw ``KeyError`` traceback and exit 1
instead of the actionable exit 2 every sibling produced.

The message inside the exception was already excellent (it lists the available
profiles and explains the likely cause). It simply never reached the operator,
because nothing caught it.

The bug was a *missing exception class in one tuple*, which no unit test of
``operator`` would ever notice — a grep would, but a grep test asserts source
text rather than behavior. So this drives each ``main()`` for real and asserts
the contract every CLI is supposed to honor. It is the same shape as
``tests/config/test_operator_catalog_ssot.py``: one test that pins a rule
across a family, rather than N tests that each pin one member.

Deliberately in-process rather than subprocess: an unknown profile fails at
config resolution, long before any adapter, network call, or credential read,
so no CLI can reach a side effect from here.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

# Every operator entry point that takes --profile. cli/lurker is an alias
# module for cli/observe with its own __main__, so it is listed on its own.
CLI_MODULES = [
    "advise",
    "apply",
    "harvest",
    "live",
    "lurker",
    "maintenance",
    "news",
    "observe",
    "operator",
    "preflight",
    "recalibrate",
    "sandbox",
    "screener",
    "shadow",
    "status",
    "web",
]

# Argv a CLI needs before it will even reach config resolution. Without these
# argparse exits first -- and it also exits 2, so the exit-code assertion alone
# would pass vacuously. That is exactly why this test additionally requires the
# error to NAME the bad profile: it is what distinguishes "refused the profile
# properly" from "never got that far."
_EXTRA_ARGV: dict[str, list[str]] = {
    # main() dispatches subcommands; needs one to reach config load.
    "web": ["serve"],
    # --target-balance is a required argument.
    "recalibrate": ["--target-balance", "100"],
}


@pytest.mark.parametrize("name", CLI_MODULES)
def test_unknown_profile_exits_2_without_a_traceback(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module: Any = importlib.import_module(f"wobblebot.cli.{name}")
    argv = ["wobblebot", *_EXTRA_ARGV.get(name, []), "--profile", "no-such-profile-xyz"]
    monkeypatch.setattr("sys.argv", argv)

    try:
        result = module.main()
    except SystemExit as exc:  # argparse-style exit
        result = exc.code
    except Exception as exc:  # noqa: BLE001 - the failure this test exists to catch
        pytest.fail(
            f"cli/{name} raised {type(exc).__name__} for an unknown --profile instead of "
            f"returning exit 2. Add the missing class to its config-load `except` tuple "
            f"(load_resolved_config raises KeyError for an unknown profile): {exc}"
        )

    assert result == 2, (
        f"cli/{name} returned exit {result!r} for an unknown --profile; the "
        "deprived-env contract is a clean exit 2 with an actionable message"
    )

    err = capsys.readouterr().err
    assert (
        "Traceback (most recent call last)" not in err
    ), f"cli/{name} printed a traceback for an unknown --profile"
    assert "no-such-profile-xyz" in err, (
        f"cli/{name} exited 2 but never named the bad profile; the operator " "cannot act on that"
    )
