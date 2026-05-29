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

**What:** investigate (and possibly implement) integration
between wobblebot and OpenClaw, the autonomous AI-agent
framework that crossed 100k GitHub stars in Feb 2026 and 250k
in March 2026 (overtook React as most-starred OSS). OpenClaw
connects LLMs to local systems + messaging + external APIs;
wobblebot's existing surfaces (Discord bot, web UI read
endpoints, future MCP server per the v1.1 entry) are natural
integration points for an OpenClaw agent that wants to monitor
or control wobblebot on the operator's behalf.

**Why deferred:** zero v1.0-blocking impact -- wobblebot already
runs end-to-end without OpenClaw. The integration question is
"do wobblebot operators ALSO use OpenClaw, and if so what
surface do they need exposed?" That's a community-signal
question we don't have data on yet.

**Likely integration surfaces (no implementation commitment):**

1. **MCP server** -- the existing v1.1 MCP-server entry in
   ``operator-ux.md`` (or wherever it lives) is the cleanest
   path. OpenClaw natively speaks MCP; if wobblebot exposes
   an MCP server with read + confirm-pending-command tools, an
   OpenClaw agent can introspect engine state and request
   actions through the ADR-002 firewall.
2. **Discord webhook ingestion** -- OpenClaw can already
   trigger wobblebot by posting to the Discord channel via
   the operator-allowlisted webhook (same path our
   ``tools/probe_discord_bot.py`` uses). No new code; just
   document the workflow.
3. **Web UI HTTP scraping** -- OpenClaw could scrape
   ``/dashboard`` / ``/health`` / ``/cost`` HTML. Crude but
   zero-effort on our side.

**What "support" might mean:**

- Lightest: a doc page ``docs/integrations/openclaw.md``
  walking through the webhook + MCP options for operators who
  want to wire wobblebot into their OpenClaw flow.
- Medium: ship a wobblebot-flavored OpenClaw "tool spec" or
  MCP descriptor that operators can drop into their OpenClaw
  config.
- Heavy: dedicated ``adapters/openclaw_transport.py`` if the
  community asks for tight integration.

**Trigger:** the first operator or community user asking "how do
I get OpenClaw to talk to wobblebot?" Operator-flagged
2026-05-24 ("openclaw has been grabbing a bunch of headlines
lately, let's make sure we support that when it comes time").
Research note: confirmed OpenClaw is an agent framework, not an
LLM, so this is integration work and not a model-compatibility
question.
