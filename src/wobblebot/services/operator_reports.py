"""Operator report builders — the LLM-condensed narrative surfaces.

Split out of ``services/operator_service.py`` at the 1000-line module
gate (P4.5). The two report builders are a cohesive unit with one
shared shape: precompute authoritative facts in code, hand the
assistant a labeled payload to narrate (the llm-app rule — the model
cites, it never counts), and fall back to a deterministic one-liner
when no assistant is wired or the summarize call fails.

``compose_status_report_narrative`` serves ``status_report`` (the
BOT's own activity); ``build_weather_report`` serves
``weather_report`` (what the MARKET is doing — P4.5, the Oracle
seed). ``OperatorService`` owns the query dispatch and the cross-DB
sub-queries; this module owns aggregation + prose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wobblebot.domain.value_objects import Symbol, fmt_usd
from wobblebot.ports.assistant import AssistantPort
from wobblebot.ports.exceptions import AssistantError, StorageError
from wobblebot.ports.operator import (
    DirectionalCall,
    GridConfigResult,
    HarvesterStatusResult,
    OpenOrdersResult,
    RecentFillsResult,
    RecentNewsResult,
    RecentProposalsResult,
    RecentSuggestionsResult,
    StatusResult,
    SymbolTrend,
    WeatherReportQuery,
    WeatherReportResult,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.services.weather_report import compute_symbol_trend, extract_directional_calls


async def compose_status_report_narrative(  # pylint: disable=too-many-arguments,too-many-locals
    assistant: AssistantPort | None,
    *,
    lookback_hours: int,
    status: StatusResult,
    open_orders: OpenOrdersResult,
    recent_fills: RecentFillsResult,
    recent_suggestions: RecentSuggestionsResult,
    recent_news: RecentNewsResult,
    harvester_status: HarvesterStatusResult,
    recent_proposals: RecentProposalsResult,
    grid_config: GridConfigResult,
) -> str:
    """Build the LLM prompt + call summarize; fall back deterministically."""
    deterministic = (
        f"Last {lookback_hours}h snapshot: balance {fmt_usd(status.total_usd_balance)}, "
        f"today's PnL {fmt_usd(status.session_pnl, signed=True)}, "
        f"{len(recent_fills.fills)} fills, {len(recent_news.items)} news items, "
        f"harvester band {harvester_status.band}, "
        f"{len(open_orders.orders)} open orders."
    )
    if assistant is None:
        return deterministic

    # Compact data blob. The COUNTS section is pre-computed and
    # authoritative — every count in the narrative MUST come from
    # this section, not from re-counting JSON arrays the LLM is
    # prone to miscount. This block addresses 2026-05-24 audit
    # finding #4: phi4 was conflating ``STATUS.recent_fill_count``
    # (engine-wide tally) with the lookback-scoped fill count from
    # RECENT_FILLS, and miscounting open_orders sides (saying
    # "five buy orders" when 5 was the total open-order count).
    open_buys = sum(1 for o in open_orders.orders if o.side == "buy")
    open_sells = sum(1 for o in open_orders.orders if o.side == "sell")
    fills_buys = sum(1 for f in recent_fills.fills if f.side == "buy")
    fills_sells = sum(1 for f in recent_fills.fills if f.side == "sell")
    counts_block = [
        f"  lookback_window_hours: {lookback_hours}",
        f"  open_orders_total: {len(open_orders.orders)}",
        f"  open_buys: {open_buys}",
        f"  open_sells: {open_sells}",
        f"  fills_in_lookback_total: {len(recent_fills.fills)}",
        f"  fills_in_lookback_buys: {fills_buys}",
        f"  fills_in_lookback_sells: {fills_sells}",
        f"  news_in_lookback: {len(recent_news.items)}",
        f"  suggestions_in_lookback: {len(recent_suggestions.suggestions)}",
        f"  proposals_in_lookback: {len(recent_proposals.proposals)}",
        f"  harvester_band: {harvester_status.band}",
        f"  total_usd_balance: {fmt_usd(status.total_usd_balance)}",
        f"  todays_realized_pnl: " f"{fmt_usd(status.session_pnl, signed=True)}",
    ]
    blob_lines = [
        f"LOOKBACK_HOURS: {lookback_hours}",
        "",
        "COUNTS (authoritative -- cite these verbatim; never re-count):",
        *counts_block,
        "",
        "STATUS (engine-wide; recent_fill_count here is NOT lookback-scoped):",
        status.model_dump_json(indent=2),
        "",
        "GRID_CONFIG (currently in effect):",
        grid_config.model_dump_json(indent=2),
        "",
        "OPEN_ORDERS:",
        open_orders.model_dump_json(indent=2),
        "",
        "RECENT_FILLS (only the lookback window):",
        recent_fills.model_dump_json(indent=2),
        "",
        "RECENT_SUGGESTIONS (only the lookback window; proposed changes, not yet applied):",
        recent_suggestions.model_dump_json(indent=2),
        "",
        "RECENT_NEWS:",
        recent_news.model_dump_json(indent=2),
        "",
        "HARVESTER_STATUS:",
        harvester_status.model_dump_json(indent=2),
        "",
        "RECENT_PROPOSALS:",
        recent_proposals.model_dump_json(indent=2),
    ]
    user_content = "\n".join(blob_lines)

    system_prompt = (
        "You are the WobbleBot operator assistant generating a status "
        "report. The operator has asked for a snapshot of what's "
        "happened since they last checked. You will receive structured "
        "JSON for every query the bot can answer; condense it into a "
        "user-friendly 2-3 paragraph plain-text narrative.\n\n"
        "**The COUNTS section is authoritative.** Every count you "
        "mention in the narrative (fills, open orders by side, news "
        "items, suggestions, proposals) MUST come from COUNTS "
        "verbatim. Do NOT re-count by inspecting JSON arrays in "
        "other sections. Do NOT use STATUS.recent_fill_count for "
        "fill counts -- that is engine-wide, not lookback-scoped. "
        "Every COUNTS field ending in ``_in_lookback`` is scoped to "
        "the requested window.\n\n"
        "**If a `_in_lookback` count is 0, say so explicitly** and "
        "do not invent activity. Examples:\n"
        "  - fills_in_lookback_total=0 -> 'no fills in the lookback window'\n"
        "  - news_in_lookback=0 -> 'no news in the lookback window'\n"
        "  - suggestions_in_lookback=0 -> 'no new advisor suggestions in "
        "the lookback window' (existing advice from earlier still "
        "applies; just don't pretend new ones arrived)\n"
        "  - proposals_in_lookback=0 -> 'no harvester proposals in the "
        "lookback window'\n\n"
        "Guidelines:\n"
        "- Lead with what changed (fills, new news, harvester movements). "
        "Static state (open orders, grid config) is secondary.\n"
        "- When discussing RECENT_SUGGESTIONS, compare proposed values "
        "against GRID_CONFIG (e.g. 'advisor recommends bumping spacing "
        "from 1.0% to 1.2%') -- don't describe suggestions in isolation.\n"
        "- Surface prices and timestamps that matter. Don't invent "
        "numbers not in the JSON.\n"
        "- If a section is empty, say so briefly; don't pad.\n"
        "- Use Markdown sparingly -- bold for headlines, plain text for the "
        "rest. No code fences, no JSON in the output.\n"
        "- Keep it under ~300 words. The operator wants signal, not noise."
    )

    try:
        narrative = await assistant.summarize(system_prompt, user_content, max_tokens=2048)
    except (AssistantError, NotImplementedError):
        return deterministic
    return narrative or deterministic


async def build_weather_report(  # pylint: disable=too-many-arguments
    query: WeatherReportQuery,
    *,
    active_symbols: tuple[Symbol, ...],
    observe_storage: StoragePort | None,
    advise_storage: StoragePort | None,
    recent_news: RecentNewsResult,
    assistant: AssistantPort | None,
) -> WeatherReportResult:
    """Aggregate the market's weather + condense via LLM (P4.5).

    Every fact is precomputed here or in ``services/weather_report.py``;
    the LLM only narrates. Sections degrade gracefully: no observe.db
    means null trends, no advise.db means zero suggestions — the report
    still renders with what's available. ``recent_news`` comes from the
    caller (``OperatorService`` owns the news sub-query and its own
    degradation path).
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=query.lookback_days)

    trends: list[SymbolTrend] = []
    for symbol in active_symbols:
        bars = []
        if observe_storage is not None:
            try:
                bars = await observe_storage.get_ohlc_bars(
                    symbol, 60, start_time=since, end_time=now
                )
            except StorageError:
                bars = []
        trends.append(compute_symbol_trend(symbol, bars, now=now))

    suggestions = []
    if advise_storage is not None:
        try:
            suggestions = await advise_storage.get_advisor_suggestions(since=since, limit=200)
        except StorageError:
            suggestions = []
    directional_calls = extract_directional_calls(suggestions, now=now)

    narrative = await _compose_weather_narrative(
        assistant,
        lookback_days=query.lookback_days,
        trends=trends,
        recent_news=recent_news,
        suggestion_count=len(suggestions),
        directional_calls=directional_calls,
    )
    return WeatherReportResult(
        lookback_days=query.lookback_days,
        narrative=narrative,
        trends=trends,
        news_count=len(recent_news.items),
        suggestion_count=len(suggestions),
        directional_calls=directional_calls,
    )


