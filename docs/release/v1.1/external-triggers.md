# External triggers — waiting on third parties

*Entries here hinge on third-party events (Kraken API changes, Kraken fee changes, the CryptoCompare evaluation deadline). Triggers are calendar- or vendor-driven, not soak-driven.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### CryptoCompare 90-day evaluation outcome

**What:** ADR-010's deferred decision. Due **2026-08-13**. If
CryptoCompare's free tier reliability hasn't met news-role needs,
swap to a different free source.

**Why deferred:** the 90-day window hasn't elapsed at v1.0.0 tag
time.

**Trigger:** **2026-08-13.** Calendar-driven, not soak-driven.

### Kraken API changes

**What:** Kraken occasionally updates its REST API (endpoint
deprecations, response-shape changes). The schema-drift tests in
`tests/config/test_schema_drift.py` and the `tests/integration/`
Kraken API drift tests are the early-warning system; the adapter
layer is the change point.

**Why deferred:** can't pre-empt. The integration test surface is
the canonical detection path.

**Trigger:** any integration test failure post-tag.

### Kraken trading fee changes

**What:** Stage 2.3 ratified "live taker fee is 0.40%, not the
mock's 0.26%". If Kraken's fee schedule shifts, the mock's
0.26% maker assumption may need updating.

**Why deferred:** can't pre-empt; the operator's first live trade
is the canonical detection event.

**Trigger:** any post-tag tiny live trade (`tools/first_real_trade.py`)
shows a different fee rate than the documented 0.40% taker / 0.26%
maker assumption.
