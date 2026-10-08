"""Doctor reads evidence without creating stores, probing providers or exposing secrets."""

import json
import sqlite3
from contextlib import closing

import pytest

from tests.services.test_delivery import seed
from tests.services.test_health_response import operator_config
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import doctor

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_unavailable_or_old_storage_is_unknown_and_never_migrated(tmp_path, existing):
    path = tmp_path / "operator.db"
    if existing:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE old_schema(id INTEGER)")
            connection.commit()
    before = path.read_bytes() if existing else None
    findings = await doctor.inspect(operator_config(path))
    assert any(row.status == "unknown" for row in findings)
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.asyncio
async def test_old_uncertain_delivery_is_not_hidden_by_recent_successes(tmp_path):
    path = tmp_path / "operator.db"
    storage = SQLiteStorageAdapter(path)
    await storage.connect()
    try:
        identifier = await seed(storage)
        await storage.claim_notification_delivery(identifier)
        for _ in range(105):
            await seed(storage)
        findings = await doctor.inspect(operator_config(path))
        delivery = next(row for row in findings if row.code == "delivery")
        assert delivery.status == "warning"
        assert delivery.evidence["rows"][0]["id"] == identifier
    finally:
        await storage.close()


def test_json_output_is_one_document_without_credentials(tmp_path, monkeypatch, capsys):
    config = operator_config(tmp_path / "absent.db")
    monkeypatch.setattr(doctor, "load_resolved_config", lambda *_args: config)
    monkeypatch.setattr("sys.argv", ["doctor", "--json"])
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "sensitive-fixture-token")
    assert doctor.main() == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["findings"] and payload["identity"]
    assert "sensitive-fixture-token" not in output
    assert not (tmp_path / "absent.db").exists()