async def _compose_weather_narrative(  # pylint: disable=too-many-arguments,too-many-locals
    assistant: AssistantPort | None,
    *,
    lookback_days: int,
    trends: list[SymbolTrend],
    recent_news: RecentNewsResult,
    suggestion_count: int,
    directional_calls: list[DirectionalCall],
) -> str:
    """Build the facts payload + call summarize; fall back deterministically."""

    def _fmt(value: float | None, suffix: str = "") -> str:
        return "null" if value is None else f"{value:+.2f}{suffix}"

    moves = [
        f"{t.symbol} {_fmt(t.change_window_pct, '%')}"
        for t in trends
        if t.change_window_pct is not None
    ]
    deterministic = (
        f"Market weather, last {lookback_days}d: "
        + ("; ".join(moves) if moves else "no bar history available")
        + f"; {len(recent_news.items)} news items."
    )
    if assistant is None:
        return deterministic

    trend_lines = [
        f"  {t.symbol}: latest={t.latest_price if t.latest_price is not None else 'null'}, "
        f"change_24h_pct={_fmt(t.change_24h_pct)}, "
        f"change_{lookback_days}d_pct={_fmt(t.change_window_pct)}, "
        f"range_position_pct="
        f"{t.range_position_pct if t.range_position_pct is not None else 'null'}, "
        f"rsi_14={t.rsi_14 if t.rsi_14 is not None else 'null'}, "
        f"adx_14={t.adx_14 if t.adx_14 is not None else 'null'}"
        for t in trends
    ]
    call_lines = [
        f"  {c.symbol}: {c.direction} over {c.horizon_hours:g}h "
        f"(confidence {c.confidence}, made {c.age_hours:.1f}h ago)"
        for c in directional_calls
    ] or ["  (none in window)"]
    blob_lines = [
        f"WINDOW_DAYS: {lookback_days}",
        "",
        "TRENDS (authoritative -- cite these verbatim; null means unknown, never zero):",
        *trend_lines,
        "",
        "COUNTS (authoritative -- cite verbatim; never re-count):",
        f"  news_in_window: {len(recent_news.items)}",
        f"  advisor_suggestions_in_window: {suggestion_count}",
        f"  gremlin_directional_calls_shown: {len(directional_calls)}",
        "",
        "GREMLIN_CALLS (a deliberately loose voice, graded later by the outcome "
        "ledger -- report them as its opinion, never as fact):",
        *call_lines,
        "",
        "NEWS_HEADLINES (the window's items, newest first):",
        recent_news.model_dump_json(indent=2),
    ]
    user_content = "\n".join(blob_lines)

    system_prompt = (
        "You are the WobbleBot operator assistant generating a MARKET "
        "weather report — what the market itself is doing, not what the "
        "bot did (that's the separate status report). You will receive "
        "precomputed per-symbol trends, counts, news headlines, and any "
        "directional calls from the gremlin advisor.\n\n"
        "**TRENDS and COUNTS are authoritative.** Every number you "
        "mention MUST come from them verbatim — never compute, never "
        "re-count the news array, never invent a figure. A null field "
        "means unknown: say the data is missing, never treat it as "
        "zero.\n\n"
        "Reading hints: range_position_pct is where the latest price "
        "sits in the window's low-high range (near 100 = at the highs, "
        "near 0 = at the lows). adx_14 above ~25 suggests trending; "
        "low adx with middling range position suggests chop. rsi_14 "
        "above ~70 / below ~30 is stretched.\n\n"
        "Guidelines:\n"
        "- Two short paragraphs, under ~200 words total: first the "
        "overall regime and the symbols that moved, then news and the "
        "advisors' reads (gremlin calls are one loose voice's opinion "
        "— attribute them, don't endorse them).\n"
        "- Describe, don't advise: no position or configuration "
        "recommendations.\n"
        "- If a section is empty, say so briefly; don't pad.\n"
        "- Markdown sparingly; no code fences, no JSON in the output."
    )

    try:
        narrative = await assistant.summarize(system_prompt, user_content, max_tokens=2048)
    except (AssistantError, NotImplementedError):
        return deterministic
    return narrative or deterministic
