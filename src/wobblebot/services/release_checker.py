"""GitHub release-check service — footer "update available" indicator (v1.1).

Polls GitHub's public releases API for wobblebot's latest tagged
release and compares it against the running version. Read-only, no
auth required — well under GitHub's 60/hr unauthenticated rate limit
at any sane poll cadence (default every 6h).

**Server-side only.** ``cli/web`` polls this from a background task
tied to its own process, never from the browser — keeps the
operator's dashboard activity from leaking to GitHub. See
``web/app.py`` for how the result is threaded into the footer.

**Never raises.** A network failure, a malformed response, or a
release tag that doesn't parse as a dotted version all degrade to
"no update available" rather than breaking the background poller or
the footer render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

_LOGGER = logging.getLogger("wobblebot.services.release_checker")

_DEFAULT_REPO = "CarlDog/wobblebot"
_DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ReleaseCheckResult:
    """Outcome of one release-check poll.

    Attributes:
        update_available: True iff a newer release than
            ``current_version`` was found.
        current_version: The version passed in (echoed for the
            footer's tooltip).
        latest_version: The latest release's version string (tag with
            a leading ``v``/``V`` stripped), or ``None`` if the check
            failed or the tag didn't parse.
        release_url: Link to the release's GitHub page, or ``None``.
    """

    update_available: bool
    current_version: str
    latest_version: str | None
    release_url: str | None


def _parse_version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse a dotted version string (``"1.10.0"``) into an int tuple.

    Tuple comparison then orders correctly where a naive string
    comparison wouldn't (``"1.10.0" < "1.9.0"`` lexicographically, but
    ``(1, 10, 0) > (1, 9, 0)`` as tuples). Returns ``None`` for
    anything that doesn't parse cleanly (pre-release suffixes like
    ``"1.1.0-rc1"``, non-numeric components) — callers treat that as
    "can't compare, assume no update" rather than guessing wrong.
    """
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


async def _fetch_latest_release_envelope(
    repo: str, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    """GET the 'latest release' envelope, or ``None`` on any failure."""
    try:
        response = await client.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        _LOGGER.warning(
            "release check failed; treating as no update available (repo=%s): %s: %s",
            repo,
            type(exc).__name__,
            exc,
            extra={"repo": repo, "error": str(exc), "error_type": type(exc).__name__},
        )
        return None
    return data if isinstance(data, dict) else None


def _extract_version_and_url(envelope: dict[str, Any]) -> tuple[str, str | None] | None:
    """Pull (version-without-v-prefix, release_url) from a release envelope."""
    tag = envelope.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    html_url = envelope.get("html_url")
    release_url = html_url if isinstance(html_url, str) and html_url else None
    return tag.lstrip("vV"), release_url


async def check_for_update(
    current_version: str,
    *,
    repo: str = _DEFAULT_REPO,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ReleaseCheckResult:
    """Fetch the latest GitHub release and compare against ``current_version``.

    Args:
        current_version: The running app's version (e.g.
            ``wobblebot.__version__``).
        repo: GitHub ``owner/repo`` to check.
        client: Optional pre-built ``httpx.AsyncClient`` (test seam).
            When ``None``, a throwaway client is created and closed
            for this single call.
        timeout_seconds: HTTP read timeout (only used when this
            function builds its own client).

    Returns:
        A :class:`ReleaseCheckResult`. ``update_available=False`` on
        any failure — this function never raises.
    """
    no_update = ReleaseCheckResult(
        update_available=False,
        current_version=current_version,
        latest_version=None,
        release_url=None,
    )
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        envelope = await _fetch_latest_release_envelope(repo, http)
    finally:
        if owns_client:
            await http.aclose()
    if envelope is None:
        return no_update

    parsed = _extract_version_and_url(envelope)
    if parsed is None:
        return no_update
    latest, release_url = parsed

    current_tuple = _parse_version_tuple(current_version)
    latest_tuple = _parse_version_tuple(latest)
    if current_tuple is None or latest_tuple is None:
        # Can't compare reliably -- still surface the version + link,
        # just don't claim an update is available.
        return ReleaseCheckResult(
            update_available=False,
            current_version=current_version,
            latest_version=latest,
            release_url=release_url,
        )

    return ReleaseCheckResult(
        update_available=latest_tuple > current_tuple,
        current_version=current_version,
        latest_version=latest,
        release_url=release_url,
    )


__all__ = ("ReleaseCheckResult", "check_for_update")
