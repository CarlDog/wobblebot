"""Web UI middleware skeletons (Stage 7.1.B placeholder).

The CSRF synchronizer-token middleware + per-IP rate-limit bucket
land in Stage 7.1.C alongside the actual login flow they protect.
This module exists in 7.1.B so the package import graph is stable;
the real implementations follow.
"""

from __future__ import annotations

# Placeholder. Stage 7.1.C will populate this module with:
# - CsrfMiddleware (synchronizer-token pattern per ADR-017 decision 7)
# - LoginRateLimitMiddleware (5 attempts / 60s per IP per ADR-017
#   decision 8)
