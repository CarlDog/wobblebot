# Trading scope — new instruments and markets

*Expansion entries carrying explicit operator-experience gates. See `standing-rules.md` for the margin/futures gate framing that survives every version boundary.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### High-frequency grid on high-volatility pairs

**What:** evaluate whether tight-spacing grid runs on memecoin /
small-cap pairs (DOGE/USD, PEPE/USD, SHIB/USD, BONK/USD) can
complete cycles on minute-scale timeframes — vs the current
BTC/USD configuration that lands ~5 cycles per WEEK.

**Conceivable in principle:** memecoins on Kraken regularly
oscillate 0.5-2% within 5-minute windows, which would let a grid
with ~0.7% spacing fill BUY/SELL pairs on the same timescale. The
engine already supports per-coin grid configs (`grid.coins.{...}`
in settings.example.yml).

**The fee math is tight:** Kraken charges 0.40% taker / 0.26%
maker. Each cycle eats 2 × fee. Spacing must be > 2 × maker
(~0.52%) just to break even on a maker-maker cycle; > 2 × taker
(~0.80%) on a marketable-marketable cycle. The config-validator
shipped in `8c1acfa` (Day 3) hard-rejects spacing ≤ 2 × maker
for exactly this reason. So the viable spacing band is narrow
(~0.6-1.0%) and assumes maker-side fills.

**Risks worth naming before any experiment:**

1. **Memecoin crashes don't bounce.** A grid keeps DCA-buying as
   price falls until the session-loss cap trips. The risk profile
   is meaningfully different from BTC — PEPE-style 30-50% one-day
   drawdowns are rare but real.
2. **Slippage and bid-ask spread.** Memecoins have wider posted
   spreads than BTC. A 0.7% grid spacing might be eaten by a
   0.3% bid-ask spread plus 0.2% slippage on small caps.
3. **Tax events scale linearly.** 100 cycles/day per asset =
   ~36,500 taxable events/year. Tools like Koinly charge per
   transaction at that volume; the operator's accounting setup
   isn't built for high-frequency yet.
4. **Adjacent to the operator-experience standing rule.** Memecoin
   trading is spot-only (not margin), so the explicit gate
   doesn't formally apply — but the spirit of the rule ("don't
   trade what you don't understand") does. Memecoin behavior is
   meaningfully different from BTC even on the same engine.

**Proposed experiment design (low blast radius):**

- Pair: `DOGE/USD` (safest of the high-vol options — deepest
  liquidity, most retail volume, longest track record on Kraken)
