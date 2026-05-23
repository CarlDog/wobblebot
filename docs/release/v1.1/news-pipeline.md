# News pipeline — ingestion + reaction

*Entries here expand the news ingestion surface and close the loop between news signal and engine behavior.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Auto-pause on news-role HIGH risk

**What:** when the News role outputs a HIGH-risk advisory for
N consecutive advise cycles, cli/live automatically transitions
the affected symbol(s) to paused state. Pause is auto-triggered
but operator-revocable via the normal pending_commands flow.

**Why high value:** today the News role produces text advisories
that the operator reads via ``/news`` or in
``advisor_suggestions``. Nothing in cli/live consumes them
automatically. If the news role consistently flags HIGH risk
(FOMC, exchange exploit, halving, regulatory action) for the
next 24h, the operator has to manually pause. If the operator
is asleep / not watching, the bot keeps trading through events
the news layer specifically flagged as dangerous.

**Implementation:** new ``advisor.auto_pause_on_news_risk:
{enabled: bool, threshold_cycles: int}`` config. cli/live polls
``advisor_suggestions WHERE role='news' AND
risk_level='HIGH'`` count over the last N cycles; if threshold
crossed, calls ``engine.pause_symbol()`` for affected symbols
and writes a ``pending_command`` audit row marked
``triggered_by='auto-news-risk'`` for the audit trail.

**Why deferred:** **tension with ADR-002** (LLM is advisory
only). Auto-pause means the news-role LLM has direct authority
to halt the engine — a non-trivial loosening of ADR-002's
fragmentation invariant. Either ADR-002 gets ratified-with-
exception, or this stays manual. Real architectural decision,
not just feature work.

**Trigger:** post-v1.0 + after the advisor outcome evaluator
ships (need history of news-role HIGH calls to calibrate the
``threshold_cycles`` value + confirm the news role's false-
positive rate is low enough to delegate authority to).

### Kraken status news adapter — first-party exchange-impact feed

**What:** new ``KrakenStatusNewsAdapter`` (sister to the existing
``RssNewsAdapter`` and ``CryptoCompareNewsAdapter``) that polls
Kraken's status-page JSON endpoints and persists results as
``news_items`` rows tagged ``kraken_status``:

- ``https://status.kraken.com/api/v2/incidents.json`` — recent
  incident history
- ``https://status.kraken.com/api/v2/incidents/unresolved.json``
  — currently-active incidents
- ``https://status.kraken.com/api/v2/scheduled-maintenances.json``
  — upcoming + completed maintenance windows

**Why high value:** today's news pipeline pulls broad crypto
headlines via RSS + CryptoCompare. Kraken's first-party feeds
about *its own service* aren't in the mix. When Kraken declares a
maintenance window or has degraded service, that's news a trading
bot SHOULD know about directly — not via Twitter or RSS scraping
of third parties. The existing ``services/kraken_health.py``
SystemStatus probe (Stage 8.4.E) only tells us *current* state
(online / maintenance / cancel_only / post_only). It doesn't say
"scheduled maintenance Friday 2 AM-4 AM UTC" or "incident: order
placement degraded since 14:00 UTC." The status news adapter
fills that gap.

**Why high-trust + high-relevance:** entries from this source are
- **first-party** (Kraken talking about Kraken)
- **operationally specific** (exact times, affected services)
- **directly actionable** (operator can pre-emptively pause cli/live
  before a known maintenance window)

Makes it a natural input for the **auto-pause on news-role HIGH
risk** entry above. A future "incident affecting BTC/USD trading"
row from ``kraken_status`` would naturally trigger the operator-
approved (or, post-evaluator, auto-) pause flow.

**Implementation:** new ``adapters/kraken_status_news.py``
mirroring the existing news adapter pattern. Each StatusPage.io
record (incidents + maintenances) maps cleanly to ``NewsItem``:
the StatusPage ``id`` becomes ``external_id`` (the dedup key),
``name`` becomes ``headline``, the most recent update body
becomes ``body``, ``impact`` becomes a structured tag, the
affected ``components`` populate ``mentioned_coins``-equivalent
field. Cadence via existing ``schedules.news`` key (every 30m
by default). New news_dedup logic should NOT merge
``kraken_status`` entries with generic RSS coverage of the same
incident — operator wants both perspectives.

**Why deferred:** new adapter (not just a settings.yml edit);
deserves a single focused commit + tests against the StatusPage.io
API shape.

**Trigger:** post-v1.0. Reasonable first v1.1 piece because it
pairs naturally with the auto-pause feature above + extends the
already-proven news pipeline.

### News pipeline gap audit vs Kraken Pro's 16 sources

