"""Tests for cli/maintenance daemon (Stage 8.2.D)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli import maintenance as cli_maintenance
from wobblebot.cli import maintenance_reconcile
from wobblebot.cli._common import PermanentAuthHalt
from wobblebot.config.cli import MaintenanceConfig
from wobblebot.domain.models import Order, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.ports.notifier import Notification

pytestmark = pytest.mark.unit


class _RecordingNotifier:
    """Captures every notification it's sent, for assertion."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send_notification(self, notification: Notification) -> None:
        self.sent.append(notification)


@pytest.fixture(autouse=True)
def _restore_wobblebot_logger() -> Iterator[None]:
    """Snapshot + restore the ``wobblebot`` logger config per test.

    Same fixture pattern as ``tests/cli/test_web.py`` —
    ``cli_maintenance.main()`` calls ``configure_logging`` which flips
    ``root.propagate = False`` on the ``wobblebot`` subtree.
    """
    root = logging.getLogger("wobblebot")
    snapshot_level = root.level
    snapshot_propagate = root.propagate
    snapshot_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.handlers = snapshot_handlers
        root.propagate = snapshot_propagate
        root.setLevel(snapshot_level)


def _make_sqlite_file(path: Path) -> None:
    """Tiny SQLite file with one row so VACUUM has something to do."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('hello')")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# _vacuum_all                                                           #
# --------------------------------------------------------------------- #


class TestVacuumAll:
    def test_runs_against_all_target_dbs(self, tmp_path: Path) -> None:
        dbs = []
        for name in ("live", "shadow", "operator"):
            p = tmp_path / f"{name}.db"
            _make_sqlite_file(p)
            dbs.append(p)
        ok = cli_maintenance._vacuum_all(dbs)
        assert ok == 3

    def test_missing_db_skipped_others_still_run(self, tmp_path: Path) -> None:
        existing = tmp_path / "live.db"
        _make_sqlite_file(existing)
        missing = tmp_path / "nope.db"
        ok = cli_maintenance._vacuum_all([existing, missing])
        assert ok == 1  # only the existing one


# --------------------------------------------------------------------- #
# _backup_all                                                           #
# --------------------------------------------------------------------- #


class TestBackupAll:
    def test_backs_up_every_target_and_prunes_retention(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        # Pre-seed 5 older backups so retention=2 keeps the new one + 1 old.
        for i in range(5):
            (tmp_path / f"backups").mkdir(exist_ok=True)
            (tmp_path / "backups" / f"live-2026010{i}-0000.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            keep_n_daily_backups=2,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 1
        # After the new backup write + retention prune, only 2 files survive.
        surviving = sorted(backup_dir.glob("live-*.db"))
        assert len(surviving) == 2

    def test_missing_db_skipped(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "nope.db")],
            backup_dir=str(tmp_path / "backups"),
            keep_n_daily_backups=7,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 0


# --------------------------------------------------------------------- #
# _verify_all (v1.1 restoration smoke test)                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestVerifyAll:
    async def test_verifies_latest_backup_for_each_target(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))
        # Produce a real backup via the same path _backup_all uses.
        assert cli_maintenance._backup_all(cfg) == 1

        verified = await cli_maintenance._verify_all(cfg, None)

        assert verified == 1

    async def test_skips_db_with_no_backup_yet(self, tmp_path: Path) -> None:
        """A DB added to target_dbs after the last backup cycle has no
        backup file yet -- this must be skipped, not treated as a
        verification failure."""
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(tmp_path / "backups"))

        notifier = _RecordingNotifier()
        verified = await cli_maintenance._verify_all(cfg, notifier)  # type: ignore[arg-type]

        assert verified == 0
        assert notifier.sent == []

    async def test_notifies_on_corrupt_backup(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # A garbled backup file -- simulates a torn/incomplete write.
        (backup_dir / "live-20260101-0000.db").write_bytes(b"not a real sqlite file" * 50)
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))

        notifier = _RecordingNotifier()
        verified = await cli_maintenance._verify_all(cfg, notifier)  # type: ignore[arg-type]

        assert verified == 0
        assert len(notifier.sent) == 1
        assert notifier.sent[0].level == "error"
        assert "live" in notifier.sent[0].title

    async def test_none_notifier_does_not_raise_on_failure(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "live-20260101-0000.db").write_bytes(b"garbage" * 50)
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))

        verified = await cli_maintenance._verify_all(cfg, None)

        assert verified == 0

    async def test_clean_backup_sends_no_notification(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        cfg = MaintenanceConfig(target_dbs=[str(src)], backup_dir=str(backup_dir))
        cli_maintenance._backup_all(cfg)

        notifier = _RecordingNotifier()
        await cli_maintenance._verify_all(cfg, notifier)  # type: ignore[arg-type]

        assert notifier.sent == []


# --------------------------------------------------------------------- #
# _prune_one_cycle                                                      #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestPruneCycle:
    async def test_no_source_db_configured_returns_zero(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            prune_source_db=None,
            archive_dir=str(tmp_path / "archive"),
        )
        deleted = await cli_maintenance._prune_one_cycle(cfg)
        assert deleted == 0

    async def test_missing_source_db_returns_zero(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            prune_source_db=str(tmp_path / "nope.db"),
            archive_dir=str(tmp_path / "archive"),
        )
        deleted = await cli_maintenance._prune_one_cycle(cfg)
        assert deleted == 0

    async def test_archives_and_deletes_old_rows(self, tmp_path: Path) -> None:
        observe_db = tmp_path / "observe.db"
        storage = SQLiteStorageAdapter(str(observe_db))
        await storage.connect()
        # 3 old snapshots (40 days old) + 2 fresh (1 day old).
        for days_ago in (40, 35, 31):
            await storage.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30000"), currency="USD"),
                Timestamp(dt=datetime.now(UTC) - timedelta(days=days_ago)),
            )
        for fresh_days_ago in (1, 0.5):
            await storage.save_price_snapshot(
                Symbol(base="BTC", quote="USD"),
                Price(amount=Decimal("30000"), currency="USD"),
                Timestamp(dt=datetime.now(UTC) - timedelta(days=fresh_days_ago)),
            )
        await storage.close()

        cfg = MaintenanceConfig(
            target_dbs=[str(observe_db)],
            prune_source_db=str(observe_db),
            archive_dir=str(tmp_path / "archive"),
            prune_price_snapshots_older_than_days=30,
        )
        deleted = await cli_maintenance._prune_one_cycle(cfg)
        assert deleted == 3
        # Verify remaining count.
        storage = SQLiteStorageAdapter(str(observe_db))
        await storage.connect()
        try:
            remaining = await storage.get_price_snapshots()
            assert len(remaining) == 2
        finally:
            await storage.close()


# --------------------------------------------------------------------- #
# Retention (ADR-036)                                                   #
# --------------------------------------------------------------------- #


async def _make_schema_db(path: Path) -> None:
    """Real-schema DB file via the adapter (so prunable tables exist)."""
    storage = SQLiteStorageAdapter(str(path))
    await storage.connect()
    await storage.close()


def _insert_old_notification(db_path: Path, *, days_ago: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO notifications (level, title, message, timestamp, context_json, "
            "forwarded, forwarded_at, created_at) VALUES ('info', 't', 'm', ?, '{}', 0, NULL, ?)",
            (stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


class TestResolveRetentionTargets:
    def test_resolves_known_tables(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            retention={"news_items": 90, "notifications": 30},
            news_db=str(tmp_path / "news.db"),
            operator_db=str(tmp_path / "op.db"),
        )
        resolved = cli_maintenance._resolve_retention_targets(cfg)
        assert ("news_items", tmp_path / "news.db", 90) in resolved
        assert ("notifications", tmp_path / "op.db", 30) in resolved

    def test_unknown_table_is_boot_error(self, tmp_path: Path) -> None:
        """The forensic ledger can't be named — 'trades' is not in the
        prunable registry, so boot refuses."""
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            retention={"trades": 90},
        )
        with pytest.raises(ValueError, match="unknown table"):
            cli_maintenance._resolve_retention_targets(cfg)

    def test_horizon_without_home_db_is_boot_error(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(
            target_dbs=[str(tmp_path / "live.db")],
            retention={"news_items": 90},
            news_db=None,
        )
        with pytest.raises(ValueError, match="needs maintenance.news_db"):
            cli_maintenance._resolve_retention_targets(cfg)

    def test_empty_retention_resolves_empty(self, tmp_path: Path) -> None:
        cfg = MaintenanceConfig(target_dbs=[str(tmp_path / "live.db")])
        assert cli_maintenance._resolve_retention_targets(cfg) == []


class TestRetentionPrunes:
    @pytest.mark.asyncio
    async def test_prunes_and_continues_past_failures(self, tmp_path: Path) -> None:
        """One target's DB is missing (logged, skipped); the other
        prunes — fail-soft, same shape as _vacuum_all."""
        op_db = tmp_path / "op.db"
        await _make_schema_db(op_db)
        _insert_old_notification(op_db, days_ago=120.0)
        _insert_old_notification(op_db, days_ago=1.0)
        targets = [
            ("news_items", tmp_path / "missing-news.db", 90),
            ("notifications", op_db, 90),
        ]
        deleted = cli_maintenance._retention_prunes(targets, tmp_path / "archive")
        assert deleted == 1
        archives = list((tmp_path / "archive").glob("notifications-*.csv.gz"))
        assert len(archives) == 1


class TestBackupDedupe:
    def test_fresh_backup_skips_cycle(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        fresh_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (backup_dir / f"live-{fresh_stamp}.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            min_backup_interval_hours=20.0,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 0
        assert len(list(backup_dir.glob("live-*.db"))) == 1

    def test_stale_backup_does_not_skip(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # Name-encoded stamp is months old even though mtime is now —
        # the dedupe must read the name, not mtime.
        (backup_dir / "live-20260101-0000.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            min_backup_interval_hours=20.0,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 1
        assert len(list(backup_dir.glob("live-*.db"))) == 2

    def test_zero_interval_disables_dedupe(self, tmp_path: Path) -> None:
        src = tmp_path / "live.db"
        _make_sqlite_file(src)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        fresh_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (backup_dir / f"live-{fresh_stamp}.db").write_bytes(b"")
        cfg = MaintenanceConfig(
            target_dbs=[str(src)],
            backup_dir=str(backup_dir),
            min_backup_interval_hours=0.0,
        )
        ok = cli_maintenance._backup_all(cfg)
        assert ok == 1


# --------------------------------------------------------------------- #
# run_reconcile_cycle / reconcile_symbols (cli/maintenance_reconcile, 2026-08-22)
# --------------------------------------------------------------------- #

BTC_USD = Symbol(base="BTC", quote="USD")
ETH_USD = Symbol(base="ETH", quote="USD")


def _fake_trade(trade_id: str, *, symbol: Symbol = BTC_USD, order_id: str | None = None) -> Trade:
    return Trade(
        id=trade_id,
        order_id=order_id or f"O-{trade_id}",
        symbol=symbol,
        side=OrderSide.BUY,
        price=Price(amount=Decimal("50000"), currency="USD"),
        amount=Amount(value=Decimal("0.001"), asset=symbol.base),
        fee=Decimal("0.02"),
        cost=Decimal("50"),
        executed_at=Timestamp(dt=datetime(2026, 5, 15, tzinfo=UTC)),
    )


class _FakeExchange:
    """Minimal ExchangePort shape: only get_trade_history is exercised.

    Returns the ACCOUNT-WIDE list unfiltered, mirroring the real
    adapter's account-wide TradesHistory (the service filters by
    symbol client-side). ``fail_with``, if set, raises on every call.
    ``call_count`` lets tests pin the one-fetch-per-cycle contract.
    """

    def __init__(
        self, trades: list[Trade] | None = None, *, fail_with: Exception | None = None
    ) -> None:
        self._trades = trades or []
        self._fail_with = fail_with
        self.call_count = 0

    async def get_trade_history(
        self, symbol: Symbol | None = None, limit: int = 100
    ) -> list[Trade]:
        self.call_count += 1
        if self._fail_with is not None:
            raise self._fail_with
        return list(self._trades)


class _ExplodingAdapter:
    """Stand-in for KrakenAdapter that fails the test if constructed —
    the guard-clause tests' mutation detector (2026-08-22 review: the
    earlier guard tests passed even with their guards deleted, because
    every OTHER precondition was also unmet and returned 0 anyway)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("guard failed: KrakenAdapter was constructed")


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _set_reader_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KRAKEN_READER_API_KEY", "public-half")
    monkeypatch.setenv("KRAKEN_READER_API_SECRET", "c2VjcmV0")  # base64("secret")


