# Infrastructure — CI, dependencies, packaging

*Build / test / dependency work. Most defer until contributors materialize or external triggers fire.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Kraken API schema drift coverage

**What:** expand ``tests/integration/test_kraken_drift.py`` to
assert response shapes against every endpoint the project
calls (Ticker, BalanceEx, AddOrder, CancelOrder, OpenOrders,
ClosedOrders, QueryOrders, TradeBalance, Assets, AssetPairs,
SystemStatus, Withdraw). Wire into CI (when CI lands) to run
weekly. Output: a single test that fails loudly when Kraken
changes a field name or response shape, before that change
silently breaks the engine.

**Why deferred:** CI doesn't exist yet (see "CI / GitHub
Actions" entry). Manual integration test run is the current
fallback.

**Trigger:** pair with the CI / GitHub Actions entry.

### CI / GitHub Actions

**What:** workflows for `make check` (test + lint + format), maybe
build-and-publish if the project ever distributes wheels.

**Why deferred:** single-operator project; `make check` locally is
sufficient. Stage 8.3 decision 8 also explicitly rejected CI perf
regression checks (CI runner variance makes them untrustworthy).

**Trigger:** the project gains contributors who can't run the
local pre-commit hooks.

### Test count growth

**What:** v1.0 ships with 1785 unit tests + 29 integration tests
(opt-in). Coverage is good but not exhaustive.

**Why deferred:** the global working-style rule's "Don't test the
impossible" applies — testing hypothetical edge cases that can't
happen under the system's constraints is busy-work.

**Trigger:** any soak-surfaced defect that the existing test suite
didn't catch. The test-for-the-bug is the canonical response.

### Python 3.14+ compatibility

**What:** the project requires Python 3.13+. Python 3.14 ships
2025 (already shipped at v1.0.0 tag time); compatibility check
deferred until v1.1.

**Why deferred:** v1.0 standardizes on 3.13 syntax/behavior.

**Trigger:** anyone on 3.14 reports test failure or a deprecation
warning we're not handling.

### SQLCipher — database encryption at rest

**What:** swap `aiosqlite` for an SQLCipher-aware binding so each
`.db` file under `data/` becomes a self-contained encrypted blob
(AES-256, transparent at the SQL layer). Adds Tier 1 protection
on top of OS-level disk encryption (Tier 0): cold backup files
that cross trust boundaries (USB drive, cloud storage, shared NAS
without volume encryption) stay encrypted.

**Why deferred from v1.0:** v1.0 covers the realistic single-
operator-local threat model via OS-level disk encryption (Tier
0 — see [`docs/deploy/encryption-at-rest.md`](../../deploy/encryption-at-rest.md)).
SQLCipher addresses the cloud/shared-storage deployment scenario
which isn't a v1.0 target.

**Why high-value at v1.1:** as deployments diversify (Synology
NAS, Raspberry Pi on UPS, cloud VPS — all weighed in the
"Always-on hosting topology" entry in operator-ux.md), backup
destinations increasingly cross trust boundaries. The moment a
WobbleBot backup file lands on object storage or an unencrypted
secondary volume, OS-level disk encryption stops applying. The
data carried in those backups (orders, trades, withdrawal
destinations, conversation turns) deserves the SQLCipher belt to
the OS-level suspenders.

**Implementation outline:**

1. **Binding selection.** `aiosqlite` doesn't natively support
   SQLCipher; need to evaluate `pysqlcipher3` + custom async
   wrapper, or `aiosqlitex` (community fork), or a different
   async path. None are as mature as `aiosqlite`; vet the chosen
   binding's release cadence + security advisory history.
2. **Key management.** Options: (a) derive key from
   `WOBBLEBOT_WEB_SESSION_SECRET` via PBKDF2 (reuses existing
   env-var infrastructure but couples web auth to DB encryption,
   which feels wrong); (b) new `WOBBLEBOT_DB_ENCRYPTION_KEY` env
   var (cleaner separation, more env-var surface); (c)
   file-based key with strict file perms (more friction, harder
   to rotate). Lean toward (b).
3. **Performance check.** SQLCipher adds ~5-15% overhead vs
   vanilla SQLite for most workloads. Run `tools/profile_storage`
   under encrypted + non-encrypted DBs; verify the p99s stay
   within tick-budget headroom.
4. **Migration.** Existing operator DBs are plaintext; need a
   one-shot tool that reads from plaintext + writes to encrypted
   format. Schema migration adjacent — same connect-then-migrate
   shape used by `_migrate_news_items_publisher_url` et al.,
   plus a one-shot encrypt-and-replace path.
