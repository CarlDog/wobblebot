"""Ingest reviewed public provider snapshots into a local, deduplicated watch ledger.

No networking, schedules, messages, price edits or model changes. Input rows contain
only source fingerprints, not page bodies or account data. First observations form
baselines; changed fingerprints form review events owned by the named project role.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROVIDERS = {"kraken", "ollama", "ollama_cloud", "anthropic", "openai", "google", "atlas"}
OWNERS = {
    "contract": "provider-integrator",
    "pricing": "provider-integrator",
    "models": "model-review-owner",
}


class Observation(BaseModel):
    """Reviewed source identity and digest; raw page/account content is excluded."""

    model_config = ConfigDict(extra="forbid")
    provider: str
    kind: Literal["contract", "pricing", "models"]
    source_url: str = Field(max_length=2048)
    observed_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("provider")
    @classmethod
    def known_provider(cls, value: str) -> str:
        if value not in PROVIDERS:
            raise ValueError("provider is not in the maintained coverage register")
        return value

    @field_validator("source_url")
    @classmethod
    def public_source(cls, value: str) -> str:
        target = urlsplit(value)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
        ):
            raise ValueError("source must be an HTTPS URL without credentials/query/fragment")
        return value

    @field_validator("observed_at")
    @classmethod
    def aware_observation(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(UTC):
            raise ValueError("observation needs a non-future timezone-aware timestamp")
        return value.astimezone(UTC)


def ingest(database: Path, observations: list[Observation]) -> dict:
    """Transactionally preserve latest source evidence and each unique transition."""
    if not observations or len(observations) > 1000:
        raise ValueError("expected 1..1000 observations")
    keys = [(row.provider, row.kind, row.source_url) for row in observations]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate source in snapshot")
    events = []
    with closing(sqlite3.connect(database, timeout=5)) as connection, connection:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sources (provider TEXT, kind TEXT, url TEXT, digest TEXT, observed_at TEXT, PRIMARY KEY(provider,kind,url))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, provider TEXT, kind TEXT, url TEXT, before_digest TEXT, after_digest TEXT, observed_at TEXT, owner TEXT)"
        )
        connection.execute("BEGIN IMMEDIATE")
        for row in observations:
            key = (row.provider, row.kind, row.source_url)
            previous = connection.execute(
                "SELECT digest, observed_at FROM sources WHERE provider=? AND kind=? AND url=?", key
            ).fetchone()
            timestamp = row.observed_at.isoformat()
            if previous is not None and timestamp < previous[1]:
                raise ValueError("snapshot is older than the stored observation")
            if previous is not None and timestamp == previous[1] and previous[0] != row.sha256:
                raise ValueError("contradictory snapshot at the same timestamp")
            if previous is not None and previous[0] != row.sha256:
                event_id = hashlib.sha256(
                    json.dumps([*key, previous[0], row.sha256]).encode()
                ).hexdigest()
                owner = OWNERS[row.kind]
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, *key, previous[0], row.sha256, timestamp, owner),
                )
                if cursor.rowcount:
                    events.append(
                        {
                            "id": event_id,
                            "provider": row.provider,
                            "kind": row.kind,
                            "source_url": row.source_url,
                            "before_sha256": previous[0],
                            "after_sha256": row.sha256,
                            "observed_at": timestamp,
                            "owner": owner,
                        }
                    )
            connection.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?) ON CONFLICT(provider,kind,url) DO UPDATE SET digest=excluded.digest, observed_at=excluded.observed_at",
                (*key, row.sha256, timestamp),
            )
        coverage = {
            (row[0], row[1]) for row in connection.execute("SELECT provider,kind FROM sources")
        }
    expected = {
        (provider, kind)
        for provider in PROVIDERS
        for kind in OWNERS
        if provider != "kraken" or kind == "contract"
    }
    return {
        "schema_version": 1,
        "changes": events,
        "missing_coverage": [
            f"{provider}:{kind}" for provider, kind in sorted(expected - coverage)
        ],
        "delivery": "local report only",
    }


def main() -> int:
    """Validate a bounded JSON snapshot before opening the local ledger."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    try:
        if args.snapshot.stat().st_size > 1_048_576:
            raise ValueError("snapshot exceeds 1 MiB")
        payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("snapshot must be a JSON list")
        observations = [Observation.model_validate(row) for row in payload]
        report = ingest(args.database, observations)
    except (OSError, ValueError, sqlite3.Error) as exc:
        logging.error(
            "Provider watch failed (%s); inspect snapshot/ledger locally", type(exc).__name__
        )
        return 2
    logging.info("%s", json.dumps(report, sort_keys=True))
    return int(bool(report["changes"] or report["missing_coverage"]))


if __name__ == "__main__":
    raise SystemExit(main())