@pytest.mark.asyncio
class TestRunReconcileCycle:
    """Guard clauses of run_reconcile_cycle. Every test makes ALL other
    preconditions valid and monkeypatches KrakenAdapter with a class
    that raises AssertionError on construction, so the test fails if
    the one guard under test is removed — the earlier versions were
    mutation-blind (empirically shown to pass with their guards
    deleted)."""

    async def test_no_source_db_configured_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_reader_creds(monkeypatch)
        monkeypatch.setattr(maintenance_reconcile, "KrakenAdapter", _ExplodingAdapter)
        cfg = MaintenanceConfig(reconcile_source_db=None)
        clean = await maintenance_reconcile.run_reconcile_cycle(
            cfg, [BTC_USD], None, PermanentAuthHalt("test")
        )
        assert clean == 0

    async def test_no_symbols_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_reader_creds(monkeypatch)
        monkeypatch.setattr(maintenance_reconcile, "KrakenAdapter", _ExplodingAdapter)
        db_path = tmp_path / "live.db"
        _make_sqlite_file(db_path)
        cfg = MaintenanceConfig(reconcile_source_db=str(db_path))
        clean = await maintenance_reconcile.run_reconcile_cycle(
            cfg, [], None, PermanentAuthHalt("test")
        )
        assert clean == 0

    async def test_halted_returns_zero_without_touching_kraken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_reader_creds(monkeypatch)
        monkeypatch.setattr(maintenance_reconcile, "KrakenAdapter", _ExplodingAdapter)
        db_path = tmp_path / "live.db"
        _make_sqlite_file(db_path)
        cfg = MaintenanceConfig(reconcile_source_db=str(db_path))
        halt = PermanentAuthHalt("test")
        halt.halted = True
        clean = await maintenance_reconcile.run_reconcile_cycle(cfg, [BTC_USD], None, halt)
        assert clean == 0

    async def test_missing_source_db_file_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_reader_creds(monkeypatch)
        monkeypatch.setattr(maintenance_reconcile, "KrakenAdapter", _ExplodingAdapter)
        cfg = MaintenanceConfig(reconcile_source_db=str(tmp_path / "nope.db"))
        clean = await maintenance_reconcile.run_reconcile_cycle(
            cfg, [BTC_USD], None, PermanentAuthHalt("test")
        )
        assert clean == 0

    async def test_missing_reader_credentials_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KRAKEN_READER_API_KEY", raising=False)
        monkeypatch.delenv("KRAKEN_READER_API_SECRET", raising=False)
        monkeypatch.setattr(maintenance_reconcile, "KrakenAdapter", _ExplodingAdapter)
        db_path = tmp_path / "live.db"
        _make_sqlite_file(db_path)
        cfg = MaintenanceConfig(reconcile_source_db=str(db_path))
        clean = await maintenance_reconcile.run_reconcile_cycle(
            cfg, [BTC_USD], None, PermanentAuthHalt("test")
        )
        assert clean == 0


