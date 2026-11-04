"""Fresh heuristic writes must not hide a budget-disabled cloud advisor."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools.healthcheck import main

from tests.services.test_health_response import operator_config
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli.operator import _heartbeat_alert_loop, _HeartbeatAlertTracker
from wobblebot.config.advisor import ArbitratorConfig, FallbackTarget
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.value_objects import Timestamp
from wobblebot.services.advisor_budget_health import advisor_uses_cloud
from wobblebot.services.daemon_health import (
    DaemonHealth,
    DaemonHealthThresholds,
    DaemonStatus,
    fetch_daemon_freshness,
)
from wobblebot.services.doctor import diagnose
from wobblebot.services.health_response import HealthObserver
from wobblebot.services.llm_call_streak import streak_from_rows

pytestmark = pytest.mark.unit
_EXAMPLE = Path(__file__).resolve().parents[2] / "config/settings.example.yml"


def _seed(path, rows, *, now):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE advisor_suggestions (created_at TEXT)")
        conn.execute("INSERT INTO advisor_suggestions VALUES (?)", (now.isoformat(),))
        conn.execute("CREATE TABLE daemon_heartbeats (daemon_name TEXT, last_seen TEXT)")
        conn.execute(
            "CREATE TABLE llm_calls (timestamp TEXT, role TEXT, success INT, error_kind TEXT)"
        )
        conn.executemany("INSERT INTO llm_calls VALUES (?,?,?,?)", rows)
        conn.commit()


def _denials(now, count=3, role="quant", kind="cost_cap_session"):
    return [
        ((now - timedelta(minutes=index + 1)).isoformat(), role, 0, kind) for index in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cost_cap_session", "cost_cap_daily"])
async def test_repeated_denials_degrade_fresh_advisor_and_isolate_roles(tmp_path, kind):
    now = datetime.now(UTC)
    path = tmp_path / "advisor #budget.db"
    rows = _denials(now, kind=kind)
    rows += [(now.isoformat(), role, 1, None) for role in ("gremlin", "operator", "risk")]
    _seed(path, rows, now=now)
    healths = await fetch_daemon_freshness(
        observe_db=None, advise_db=path, operator_db=path, now=now
    )
    advisor = next(row for row in healths if row.name == "cli/advise")
    assert advisor.status is DaemonStatus.DEGRADED
    assert advisor.last_seen == now
    assert "quant: 3 consecutive cost-cap denials" in advisor.detail
    assert kind in advisor.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "two", "operator", "recovered", "expired"])
async def test_legitimate_no_cloud_use_and_recovery_remain_fresh(tmp_path, case):
    now = datetime.now(UTC)
    rows = {
        "empty": [],
        "two": _denials(now, 2),
        "operator": _denials(now, role="operator"),
        "recovered": _denials(now) + [(now.isoformat(), "quant", 1, None)],
        "expired": _denials(now - timedelta(hours=25)),
    }[case]
    path = tmp_path / "advisor.db"
    _seed(path, rows, now=now)
    healths = await fetch_daemon_freshness(
        observe_db=None,
        advise_db=path,
        operator_db=path,
        now=now,
        require_advisor_budget_evidence=True,
    )
    assert next(row for row in healths if row.name == "cli/advise").status is DaemonStatus.FRESH


@pytest.mark.asyncio
@pytest.mark.parametrize("required,expected", [(True, "unknown"), (False, "fresh")])
async def test_missing_ledger_is_unknown_only_when_cloud_evidence_required(
    tmp_path, required, expected
):
    now = datetime.now(UTC)
    path = tmp_path / "advisor.db"
    _seed(path, [], now=now)
    healths = await fetch_daemon_freshness(
        observe_db=None,
        advise_db=path,
        operator_db=tmp_path / "missing.db",
        now=now,
        require_advisor_budget_evidence=required,
    )
    advisor = next(row for row in healths if row.name == "cli/advise")
    assert advisor.status == expected
    assert not (tmp_path / "missing.db").exists()


def test_non_budget_failure_breaks_cost_denial_streak():
    now = datetime.now(UTC)
    rows = [(stamp, ok, kind) for stamp, _role, ok, kind in _denials(now)]
    rows[1] = (rows[1][0], 0, "timeout")
    streak = streak_from_rows("quant", rows)
    assert streak.consecutive_failures == 3
    assert streak.consecutive_cost_denials == 1


def test_cloud_detection_respects_active_routes_and_explicit_fallbacks():
    config = load_resolved_config(_EXAMPLE, "cpu-only")
    assert advisor_uses_cloud(config)
    local = config.advisor.model_copy(
        update={"engine": "llm", "type": "single", "provider": "ollama", "fallbacks": []}
    )
    assert not advisor_uses_cloud(config.model_copy(update={"advisor": local}))
    inert_arbitrator = local.model_copy(
        update={
            "aggregator": "arbitrator",
            "arbitrator": ArbitratorConfig(
                provider="anthropic", model="fixture", prompt_file="fixture"
            ),
        }
    )
    assert not advisor_uses_cloud(config.model_copy(update={"advisor": inert_arbitrator}))
    heuristic = config.advisor.model_copy(update={"engine": "heuristic"})
    assert not advisor_uses_cloud(config.model_copy(update={"advisor": heuristic}))
    target = FallbackTarget(provider="anthropic", model="fixture")
    with_fallback = local.model_copy(update={"fallbacks": [target]})
    assert advisor_uses_cloud(config.model_copy(update={"advisor": with_fallback}))


def test_real_container_probe_reads_the_advisor_cost_ledger(tmp_path, monkeypatch, capsys):
    now = datetime.now(UTC)
    path = tmp_path / "actual-operator.db"
    _seed(path, _denials(now), now=now)
    config = load_resolved_config(_EXAMPLE, "cpu-only")
    config = config.model_copy(
        update={
            "advise": config.advise.model_copy(update={"db": str(path)}),
            "operator": config.operator.model_copy(update={"operator_db": str(path)}),
            # This optional dashboard path must not choose the daemon's cost ledger.
            "web": config.web.model_copy(update={"operator_db": str(tmp_path / "wrong.db")}),
        }
    )
    monkeypatch.setattr("tools.healthcheck.load_resolved_config", lambda **_kwargs: config)
    monkeypatch.setenv("WOBBLEBOT_HEALTH_DAEMON", "cli/advise")
    monkeypatch.delenv("WOBBLEBOT_HEALTH_URL", raising=False)
    assert main(["--container"]) == 1
    output = capsys.readouterr().out
    assert "degraded" in output and "3 consecutive cost-cap denials" in output
    assert not (tmp_path / "wrong.db").exists()
    # A deliberate switch to the free heuristic engine does not inherit a
    # stale cloud-seat alarm from the same cost ledger.
    config = config.model_copy(
        update={"advisor": config.advisor.model_copy(update={"engine": "heuristic"})}
    )
    assert main(["--container"]) == 0


def test_legacy_operator_alerts_on_budget_degradation_then_recovers():
    now = datetime.now(UTC)
    degraded = DaemonHealth(
        name="cli/advise",
        label="Trading Advisor",
        status=DaemonStatus.DEGRADED,
        last_seen=now,
        threshold_seconds=3600,
        detail="quant: 3 consecutive cost-cap denials (cost_cap_session)",
    )
    tracker = _HeartbeatAlertTracker(repeat_seconds=3600)
    (alert,) = tracker.evaluate([degraded], now)
    assert alert[0] == "warning"
    assert "budget blocked" in alert[1] and "cost-cap denials" in alert[2]
    assert tracker.evaluate([degraded], now + timedelta(minutes=1)) == []
    (repeat,) = tracker.evaluate([degraded], now + timedelta(hours=2))
    assert "still blocked" in repeat[1]
    (recovery,) = tracker.evaluate([replace(degraded, status=DaemonStatus.FRESH)], now)
    assert recovery[0] == "info"
    assert tracker.evaluate([replace(degraded, status=DaemonStatus.FRESH)], now) == []


@pytest.mark.asyncio
async def test_doctor_and_independent_observer_report_denials_and_recovery(tmp_path):
    now = datetime.now(UTC)
    advise_path = tmp_path / "advise.db"
    operator_path = tmp_path / "operator.db"
    _seed(advise_path, [], now=now)
    base = load_resolved_config(_EXAMPLE, "cpu-only")
    config = operator_config(operator_path)
    config = config.model_copy(
        update={
            "advise": base.advise.model_copy(update={"db": str(advise_path)}),
            "delivery": config.delivery.model_copy(update={"advise_db": str(advise_path)}),
        }
    )
    storage = SQLiteStorageAdapter(operator_path)
    await storage.connect()
    try:
        for timestamp, role, success, error_kind in _denials(now):
            await storage.save_llm_call(
                LLMCallRecord(
                    timestamp=Timestamp(dt=datetime.fromisoformat(timestamp)),
                    role=role,
                    provider="openai",
                    model="fixture",
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0,
                    success=bool(success),
                    error_kind=error_kind,
                )
            )
        findings = await diagnose(config, storage)
        finding = next(row for row in findings if row.code == "daemon:cli/advise")
        assert finding.status == "warning"
        assert "3 consecutive cost-cap denials" in finding.evidence["detail"]
        observer = HealthObserver(config, storage, started_at=now)
        assert await observer.poll(now=now) == 1
        assert await observer.poll(now=now) == 0
        (notification,) = await storage.get_notifications()
        assert notification.notification.level == "critical"
        assert "cost-cap denials" in notification.notification.message
        await storage.save_llm_call(
            LLMCallRecord(
                timestamp=Timestamp(dt=now),
                role="quant",
                provider="openai",
                model="fixture",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0,
                success=True,
            )
        )
        assert await observer.poll(now=now) == 1
        findings = await diagnose(config, storage)
        assert next(row for row in findings if row.code == "daemon:cli/advise").status == "ok"
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud_enabled", [True, False])
async def test_legacy_monitor_respects_cloud_to_local_switch(tmp_path, monkeypatch, cloud_enabled):
    now = datetime.now(UTC)
    path = tmp_path / "advisor.db"
    _seed(path, _denials(now), now=now)

    async def _one_poll(callback, **_kwargs):
        await callback()

    monkeypatch.setattr("wobblebot.cli.operator.run_poll_loop", _one_poll)
    notifier = AsyncMock()
    await _heartbeat_alert_loop(
        notifier=notifier,
        observe_db=None,
        advise_db=path,
        operator_db=path,
        thresholds=DaemonHealthThresholds(),
        stop_event=asyncio.Event(),
        # Other daemons have no evidence in this focused fixture.
        muted=frozenset({"cli/live", "cli/harvest", "cli/news", "cli/maintenance", "cli/observe"}),
        require_advisor_budget_evidence=cloud_enabled,
    )
    assert notifier.send_notification.await_count == int(cloud_enabled)
    if cloud_enabled:
        assert "cost-cap denials" in notifier.send_notification.await_args.args[0].message
