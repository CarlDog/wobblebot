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

    Cost of a call:
        cost = (tokens_in  * input_per_million_usd  / 1_000_000)
             + (tokens_out * output_per_million_usd / 1_000_000)
             + (tokens_reasoning *
                (reasoning_per_million_usd or output_per_million_usd)
                / 1_000_000)

    ``reasoning_per_million_usd=None`` means "fall back to output
    rate" (which is what Anthropic + OpenAI bill in practice). Google
    Gemini 2.5 charges a different rate for thoughts so the
    Gemini entries override.
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
        verified_date: When the operator last confirmed this price.
            Drives ``test_pricing_freshness``.
    """

    provider: LLMProvider
    model: str = Field(..., min_length=1)
    input_per_million_usd: Decimal = Field(..., ge=Decimal("0"))
    output_per_million_usd: Decimal = Field(..., ge=Decimal("0"))
    reasoning_per_million_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    verified_date: date

    class Config:
        frozen = True


# Anchor verified_date for Phase 6 kickoff. Operators bump per entry as
# they re-verify. The freshness test fails the CI suite when any entry
# is >180 days behind today.
_VERIFIED_2026_01 = date(2026, 1, 15)
# Newer-model sweep verification (2026-05-29): current flagships + tiers
# pulled from each provider's official pricing page (see per-entry URLs).
_VERIFIED_2026_05 = date(2026, 5, 29)


_PRICING: dict[tuple[LLMProvider, str], LLMPricePoint] = {
    # --- Anthropic ---
    # https://www.anthropic.com/pricing — Sonnet + Opus tiers; thinking
    # tokens billed at output rate (no separate reasoning column needed;
    # convention recommends adapters fold thinking into output).
    ("anthropic", "claude-sonnet-4-6"): LLMPricePoint(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_per_million_usd=Decimal("3.00"),
        output_per_million_usd=Decimal("15.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_01,
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
        verified_date=_VERIFIED_2026_05,
    ),
    ("anthropic", "claude-opus-4-7"): LLMPricePoint(
        provider="anthropic",
        model="claude-opus-4-7",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("25.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("anthropic", "claude-opus-4-6"): LLMPricePoint(
        provider="anthropic",
        model="claude-opus-4-6",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("25.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("anthropic", "claude-haiku-4-5-20251001"): LLMPricePoint(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        input_per_million_usd=Decimal("1.00"),
        output_per_million_usd=Decimal("5.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("anthropic", "claude-haiku-4-5"): LLMPricePoint(
        provider="anthropic",
        model="claude-haiku-4-5",
        input_per_million_usd=Decimal("1.00"),
        output_per_million_usd=Decimal("5.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    # --- OpenAI ---
    # https://openai.com/api/pricing — chat models + o-series reasoning.
    # o-series: reasoning tokens billed at output rate.
    ("openai", "gpt-4o"): LLMPricePoint(
        provider="openai",
        model="gpt-4o",
        input_per_million_usd=Decimal("2.50"),
        output_per_million_usd=Decimal("10.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_01,
    ),
    ("openai", "gpt-4o-mini"): LLMPricePoint(
        provider="openai",
        model="gpt-4o-mini",
        input_per_million_usd=Decimal("0.15"),
        output_per_million_usd=Decimal("0.60"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_01,
    ),
    ("openai", "o1"): LLMPricePoint(
        provider="openai",
        model="o1",
        input_per_million_usd=Decimal("15.00"),
        output_per_million_usd=Decimal("60.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_01,
    ),
    ("openai", "o3-mini"): LLMPricePoint(
        provider="openai",
        model="o3-mini",
        input_per_million_usd=Decimal("1.10"),
        output_per_million_usd=Decimal("4.40"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_01,
    ),
    # 2026-05-29 verified against developers.openai.com/api/docs/models/*.
    # gpt-5 family + o-series are reasoning models; reasoning tokens bill at
    # the output rate (None falls back to output per this module's convention).
    ("openai", "gpt-5.5"): LLMPricePoint(
        provider="openai",
        model="gpt-5.5",
        input_per_million_usd=Decimal("5.00"),
        output_per_million_usd=Decimal("30.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("openai", "gpt-5.5-pro"): LLMPricePoint(
        provider="openai",
        model="gpt-5.5-pro",
        input_per_million_usd=Decimal("30.00"),
        output_per_million_usd=Decimal("180.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
    ),
    ("openai", "gpt-5-mini"): LLMPricePoint(
        provider="openai",
        model="gpt-5-mini",
        input_per_million_usd=Decimal("0.25"),
        output_per_million_usd=Decimal("2.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_05,
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
    # https://ai.google.dev/pricing — Gemini 2.5 family. Gemini Flash
    # in thinking mode bills thoughts at a higher rate than regular
    # output, so reasoning_per_million_usd is explicit (additive to
    # output per the convention).
    ("google", "gemini-2.5-pro"): LLMPricePoint(
        provider="google",
        model="gemini-2.5-pro",
        input_per_million_usd=Decimal("1.25"),
        output_per_million_usd=Decimal("10.00"),
        reasoning_per_million_usd=None,
        verified_date=_VERIFIED_2026_01,
    ),
    ("google", "gemini-2.5-flash"): LLMPricePoint(
        provider="google",
        model="gemini-2.5-flash",
        input_per_million_usd=Decimal("0.30"),
        output_per_million_usd=Decimal("2.50"),
        reasoning_per_million_usd=Decimal("3.50"),
        verified_date=_VERIFIED_2026_01,
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


def cost_for(
    provider: LLMProvider,
    model: str,
    tokens_in: int,
    tokens_out: int,
    tokens_reasoning: int = 0,
) -> Decimal:
    """Compute USD cost of one call from token counts.

    Convention: ``tokens_reasoning`` is additive to ``tokens_out`` per
    this module's docstring. The reasoning-token rate falls back to
    the output rate when the model's price point doesn't carry an
    explicit override.

    Returns a ``Decimal`` quantized to 6 decimal places (matching the
    ``llm_calls.cost_usd`` column precision).

    Raises:
        PricingLookupError: If the (provider, model) isn't priced.
        ValueError: If any token count is negative.
    """
    if tokens_in < 0 or tokens_out < 0 or tokens_reasoning < 0:
        raise ValueError(
            f"Token counts must be non-negative; got "
            f"in={tokens_in} out={tokens_out} reasoning={tokens_reasoning}"
        )
    price = get_price_point(provider, model)
    million = Decimal("1000000")
    reasoning_rate = price.reasoning_per_million_usd or price.output_per_million_usd
    cost = (
        Decimal(tokens_in) * price.input_per_million_usd / million
        + Decimal(tokens_out) * price.output_per_million_usd / million
        + Decimal(tokens_reasoning) * reasoning_rate / million
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