5. **Backup encryption.** `cli/maintenance`'s backup task uses
   SQLite's online `.backup` API — verify SQLCipher backups
   stay encrypted (they should; the API operates at the same
   layer SQLCipher hooks into).
6. **Key rotation.** Operator may need to rotate the encryption
   key periodically. `PRAGMA rekey` handles this in SQLCipher;
   wrap as `tools/db_rekey.py`.

**Selective encryption decision worth flagging:** `observe.db`
and `news.db` hold public market data + RSS feed content —
nothing sensitive. Could remain unencrypted to save the
performance overhead. But mixed-encryption introduces operator
mental load ("which DBs need the key on connect?"). Cleanest
posture: encrypt all of them with the same key; the perf hit on
the read-heavy DBs is the cost of operational simplicity.

**Trigger:** v1.1 deployment plans materialize that involve
backups crossing trust boundaries (cloud destinations, shared
storage). Until then, OS-level disk encryption + the existing
plaintext-but-bcrypted-passwords posture is the right answer.

**Companion:** the "Always-on hosting topology" entry in
operator-ux.md weighs Synology/Pi/cloud deployment shapes;
SQLCipher becomes load-bearing the moment that entry picks
a cloud or shared-storage path.

### Test fixture consolidation — bare ":memory:" SQLite storage

**What:** 40 test files each declare an identical
``@pytest_asyncio.fixture async def storage()`` that constructs
``SQLiteStorageAdapter(":memory:")``, connects, yields, closes.
Same 4 lines × 40 files = ~160 LOC of mechanical duplication.

**Why deferred from v1.0:** audit-rated HIGH severity (#11 in
the 2026-05-23 code-reuse pass) but the practical migration cost
exceeds the win. Field names vary across the suite (``storage``
vs ``operator_storage`` vs ``live_storage`` vs ``news_storage``)
which means a shared ``memory_storage`` fixture in
``tests/conftest.py`` would require renaming the fixture
PARAMETER in every test method's signature too — invasive across
~38 files and N test methods each. The bug-prevention framing
("a pragma change requires editing 38 files") is already
mitigated because ``SQLiteStorageAdapter.connect()`` owns the
pragma setup (WAL + ``synchronous=NORMAL`` per Stage 8.3), not
the fixture body.

**Trigger:** if the SQLite setup discipline ever needs to grow
beyond what ``connect()`` owns — e.g., uniform ``caplog``
attachment, foreign-keys-on verification at fixture level, or a
new pragma that's NOT adapter-owned — then 40-file uniformity
matters and the rename effort becomes worth it.

**Sketch:** ``tests/conftest.py`` gets
``@pytest_asyncio.fixture async def memory_storage`` (bare
connect/yield/close). Each per-file ``storage`` fixture becomes
a one-line alias: ``@pytest.fixture; def storage(memory_storage):
return memory_storage`` — or fixture-renaming sweep across the
suite (preferred but bigger).

### WiredSnapshot base class + load_with_degrade helper for web routes

**What:** 5 of 6 web-route snapshot dataclasses share a
``wired: bool`` (or ``live_wired: bool``) + ``error: str | None``
shape; all 6 ``_load_snapshot`` functions share a 3-branch
"unwired / StorageError / success" skeleton. The audit's #7 + #8
findings propose a ``WiredSnapshot`` base in
``src/wobblebot/web/snapshots.py`` and a ``load_with_degrade``
helper in ``src/wobblebot/web/routes/_common.py``.

**Why deferred from v1.0:** the inheritance approach is
half-broken by naming inconsistency:

- ``AdvisorSnapshot`` / ``HarvesterSnapshot`` / ``NewsSnapshot``
  use ``wired: bool``.
- ``StatusSnapshot`` / ``TradingFeesSnapshot`` use
  ``live_wired: bool``.
- ``CostSnapshot`` has no wired flag at all (operator.db is
  mandatory, never None).

So a single base class covers 3 of 6 cleanly; the other 3 need a
field rename + template + route changes spread across 8+ sites
before the base class earns its keep. The load-helper alone
(without the base) saves only ~3 lines per file × 6 = ~18 LOC,
which is below the threshold for a focused refactor.

**Trigger:** add a new dashboard surface that needs the
unwired-fallback pattern (the natural moment when the absence of
the helper would force a 7th copy of the boilerplate). At that
point: rename ``live_wired`` to ``wired`` in the existing
StatusSnapshot + TradingFeesSnapshot + templates as the same
commit, then introduce the base class.

**Bug class the deferral keeps open:** a future snapshot author
who forgets the ``wired`` field on a new cross-DB dashboard.
Until extraction, code review (and the test suite — every web
test asserts against the snapshot fields) catches this.
