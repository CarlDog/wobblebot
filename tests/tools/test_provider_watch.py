"""Manual watch receipts survive restart without reseating models or rewriting prices."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tools.provider_watch import Observation, ingest

pytestmark = pytest.mark.unit


def observation(digest="a", kind="contract", age=2, **overrides):
    return Observation.model_validate(
        {
            "provider": "atlas",
            "kind": kind,
            "source_url": "https://docs.example.test/contract",
            "observed_at": datetime.now(UTC) - timedelta(days=age),
            "sha256": digest * 64,
            **overrides,
        }
    )


def test_baseline_change_dedup_and_distinct_model_owner(tmp_path):
    path = tmp_path / "watch.db"
    assert not ingest(path, [observation()])["changes"]
    snapshot = [observation("b", age=1)]
    report = ingest(path, snapshot)
    assert len(report["changes"]) == 1
    assert report["changes"][0]["owner"] == "provider-integrator"
    assert not ingest(path, snapshot)["changes"]
    ingest(path, [observation(kind="models")])
    model_event = ingest(path, [observation("c", kind="models", age=1)])["changes"][0]
    assert model_event["owner"] == "model-review-owner"
    assert "openai:pricing" in report["missing_coverage"]


def test_old_or_conflicting_snapshot_rolls_back_whole_batch(tmp_path):
    path = tmp_path / "watch.db"
    row = observation(age=1)
    ingest(path, [row])
    with pytest.raises(ValueError):
        ingest(path, [observation("b", age=2), observation(kind="models")])
    with pytest.raises(ValueError):
        ingest(path, [row.model_copy(update={"sha256": "b" * 64})])
    assert not ingest(path, [row])["changes"]
    assert "atlas:models" in ingest(path, [row])["missing_coverage"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_url": "https://example.test/?api_key=secret"},
        {"provider": "unowned"},
        {"raw_body": "private"},
        {"observed_at": datetime.now(UTC) + timedelta(days=1)},
    ],
)
def test_unreviewable_snapshot_refused(overrides):
    with pytest.raises(ValidationError):
        observation(**overrides)
