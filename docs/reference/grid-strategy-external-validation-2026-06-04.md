# Grid Strategy — External Evidence Validation (2026-06-04)

**What this is.** An independent check of WobbleBot's internal grid/advisor verdict
against *trusted, third-party finance literature* — not the project's own backtester.
It exists because every prior conclusion came from one in-house tool; if that tool has a
bug or a baked-in assumption, the conclusion inherits it. This document tests the major
claims against outside sources, grades each source's quality harshly, and is explicit
about where the outside world **confirms**, **only-theory-supports**, or **can't reach**
the internal claims.

**Companion docs.** The internal research synthesis is
[`grid-strategy-research-synthesis-2026-05-30.md`](grid-strategy-research-synthesis-2026-05-30.md)
(the backtest + regime experiments). This doc validates that synthesis from outside.

**Method.** A 9-agent workflow: 6 web researchers (one per claim) that *fetched* and
graded sources, an adjudicator that counted only high/medium-quality non-vendor sources,
and a **citation-verification skeptic** that independently re-fetched the load-bearing
URLs. Result: **10/10 cited sources verified real, 0 fabricated.** The grid-bot web
landscape is ~80% exchange/bot-vendor marketing (conflicted) — all graded *low* and
**excluded** from the verdicts.

---

## The honest headline

**Outside evidence STRENGTHENS the internal verdict — but unevenly, and almost entirely
at the level of *why* the strategy is weak, not the *exact numbers* WobbleBot measured.**

- The **mechanisms** (a grid is a short-volatility bet that loses in trends; fees set a
  spacing floor; market-timing almost never pays; active-after-costs loses to passive)
  are confirmed by good academic work and, for the active-vs-passive backdrop, by
  gold-standard industry data.
- The **specific statistics** from WobbleBot's backtester (the "22–44% of windows," the
  "~71% worse drawdown," the "+19.8% vs +67.8%") are found *nowhere* outside WobbleBot.
  No independent study ran those exact tests. They are neither confirmed nor refuted —
  they stand alone.
- The strongest external support is **theory plus a strong analogy from a different
  field** (equities mutual funds → crypto grid bot). Direct, peer-reviewed study of a
  *plain crypto grid vs. holding* is genuinely thin — effectively **one** usable paper.

**Nothing external contradicts the core thesis** that a tiny-capital static crypto grid
is a poor bet versus holding; **nothing external proves the specific numbers** either.

---

## The verified sources

Every URL below was independently fetched and confirmed by the citation skeptic.

**Grid is a no-edge / short-volatility structure:**
- Chen, Chen & Jang, *Dynamic Grid Trading Strategy: From Zero Expectation to Market
  Outperformance* — **arXiv:2506.11921**. Proves a static grid's expected return is
  *essentially zero* before fees, **net-negative after**. (A preprint; authors are
  motivated to make the plain grid look weak; headline numbers are for their *own*
  enhanced variant.)
- Lempérière / Bouchaud et al., *Risk Premia: Asymmetric Tail Risks and Excess Returns*
  — **arXiv:1409.7720**. Short-volatility strategies carry **negative skew** (small
  steady gains, rare large losses); trend-following is the mirror image.

**Fee floor (confirmed):**
- Martin & Schöneborn, *Mean Reversion Pays, but Costs* (RISK 24(2), 2011) —
  **arXiv:1103.4934**. *Formally proves* a mean-reversion strategy needs a no-trade
  buffer that **grows with the per-trade cost**.
- Frazzini, Israel & Moskowitz, *Trading Costs of Asset Pricing Anomalies* — the most
  reversion-like, highest-frequency strategy is the most cost-constrained (on ~$1T of
  real trades).

**Market timing is brutally hard (confirmed):**
- Estrada (2009), *Black Swans, Market Timing and the Dow*, Applied Economics Letters —
  missing the best 10 of ~29,190 Dow days costs **65% of terminal wealth**.
- Sharpe (1975) — you need roughly **70–74% call accuracy** just to match holding.

**…but timing/regime models are NOT categorical losers (the disconfirming evidence):**
- Bulla et al. (2011), *J. of Asset Management* — a realistic regime strategy that **is
  profitable after costs**, but the benefit is ~**41% lower volatility**, not return.
- Ang & Bekaert (2004), *Financial Analysts Journal* — out-of-sample regime detection
  that **"dominated static strategies."**

**Active < passive after costs (confirmed; strongest evidence base):**
- SPIVA (S&P Dow Jones Indices), Morningstar Active/Passive Barometer, and Fama & French
  (2010), *J. of Finance* — three independent, survivorship-corrected lines converging on
  ~80–92% of active funds trailing passive.

---

## Claim-by-claim verdict

