"""Phase 7 web UI package (ADR-016 + ADR-017).

Sibling to ``src/wobblebot/cli/`` — server-rendered Jinja2 + HTMX
dashboard for at-a-glance observability + ADR-013-firewalled
mutations (pause / resume / stop via ``pending_commands``).

Entry point: ``wobblebot.web.app.create_app(...)`` returns a FastAPI
instance the ``cli/web`` daemon hands to uvicorn. Sub-packages:

- :mod:`wobblebot.web.routes` — feature-area APIRouters (auth,
  cost, status, advisor, harvester, news, audit).
- :mod:`wobblebot.web.middleware` — session, CSRF, rate-limit.
- :mod:`wobblebot.web.auth` — password hashing + login flow.
- :mod:`wobblebot.web.dependencies` — FastAPI dependency factories
  threading StoragePort / OperatorService into routes.
"""

from wobblebot.web.app import create_app

__all__ = ["create_app"]
