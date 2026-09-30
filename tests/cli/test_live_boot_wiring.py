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

    def direct_try_body_awaits(name: str) -> list[int]:
        """Lines where ``await name(...)`` is a DIRECT statement of a
        top-level ``try:`` body -- not nested under an if/for/while/with,
        where a guard could make it unreachable. The 2026-09-18 fix-round
        review wrapped the await in ``if False:`` and the presence-and-
        ordering check above stayed green (mutant M8b)."""
        found: list[int] = []
        for stmt in tree.body:
            if not isinstance(stmt, ast.Try):
                continue
            for inner in stmt.body:
                if (
                    isinstance(inner, ast.Expr)
                    and isinstance(inner.value, ast.Await)
                    and isinstance(inner.value.value, ast.Call)
                    and isinstance(inner.value.value.func, ast.Name)
                    and inner.value.value.func.id == name
                ):
                    found.append(inner.lineno)
        return found

    reconcile = awaited_call_lines("apply_reconciliation")
    resume = direct_try_body_awaits("_resume_pending_fill_trades")
    assert reconcile, "_main_async never awaits apply_reconciliation"
    assert resume, (
        "_main_async must await _resume_pending_fill_trades as a direct statement "
        "of a top-level try body"
    )
    assert min(resume) > max(reconcile), "boot resume must follow reconciliation"
