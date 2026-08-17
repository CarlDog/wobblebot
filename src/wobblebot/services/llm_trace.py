"""Ambient per-evaluation trace id for cloud-LLM call records (P4.4a).

A ``ContextVar`` carries the current trace id so the sanctioned LLM
plumbing (``services/llm_cloud_call.py``) can stamp
``LLMCallRecord.trace_id`` at record-build time without threading a
parameter through every ``AdvisorPort`` signature and adapter between
the caller and the chokepoint. asyncio-safe by construction: each task
sees its own value, so concurrent evaluations can't cross-stamp.

The producer side (``cli/advise``) opens one :func:`llm_trace` scope
per symbol-evaluation; every cloud call made inside — the quant
escalation, its retries, a future MoE fan-out — lands in ``llm_calls``
with the same trace id, which is what the ``/cost`` by-cycle view
groups on (P4.7). Outside any scope the ambient id is ``None`` and
records stay untraced, exactly as they were before this shipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TRACE_ID: ContextVar[str | None] = ContextVar("wobblebot_llm_trace_id", default=None)


def current_trace_id() -> str | None:
    """The ambient trace id, or ``None`` outside any :func:`llm_trace` scope."""
    return _TRACE_ID.get()


@contextmanager
def llm_trace(trace_id: str) -> Iterator[str]:
    """Scope every cloud-LLM record built inside to ``trace_id``.

    Re-entrant: an inner scope shadows the outer one and restores it on
    exit (ContextVar token reset), so nested callers can't leak their
    id into the enclosing evaluation.
    """
    token = _TRACE_ID.set(trace_id)
    try:
        yield trace_id
    finally:
        _TRACE_ID.reset(token)
