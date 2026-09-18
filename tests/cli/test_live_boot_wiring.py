"""Structural guards on cli/live's boot sequence (ADR-046).

Nothing in the suite can drive ``_main_async`` end to end (it needs a real
config, Kraken credentials and the operator storages), so wiring that lives
only there is pinned by reading the function object's own source, the same
remedy ``test_operator_supervision`` uses.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from wobblebot.cli import live as live_module

pytestmark = pytest.mark.unit


def test_main_async_awaits_the_boot_resume_after_reconciliation() -> None:
    """Nothing in the suite can drive ``_main_async`` (it needs a real
    config, Kraken credentials and the operator storages), and deleting the
    one line that calls ``_resume_pending_fill_trades`` left 124 cli/live
    tests green in the 2026-09-18 review. Same remedy as
    ``test_operator_supervision``: read the function object's own source and
    require the await, placed after the reconciliation that may write the
    markers it indexes. Rooted in the function object, so a rename breaks
    the import instead of quietly scanning nothing."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(live_module._main_async))).body[0]
    assert isinstance(tree, ast.AsyncFunctionDef)

    def awaited_call_lines(name: str) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == name
        ]

    reconcile = awaited_call_lines("apply_reconciliation")
    resume = awaited_call_lines("_resume_pending_fill_trades")
    assert reconcile, "_main_async never awaits apply_reconciliation"
    assert resume, "_main_async never awaits _resume_pending_fill_trades"
    assert min(resume) > max(reconcile), "boot resume must follow reconciliation"
