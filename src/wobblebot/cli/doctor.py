"""Read-only human/JSON diagnostics; no credentials, repairs or remote probes.

Exit 0: all observed findings healthy; 1: warning/unknown; 2: invalid configuration.
"""

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import CONFIG_LOAD_ERRORS, add_config_args, config_load_exit
from wobblebot.config.identity import runtime_identity
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.doctor import Finding, diagnose


async def inspect(config: WobbleBotConfig) -> list[Finding]:
    """Open only mode=ro; missing databases remain absent."""
    storage = None
    if config.operator is not None:
        candidate = SQLiteStorageAdapter(config.operator.operator_db, read_only=True)
        try:
            await candidate.connect()
            storage = candidate
        except StorageError:
            await candidate.close()
    try:
        findings = await diagnose(config, storage)
        if config.operator is not None:
            try:
                free = shutil.disk_usage(Path(config.operator.operator_db).resolve().parent).free
                findings.append(
                    Finding(
                        "disk-free",
                        "ok",
                        "Observed free bytes; no alert threshold adopted",
                        {"bytes": free},
                    )
                )
            except OSError:
                findings.append(
                    Finding("disk-free", "unknown", "Filesystem evidence unavailable", {})
                )
        return findings
    finally:
        if storage is not None:
            await storage.close()


def main() -> int:
    """Render stable machine records without logging config/environment content."""
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument("--json", action="store_true", help="Emit a single JSON document")
    args = parser.parse_args()
    try:
        config = load_resolved_config(args.config, args.profile)
    except CONFIG_LOAD_ERRORS as exc:
        return config_load_exit(exc)
    findings = asyncio.run(inspect(config))
    payload = {
        "schema_version": 1,
        "identity": runtime_identity(config.model_dump(mode="json"), args.profile),
        "findings": [finding.as_dict() for finding in findings],
    }
    output = logging.getLogger("wobblebot.doctor.output")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    output.addHandler(handler)
    output.propagate = False
    output.setLevel(logging.INFO)
    try:
        if args.json:
            output.info(json.dumps(payload, sort_keys=True))
        else:
            for finding in findings:
                output.info("%s %s: %s", finding.status.upper(), finding.code, finding.summary)
    finally:
        output.removeHandler(handler)
        handler.close()
    return int(any(finding.status != "ok" for finding in findings))


if __name__ == "__main__":
    raise SystemExit(main())
