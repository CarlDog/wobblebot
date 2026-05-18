"""Web UI route packages.

Each feature area lives in its own module exposing an ``APIRouter``
named ``router``; ``app.py`` mounts them via ``include_router``.

Stage 7.1.B ships the package init + the ``auth`` + ``pages``
stubs. Feature routers (``cost``, ``status``, ``advisor``,
``harvester``, ``news``, ``audit``) land in Stages 7.2–7.4.
"""
