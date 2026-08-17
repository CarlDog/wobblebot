"""Tests for the ambient LLM trace scope (P4.4a)."""

from __future__ import annotations

import asyncio

import pytest

from wobblebot.services.llm_trace import current_trace_id, llm_trace

pytestmark = pytest.mark.unit


def test_no_scope_means_none() -> None:
    assert current_trace_id() is None


def test_scope_sets_and_restores() -> None:
    with llm_trace("cycle-1") as trace_id:
        assert trace_id == "cycle-1"
        assert current_trace_id() == "cycle-1"
    assert current_trace_id() is None


def test_nested_scope_shadows_and_restores_outer() -> None:
    with llm_trace("outer"):
        with llm_trace("inner"):
            assert current_trace_id() == "inner"
        assert current_trace_id() == "outer"
    assert current_trace_id() is None


def test_scope_restores_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with llm_trace("doomed"):
            raise RuntimeError("boom")
    assert current_trace_id() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_see_their_own_trace() -> None:
    """The ContextVar guarantee the module docstring claims: parallel
    evaluations cannot cross-stamp each other's records."""

    async def observe(trace_id: str) -> str | None:
        with llm_trace(trace_id):
            await asyncio.sleep(0)  # force an interleave point
            return current_trace_id()

    seen = await asyncio.gather(observe("task-a"), observe("task-b"))
    assert seen == ["task-a", "task-b"]
