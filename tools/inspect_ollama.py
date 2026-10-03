"""Read local Ollama model identity without inference, prompts, pulls or storage writes.

Run before a probe battery and keep this JSON with its receipt. This is observed
server metadata, not host attestation. Unknown digest/version returns exit 1.
"""

import argparse
import asyncio
import json
import logging
import sys

import httpx

from wobblebot.adapters.ollama_native import model_identity
from wobblebot.ports.exceptions import AdvisorError


async def inspect(base_url: str, model: str) -> dict:
    """Read only show/version/tags using bounded requests."""
    async with httpx.AsyncClient(timeout=10) as client:
        return await model_identity(client, base_url.rstrip("/"), model, AdvisorError)


def main() -> int:
    """Emit allowlisted metadata only, never server error bodies or prompts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    try:
        result = asyncio.run(inspect(args.base_url, args.model))
    except AdvisorError as exc:
        logging.error("Ollama inspection failed: %s", exc)
        return 2
    logging.info("%s", json.dumps({"schema_version": 1, **result}, sort_keys=True))
    return int(any(value is None for value in result.values()))


if __name__ == "__main__":
    raise SystemExit(main())
