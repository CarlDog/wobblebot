"""Local model metadata for legacy inference-only test handlers.

Preflight refusal/identity contracts have separate transport-level tests. This
wrapper leaves each existing inference/error assertion on its original handler.
"""

import httpx


def local_transport(handler):
    def dispatch(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        return handler(request)

    return httpx.MockTransport(dispatch)