**Context (2026-05-23 soak day 6):** the operator compared
WobbleBot's news pipeline against the 16 sources Kraken Pro's home-
page News widget surfaces by default. 9 of the 16 are covered today
(direct RSS or via CryptoCompare aggregator: CoinDesk, CoinGape,
Decrypt, The Block, BeInCrypto, Cryptonews, crypto.news, Bloomberg
crypto section, Financial Times crypto section). The 7 uncovered
fall into four tiers with very different cost/value profiles —
this entry is the umbrella; v1.1 work decides which tiers to act
on.

**Tier 1 — quality crypto research, no public feed: Messari.**
Messari is the most-defensible "missing crypto source." Public RSS
endpoints (`/feed`, `/rss`, `/feed.xml`, `/atom.xml`,
`/feeds/news.xml`) all return 403 or 429 (bot-blocked / Astro error
page); Messari is also absent from CryptoCompare's aggregated
source list. They moved to a paid API model (``data.messari.io``
for market data; news + research behind their paid platform). To
add Messari coverage we'd need a paid-API adapter — not a one-line
settings change. **Trigger:** if a v1.1 operator wants research-
quality narrative coverage above what CoinDesk + The Block + Decrypt
provide, evaluate Messari's paid tier. New ``MessariNewsAdapter``
mirroring the cryptocompare adapter pattern; API key in env;
``messari:`` block in ``news:`` config. Defer until the demand
materialises — at present the existing sources cover the same
narrative beat at acceptable depth.

**Tier 2 — general news with macro-event signal: Reuters.** Public
RSS feeds exist (e.g. ``https://www.reuters.com/business/finance/rss``)
and would surface broad market-moving stories that crypto-only feeds
miss (Fed rate decisions, regulatory news, geopolitical shocks).
**Why deferred:** crypto-specific feeds already cover most market-
moving events with a 0-30min lag; adding Reuters increases volume
and dedup-pressure for marginal additional signal. Easy add when a
v1.1 operator wants broader macro context — single new entry in
``news.sources`` plus a quick dedup-tuning pass to make sure the
fuzzy-dedup module catches "Fed raises rates" headlines that appear
in both crypto and general feeds.

**Tier 3 — stocks-and-finance, crypto-incidental: Barron's,
MarketBest, MarketScreener, Stock Titan, PR Newswire, 24/7 Wall
St.** Crypto coverage is incidental; adding them inflates the
dedup/noise filter work for marginal signal. **Trigger:** if a v1.x
adds equities/futures grids, revisit Tier 3 sources — they become
core to that watchlist even though they're noise for spot crypto.

**Tier 4 — general tech, off-topic: 404 Media, 9to5Google, 9to5Mac.**
Kraken includes these because their home page is a general
dashboard, not a crypto-trading-specific feed. For WobbleBot they
are off-topic. **Decision:** don't add unless an operator
specifically asks.

**Adjacent v1.1 instrumentation gap surfaced during this audit:**
the existing ``CryptoCompareNewsAdapter`` throws away the
``source_info.name`` field on each item — so ``news_items.source``
shows ``cryptocompare`` rather than the original publisher (CoinDesk,
Bloomberg crypto section, etc.). Operators can see "we got 3882
items from cryptocompare" but not "how many of those originated
from CoinDesk vs Blockworks vs The Daily Hodl." Small fix:
``_row_to_news_item`` captures ``raw.get('source_info', {}).get('name')``
into a new ``publisher`` field on ``NewsItem``, schema migration
add a ``publisher`` TEXT column to ``news_items`` (default NULL so
older rows stay valid). Splits cleanly from the source-tier audit
above — separate v1.1 entry, separate commit.

**✅ Publisher-attribution gap shipped in v1.0 (2026-05-23,
`9dd8640`).** The operator promoted this gap into the v1.0 scope
during soak day 6 alongside the related click-through-URL request.
``NewsItem`` gained both ``publisher: str | None`` (from
``source_info.name``) and ``url: str | None`` (from the top-level
``url`` field for CryptoCompare and entry ``link`` for RSS). Schema
migration in ``_migrate_news_items_publisher_url`` is idempotent
PRAGMA-checked. ``news.html`` renders the headline as
``<a target="_blank" rel="noopener noreferrer">`` when ``url`` is
present and surfaces ``publisher`` as a small italic label next to
the source tag. The wider Tier 1-4 source-coverage work above
(Messari / Reuters / stocks-finance / general-tech) is still
deferred per the original deferral reasoning.

**Why deferred:** none of the Tier 1-4 work is gating v1.0 launch;
the soak's news pipeline ran 3882 cryptocompare + 460 direct-RSS
items in 24h across 8 sources, which is volume sufficient for the
news-role expert to keep producing calibrated outputs. The
instrumentation gap (publisher attribution) is observability-only;
no behavior changes hinge on it.

**Trigger:** post-v1.0 when (a) the news-role expert's signal
quality degrades and additional sources might help calibrate, OR
(b) the operator wants per-publisher source-quality metrics (which
requires the attribution fix first).
