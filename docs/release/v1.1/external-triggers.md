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

*Note: cloud-LLM provider pricing / model / API-surface re-verification
was consolidated into the **LLM provider drift watcher** entry in
`infrastructure.md` (2026-05-29) — it shares that entry's watcher
machinery rather than standing alone here.*

### OpenClaw integration — wobblebot as a callable tool

**Research status:** completed 2026-08-27. See the source-backed
[`OpenClaw integration assessment`](../../reference/openclaw-integration-assessment-2026-08-27.md).
The assessment is not an implementation commitment or ADR.

**What:** evaluate integration between wobblebot and
[`openclaw/openclaw`](https://github.com/openclaw/openclaw), a general agent/channel Gateway.
This is integration work, not model compatibility or a replacement for wobblebot's advisor.

**Assessment conclusion:** borrow narrow reliability and security patterns, but do not embed
OpenClaw or make it a financial control plane. If an operator demonstrates a real workflow, the
preferred integration is a generic, authenticated wobblebot-owned MCP service:

1. **Read-only first** -- expose existing typed status, health, notifications, cost, suggestion,
   outcome, and weather-report queries. The service receives no Kraken or other wobblebot secrets.
2. **Request creation only, if later justified** -- a separately ratified MCP mutation may queue a
   typed `awaiting_confirmation` command. It may never approve, reject, execute, rewrite config, or
   originate a withdrawal. Human confirmation remains in authenticated wobblebot web/Discord.

The old idea of a `confirm-pending-command` tool is rejected: letting the requesting agent approve
its own action collapses the ADR-002/ADR-034 human firewall. HTML scraping is also rejected as a
supported contract because it is brittle and loses typed semantics. A dedicated
`adapters/openclaw_transport.py` is not justified while portable MCP can serve other clients too.

**Existing zero-code probe:** `tools/probe_discord_bot.py` proves that an explicitly allowlisted
webhook identity can exercise the inbound Discord path. It cannot perform confirmation reactions,
so it is a controlled test/experiment rather than an approval mechanism or preferred durable
integration.

**Why implementation remains deferred:** wobblebot already runs end-to-end without OpenClaw, no
MCP server exists, and this research request does not prove that an operator runs OpenClaw or needs
a production workflow. The deployment-isolation and atomic-command prerequisites in the assessment
also outrank an external-agent surface.

**Implementation trigger:** an operator confirms an actual OpenClaw deployment and a concrete
read-only wobblebot query workflow. Any mutation request is a second trigger and requires its own
ADR after the prerequisite safety corrections.