| # | Internal claim | External verdict |
|---|---|---|
| C1 | A plain grid underperforms buy-and-hold over full cycles | **Direction confirmed** (mechanism: arXiv:2506.11921). The *22–44%* statistic is internal-only. |
| C2 | The grid is **risk-worse** than holding (bigger drawdown) | ⚠️ **Weakest.** Short-vol theory supports it, but the only direct *grid-drawdown* studies credit *enhanced* grids with **better** drawdown than hold — in tension. The ~71% magnitude is internal-only. |
| C3 | Grid = short-vol / mean-reversion bet, loses in trends | **Theory strongly supports** (Bouchaud skew; short-straddle equivalence; arXiv:2506.11921). No direct crypto-grid study. |
| C4 | Perfect foresight beats hold; **every** realistic detector loses | **Core confirmed** (Estrada, Sharpe). The **absolute "every detector loses" is overstated** — Bulla & Ang-Bekaert show realistic detectors that aren't categorical losers (their edge is *risk reduction*, not return). |
| C5 | Mechanically going to cash on imperfect signals destroys returns | **Mechanism confirmed**; the absolute "destroys" framing is stronger than the split record. A *naive* high-false-alarm rule bleeds returns. |
| C6 | Spacing must clear ~2× the round-trip fee to profit | **Confirmed by trusted sources** (Martin-Schöneborn + CFA arithmetic + FIM). The exact "2×" is trivial arithmetic, not a quoted theorem. |
| C7 | $100 capital caps dollar returns to pennies | **Theory supports** (Alcalá-Fahim fixed-cost drag) + arithmetic; no direct study of the $100 case. |
| C8 | Active strategies underperform passive after costs | **Confirmed** (SPIVA, Morningstar, Fama-French) — with the caveat that it's an **equities-fund analogy**, a strong prior, not a direct crypto measurement. |

---

## Corrections the external check forces on the internal synthesis

These are the places the outside record is *less* extreme than the internal phrasing.
They should be applied wherever the internal claims are restated:

1. **Soften C2 (the grid is "risk-worse").** It is the single weakest claim. The short-vol
   *mechanism* is sound, but the only direct grid-drawdown measurements in the literature
   run the *opposite* way (about enhanced grids). Don't lead with "the grid is riskier
   than holding" as if it were externally established — it rests on the backtester alone.
2. **Qualify the C4/C5 absolutes.** "*Every* realistic detector underperforms" / "going to
   cash *destroys* returns" overstate a genuinely split literature. The defensible version:
   *"the realistic-detection return edge is tiny, fragile, and usually eaten by costs"* —
   and realistic regime models mostly buy **lower risk**, not higher return. (That last
   point also cuts against C2.)

---

## Confidence tiers

**HIGH — now backed by independent evidence (theory + good data):**
- C8 (active < passive after costs) — *equities-fund analogy, not a crypto measurement*.
- C6 (fee floor exists and grows with cost).
- C4/C5 *core* (timing value concentrates in unpredictable days; the accuracy bar is brutal).

**MEDIUM — solid theory + a strong-but-asserted analogy, no direct crypto-grid study:**
- C3 (grid = short-vol bet that loses in trends).
- C1 *mechanism* (a plain grid has no edge before fees, loses after).
- C7 *mechanism* (fixed costs dominate at small scale).

**INTERNAL-ONLY — neither confirmed nor refuted; treat with caution:**
- Every WobbleBot-specific number: the 22–44% window win-rate (C1), the ~71% worse-drawdown
  figure (C2), the +19.8% / +67.8% / −88% detector results (C4), the exact 2×-Kraken-fee
  threshold (C6), the "$100 caps to pennies" dollar claim (C7).
- **C2 as a whole** (risk-worse) and the **C4/C5 absolutes** — phrased more categorically
  than the independent record bears (see Corrections above).

---

## Method caveats (honest)

- **Source landscape is bimodal.** Crypto-grid-specific independent evidence is *thin*
  (one preprint + one un-quantified student capstone); the popular material is ~80% vendor
  marketing and was excluded. The macro/timing/active-vs-passive claims sit on an *unusually
  good* peer-reviewed base.
- **Fetch friction.** A few primaries (Sharpe 1975, SPIVA pages, the Fama-French PDF)
  returned 403/unparseable; their figures were verified via DOI registries and
  mutually-consistent reputable secondaries, one citation-hop removed. The citation skeptic
  flagged that the earlier "fully primary-verified" label on Estrada was slightly
  overstated — corrected here.
- **A keyword-search trap was avoided:** Palazzi (2025), *Beating Passive Strategies in the
  Bullish Crypto Market*, looks on-topic but studies *pairs trading*, not grids — it was
  correctly excluded.
- **The disconfirming sources were surfaced, not buried** (Bulla, Ang-Bekaert, the
  enhanced-grid drawdown result) — the tell that this is an honest check, not a rubber stamp.

---

## Note on the internal synthesis's config-derived claims

The internal deep-research pass read the *committed* `settings.example.yml`, which had
drifted from the live config. Two of its peripheral claims were stale-example artifacts and
are **corrected**: (a) "per-coin spacings DOGE 2% / ADA 1.5% / SOL 1% / ETH 1% may be live"
— withdrawn; the live config runs a **uniform 3%** across all coins (only DOGE's *order
size* is overridden, for Kraken's ordermin). (b) "BTC is the only live symbol" — the live
`live.symbols` is six coins (BTC/ETH/SOL/XRP/DOGE/ADA). The synthesis's **backbone** (the
C1–C8 strategy claims above) is config-independent and unaffected.