@pytest.mark.asyncio
class TestReconcileSymbols:
    """The diff-and-alert core, with a fake exchange and real in-memory
    storage — never needs real Kraken credentials."""

    async def test_clean_symbol_counts_as_clean_no_notification(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        trade = _fake_trade("T1")
        await storage.save_trade(trade)
        exchange = _FakeExchange([trade])
        notifier = _RecordingNotifier()

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD],
            notifier,  # type: ignore[arg-type]
            PermanentAuthHalt("test"),
        )

        assert clean == 1
        assert notifier.sent == []

    async def test_one_account_fetch_covers_all_symbols(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """2026-08-22 review: per-symbol fetches re-walked the identical
        account-wide TradesHistory once per symbol (N x 20 private
        calls against Kraken's account-wide limiter). Pins the fix:
        exactly ONE exchange call regardless of symbol count."""
        btc_trade = _fake_trade("T-BTC")
        eth_trade = _fake_trade("T-ETH", symbol=ETH_USD)
        await storage.save_trade(btc_trade)
        await storage.save_trade(eth_trade)
        exchange = _FakeExchange([btc_trade, eth_trade])

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD, ETH_USD],
            None,
            PermanentAuthHalt("test"),
        )

        assert clean == 2
        assert exchange.call_count == 1

    async def test_missing_trade_notifies_critical_and_is_not_clean(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """The exact confirmed incident shape: a Kraken trade absent
        from local storage (and NOT on a locally-open order) must
        notify at critical, not just log."""
        present = _fake_trade("PRESENT")
        missing = _fake_trade("MISSING")
        await storage.save_trade(present)
        exchange = _FakeExchange([present, missing])
        notifier = _RecordingNotifier()

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD],
            notifier,  # type: ignore[arg-type]
            PermanentAuthHalt("test"),
        )

        assert clean == 0
        assert len(notifier.sent) == 1
        assert notifier.sent[0].level == "critical"
        assert "BTC/USD" in notifier.sent[0].title

    async def test_trade_on_locally_open_order_is_deferred_not_paged(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """2026-08-22 review: the engine persists trades only at
        terminal order status, so a partial fill on an order still
        resting on the book sits in Kraken's history for hours/days
        with no local row — correct behavior, not a gap. Must log,
        never page."""
        open_order = Order(
            exchange_id="OID-RESTING",
            symbol=BTC_USD,
            side="buy",  # type: ignore[arg-type]
            price=Price(amount=Decimal("50000"), currency="USD"),
            amount=Amount(value=Decimal("0.002"), asset="BTC"),
            status="open",
            created_at=Timestamp(dt=datetime(2026, 5, 15, tzinfo=UTC)),
        )
        await storage.save_order(open_order)
        partial_fill = _fake_trade("T-PARTIAL", order_id="OID-RESTING")
        exchange = _FakeExchange([partial_fill])
        notifier = _RecordingNotifier()

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD],
            notifier,  # type: ignore[arg-type]
            PermanentAuthHalt("test"),
        )

        assert clean == 1, "a deferred trade is not a gap; the symbol is clean"
        assert notifier.sent == []

    async def test_none_notifier_does_not_raise_on_gap(self, storage: SQLiteStorageAdapter) -> None:
        exchange = _FakeExchange([_fake_trade("MISSING")])

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD],
            None,
            PermanentAuthHalt("test"),
        )

        assert clean == 0

    async def test_transient_fetch_failure_skips_cycle_without_halting(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """A non-auth ExchangeError (timeout, 5xx) on the account fetch
        must not count as a strike — only a confirmed-dead key should
        ever halt. The whole cycle skips (one fetch, nothing to diff)."""
        exchange = _FakeExchange(fail_with=ExchangeError("simulated timeout"))
        halt = PermanentAuthHalt("test")

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD, ETH_USD],
            None,
            halt,
        )

        assert clean == 0
        assert exchange.call_count == 1, "one account fetch, not one per symbol"
        assert halt.halted is False
        assert halt.strikes == 0

    async def test_third_permanent_auth_failure_across_cycles_halts(
        self, storage: SQLiteStorageAdapter
    ) -> None:
        """ADR-037: the halt object lives across cycles; three
        consecutive permanent-auth failures — one per daily cycle —
        halt the task and page once."""
        auth_error = ExchangeError("invalid key", codes=["EAPI:Invalid key"])
        exchange = _FakeExchange(fail_with=auth_error)
        halt = PermanentAuthHalt("test")
        notifier = _RecordingNotifier()

        for _ in range(3):
            clean = await maintenance_reconcile.reconcile_symbols(
                exchange,  # type: ignore[arg-type]
                storage,
                [BTC_USD],
                notifier,  # type: ignore[arg-type]
                halt,
            )
            assert clean == 0

        assert halt.halted is True
        halted_pages = [n for n in notifier.sent if "halted" in n.title.lower()]
        assert len(halted_pages) == 1, "the halt pages exactly once, on the third strike"
        assert halted_pages[0].level == "critical"

    async def test_unexpected_exception_is_contained(self, storage: SQLiteStorageAdapter) -> None:
        """Belt-and-suspenders daemon isolation: an exception that is
        neither WobbleBotPortError nor anything the hardened adapter
        should emit must still not escape (asyncio.gather in
        _main_async has no return_exceptions — an escape would kill
        all five maintenance tasks). Never a halt strike."""
        exchange = _FakeExchange(fail_with=ValueError("genuinely unforeseen"))
        halt = PermanentAuthHalt("test")

        clean = await maintenance_reconcile.reconcile_symbols(
            exchange,  # type: ignore[arg-type]
            storage,
            [BTC_USD],
            None,
            halt,
        )

        assert clean == 0
        assert halt.halted is False
        assert halt.strikes == 0

    async def test_local_storage_read_failure_is_fail_soft_and_never_pages(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A corrupt/unreadable local DB degrades exactly like an
        exchange failure: warning logged, no gap page (a broken local
        read is NOT evidence of a trade gap), other symbols unaffected.
        SQLite defers file validation past connect() for mode=ro, so
        the realistic failure surfaces at the first query — this
        exercises that actual path."""
        garbled = tmp_path / "corrupt.db"
        garbled.write_bytes(b"not a real sqlite file" * 10)
        bad_storage = SQLiteStorageAdapter(str(garbled), read_only=True)
        await bad_storage.connect()  # succeeds -- validation deferred
        exchange = _FakeExchange([_fake_trade("T1")])
        notifier = _RecordingNotifier()

        try:
            with caplog.at_level(logging.WARNING, logger="wobblebot.cli.maintenance"):
                clean = await maintenance_reconcile.reconcile_symbols(
                    exchange,  # type: ignore[arg-type]
                    bad_storage,
                    [BTC_USD],
                    notifier,  # type: ignore[arg-type]
                    PermanentAuthHalt("test"),
                )
        finally:
            await bad_storage.close()

        assert clean == 0
        assert notifier.sent == [], "a broken local read must never page a gap"
        assert any("reconcile failed for" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------- #
# --------------------------------------------------------------------- #
# main() pre-async-dispatch paths                                       #
# --------------------------------------------------------------------- #


class TestMain:
    def test_bad_config_path_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        rc = cli_maintenance.main(["--config", str(tmp_path / "nope.yml")])
        assert rc == 2