- Order size: $5 (matches the operator's BTC starter run)
- Spacing: 0.7% (above the 2 × maker validator floor)
- Levels: 2 above + 2 below ($20 total exposure)
- Runtime cap: 1 hour
- Session loss cap: $1.00 (tight; memecoins move fast)

After the 1-hour session, measure: cycles completed, gross spread
captured, net PnL after fees. If the math works, expand cautiously.

**Why deferred:** not a v1.0 blocker; conceptually adjacent to but
distinct from the multi-asset expansion entry below. v1.0 ships
BTC grid as the validated configuration. Logging here so the
"could we cycle every few minutes?" question doesn't get
re-asked from scratch later.

**Trigger:** operator wants to expand instrument coverage AND has
the tax / accounting pipeline ready for high cycle volume (Koinly
or similar at the transaction tier needed). Pair with the
multi-asset expansion entry below.

### Multi-asset / multi-exchange expansion

**What:** broaden wobblebot beyond Kraken-spot-crypto to additional
instruments and venues. Three independent threads:

1. **More Kraken crypto pairs** — turn on the `grid.coins.{DOGE,
   ADA, SOL, MATIC, ETH}` entries that already exist in
   `settings.example.yml`. Stage 2.4 made the engine multi-symbol;
   the only blocker is the operator's risk-budget allocation.
   Choppy alts (DOGE, MATIC) are arguably better grid candidates
   than BTC because they oscillate inside wider relative ranges.
   **Effort: trivial (config change).**
2. **Additional exchange adapters** — `BinanceAdapter`,
   `CoinbaseAdapter`, etc. The `ExchangePort` contract is small
   (~6 methods: `get_current_price`, `place_order`,
   `cancel_order`, `get_open_orders`, `get_balance`, asset-pairs
   metadata). New adapters multiply available instruments + de-
   risk single-exchange dependency. Each adapter is its own
   slice of work; signing schemes + rate-limit policies differ
   per exchange. **Effort: ~1 phase per exchange.**
3. **Kraken Securities equities (US stocks + ETFs)** — committed
   as the **Phase 9** development track per operator decision
   2026-05-20. See dedicated Phase 9 section in
   `docs/planning/roadmap.md` for slicing. **Forex remains a
   strategically interesting candidate** for whatever venue ever
   offers it through a wobblebot-shaped API (24/5 continuous +
   no PDT rules + often range-bound).

**Why deferred (threads 1 + 2):** v1.0 is single-exchange single-
asset-class (Kraken-spot-crypto) by scope choice. Adding more is
feature work, and v1.0 is in documentation freeze per
stage-8.4-design.md decision 3. Per-thread effort varies wildly —
turning on more Kraken crypto pairs is trivial; adding a new
exchange adapter is a phase of work.

**Trigger:** soak passes for current BTC/USD-only config; operator
wants to widen the grid surface; OR the operator gains confidence
in the engine and wants to allocate to additional instruments.
Soak observation that grids profit from chop = obvious extension
to additional choppy instruments wherever available.

### Configurable quote currency (non-USD: EUR / GBP / ...)

**What:** let an operator run wobblebot denominated in a fiat other
than USD (EUR, GBP, etc.) — a `quote_currency` setting plus a
settings-page selector. Surfaced 2026-05-29 when the operator noticed
Kraken's downloadable history splits by quote (XBTUSD vs XBTEUR);
relevant to the OSS audience (non-US deployers), not the operator's
own US-denominated use.

**Most of this already works** — the architecture is quote-agnostic by
construction:

- `Symbol` carries `quote` as a free field (its own docstring lists
  "USD, USDT"); `Symbol(base="BTC", quote="EUR")` is representable
  today.
- `KrakenAdapter` builds the pair from base+quote, so `XBTEUR` would
  resolve without code change.
- The metrics the advisor/heuristic read (`compute_volatility`,
  `compute_max_drawdown`, flatness) are *fractional* — currency-blind.
- The fee floor (2 × maker ≈ 0.52% spacing) is percentage-based —
  currency-blind.

**The actual work is naming + display + an audit, not a rearchitecture:**

1. **Rename / re-document the `*_usd` knobs.** `order_size_usd`,
   `max_per_coin_exposure_usd`, `max_total_exposure_usd`,
   `max_daily_spend_usd` are really *quote-denominated* — they'd
   function for EUR as-is, but the names lie. Either rename to
   `*_quote` or document that the number is in the configured quote.
2. **Display layer.** `cli/status` + the web status/cost cards
   hardcode `$` / "USD" labels; render the quote symbol instead.
3. **`quote_currency` setting** (default `USD`) + settings-page
   selector; thread into the display layer.
4. **Leave genuinely-USD things alone.** LLM cost tracking
   (`llm_pricing` / `llm_cost`) is USD because *provider billing* is
   USD regardless of the trading pair — NOT a quote-currency concern.
   The harvester→bank withdrawal is a separate currency question (what
   your bank takes).

**Scope guard:** one quote currency *per deployment* (default USD), not
mixed quotes within one bot. Mixing USD + EUR pairs would fragment the
balance view, the portfolio-value rollup, and the harvester threshold —
that complexity isn't worth it. Single quote per deployment is the
80/20.

**Why deferred:** v1.0 is USD-only by scope choice, and the operator's
own use is US-denominated, so there's no self-need — this is purely an
OSS-friendliness feature for non-US deployers. Pairs naturally with the
friend-deployment onboarding thread in `operator-ux.md`.

**Trigger:** a non-US user actually wants to deploy in EUR/GBP, OR the
friend-deployment onboarding work picks it up (a quote selector is a
natural wizard step). **Effort: small** — a config field, a display-
layer pass, and a one-time audit of the `*_usd` names.

**Cross-references:** orthogonal to the **Multi-asset / multi-exchange
expansion** entry above (that adds *base* instruments/venues; this
changes the *quote* denomination) and to friend-deployment onboarding
in `operator-ux.md`.

### Kraken Securities equities support (Phase 9 committed track)

**Status:** Operator-committed 2026-05-20. Will be Phase 9
following v1.0 tag. Detailed slicing lives in
`docs/planning/roadmap.md`'s Phase 9 section.

**What:** extend `KrakenAdapter` for the `asset_class` parameter
Kraken introduced in August 2025 for equities trading; add a
PDT-aware grid variant; integrate earnings-calendar awareness;
ship a tax export feature. Engine code largely unchanged; the
adaptation is in the adapter, safety layer, and operator-facing
tooling.

**Why this is a committed track, not a maybe-candidate:**

- **Strategic case is strong.** Equities provide genuine
  decorrelation from crypto (alt-to-alt crypto grids are highly
  correlated; stock-to-crypto less so). 11,000+ symbols vs. ~50
  liquid Kraken crypto pairs = vastly larger candidate universe.
  Volatile single-stocks (TSLA, NVDA, etc.) have wider daily
  ranges than BTC. Decorrelation × universe-size × volatility =
  real edge multiplier.
- **Architectural cost is modest.** Kraken added equities via an
  additive `asset_class` parameter on existing REST endpoints.
  No parallel API to integrate; same authentication, signing,
  rate-limits, error model.
- **The 24h M-F + commission-free combination on Kraken Pro**
  knocks out two of the historical equity-grid blockers.

**The central design constraint: PDT (Pattern Day Trader) rule.**

SEC limits accounts under $25k equity to 3 day-trades per 5
trading days. Kraken Securities LLC is FINRA-regulated; PDT
applies. **The operator's stated capital reality is "$25k is
aspirational; realistic trajectory is $100 → $1000+ via deposits
+ grid earnings over years."** Therefore the PDT-aware design
is the load-bearing piece of Phase 9, not a flag.

**Key insight:** PDT defines a "day trade" as buying AND selling
the same security WITHIN THE SAME TRADING DAY. Multi-day cycles
don't count. The grid for equities under PDT becomes a
"multi-day swing grid" with wider spacing (3-5% vs crypto's 1%)
so individual cycles span multiple sessions. Same engine code,
different configuration.

**PDT-aware engine behavior** (the Phase 9 hard requirement):
maintain a rolling 5-trading-day count of round-trip same-day
fills; refuse to place a counter-order that *could* complete a
same-day round-trip if it would push the count to 4 in 5 days.
The reconciler patterns from Stage 8.1 transfer cleanly here —
this is the same "engine knows its own history and respects
limits" shape.

**Realistic threshold for equities to make economic sense.**
Below ~$500 account equity, stock grids are too small to be
meaningful. $100 → $20 allocated to stocks → 3+3 grid at $3/order
= 0.0075 shares of TSLA per order = cycle profit in single cents.
So Phase 9 ships when the operator's account reaches a viable
threshold; **the design work happens in advance** so the project
is ready when the capital is.

**Open design questions for Phase 9 ADR-019 to settle:**

1. **PDT counter:** 5-trading-day rolling window vs. calendar-week
   approximation? Trading-day awareness adds calendar dependency
   (NYSE holiday calendar, half-days, early closes).
2. **Day-trade vs swing classification:** when does a "fill +
   counter-fill" pair count as a day trade for our purposes? At
   actual execution timestamp? At intent timestamp?
3. **Same-security wash-sale tracking:** the IRS wash sale rule
   disallows loss deductions for substantially identical
   securities bought within 30 days. The grid does this every
   cycle by design. Tax-export feature needs to flag/track
   wash-sale lots.
4. **Earnings-pause posture:** hard pause N days before/after
   announced earnings? Configurable per-symbol? Driven from
   what data source (Alpaca? EDGAR? a third-party calendar)?
5. **Cycle pacing under T+1 settlement:** can the counter-order
   place before settlement, or do we wait? Cash account rules
   are stricter; margin account rules introduce margin's risk.
6. **Multi-grid portfolio sizing:** capital allocator across
   crypto + stocks. Operator's $100 → $1000 trajectory means
   the allocator is a real concern, not a hypothetical one.

**Trigger to start Phase 9 work:** v1.0 tagged + soak observations
indicating the engine + operator workflow is stable. The capital
threshold for ACTIVATION (operator reaches ~$500+ account equity)
is a separate gate from the design-work threshold.

### Margin trading support

**What:** opt-in leveraged trading on margin-enabled symbols.
Kraken supports 2x-5x leverage on crypto-spot pairs. `KrakenAdapter`
grows a `leverage` parameter on AddOrder; the deferred-since-Stage-1
`Position` domain model finally lands ("margin-specific; spot
trading doesn't need it" — Stage 1's note explicitly anticipated
this future). Architecturally a clean extension; hex pays off.

**Why deferred (strongly):** **margin inverts the project's risk
model**. The entire v1.0 safety posture is bounded losses + no
single-actor money movement:

- ADR-002 (LLM advisory-only) + ADR-003 (Harvester sole transfer
  authority) keep the operator + engine as the only money-movers.
- Stage 2.3 hard caps + per-coin / per-session / per-day exposure
  limits bound the worst-case loss.
- Grid offside (ADR-006 decision 1) = engine parks honestly;
  worst outcome is paper drawdown until the operator intervenes.

Margin introduces a third money-mover the engine doesn't control:
**Kraken's liquidation engine**. Forced position closes happen
**outside** the GridEngine's safety check. A 20% adverse move at
5x leverage = account potentially zeroed before any internal cap
fires. Margin rollover fees (~0.01-0.02% per 4h on Kraken)
accrue continuously even when the bot isn't trading, eating into
the grid's ~$0.05/cycle profit margin.

**The 2026-05-19 outage stress-tested the v1.0 design exactly
right.** Bounded blast radius meant the worst outcome was one
orphaned $10 BUY's worth of BTC inventory; manual recovery cost
zero dollars. The same outage with 5x margin would have been
qualitatively different: rollover charges accruing for hours
while cli/live was dead, position potentially margin-called by
an adverse intraday tick. Adding margin would require ratifying
a new risk model.

**Minimum viable margin design (for if/when v1.2+ tackles it):**
- Opt-in per-symbol (`grid.coins.<COIN>.leverage`); default 1x
  preserves current spot behavior exactly.
- Separate margin-aware safety caps (`margin_call_distance_percentage`,
  `max_leverage_per_coin`, `margin_rollover_daily_budget_usd`).
- New `MarginCallNotification` class with `level=critical` for
  margin-call warnings (would be the first non-Harvester
  critical notification in the project).
- A new ADR ratifying the risk-model change explicitly. Margin
  is too load-bearing a decision to slip in as a feature flag.

**Trigger — STRONG OPERATOR EXPERIENCE GATE.** The operator
explicitly told Claude on 2026-05-20 that they have **no prior
margin trading experience** and asked Claude to be a guardrail.
Therefore margin should NOT be enabled until **all four** of
these have happened, in order:

1. **Sustained spot grid experience.** Many months of comfortable
   spot operation with wobblebot — long enough to have witnessed
   at least one strong adverse trend that the grid weathered.
   "Sustained" means the operator stops asking what offside
   means and starts predicting when it'll happen.

2. **Paper-trade margin separately.** Kraken offers a demo
   account; many other brokers also do. The operator runs a
   margin position (NOT through wobblebot) for at least 3-6
   months, manually, with daily tracking. The goal is to feel
   what a margin call looks like in real-time on a portfolio
   you're actively watching.

3. **Read at least one detailed post-mortem of a leveraged grid
   blow-up.** There are good public write-ups from the 2021-2022
   crypto crashes. The reading list should include at minimum
   one funding-rate-driven loss and one liquidation-cascade loss.

4. **Explicitly want leveraged exposure as a separate financial
   decision** — not "I want wobblebot to make more money," but
   "I have decided I want leveraged exposure to crypto and
   wobblebot is the vehicle." These are different decisions and
   conflating them is a common path into losses.

Only after ALL FOUR have happened should margin be considered
in scope. Until then, Claude should push back if the operator
asks for it, regardless of how the soak has gone. v1.2+ at the
absolute earliest; realistically v1.3+ or never.

### Futures trading support (long-short grid variant)

**What:** opt-in trading against Kraken Futures (futures.kraken.com
— separate platform from spot, with its own API endpoints + SDK +
signing scheme). New `KrakenFuturesAdapter` implementing
`ExchangePort` (or a futures-specialized port if the contract
surface diverges enough). New `Position` + `Contract` domain
entities; new SQLite tables for `positions` + `funding_payments`.

The strategically interesting capability is **long-short grids**:
above the anchor, place short-then-cover layers; below, long-then-sell.
Symmetric chop capture in both directions. The current spot grid
is structurally long-biased and bleeds in downtrends; futures
gridding could profit from chop regardless of trend direction.

**Why deferred (substantially):** futures is qualitatively further
from spot's risk model than margin is. Every con margin has,
futures has more pronounced:

- **Inherent leverage** (not opt-in; perpetual swaps typically
  start at 5x-50x depending on the contract).
- **Mark price liquidation** — exchanges use a combined mark
  formula (last + index + funding-adjusted); a spot crash can
  liquidate a futures position even when the futures price is
  briefly elsewhere.
- **Funding rate** — perpetuals pay/receive every 8h on Kraken
  Futures. Positive funding for the favored side, negative for
  the other. Can be INCOME (rare but real) or perpetual cost
  (common). The grid would need funding-rate-aware position
  sizing to avoid being on the wrong side.
- **Cascade mechanics** — insurance funds, auto-deleveraging
  (ADL), can force-close even non-liquidated positions during
  exchange stress.
- **Segregated balance** — futures wallets are separate from
  spot wallets on Kraken. Transfers between are operator
  actions; the engine can't auto-rebalance.
- **Substantial domain modeling** — `Position` (deferred since
  Stage 1) finally lands AND grows futures-specific fields
  (`mark_price`, `liquidation_price`, `unrealized_pnl`,
  `funding_paid`, `contracts`). New storage tables for
  positions + funding payment audit trail. Reconciler needs
  to handle "position" state in addition to "open orders".

**Compared to margin (which is already a v1.2+ candidate):**
- Margin = "the spot grid, but bigger" (architectural extension)
- Futures = "a different strategy variant" (long-short mechanics,
  funding-aware sizing, new domain model). **At minimum its own
  ADR**; arguably its own design doc since the strategy semantics
  diverge enough to be a different system that happens to share
  the engine substrate.

**Trigger — STRICTLY STRONGER OPERATOR EXPERIENCE GATE THAN
MARGIN.** Per the operator's 2026-05-20 statement of inexperience,
futures should NOT be enabled until **all four margin gates have
been cleared AND three additional futures-specific gates**:

(margin gates 1-4 above, plus:)

5. **Margin trading has actually been used through wobblebot in
   real money, not paper, for at least 6 months.** The operator
   has personally felt margin's behavior in this codebase, not
   just in paper.

6. **Specific futures literacy.** The operator can verbally
   explain (without looking it up): (a) what a perpetual swap
   is and how it differs from a dated future; (b) what the
   funding rate is, who pays whom, and on what schedule for
   Kraken Futures; (c) what mark price is and why it can
   differ from last-trade price; (d) what auto-deleveraging
   (ADL) means and when it fires.

7. **Read at least one good futures-grid blow-up post-mortem.**
   These exist; the funding-rate-flip-on-leveraged-long pattern
   is well-documented from 2021-2022. The operator names the
   specific scenario that scared them and how their proposed
   guardrails address it.

Explicitly NOT triggered by "I want more upside," "shorting
sounds cool," or "the soak passed so let's go bigger." If the
operator asks for futures before all 7 gates are clear, Claude
should push back unambiguously. v1.3+ at the absolute earliest;
realistically a research project that may never ship.

**Open question worth flagging now:** the long-short symmetry
property doesn't strictly require futures — spot wallets that
support short selling (via lending, e.g. Kraken's spot margin or
certain DeFi protocols) could achieve the same strategy with
different mechanics. If the operator ever wants the shorting
capability without futures' complexity, evaluating spot-shorting
through margin first is the smaller step.
