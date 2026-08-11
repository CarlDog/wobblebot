"""Cloud-LLM per-million-token pricing table (Phase 6 / ADR-014 decision 6).

Pricing is **code, not config**: a fact about reality maintained
alongside the codebase with verifiable provenance. Each entry carries
a ``verified_date`` (when an operator last confirmed the price against
the provider's pricing page) plus an inline comment with the page URL.
A unit test (``tests/services/test_llm_pricing_freshness.py``) fails
when any entry's ``verified_date`` is more than 180 days behind today,
forcing a periodic re-verification decision rather than letting prices
silently rot.

Convention for thinking-mode pricing:
    Provider APIs disagree on whether thinking / reasoning tokens
    overlap with regular output tokens. The convention this module
    enforces is **``tokens_reasoning`` is additive to ``tokens_out``** —
    cloud adapters (Stages 6.2-6.4) must normalize on read so the
    cost-record columns satisfy this invariant.

Convention for cache-token pricing (ADR-033):
    Cache counts follow the same disjoint-bucket rule — extractors
    normalize so ``tokens_in`` holds ONLY uncached prompt tokens and
    ``tokens_cache_read`` / ``tokens_cache_write`` are separate,
    non-overlapping buckets (OpenAI/Gemini include cached in their
    prompt totals → adapters subtract; Anthropic reports them disjoint
    → passthrough).

    Cost of a call:
        cost = (tokens_in  * input_per_million_usd  / 1_000_000)
             + (tokens_out * output_per_million_usd / 1_000_000)
             + (tokens_reasoning *
                (reasoning_per_million_usd or output_per_million_usd)
                / 1_000_000)
             + (tokens_cache_read *
                (cached_input_per_million_usd or input_per_million_usd)
                / 1_000_000)
             + (tokens_cache_write *
                (cache_write_per_million_usd or input_per_million_usd)
                / 1_000_000)

    ``reasoning_per_million_usd=None`` means "fall back to output
    rate", which is what every provider in the table currently bills.
    The override column is retained because Gemini 2.5 Flash did charge
    a separate thoughts rate during preview; Google has since folded
    thinking into the output rate ("Output price (including thinking
    tokens)"), so no entry overrides today. Keep the column — it costs
    nothing and the next provider to unbundle will need it.

    ``cached_input_per_million_usd=None`` falls back to the FULL input
    rate — deliberately conservative: an entry nobody has re-verified
    over-prices cached tokens (exactly the pre-ADR-033 behavior) and
    can never silently under-report spend. Same fallback for
    ``cache_write_per_million_usd``, which only Anthropic bills (1.25x
    input for the 5-minute TTL; the 1-hour TTL's 2x premium doesn't
    fit a single column and is moot while wobblebot never sends
    ``cache_control`` — revisit if ADR-033's deferral is ever lifted).
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from wobblebot.domain.llm_cost import LLMProvider

# Cost precision: 6 decimal places matches the ``cost_usd Decimal(10,6)``
# column in the ``llm_calls`` table. Penny + 4 sub-digits is enough for
# any per-call charge at current cloud rates (smallest call ~$0.000001).
_COST_QUANTIZER = Decimal("0.000001")


class LLMPricePoint(BaseModel):
    """Per-million-token USD rates for one (provider, model) pair.

    Attributes:
        provider: Cloud provider.
        model: Provider's model identifier.
        input_per_million_usd: Price per million prompt tokens.
        output_per_million_usd: Price per million completion tokens
            (NOT including thinking / reasoning per the convention
            above).
        reasoning_per_million_usd: Optional override for thinking-mode
            tokens. ``None`` means fall back to ``output_per_million_usd``.
        cached_input_per_million_usd: Price per million CACHE-READ
            prompt tokens (ADR-033). ``None`` falls back to the full
            ``input_per_million_usd`` — conservative over-pricing for
            entries whose cached rate hasn't been verified.
        cache_write_per_million_usd: Price per million cache-WRITE
            tokens (Anthropic's 5-minute-TTL 1.25x premium; OpenAI and
            Gemini implicit caching have no billed write step).
            ``None`` falls back to ``input_per_million_usd``.
        verified_date: When the operator last confirmed this price.
            Drives ``test_pricing_freshness``. Bumping it asserts the
            WHOLE entry was re-checked — every rate column, not just
            the one being edited.
    """

    provider: LLMProvider
    model: str = Field(..., min_length=1)
    input_per_million_usd: Decimal = Field(..., ge=Decimal("0"))
    output_per_million_usd: Decimal = Field(..., ge=Decimal("0"))
    reasoning_per_million_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    cached_input_per_million_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    cache_write_per_million_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    verified_date: date

    class Config:
        frozen = True


# Operators bump per entry as they re-verify. The freshness test fails
# the CI suite when any entry is >180 days behind today. Anchors are
# dated to the day because a single month can hold two sweeps.
#
# The Phase 6 kickoff anchor (2026-01-15) is gone: all seven of its
# entries were re-verified in the 2026-07-23 sweep below.
#
# Newer-model sweep verification (2026-05-29): current flagships + tiers
# pulled from each provider's official pricing page (see per-entry URLs).
_VERIFIED_2026_05 = date(2026, 5, 29)
# Legacy-entry re-verification sweep (2026-07-23), clearing the last of
# the 2026-01-15 anchor. Six of seven rates confirmed unchanged; the
# gemini-2.5-flash thoughts override was stale and is now removed (see
# the Google block). Sources: claude-sonnet-4-6 against
# https://platform.claude.com/docs/en/about-claude/pricing; the OpenAI
# rates against the per-model docs pages
# (developers.openai.com/api/docs/models/<id>), because the four legacy
# models have been dropped from the top-level pricing table.
_VERIFIED_2026_07_23 = date(2026, 7, 23)
# Advisor-model-review candidate sweep (2026-07-31): entries added so the
# monthly routine's bake-off can probe them under the ADR-014 gate
# (wobblebot#23). Sources per entry.
_VERIFIED_2026_07_31 = date(2026, 7, 31)
# ADR-033 cached-rate sweep (2026-08-02): entries below carrying this
# anchor had input + output + cached rates confirmed together on this
# date. Sources: Anthropic model-pricing table at
# platform.claude.com/docs/en/about-claude/pricing (5m Cache Writes /
# Cache Hits & Refreshes columns); OpenAI per-model docs pages +
# developers.openai.com/api/docs/pricing ("Cached input" column);
# ai.google.dev/gemini-api/docs/pricing ("Context caching" per-token
# rate — the separate per-hour storage fee applies only to EXPLICIT
# cached content, which wobblebot never creates, and is not modeled).
# Entries NOT in this sweep keep their old dates and cached=None
# (fallback to full input rate — conservative).
_VERIFIED_2026_08_02 = date(2026, 8, 2)
# Claude-5-generation sweep (2026-08-10): the probe batteries needed
# opus-5 / sonnet-5 priced before the ADR-014 gate would let them run
# (``get_price_point`` RAISES on an unmodeled pair — it does not fall
# back to a heuristic). Source: the Anthropic model-pricing table at
# platform.claude.com/docs/en/about-claude/pricing, read on this date.
_VERIFIED_2026_08_10 = date(2026, 8, 10)


_PRICING: dict[tuple[LLMProvider, str], LLMPricePoint] = {
    # --- Anthropic ---
    # https://www.anthropic.com/pricing — Sonnet + Opus tiers; thinking
    # tokens billed at output rate (no separate reasoning column needed;
    # convention recommends adapters fold thinking into output).
    # Cache columns follow Anthropic's published multipliers (read 0.1x
    # input, 5m write 1.25x input) — confirmed as absolute rates on the
    # pricing table 2026-08-02. Counts stay 0 while ADR-033 defers
    # cache_control, but the rates are real if that ever lifts.
    # Claude 5 generation (verified 2026-08-10). Opus 5 matches the 4.5-4.8
    # Opus tier exactly ($5/$25, 0.1x read, 1.25x 5m write).
    ("anthropic", "claude-opus-5"): LLMPricePoint(
        provider="anthropic",
        model="claude-opus-5",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("25.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.50"),
        cache_write_per_million_usd=Decimal("6.25"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    # ⚠️ Sonnet 5 carries INTRODUCTORY pricing of $2/$10 (read 0.20, 5m
    # write 2.50) that expires 2026-08-31, reverting to the standard
    # $3/$15 on 2026-09-01. This entry deliberately encodes the STANDARD
    # rate, not the introductory one, for two reasons: (a) the module's
    # stated convention is to over-price rather than ever under-report,
    # and (b) an entry pinned to the intro rate would go silently wrong
    # three weeks from now — the 180-day freshness test cannot catch a
    # price that changes on a known future date. Consequence while the
    # promo lasts: recorded spend for this model reads ~50% HIGH, and
    # ADR-014's caps bind proportionally early. Both are safe directions.
    # Revisit after 2026-09-01, when the table becomes exact on its own.
    ("anthropic", "claude-sonnet-5"): LLMPricePoint(
        provider="anthropic",
        model="claude-sonnet-5",
        input_per_million_usd=Decimal("3.00"),
        output_per_million_usd=Decimal("15.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.30"),
        cache_write_per_million_usd=Decimal("3.75"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("anthropic", "claude-sonnet-4-6"): LLMPricePoint(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_per_million_usd=Decimal("3.00"),
        output_per_million_usd=Decimal("15.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.30"),
        cache_write_per_million_usd=Decimal("3.75"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    # Opus tier dropped to $5/$25 (verified 2026-05-29 against
    # https://platform.claude.com/docs/en/about-claude/pricing) — the
    # prior $15/$75 was stale. Extended-thinking tokens bill at output rate.
    ("anthropic", "claude-opus-4-8"): LLMPricePoint(
        provider="anthropic",
        model="claude-opus-4-8",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("25.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.50"),
        cache_write_per_million_usd=Decimal("6.25"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("anthropic", "claude-opus-4-7"): LLMPricePoint(
        provider="anthropic",
        model="claude-opus-4-7",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("25.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.50"),
        cache_write_per_million_usd=Decimal("6.25"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("anthropic", "claude-opus-4-6"): LLMPricePoint(
        provider="anthropic",
        model="claude-opus-4-6",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("25.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.50"),
        cache_write_per_million_usd=Decimal("6.25"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("anthropic", "claude-haiku-4-5-20251001"): LLMPricePoint(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        input_per_million_usd=Decimal("1.00"),
        output_per_million_usd=Decimal("5.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.10"),
        cache_write_per_million_usd=Decimal("1.25"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("anthropic", "claude-haiku-4-5"): LLMPricePoint(
        provider="anthropic",
        model="claude-haiku-4-5",
        input_per_million_usd=Decimal("1.00"),
        output_per_million_usd=Decimal("5.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.10"),
        cache_write_per_million_usd=Decimal("1.25"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    # --- OpenAI ---
    # o-series: reasoning tokens billed at output rate.
    # NOTE (2026-07-23): the four models below are no longer listed on the
    # top-level pricing page (developers.openai.com/api/docs/pricing, which
    # is where openai.com/api/pricing and platform.openai.com/docs/pricing
    # now redirect) — that page shows only the gpt-5.x line. They are still
    # live and priced on their own docs pages, so verify each at
    # developers.openai.com/api/docs/models/<model-id>. Rates below were
    # confirmed unchanged there on 2026-07-23; only dated snapshots
    # (gpt-4o-2024-08-06, o1-2024-12-17, o3-mini-2025-01-31) carry a
    # "Deprecated" tag, not the floating aliases we bill against.
    ("openai", "gpt-4o"): LLMPricePoint(
        provider="openai",
        model="gpt-4o",
        input_per_million_usd=Decimal("2.50"),
        output_per_million_usd=Decimal("10.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_07_23,
    ),
    ("openai", "gpt-4o-mini"): LLMPricePoint(
        provider="openai",
        model="gpt-4o-mini",
        input_per_million_usd=Decimal("0.15"),
        output_per_million_usd=Decimal("0.60"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_07_23,
    ),
    ("openai", "o1"): LLMPricePoint(
        provider="openai",
        model="o1",
        input_per_million_usd=Decimal("15.00"),
        output_per_million_usd=Decimal("60.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_07_23,
    ),
    ("openai", "o3-mini"): LLMPricePoint(
        provider="openai",
        model="o3-mini",
        input_per_million_usd=Decimal("1.10"),
        output_per_million_usd=Decimal("4.40"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_07_23,
    ),
    # 2026-05-29 verified against developers.openai.com/api/docs/models/*.
    # gpt-5 family + o-series are reasoning models; reasoning tokens bill at
    # the output rate (None falls back to output per this module's convention).
    # OpenAI prompt caching is automatic (no opt-in, no write premium →
    # cache_write stays None). "Cached input" rates confirmed 2026-08-02
    # against developers.openai.com/api/docs/pricing + per-model pages.
    ("openai", "gpt-5.5"): LLMPricePoint(
        provider="openai",
        model="gpt-5.5",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("30.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.50"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    # gpt-5.5-pro has NO cached-input rate (pricing table shows "—", so
    # its cached_tokens is expected to stay 0) — None keeps the
    # full-input-rate fallback if that ever changes.
    ("openai", "gpt-5.5-pro"): LLMPricePoint(
        provider="openai",
        model="gpt-5.5-pro",
        input_per_million_usd=Decimal("30.00"),
        output_per_million_usd=Decimal("180.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("openai", "gpt-5-mini"): LLMPricePoint(
        provider="openai",
        model="gpt-5-mini",
        input_per_million_usd=Decimal("0.25"),
        output_per_million_usd=Decimal("2.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.025"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    # gpt-5.4 mini/nano tiers verified 2026-07-31 against
    # developers.openai.com/api/docs/models/gpt-5.4-mini and /gpt-5.4-nano.
    # Reasoning models; reasoning bills at the output rate -> None.
    ("openai", "gpt-5.4-mini"): LLMPricePoint(
        provider="openai",
        model="gpt-5.4-mini",
        input_per_million_usd=Decimal("0.75"),
        output_per_million_usd=Decimal("4.50"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.075"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("openai", "gpt-5.4-nano"): LLMPricePoint(
        provider="openai",
        model="gpt-5.4-nano",
        input_per_million_usd=Decimal("0.20"),
        output_per_million_usd=Decimal("1.25"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.02"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("openai", "o3"): LLMPricePoint(
        provider="openai",
        model="o3",
        input_per_million_usd=Decimal("2.00"),
        output_per_million_usd=Decimal("8.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("openai", "o4-mini"): LLMPricePoint(
        provider="openai",
        model="o4-mini",
        input_per_million_usd=Decimal("1.10"),
        output_per_million_usd=Decimal("4.40"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    # --- Google ---
    # https://ai.google.dev/gemini-api/docs/pricing — Gemini 2.5 family.
    # Pro rates are the <=200k small-prompt tier (>200k is $2.50/$15.00;
    # our prompts are ~1k tokens), matching the gen-3 entries below.
    #
    # 2026-07-23: gemini-2.5-flash previously carried a $3.50/1M thoughts
    # override, correct when Flash was in preview and billed thinking
    # separately from its then-$0.60 output rate. Google has since folded
    # the two together — the page now reads "Output price (including
    # thinking tokens)" at $2.50 — so the override was double-counting
    # thinking at 1.4x the real rate. Dropped to None (falls back to
    # output) per this module's convention.
    # "Context caching" per-token rates confirmed 2026-08-02 (<=200k
    # small-prompt tier for Pro, matching the input/output tier choice
    # above). Implicit caching bills these on cachedContentTokenCount;
    # the separate per-hour storage fee applies only to explicit cached
    # content, which wobblebot never creates. No write premium → None.
    ("google", "gemini-2.5-pro"): LLMPricePoint(
        provider="google",
        model="gemini-2.5-pro",
        input_per_million_usd=Decimal("1.25"),
        output_per_million_usd=Decimal("10.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.125"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    ("google", "gemini-2.5-flash"): LLMPricePoint(
        provider="google",
        model="gemini-2.5-flash",
        input_per_million_usd=Decimal("0.30"),
        output_per_million_usd=Decimal("2.50"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.03"),
        verified_date=_VERIFIED_2026_08_02,
    ),
    # Gemini 3.x (verified 2026-05-29 against ai.google.dev/gemini-api/docs/
    # pricing). Unlike 2.5-flash, gen-3 bills thinking tokens at the OUTPUT
    # rate (no separate thoughts rate) -> None. Pro rates are the <=200k
    # small-prompt tier (>200k is higher; our prompts are ~1k tokens).
    ("google", "gemini-3.1-pro-preview"): LLMPricePoint(
        provider="google",
        model="gemini-3.1-pro-preview",
        input_per_million_usd=Decimal("2.00"),
        output_per_million_usd=Decimal("12.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("google", "gemini-3-pro-preview"): LLMPricePoint(
        provider="google",
        model="gemini-3-pro-preview",
        input_per_million_usd=Decimal("2.00"),
        output_per_million_usd=Decimal("12.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("google", "gemini-3.5-flash"): LLMPricePoint(
        provider="google",
        model="gemini-3.5-flash",
        input_per_million_usd=Decimal("1.50"),
        output_per_million_usd=Decimal("9.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    # Verified 2026-07-31 against ai.google.dev/gemini-api/docs/pricing
    # ("Output price (including thinking tokens)" -> None). NOTE: the whole
    # gen-3.5+ flash line sits at ~7x gpt-5-mini per call at freejudge token
    # volumes — priced for completeness, outside the escalation seat's
    # current cost class (routine threshold: <=3x champion).
    # --- Atlas Cloud (OpenAI-compatible gateway; keyed under "openai"
    # because that is the adapter it reuses — a bare OpenAI model id
    # never contains "/", so the namespaced ids cannot collide) ---
    #
    # Verified 2026-08-10 against Atlas's own published catalogue, which
    # embeds per-model flat rates in the payload behind
    # https://www.atlascloud.ai/pricing (61 of its models are priced
    # publicly; its ~25 Anthropic entries are NOT — do not infer those
    # from upstream list prices, see .env.example).
    #
    # These exist so the ADR-014 gate will admit them at all: it RAISES
    # on an unpriced (provider, model) rather than estimating. Scope is
    # PROBES — no daemon selects an `atlas` provider.
    #
    # SELECTION IS CAPABILITY-FIRST, NOT PRICE-FIRST (operator
    # correction, 2026-08-10). The routine's <=3x cost gate is a filter
    # applied when DECIDING a switch, not a way to pick a roster —
    # and this battery has spent all day killing the cheap tier
    # (claude-haiku-4-5 worst-ever on quant; every local 3B-14B at
    # chance). A sweep of flash/mini variants would mostly re-derive
    # that. So: flagship/frontier models that are unreachable natively,
    # plus two mid-tier entries to test whether cheap is viable at all.
    # Flagship tier — the capability question, cost gate notwithstanding.
    ("openai", "moonshotai/kimi-k3"): LLMPricePoint(
        provider="openai",
        model="moonshotai/kimi-k3",
        input_per_million_usd=Decimal("3.00"),
        output_per_million_usd=Decimal("15.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.30"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "deepseek-ai/deepseek-v4-pro"): LLMPricePoint(
        provider="openai",
        model="deepseek-ai/deepseek-v4-pro",
        input_per_million_usd=Decimal("1.68"),
        output_per_million_usd=Decimal("3.38"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.13"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "xai/grok-4.5"): LLMPricePoint(
        provider="openai",
        model="xai/grok-4.5",
        input_per_million_usd=Decimal("2.00"),
        output_per_million_usd=Decimal("6.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.50"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "qwen/qwen3.8-max"): LLMPricePoint(
        provider="openai",
        model="qwen/qwen3.8-max",
        input_per_million_usd=Decimal("2.00"),
        output_per_million_usd=Decimal("6.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "zai-org/glm-5.2"): LLMPricePoint(
        provider="openai",
        model="zai-org/glm-5.2",
        input_per_million_usd=Decimal("1.26"),
        output_per_million_usd=Decimal("3.96"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.234"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    # Mid tier — is a cheaper model viable on this task at all?
    ("openai", "moonshotai/kimi-k2.6"): LLMPricePoint(
        provider="openai",
        model="moonshotai/kimi-k2.6",
        input_per_million_usd=Decimal("0.95"),
        output_per_million_usd=Decimal("4.00"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.16"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "minimaxai/minimax-m3"): LLMPricePoint(
        provider="openai",
        model="minimaxai/minimax-m3",
        input_per_million_usd=Decimal("0.30"),
        output_per_million_usd=Decimal("1.20"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.06"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    # Small/cheap tier — the CAPABILITY-FLOOR test (operator-authorized
    # 2026-08-10). The flagship sweep found 10 of 11 current models emit
    # ZERO unsafe calls, i.e. that axis has saturated among frontier
    # models; these establish where it stops being saturated. Priced for
    # the probe gate only.
    ("openai", "qwen/qwen3.5-flash"): LLMPricePoint(
        provider="openai",
        model="qwen/qwen3.5-flash",
        input_per_million_usd=Decimal("0.10"),
        output_per_million_usd=Decimal("0.40"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "xiaomi/mimo-v2.5"): LLMPricePoint(
        provider="openai",
        model="xiaomi/mimo-v2.5",
        input_per_million_usd=Decimal("0.14"),
        output_per_million_usd=Decimal("0.28"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.003"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "deepseek-ai/deepseek-v4-flash"): LLMPricePoint(
        provider="openai",
        model="deepseek-ai/deepseek-v4-flash",
        input_per_million_usd=Decimal("0.14"),
        output_per_million_usd=Decimal("0.28"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.028"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("openai", "bytedance/doubao-seed-2.0-mini-260428"): LLMPricePoint(
        provider="openai",
        model="bytedance/doubao-seed-2.0-mini-260428",
        input_per_million_usd=Decimal("0.10"),
        output_per_million_usd=Decimal("0.40"),
        reasoning_per_million_usd=None,
        cached_input_per_million_usd=Decimal("0.02"),
        verified_date=_VERIFIED_2026_08_10,
    ),
    ("google", "gemini-3.6-flash"): LLMPricePoint(
        provider="google",
        model="gemini-3.6-flash",
        input_per_million_usd=Decimal("1.50"),
        output_per_million_usd=Decimal("7.50"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_07_31,
    ),
}


class PricingLookupError(Exception):
    """Raised when ``cost_for`` is asked about an unmodeled (provider, model).

    Cloud adapters (Stages 6.2-6.4) should fail loudly if an operator
    configures a model that isn't in the pricing table — silent zero
    cost would defeat ADR-014's whole purpose. Add the model + verify
    the price, then re-run.
    """


def get_price_point(provider: LLMProvider, model: str) -> LLMPricePoint:
    """Look up the pricing entry for ``(provider, model)`` or raise.

    Raises:
        PricingLookupError: If the pair isn't in the table.
    """
    try:
        return _PRICING[(provider, model)]
    except KeyError as exc:
        raise PricingLookupError(
            f"No pricing entry for provider={provider!r} model={model!r}; "
            f"add it to services/llm_pricing.py with a verified_date."
        ) from exc


def cost_for(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    provider: LLMProvider,
    model: str,
    tokens_in: int,
    tokens_out: int,
    tokens_reasoning: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_write: int = 0,
) -> Decimal:
    """Compute USD cost of one call from token counts.

    Every count is a disjoint bucket per this module's docstring:
    ``tokens_reasoning`` is additive to ``tokens_out``, and
    ``tokens_in`` holds only UNCACHED prompt tokens with the cache
    buckets carried separately (ADR-033). Rates fall back per column —
    reasoning → output rate, cached-read and cache-write → full input
    rate (conservative: over-prices, never under-reports).

    Returns a ``Decimal`` quantized to 6 decimal places (matching the
    ``llm_calls.cost_usd`` column precision).

    Raises:
        PricingLookupError: If the (provider, model) isn't priced.
        ValueError: If any token count is negative.
    """
    if min(tokens_in, tokens_out, tokens_reasoning, tokens_cache_read, tokens_cache_write) < 0:
        raise ValueError(
            f"Token counts must be non-negative; got "
            f"in={tokens_in} out={tokens_out} reasoning={tokens_reasoning} "
            f"cache_read={tokens_cache_read} cache_write={tokens_cache_write}"
        )
    price = get_price_point(provider, model)
    million = Decimal("1000000")
    reasoning_rate = price.reasoning_per_million_usd or price.output_per_million_usd
    cached_rate = price.cached_input_per_million_usd
    if cached_rate is None:
        cached_rate = price.input_per_million_usd
    cache_write_rate = price.cache_write_per_million_usd
    if cache_write_rate is None:
        cache_write_rate = price.input_per_million_usd
    cost = (
        Decimal(tokens_in) * price.input_per_million_usd / million
        + Decimal(tokens_out) * price.output_per_million_usd / million
        + Decimal(tokens_reasoning) * reasoning_rate / million
        + Decimal(tokens_cache_read) * cached_rate / million
        + Decimal(tokens_cache_write) * cache_write_rate / million
    )
    return cost.quantize(_COST_QUANTIZER, rounding=ROUND_HALF_UP)


def all_price_points() -> list[LLMPricePoint]:
    """Return every modeled price point. Used by the freshness test
    and ``tools/show_llm_costs`` for per-model summaries."""
    return list(_PRICING.values())


def estimate_cost_ceiling(
    *,
    provider: LLMProvider,
    model: str,
    prompt_text: str,
    max_tokens: int,
) -> Decimal:
    """Conservative cost ceiling for any cloud-LLM call (ADR-014 decision 4).

    Used by every cloud adapter's pre-call gate-check. The estimate is
    a worst-case upper bound for plain-completion calls and a
    conservative under-bound for thinking-mode calls (where the
    runtime ``thoughtsTokenCount`` isn't predictable):

    - Tokens in = ``len(prompt_text) // 4`` (standard rule-of-thumb;
      provider tokenizers vary ~10% from this for English text).
    - Tokens out = ``max_tokens`` (the model's hard ceiling).
    - Reasoning tokens = 0 (folded into max_tokens at output rate for
      Anthropic + OpenAI; for Gemini-flash thinking the actual rate is
      higher than output, so accumulated overshoot is caught by the
      daily-cap sliding window rather than the per-call estimate).

    Promoted from per-adapter copies at Stage 6.5.A close audit —
    three adapters had byte-identical implementations differing only
    in the provider literal.
    """
    tokens_in_est = max(1, len(prompt_text) // 4)
    return cost_for(
        provider=provider,
        model=model,
        tokens_in=tokens_in_est,
        tokens_out=max_tokens,
        tokens_reasoning=0,
    )
