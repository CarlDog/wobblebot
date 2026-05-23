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
