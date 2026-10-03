"""Bounded startup fingerprints; never serialize config or environment values."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


def _fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def runtime_identity(config: dict[str, Any], profile: str | None) -> dict[str, Any]:
    """Return only allowlisted identity fields and hashes, not configuration data.

    Image identity is declared by the deployment, not independently attested
    from inside an unprivileged container. Host/image inspection must verify it.
    """
    revision = os.environ.get("WOBBLEBOT_BUILD_REVISION", "unknown")
    digest = os.environ.get("WOBBLEBOT_IMAGE_DIGEST", "unknown")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        revision = "unknown"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        digest = "unknown"
    assets: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for name, item in sorted(value.items()):
                visit(item, name)
        elif isinstance(value, list):
            for item in value:
                visit(item, key)
        elif key in {"prompt_file", "heuristic_file"} and isinstance(value, str):
            assets.append(_fingerprint(Path(value)))

    visit(config)
    lock = Path("/opt/wobblebot/requirements.lock")
    if not lock.is_file():
        lock = Path("requirements.lock")
    # Profile names are operator-controlled too: encode unexpected text as a hash.
    profile_label = profile or "base"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile_label):
        profile_label = "sha256:" + hashlib.sha256(profile_label.encode()).hexdigest()
    return {
        "revision": revision,
        "declared_image_digest": digest,
        "lock_sha256": _fingerprint(lock),
        "profile": profile_label,
        "resolved_config_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "asset_count": len(assets),
        "assets_sha256": hashlib.sha256(json.dumps(assets).encode()).hexdigest(),
        "unavailable_assets": assets.count("unavailable"),
    }
