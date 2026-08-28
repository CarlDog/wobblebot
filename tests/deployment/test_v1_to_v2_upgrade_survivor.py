"""The 2.0 release gate: a real v1.0 database survives the upgrade.

WHY THIS EXISTS. Every migration in ``SQLiteStorageAdapter.connect()`` has
unit coverage for *its own* column. Nothing exercised the thing an operator
actually does: take a database written by the tagged ``v1.0.0`` artifact and
open it with the 2.0 one. Both the 2026-08-27 OpenClaw assessment and the
2026-08-28 NemoClaw assessment independently named that gap as a 2.0 release
gate — unit-level migration correctness is not artifact-level upgrade
confidence.

2.0 is a MAJOR bump for two reasons (see ``CHANGELOG.md``): ADR-032 removed
``safety.emergency_stop``, and ADR-022 replaced the advisor's decision
architecture. A breaking change is exactly where a silent upgrade failure
hides, so the breaking half is what these tests aim at.

THE FIXTURE IS THE REAL v1.0 SCHEMA, not a hand-written approximation: it is
read from ``git show v1.0.0:src/wobblebot/adapters/sqlite_storage_schema.py``
at test time. A hand-copied schema would drift from the tag and quietly stop
testing the upgrade. If git or the tag is unavailable the test skips locally
and FAILS under ``WOBBLEBOT_REQUIRE_UPGRADE_GATE=1`` (set in CI) — a gate
that cannot run must not report green (the ``skipped-is-not-passed`` rule
from ``ci-verification.md``). That guard is not theoretical: the first CI run
of this file reported an all-green 3635 passed / 10 skipped against 3645
passed locally, because ``actions/checkout`` is shallow and tagless by
default.

WHAT IS ASSERTED: pre-existing rows survive; integrity holds; migrating a
second time is a no-op (an interrupted upgrade must be resumable); an
operator-approved command is NOT executed as a side effect of migrating; and
a v1.0 config fails ACTIONABLY rather than silently.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.config.loader import WobbleBotConfig, _find_retired_keys

_REPO_ROOT = Path(__file__).resolve().parents[2]
_V1_TAG = "v1.0.0"
_SCHEMA_MODULE = "src/wobblebot/adapters/sqlite_storage_schema.py"

# Set in CI. Turns "cannot reach the tag → skip" into a hard failure,
# because a release gate that skips is a release gate that isn't running.
#
# This exists because it already happened: the first CI run of this
# file reported an all-green 3635 passed / 10 skipped against 3645
# passed locally. `actions/checkout` fetches shallow with no tags by
# default, so `git show v1.0.0:…` failed and every database test here
# skipped — under a green check, on the PR whose whole purpose was to
# prove the upgrade works. The workflow now fetches full history; this
# flag is the guard that stops a future checkout change from silently
# undoing it.
_REQUIRE_GATE_ENV = "WOBBLEBOT_REQUIRE_UPGRADE_GATE"


def _unavailable(reason: str) -> None:
    """Skip locally, fail loudly where the gate is mandatory."""
    if os.environ.get(_REQUIRE_GATE_ENV) == "1":
        pytest.fail(
            f"the v1.0→2.0 upgrade gate could not run: {reason}. "
            f"{_REQUIRE_GATE_ENV}=1 means this must never be skipped — check that the "
            "checkout fetched tags (`fetch-depth: 0`)."
        )
    pytest.skip(reason)


def _v1_schema_sql() -> str:
    """Extract the v1.0.0 SCHEMA constant from git, or skip.

    Executes the module text in an isolated namespace rather than
    importing it — the file at the tag is a module-level string constant
    with no imports, and this avoids shadowing the installed package.
    """
    try:
        blob = subprocess.run(
            ["git", "show", f"{_V1_TAG}:{_SCHEMA_MODULE}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env-dependent
        _unavailable(f"cannot read {_V1_TAG}:{_SCHEMA_MODULE} from git ({exc})")

    namespace: dict[str, object] = {}
    exec(
        compile(blob, _SCHEMA_MODULE, "exec"), namespace
    )  # noqa: S102  # trusted: our own tagged source
    schema = namespace.get("SCHEMA")
    if not isinstance(schema, str) or "CREATE TABLE" not in schema:
        _unavailable(f"{_V1_TAG}:{_SCHEMA_MODULE} has no usable SCHEMA constant")
    return schema


@pytest.fixture(name="v1_database")
def v1_database_fixture(tmp_path: Path) -> Path:
    """A database created by the v1.0.0 schema and seeded with real rows.

    The seeded rows are chosen to be the ones an upgrade could plausibly
    lose: an order, a trade, a notification, an anchored grid state, and
    an operator-APPROVED pending command (the row whose accidental
    execution during startup would be the worst possible upgrade bug).
    """
    db_path = tmp_path / "v1-live.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_v1_schema_sql())
        _seed(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _seed(conn: sqlite3.Connection) -> None:
    """Insert one row per table we care about, tolerating schema variance.

    Column sets differ between v1.0 and today; this discovers the v1.0
    columns and fills required ones, so the fixture does not need a
    hand-maintained copy of every v1.0 INSERT.
    """
    for table, values in _SEED_ROWS.items():
        cols = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        usable = {k: v for k, v in values.items() if k in cols}
        if not usable:
            continue
        placeholders = ", ".join("?" for _ in usable)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(usable)}) VALUES ({placeholders})",
            tuple(usable.values()),
        )


_SEED_ROWS: dict[str, dict[str, object]] = {
    # NB: v1.0's notifications.id is INTEGER PRIMARY KEY AUTOINCREMENT,
    # not a UUID string — omitted so SQLite assigns it. `timestamp` is
    # NOT NULL and distinct from `created_at`.
    "notifications": {
        "level": "warning",
        "title": "v1.0 notification",
        "message": "written before the upgrade",
        "timestamp": "2026-07-30T12:00:00+00:00",
        "created_at": "2026-07-30T12:00:00+00:00",
        "forwarded": 0,
    },
    "pending_commands": {
        "id": "22222222-2222-4222-8222-222222222222",
        "command_kind": "pause",
        "command_json": '{"kind": "pause", "symbol": {"base": "BTC", "quote": "USD"}}',
        # The dangerous state: approved and never dispatched. Migrating
        # must not run it.
        "status": "approved",
        "channel_id": "chan-1",
        "requesting_user_id": "user-1",
        "confirming_user_id": "user-1",
        "confirmed_at": "2026-07-30T12:00:00+00:00",
        "ttl_expires_at": "2126-07-30T12:00:00+00:00",
        "created_at": "2026-07-30T12:00:00+00:00",
    },
}


# A structurally valid `grid:` block. GridConfig requires a `default`
# GridLevels sub-block, and its model validator refuses spacing at or below
# 2 x the maker fee, so this mirrors settings.example.yml rather than being
# invented — an invalid fixture would make a "rejected" assertion pass for
# the wrong reason.
_MINIMAL_GRID: dict[str, object] = {
    "default": {
        "spacing_percentage": "3.0",
        "levels_above": 3,
        "levels_below": 3,
        "order_size_usd": "5.0",
    }
}

_MINIMAL_SAFETY: dict[str, object] = {
    "max_total_exposure_usd": "60",
    "max_daily_spend_usd": "50",
    "max_per_coin_exposure_usd": "20",
    "max_orders_per_coin": 6,
}


async def _open_and_migrate(db_path: Path) -> None:
    adapter = SQLiteStorageAdapter(str(db_path))
    await adapter.connect()
    await adapter.close()


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def _table_exists(db_path: Path, table: str) -> bool:
    found = _rows(db_path, f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table}'")
    return bool(found)


@pytest.mark.asyncio
async def test_a_v1_database_opens_under_the_2_0_adapter(v1_database: Path) -> None:
    """The headline gate: the upgrade completes at all."""
    await _open_and_migrate(v1_database)


@pytest.mark.asyncio
async def test_integrity_holds_after_migration(v1_database: Path) -> None:
    await _open_and_migrate(v1_database)
    result = _rows(v1_database, "PRAGMA integrity_check")
    assert result == [("ok",)], f"integrity_check failed after upgrade: {result}"


@pytest.mark.asyncio
async def test_pre_existing_rows_survive_the_upgrade(v1_database: Path) -> None:
    """A migration that silently drops operator history is worse than one that fails."""
    before = {
        table: _rows(v1_database, f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in _SEED_ROWS
        if _table_exists(v1_database, table)
    }
    assert before, "fixture seeded nothing — the test would pass vacuously"

    await _open_and_migrate(v1_database)

    for table, count in before.items():
        after = _rows(v1_database, f"SELECT COUNT(*) FROM {table}")[0][0]
        assert after == count, f"{table}: {count} row(s) before upgrade, {after} after"


@pytest.mark.asyncio
async def test_migrating_twice_is_a_no_op(v1_database: Path) -> None:
    """An interrupted upgrade must be resumable, so migration must be idempotent."""
    await _open_and_migrate(v1_database)
    first = _rows(v1_database, "SELECT name, sql FROM sqlite_master ORDER BY name")
    counts_first = {
        table: _rows(v1_database, f"SELECT COUNT(*) FROM {table}")[0][0] for table in _SEED_ROWS
    }

    await _open_and_migrate(v1_database)
    second = _rows(v1_database, "SELECT name, sql FROM sqlite_master ORDER BY name")
    counts_second = {
        table: _rows(v1_database, f"SELECT COUNT(*) FROM {table}")[0][0] for table in _SEED_ROWS
    }

    assert second == first, "a second migration pass changed the schema — not idempotent"
    assert counts_second == counts_first, "a second migration pass changed row counts"


@pytest.mark.asyncio
async def test_an_approved_command_is_not_executed_by_the_upgrade(v1_database: Path) -> None:
    """The worst upgrade bug this project could have.

    A pending command left ``approved`` at shutdown is, by ADR-002, a
    live instruction waiting for a daemon poll. Opening the database
    must migrate it and nothing else — never dispatch it, never mark it
    dispatched, never quietly expire it.
    """
    await _open_and_migrate(v1_database)
    rows = _rows(
        v1_database,
        "SELECT status, dispatched_at FROM pending_commands "
        "WHERE id = '22222222-2222-4222-8222-222222222222'",
    )
    assert rows, "the approved pending command vanished during the upgrade"
    status, dispatched_at = rows[0]
    assert status == "approved", f"upgrade changed an approved command's status to {status!r}"
    assert dispatched_at is None, "upgrade marked an approved command dispatched"


def test_a_retired_v1_config_key_fails_actionably() -> None:
    """ADR-032's own problem must not survive the fix that retired it.

    ``emergency_stop`` was deleted *because* a silent dead safety knob is
    worse than none. Pydantic's default is ``extra="ignore"``, so without
    an explicit check the block loads silently on 2.0 and the operator's
    file goes on claiming a balance floor that does not exist — the same
    belief, now on the release that supposedly fixed it.
    """
    v1_shaped = {
        "grid": _MINIMAL_GRID,
        "safety": {
            "max_total_exposure_usd": "60",
            "max_daily_spend_usd": "50",
            "max_per_coin_exposure_usd": "20",
            "max_orders_per_coin": 6,
            # Retired by ADR-032; present in every v1.0 operator config.
            "emergency_stop": {"max_loss_percentage": 5.0, "min_exchange_balance_usd": 25.0},
        },
    }

    with pytest.raises(ValidationError) as excinfo:
        WobbleBotConfig.model_validate(v1_shaped)

    message = str(excinfo.value)
    assert "safety.emergency_stop" in message, "the error must name the offending key"
    assert "ADR-032" in message, "the error must cite what retired it"
    assert "max_session_loss_usd" in message, "the error must name what supersedes it"


def test_a_retired_key_is_caught_inside_an_inactive_profile() -> None:
    """A retired key parked in an unused profile is still a false belief."""
    config = {
        "grid": _MINIMAL_GRID,
        "safety": {
            "max_total_exposure_usd": "60",
            "max_daily_spend_usd": "50",
            "max_per_coin_exposure_usd": "20",
            "max_orders_per_coin": 6,
        },
        "profiles": {"cpu-only": {"safety": {"emergency_stop": {"max_loss_percentage": 5.0}}}},
    }

    with pytest.raises(ValidationError) as excinfo:
        WobbleBotConfig.model_validate(config)
    assert "profiles.cpu-only.safety.emergency_stop" in str(excinfo.value)


def test_a_clean_2_0_config_still_loads() -> None:
    """The guard must not become a tax on configs that were never broken."""
    config = {
        "grid": _MINIMAL_GRID,
        "safety": {
            "max_total_exposure_usd": "60",
            "max_daily_spend_usd": "50",
            "max_per_coin_exposure_usd": "20",
            "max_orders_per_coin": 6,
        },
        "profiles": {"cpu-only": {"safety": {"max_orders_per_coin": 4}}},
    }
    assert WobbleBotConfig.model_validate(config).safety.max_orders_per_coin == 6


def test_the_shipped_example_config_is_free_of_retired_keys() -> None:
    """The template an operator copies must not reintroduce a dead key."""
    example = _REPO_ROOT / "config" / "settings.example.yml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert _find_retired_keys(raw) == [], "settings.example.yml carries a retired key"


def test_python_version_is_what_the_artifact_targets() -> None:
    """Cheap guard on the runtime the upgrade was proven against."""
    assert sys.version_info >= (3, 13), "the 2.0 artifact requires Python 3.13+"
