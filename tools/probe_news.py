"""Probe the MoE NEWS seat — signal-vs-noise on a labelled headline battery.

Third battery in the set, after ``probe_advisor.py`` (quant) and
``probe_arbitrator.py`` (arbitrator). Each grades a different rubric,
taken from that role's own prompt file — grading one role with another's
oracle produces authoritative-looking numbers that measure the wrong
thing.

The news rubric comes from ``config/prompts/news.md`` and is unusually
crisp, because the role is deliberately narrow:

- **One lever, one direction.** ``spacing_percentage``, and only WIDEN or
  HOLD. "A quiet tape is a reason to leave the grid alone (HOLD), **not**
  to narrow it" — and a sub-configured tighten cannot land anyway.
- **Nothing substantive → ``confidence: low`` and NO recommendations.**
  "Do not fabricate a narrative to have something to say."
- **Noise is not signal.** "A single headline rarely justifies a change; a
  confluence of related stories or one high-impact event (regulatory
  action, exchange suspension, a hack) might."
- **Stay in lane.** "Don't touch ``order_size_usd`` or level counts; those
  aren't the news dimension's call — omit them."

That last one is checked on EVERY fixture, not just its own: a model that
reaches for the risk expert's levers has left its role regardless of
whether its spacing call was right.

**Why a labelled battery rather than live headlines.** Live news is
unlabelled — there is no answer key for "was widening correct on
2026-08-10?" without waiting for the outcome, which is ADR-035's ledger,
not a probe. These windows are hand-labelled by materiality so the
signal-vs-noise judgment is gradeable today.

**Two fixture sets** (``--fixture-set``, default ``gen2``):

- ``v1`` — the original twelve. **CEILINGED**: haiku-4-5, sonnet-5 and
  opus-5 all scored 12/12 on 2026-08-10, so it can no longer rank the
  models it exists to rank. Kept unedited so historical scores stay
  comparable, the way ``quant/heldout`` was frozen rather than fixed.
- ``gen2`` — v1 plus ten boundary cases (2026-08-11). Every one of v1's
  windows is unambiguous; these sit where news.md's actual line falls,
  and half the added hold-cases are seeded with a named trigger word
  ("regulatory", "withdrawals", "hack") so a keyword-matcher is
  punished rather than rewarded. See ``_fixtures_gen2_extra``.

Note v1 was never UNSOUND — its constant baseline is 58% against 100%
for the best real model. It was EXHAUSTED, which is a different problem
with a cheaper fix: more fixtures, not a new instrument.

**Degenerate-strategy defence** (the lesson from the quant battery's
2026-05-29 rework, where a constant "+10% widen" scored 11/18 by
accident): both sets are deliberately imbalanced toward HOLD — 7/5 on
v1, 13/9 on gen2 — because the honest real-world prior is that most news
windows warrant nothing. Constant-HOLD therefore caps at 58%/59% and
constant-WIDEN at 42%/41%, so neither degenerate strategy can look
competent, and the per-fixture profile separates them from a reasoner.
``tests/tools/test_probe_news.py`` pins those baselines.

Usage::

    python tools/probe_news.py --model qwen3.6:35b-a3b-q8_0
    python tools/probe_news.py --provider openai --model gpt-5-mini --max-tokens 4000
    python tools/probe_news.py --provider atlas --model <atlas-model>
    python tools/probe_news.py --fixture-set v1 --model ...   # historical
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from wobblebot.cli._common import load_operator_env
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import load_prompt
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.advisor import (
    AdvisorRecommendation,
    CurrentGridParams,
    NewsItemSummary,
    PerformanceSummary,
)
from wobblebot.ports.exceptions import AdvisorError

_LOGGER = logging.getLogger("wobblebot.tools.probe_news")

# The live grid these fixtures are judged against. WIDEN means "> this";
# anything <= this is a tighten the auto-apply floor would discard.
_CURRENT_SPACING = 3.0
_CURRENT_ORDER_SIZE = 5.0
# news.md: the role's ONLY lever is spacing. Emitting either of these is
# out-of-lane regardless of the spacing call.
_OUT_OF_LANE_KEYS = ("order_size_usd", "levels_above", "levels_below")
# Float slop when deciding "did the model restate the current spacing".
_ECHO_TOLERANCE = 0.001

_CLOUD_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "atlas": "ATLASCLOUD_API_KEY",
}
_ATLAS_BASE_URL = "https://api.atlascloud.ai"


def _item(
    headline: str, *, source: str = "rss:coindesk", hours_ago: float = 2.0
) -> NewsItemSummary:
    return NewsItemSummary(
        source=source,
        published_at=Timestamp(dt=datetime.now(UTC) - timedelta(hours=hours_ago)),
        headline=headline,
        mentioned_coins=["BTC"],
    )


@dataclass(frozen=True)
class NewsFixture:
    """One labelled news window.

    ``expect`` is "widen" or "hold". A hold means the spacing key must be
    OMITTED — news.md says a quiet tape is not a reason to narrow, and an
    explicit restatement of the current value is still a change the role
    was told not to make.
    """

    name: str
    expect: str
    items: list[NewsItemSummary]
    why: str
    expect_low_confidence: bool = False


def _fixtures() -> list[NewsFixture]:
    """Twelve windows: 7 hold, 5 widen. Imbalanced on purpose (see module
    docstring) so neither constant strategy scores respectably."""
    return [
        # ---- HOLD: nothing substantive -------------------------------
        NewsFixture(
            name="empty_window",
            expect="hold",
            items=[],
            why="No headlines at all. news.md: low confidence, no recommendations.",
            expect_low_confidence=True,
        ),
        NewsFixture(
            name="single_price_clickbait",
            expect="hold",
            items=[_item("Analyst: Bitcoin could hit $150,000 by year end")],
            why="One speculative price-target story is noise, not signal.",
        ),
        NewsFixture(
            name="unrelated_trivia_volume",
            expect="hold",
            items=[
                _item("NFT project announces new roadmap"),
                _item("Crypto influencer launches podcast"),
                _item("Survey: 40% of Gen Z has heard of blockchain"),
                _item("Exchange adds new referral programme"),
                _item("Memecoin rallies 12% on no news"),
            ],
            why="VOLUME is not signal — five trivial stories still warrant nothing.",
        ),
        NewsFixture(
            name="bullish_rally_coverage",
            expect="hold",
            items=[
                _item("Bitcoin breaks $70k as ETF inflows accelerate"),
                _item("Institutional demand strongest since January"),
            ],
            why="Good news is not a reason to NARROW. Discriminator for tighten-on-optimism.",
        ),
        NewsFixture(
            name="stale_resolved_incident",
            expect="hold",
            items=[
                _item("Exchange reports brief API degradation, since resolved", hours_ago=20.0),
            ],
            why="Resolved and 20h old — no forward-looking disruption to widen against.",
        ),
        NewsFixture(
            name="other_chain_incident",
            expect="hold",
            items=[
                _item("Small DeFi protocol on an unrelated chain exploited for $2m"),
            ],
            why="Real but immaterial to a BTC/USD grid on Kraken.",
        ),
        NewsFixture(
            name="routine_scheduled_nonevent",
            expect="hold",
            items=[_item("Quarterly options expiry passes without incident")],
            why="A scheduled event that already resolved uneventfully.",
        ),
        # ---- WIDEN: material, forward-looking disruption --------------
        NewsFixture(
            name="exchange_suspension",
            expect="widen",
            items=[
                _item(
                    "Kraken suspends BTC deposits and withdrawals pending wallet maintenance",
                    source="kraken_status",
                    hours_ago=0.5,
                ),
            ],
            why="news.md names exchange suspension as a high-impact single event.",
        ),
        NewsFixture(
            name="major_exchange_hack",
            expect="widen",
            items=[
                _item("Top-5 exchange confirms $200m hot-wallet breach", hours_ago=1.0),
                _item("Withdrawals halted as investigation begins", hours_ago=0.5),
            ],
            why="A hack — named explicitly in the prompt as warranting a change.",
        ),
        NewsFixture(
            name="regulatory_action",
            expect="widen",
            items=[
                _item("SEC files enforcement action against major crypto exchange"),
                _item("Trading venues review US listings after filing"),
            ],
            why="Regulatory action, named explicitly in the prompt.",
        ),
        NewsFixture(
            name="macro_print_imminent",
            expect="widen",
            items=[
                _item("CPI print due in two hours; consensus split", hours_ago=0.2),
                _item("Rate-cut odds swing sharply ahead of data", hours_ago=0.5),
            ],
            why="A Fed/CPI print is the prompt's own example of expected volatility.",
        ),
        NewsFixture(
            name="confluence_of_related_stories",
            expect="widen",
            items=[
                _item("Large dormant whale wallet moves 40,000 BTC to exchanges"),
                _item("Second whale transfer detected within the hour", hours_ago=0.7),
                _item("Order books thin as market makers step back", hours_ago=0.4),
            ],
            why="Confluence of related stories — the prompt's stated bar for acting.",
        ),
    ]


def _fixtures_gen2_extra() -> list[NewsFixture]:
    """Ten boundary cases — the reason `gen2` exists (2026-08-11).

    `v1` CEILINGED: haiku-4-5, sonnet-5 and opus-5 all scored 12/12, so it
    could no longer rank the models it was built to rank. Its twelve
    windows are each unambiguous — a competent reader cannot miss them.
    The battery was never unsound (constant baseline 58% vs 100% best,
    +42% margin); it was EXHAUSTED. Same diagnosis, same fix, as the
    quant seat's `hard` -> `gen3` rebuild.

    These ten sit on the decision boundary news.md actually draws. Two
    design rules, both learned the hard way:

    1. **Every label is derivable from news.md's own words**, quoted in
       `why`. Two `hard` fixtures had to be withdrawn for encoding rules
       the prompt never stated — a fixture the prompt doesn't support
       measures the fixture author, not the model.
    2. **Trigger WORDS are decoys, not answers.** news.md names
       "regulatory action, exchange suspension, a hack" as high-impact.
       Half of these hold-cases contain one of those words and still
       warrant nothing, because the prompt's actual test is whether
       disruption lies AHEAD ("WIDEN *ahead of* expected volatility or
       disruption"). Keyword-matching scores badly here on purpose.
    """
    return [
        # ---- HOLD: the trigger word is present, the disruption is not ----
        NewsFixture(
            name="resolved_major_incident",
            expect="hold",
            items=[
                _item(
                    "Kraken completes wallet maintenance; BTC deposits and "
                    "withdrawals fully restored",
                    source="kraken_status",
                    hours_ago=0.5,
                ),
            ],
            why=(
                "The twin of exchange_suspension, inverted. Same venue, same "
                "words ('withdrawals', exchange status feed), but this is "
                "RESOLUTION. news.md widens 'ahead of expected volatility or "
                "disruption' — there is none ahead. Keyword-matching says widen."
            ),
        ),
        NewsFixture(
            name="favorable_regulatory_clarity",
            expect="hold",
            items=[
                _item("Regulator publishes long-awaited licensing framework for exchanges"),
                _item("Industry groups welcome the rules; compliance begins next January"),
            ],
            why=(
                "'Regulatory action' is a named high-impact trigger, so the "
                "decoy is strong — but the prompt's test is expected volatility "
                "or disruption, and a welcomed framework effective next year is "
                "neither. Twin of regulatory_deadline_imminent below."
            ),
        ),
        NewsFixture(
            name="distant_macro_event",
            expect="hold",
            items=[_item("Fed meeting scheduled for next month; economists split on the outcome")],
            why=(
                "A Fed print is the prompt's own widen example — but 'ahead of' "
                "has to mean within an actionable horizon. Twin of "
                "macro_print_imminent, which is the same event two hours out."
            ),
        ),
        NewsFixture(
            name="incident_then_all_clear",
            expect="hold",
            items=[
                _item("Exchange reports delayed BTC withdrawals", hours_ago=2.0),
                _item(
                    "Exchange confirms issue resolved; withdrawals processing normally",
                    hours_ago=0.3,
                ),
            ],
            why=(
                "Tests whether the model READS THE CLOCK. Taken alone the first "
                "headline warrants widening; the newer one retires it. No "
                "forward-looking disruption remains."
            ),
        ),
        NewsFixture(
            name="related_but_immaterial_confluence",
            expect="hold",
            items=[
                _item("Exchange announces redesigned trading interface"),
                _item("Exchange adds two new fee tiers for high-volume traders"),
                _item("Exchange publishes updated mobile app", hours_ago=1.5),
                _item("Exchange teases loyalty programme for Q4", hours_ago=2.0),
            ],
            why=(
                "'A confluence of related stories MIGHT' justify a change — "
                "'might', and these are related, confluent, and immaterial. "
                "Tests that confluence is not a magic word. The twin of "
                "unrelated_trivia_volume, which fails the 'related' test instead."
            ),
        ),
        NewsFixture(
            name="rumor_debunked_on_chain",
            expect="hold",
            items=[
                _item("Anonymous social post claims a large exchange faces liquidity strain"),
                _item(
                    "Exchange publishes live proof-of-reserves; independent on-chain "
                    "analysts confirm balances intact",
                    hours_ago=0.4,
                ),
            ],
            why=(
                "news.md: 'a single headline rarely justifies a change' and 'do "
                "not fabricate a narrative to have something to say.' An "
                "anonymous claim, affirmatively DISPROVEN with third-party "
                "on-chain confirmation, is the textbook case for both.\n\n"
                "REVISED 2026-08-11 after its first run. The original said "
                "'unconfirmed report' + 'exchange DENIES the report', and 3 of 4 "
                "models widened on it. On audit they were defensible: a merely-"
                "denied liquidity rumour about a large exchange is live "
                "'exchange-outage chatter', which news.md names as a widen "
                "trigger, and markets do move on denied rumours. The prompt "
                "permitted BOTH readings, so the fixture was measuring the "
                "author's fiat, not the model — the same defect that withdrew "
                "two `hard` fixtures. Fixed by making the EVIDENCE dispositive "
                "(disproven, not merely denied) rather than by relabelling, so "
                "it still tests 'can you tell a debunked rumour from a live "
                "one' — which news.md's noise-vs-signal rule does cover."
            ),
        ),
        # ---- WIDEN: material and forward-looking, but not keyword-obvious ----
        NewsFixture(
            name="two_step_stablecoin_stress",
            expect="widen",
            items=[
                _item(
                    "Stablecoin issuer discloses shortfall in reserve attestation", hours_ago=1.2
                ),
                _item("Two venues halt trading in that stablecoin's pairs", hours_ago=0.4),
            ],
            why=(
                "Neither headline names a hack, a suspension or a regulator, so "
                "keyword-matching misses it — but it is exactly the prompt's "
                "'confluence of related stories', and it is forward-looking: "
                "the halts are spreading, not resolved."
            ),
        ),
        NewsFixture(
            name="liquidity_withdrawal_lane_trap",
            expect="widen",
            items=[
                _item("Market makers pull quotes; major-pair order books thinnest in a year"),
                _item("Slippage on large orders spikes across venues", hours_ago=0.5),
            ],
            why=(
                "Correct answer is widen spacing — AND the window is engineered "
                "to make order_size_usd feel like the right lever, which news.md "
                "explicitly forbids ('omit them'). The out-of-lane check is "
                "graded everywhere; here it is actually tempted."
            ),
        ),
        NewsFixture(
            name="regulatory_deadline_imminent",
            expect="widen",
            items=[
                _item("New listing rules take effect at 00:00 UTC tonight", hours_ago=1.0),
                _item(
                    "Venues confirm several pairs will be delisted at the cutover", hours_ago=0.6
                ),
            ],
            why=(
                "The true-positive twin of favorable_regulatory_clarity: same "
                "category word, but with a named near-term cutover and a "
                "concrete market effect. Regulatory action AHEAD of volatility."
            ),
        ),
        NewsFixture(
            name="degrading_venue_health",
            expect="widen",
            items=[
                _item(
                    "Kraken status: elevated error rates on order placement",
                    source="kraken_status",
                    hours_ago=0.6,
                ),
                _item("Users report failed cancellations and stale fills", hours_ago=0.3),
            ],
            why=(
                "'Exchange-outage chatter' is a named widen trigger. Degrading "
                "and unresolved at OUR venue, where a grid's cancel/replace loop "
                "lives — the opposite of resolved_major_incident."
            ),
        ),
    ]


FIXTURE_SETS: dict[str, list[NewsFixture]] = {
    "v1": _fixtures(),
    "gen2": _fixtures() + _fixtures_gen2_extra(),
}


def _summary(items: list[NewsItemSummary]) -> PerformanceSummary:
    """A deliberately UNREMARKABLE market window.

    Metrics are held constant and calm across every fixture so the only
    thing varying is the news. If a model's call tracks the metrics it
    will score at chance — which is the correct verdict for a news expert.
    """
    return PerformanceSummary(
        symbol="BTC/USD",
        lookback_hours=6.0,
        latest_price=64000.0,
        snapshot_count=720,
        volatility=0.004,
        max_drawdown=-0.008,
        flatness=0.9,
        cycle_count=3,
        win_rate=0.66,
        total_pnl=0.12,
        active_orders=6,
        current_grid=CurrentGridParams(
            spacing_percentage=_CURRENT_SPACING,
            levels_above=3,
            levels_below=3,
            order_size_usd=_CURRENT_ORDER_SIZE,
        ),
        recent_news=items,
    )


@dataclass
class Verdict:
    fixture: str
    verdict: str
    ok: bool
    detail: str


@dataclass
class Scorecard:
    label: str
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(1 for v in self.verdicts if v.ok)


def _grade(fx: NewsFixture, rec: AdvisorRecommendation) -> Verdict:
    """Grade one call. Out-of-lane keys fail the fixture outright."""
    recs = rec.recommendations or {}
    out_of_lane = [k for k in _OUT_OF_LANE_KEYS if k in recs]
    if out_of_lane:
        return Verdict(
            fx.name, "OUT_OF_LANE", False, f"touched {out_of_lane} — not the news role's levers"
        )

    spacing = recs.get("spacing_percentage")

    # An ECHO of the current spacing is semantically a HOLD, not a change.
    # news.md prefers omission ("Omit any field you don't want to change"),
    # but grading a restatement of the live value as a TIGHTEN would call
    # the correct decision the most dangerous failure mode. Caught
    # 2026-08-10 when a first draft of this rubric scored qwen3.6 at 5/12
    # for emitting exactly 3.0 on five quiet windows — the right call,
    # mislabelled. Only spacing STRICTLY BELOW current is a real tighten.
    def _is_echo(v: float) -> bool:
        return abs(v - _CURRENT_SPACING) <= _ECHO_TOLERANCE

    if fx.expect == "hold":
        if spacing is None:
            if fx.expect_low_confidence and rec.confidence != "low":
                return Verdict(
                    fx.name,
                    "HELD_OVERCONFIDENT",
                    False,
                    f"held but confidence={rec.confidence}, expected low on an empty window",
                )
            return Verdict(fx.name, "OK", True, "held (omitted)")
        try:
            val = float(spacing)
        except (TypeError, ValueError):
            return Verdict(fx.name, "BAD_VALUE", False, f"non-numeric spacing {spacing!r}")
        if _is_echo(val):
            return Verdict(fx.name, "OK", True, f"held (echoed current {val})")
        if val < _CURRENT_SPACING:
            return Verdict(
                fx.name,
                "TIGHTEN",
                False,
                f"emitted {val} < live {_CURRENT_SPACING} — never tighten",
            )
        return Verdict(fx.name, "OVERTRADE", False, f"widened to {val} on a non-event")

    # expect widen
    if spacing is None:
        return Verdict(fx.name, "MISS", False, "held through a material event")
    try:
        val = float(spacing)
    except (TypeError, ValueError):
        return Verdict(fx.name, "BAD_VALUE", False, f"non-numeric spacing {spacing!r}")
    if _is_echo(val):
        return Verdict(fx.name, "MISS", False, f"echoed current {val} through a material event")
    if val < _CURRENT_SPACING:
        return Verdict(
            fx.name, "WRONG", False, f"emitted {val} < live {_CURRENT_SPACING} — tightened into it"
        )
    return Verdict(fx.name, "OK", True, f"widened to {val}")


def _build(args: argparse.Namespace) -> tuple[Any, Any]:
    prompt = load_prompt(Path(args.prompt_file))
    if args.provider == "ollama":
        from wobblebot.adapters.ollama import OllamaAdapter

        return (
            OllamaAdapter(
                model=args.model,
                prompt=prompt,
                role="news",
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
            ),
            None,
        )
    return _build_cloud(args, prompt)


def _build_cloud(args: argparse.Namespace, prompt: Any) -> tuple[Any, Any]:
    from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
    from wobblebot.services.llm_cost_gate import LLMCostConfig, SessionCostTracker
    from wobblebot.services.llm_retry import LLMRetryConfig

    key_var = _CLOUD_KEY_ENV[args.provider]
    api_key = os.environ.get(key_var)
    if not api_key:
        raise SystemExit(f"{key_var} missing from environment (.env / shell)")
    storage = SQLiteStorageAdapter("data/probe_llm_cost.db")
    common: dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        "role": "news",
        "api_key": api_key,
        "storage": storage,
        "session_tracker": SessionCostTracker(),
        "cost_config": LLMCostConfig(
            max_spend_per_day_usd=Decimal(str(args.daily_cap)),
            max_spend_per_session_usd=Decimal(str(args.session_cap)),
            enforce=True,
        ),
        "retry_config": LLMRetryConfig(max_retries=2, initial_backoff_seconds=1.0),
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout_seconds": args.timeout_seconds,
    }
    if args.provider in ("openai", "atlas"):
        from wobblebot.adapters.openai import OpenAIAdvisorAdapter

        if args.provider == "atlas":
            return OpenAIAdvisorAdapter(base_url=_ATLAS_BASE_URL, **common), storage
        return (
            OpenAIAdvisorAdapter(
                organization=os.environ.get("OPENAI_ORGANIZATION") or None, **common
            ),
            storage,
        )
    if args.provider == "anthropic":
        from wobblebot.adapters.anthropic import AnthropicAdvisorAdapter

        return AnthropicAdvisorAdapter(**common), storage
    from wobblebot.adapters.google import GoogleAdvisorAdapter

    return GoogleAdvisorAdapter(**common), storage


async def _run(args: argparse.Namespace) -> int:
    fixtures = FIXTURE_SETS[args.fixture_set]
    holds = sum(1 for f in fixtures if f.expect == "hold")
    _LOGGER.info(
        "news battery [%s]: %d fixtures (%d hold / %d widen); constant-HOLD caps at %d, "
        "constant-WIDEN at %d",
        args.fixture_set,
        len(fixtures),
        holds,
        len(fixtures) - holds,
        holds,
        len(fixtures) - holds,
    )

    llm, storage = _build(args)
    if storage is not None:
        await storage.connect()
    card = Scorecard(label=f"{args.provider}:{args.model}")
    try:
        for fx in fixtures:
            try:
                rec = await llm.get_recommendation(_summary(fx.items))
            except AdvisorError as exc:
                card.verdicts.append(Verdict(fx.name, "ERROR", False, str(exc)[:90]))
                continue
            card.verdicts.append(_grade(fx, rec))
    finally:
        aclose = getattr(llm, "aclose", None)
        if aclose is not None:
            await aclose()
        if storage is not None:
            await storage.close()

    _LOGGER.info("=== %s: %d/%d ===", card.label, card.score, len(fixtures))
    for v in card.verdicts:
        _LOGGER.info("  %-30s %-18s %s", v.fixture, v.verdict, v.detail)
    if args.json:
        print(
            "JSON_RESULT: "
            + json.dumps(
                {
                    "model": args.model,
                    "provider": args.provider,
                    "score": card.score,
                    "max_score": len(fixtures),
                    "verdicts": [
                        {"fixture": v.fixture, "verdict": v.verdict, "ok": v.ok}
                        for v in card.verdicts
                    ],
                }
            )
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider", default="ollama", choices=("ollama", "openai", "anthropic", "google", "atlas")
    )
    parser.add_argument("--prompt-file", default="config/prompts/news.md")
    parser.add_argument(
        "--fixture-set",
        choices=tuple(FIXTURE_SETS),
        default="gen2",
        help="v1 = the original twelve (CEILINGED - three models at 12/12); "
        "gen2 = v1 plus ten boundary cases. Default gen2.",
    )
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--session-cap", type=float, default=2.0)
    parser.add_argument("--daily-cap", type=float, default=5.0)
    parser.add_argument("--log-format", choices=("plain", "json"), default="plain")
    parser.add_argument("--json", action="store_true")
    # Match probe_freejudge / probe_risk: this module's docstring and
    # fixture rationales are non-ASCII, and argparse prints the docstring
    # as its description — on a cp1252 console even `--help` raised
    # UnicodeEncodeError before this (pre-existing; caught 2026-08-11).
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    args = parser.parse_args()
    configure_logging(log_format=args.log_format)
    load_operator_env()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
