"""Web UI route packages.

Each feature area lives in its own module exposing an ``APIRouter``
named ``router``; ``app.py`` mounts them via ``include_router``.

Stage 7.1.B ships the package init + the ``auth`` + ``pages``
stubs. Feature routers (``cost``, ``status``, ``advisor``,
``harvester``, ``news``, ``history``) land in Stages 7.2–7.4.
(``history`` was renamed from ``audit`` on 2026-05-25 to
disambiguate from the v1.1 Auditor daemon.)
"""
