"""Render ``QueryResult`` variants as Discord embed payloads.

Replaces the v1 ``_summarize_query_result`` JSON-blob approach in
``cli/operator`` with structured embeds — title, description, fields,
color. Each per-variant renderer is a pure function over a typed
``QueryResult`` and returns a ``dict[str, Any]`` matching the kwargs
of ``DiscordTransport.send_embed``.

Color constants are defined locally (mirroring the adapter's) so
this service stays hex-layer-clean — services don't import from
adapters per ADR-001.
"""

from __future__ import annotations

from typing import Any

from wobblebot.ports.operator_results import (
    FillEntry,
    GridConfigResult,
    HarvesterStatusResult,
    HelpEntry,
    HelpResult,
    NewsEntry,
    OpenOrderEntry,
    OpenOrdersResult,
    ProposalEntry,
    QueryResult,
    RecentFillsResult,
    RecentNewsResult,
    RecentProposalsResult,
    RecentSuggestionsResult,
    StatusResult,
    SuggestionEntry,
    SymbolStatusEntry,
)

# Discord embed colors (mirror of adapters/discord_transport.py
# constants — duplicated here because services cannot import adapters).
COLOR_INFO = 0x3498DB
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF39C12
COLOR_ERROR = 0xE74C3C

# Discord caps embed fields at 25 and each value at 1024 chars; we
# self-limit lower to keep the embed readable on mobile.
_MAX_LIST_ENTRIES = 10
_MAX_FIELD_VALUE_CHARS = 1000


def render_query_embed(result: QueryResult) -> dict[str, Any]:  # pylint: disable=too-many-return-statements
    """Convert any ``QueryResult`` variant to ``send_embed`` kwargs.

    Dispatches on the discriminated union; each per-variant helper
    returns the kwargs dict directly. New variants must be added to
    the ``match`` block — there is no default fallthrough.
    """
    match result:
        case StatusResult():
            return _render_status(result)
        case OpenOrdersResult():
            return _render_open_orders(result)
        case RecentFillsResult():
            return _render_recent_fills(result)
        case RecentSuggestionsResult():
            return _render_recent_suggestions(result)
        case RecentNewsResult():
            return _render_recent_news(result)
        case HarvesterStatusResult():
            return _render_harvester_status(result)
        case RecentProposalsResult():
            return _render_recent_proposals(result)
        case GridConfigResult():
            return _render_grid_config(result)
        case HelpResult():
            return _render_help(result)


# --------------------------------------------------------------------- #
# Per-variant renderers                                                 #
# --------------------------------------------------------------------- #


def _render_status(result: StatusResult) -> dict[str, Any]:
    paused = [s for s in result.symbols if s.state == "paused"]
    color = COLOR_WARNING if paused else COLOR_SUCCESS
    runtime_min, runtime_sec = divmod(int(result.session_runtime_seconds), 60)
    runtime_h, runtime_min = divmod(runtime_min, 60)
    desc_lines = [
        f"**Balance**: ${result.total_usd_balance:,.2f}",
        f"**Session PnL**: ${result.session_pnl:+,.4f}",
        f"**Runtime**: {runtime_h}h {runtime_min:02d}m {runtime_sec:02d}s",
        f"**Recent fills**: {result.recent_fill_count}",
    ]
    fields = [(_status_field_name(s), _status_field_value(s)) for s in result.symbols]
    return {
        "title": "Engine status",
        "description": "\n".join(desc_lines),
        "color": color,
        "fields": fields,
    }


def _status_field_name(entry: SymbolStatusEntry) -> str:
    badge = "▶" if entry.state == "active" else "⏸"
    return f"{badge} {entry.symbol}"


def _status_field_value(entry: SymbolStatusEntry) -> str:
    return f"state: `{entry.state}` • open orders: {entry.open_order_count}"


def _render_open_orders(result: OpenOrdersResult) -> dict[str, Any]:
    scope = result.symbol if result.symbol else "all symbols"
    if not result.orders:
        return {
            "title": f"Open orders — {scope}",
            "description": "_No open orders._",
            "color": COLOR_INFO,
            "fields": [],
        }
    rendered, overflow = _take_with_overflow(result.orders, _MAX_LIST_ENTRIES)
    fields = [(f"{o.side.upper()} {o.symbol}", _open_order_value(o)) for o in rendered]
    if overflow:
        fields.append(("…", f"{overflow} more not shown"))
    return {
        "title": f"Open orders — {scope}",
        "description": f"**{len(result.orders)}** open",
        "color": COLOR_INFO,
        "fields": fields,
    }


def _open_order_value(order: OpenOrderEntry) -> str:
    return (
        f"price `${order.price:,.4f}` • amount `{order.amount:.8f}`\n"
        f"id `{order.order_id[:12]}…`\n"
        f"created `{order.created_at.dt.isoformat(timespec='seconds')}`"
    )


def _render_recent_fills(result: RecentFillsResult) -> dict[str, Any]:
    scope = result.symbol if result.symbol else "all symbols"
    title = f"Recent fills — {scope} ({result.lookback_hours}h)"
    if not result.fills:
        return {
            "title": title,
            "description": "_No fills in the lookback window._",
            "color": COLOR_INFO,
            "fields": [],
        }
    rendered, overflow = _take_with_overflow(result.fills, _MAX_LIST_ENTRIES)
    fields = [(f"{f.side.upper()} {f.symbol}", _fill_value(f)) for f in rendered]
    if overflow:
        fields.append(("…", f"{overflow} more not shown"))
    return {
        "title": title,
        "description": f"**{len(result.fills)}** fills",
        "color": COLOR_INFO,
        "fields": fields,
    }


def _fill_value(fill: FillEntry) -> str:
    pnl_str = f" • PnL `${fill.pnl:+,.4f}`" if fill.pnl is not None else ""
    return (
        f"price `${fill.price:,.4f}` • amount `{fill.amount:.8f}`{pnl_str}\n"
        f"filled `{fill.filled_at.dt.isoformat(timespec='seconds')}`"
    )


def _render_recent_suggestions(result: RecentSuggestionsResult) -> dict[str, Any]:
    scope = result.symbol if result.symbol else "all symbols"
    if not result.suggestions:
        return {
            "title": f"Recent advisor suggestions — {scope}",
            "description": "_No suggestions found._",
            "color": COLOR_INFO,
            "fields": [],
        }
    rendered, overflow = _take_with_overflow(result.suggestions, _MAX_LIST_ENTRIES)
    fields = [(f"{s.symbol} • {s.model_name}", _suggestion_value(s)) for s in rendered]
    if overflow:
        fields.append(("…", f"{overflow} more not shown"))
    return {
        "title": f"Recent advisor suggestions — {scope}",
        "description": f"**{len(result.suggestions)}** suggestions",
        "color": COLOR_INFO,
        "fields": fields,
    }


def _suggestion_value(entry: SuggestionEntry) -> str:
    rationale = _truncate(entry.rationale, _MAX_FIELD_VALUE_CHARS - 120)
    return (
        f"confidence `{entry.confidence}` • id `{entry.recommendation_id[:12]}…`\n"
        f"created `{entry.created_at.dt.isoformat(timespec='seconds')}`\n"
        f"{rationale}"
    )


def _render_recent_news(result: RecentNewsResult) -> dict[str, Any]:
    title = f"Recent news ({result.lookback_hours}h)"
    if not result.items:
        return {
            "title": title,
            "description": "_No news items in the lookback window._",
            "color": COLOR_INFO,
            "fields": [],
        }
    rendered, overflow = _take_with_overflow(result.items, _MAX_LIST_ENTRIES)
    fields = [(_news_field_name(n), _news_value(n)) for n in rendered]
    if overflow:
        fields.append(("…", f"{overflow} more not shown"))
    return {
        "title": title,
        "description": f"**{len(result.items)}** items",
        "color": COLOR_INFO,
        "fields": fields,
    }


def _news_field_name(entry: NewsEntry) -> str:
    return _truncate(entry.headline, 250)


def _news_value(entry: NewsEntry) -> str:
    parts = [f"`{entry.source}`"]
    if entry.sentiment_score is not None:
        parts.append(f"sentiment `{entry.sentiment_score:+.2f}`")
    if entry.mentioned_coins:
        parts.append(f"coins: {', '.join(entry.mentioned_coins)}")
    parts.append(f"published `{entry.published_at.dt.isoformat(timespec='minutes')}`")
    return " • ".join(parts)


def _render_harvester_status(result: HarvesterStatusResult) -> dict[str, Any]:
    color_map = {
        "deficit": COLOR_WARNING,
        "topup": COLOR_INFO,
        "hold": COLOR_SUCCESS,
        "surplus": COLOR_WARNING,
    }
    color = color_map.get(result.band, COLOR_INFO)
    desc_lines = [
        f"**Enabled**: {'yes' if result.enabled else 'no'}",
        f"**Asset**: {result.asset}",
        f"**Balance**: `{result.current_balance:,.2f}`",
        f"**Band**: `{result.band}`",
    ]
    fields: list[tuple[str, str]] = []
    if result.latest_proposal_id is not None:
        fields.append(
            (
                "Latest proposal",
                (
                    f"id `{result.latest_proposal_id[:12]}…`\n"
                    f"direction `{result.latest_proposal_direction}` • "
                    f"amount `{result.latest_proposal_amount}`"
                ),
            )
        )
    return {
        "title": "Harvester status",
        "description": "\n".join(desc_lines),
        "color": color,
        "fields": fields,
    }


def _render_recent_proposals(result: RecentProposalsResult) -> dict[str, Any]:
    direction_label = result.direction or "all directions"
    title = f"Recent harvester proposals — {direction_label} ({result.lookback_hours}h)"
    if not result.proposals:
        return {
            "title": title,
            "description": "_No proposals in the lookback window._",
            "color": COLOR_INFO,
            "fields": [],
        }
    rendered, overflow = _take_with_overflow(result.proposals, _MAX_LIST_ENTRIES)
    fields = [(_proposal_field_name(p), _proposal_value(p)) for p in rendered]
    if overflow:
        fields.append(("…", f"{overflow} more not shown"))
    return {
        "title": title,
        "description": f"**{len(result.proposals)}** proposals",
        "color": COLOR_INFO,
        "fields": fields,
    }


def _proposal_field_name(entry: ProposalEntry) -> str:
    return f"{entry.direction} • {entry.amount:,.2f} {entry.asset}"


def _proposal_value(entry: ProposalEntry) -> str:
    rationale = _truncate(entry.rationale, _MAX_FIELD_VALUE_CHARS - 100)
    return (
        f"id `{entry.proposal_id[:12]}…`\n"
        f"created `{entry.created_at.dt.isoformat(timespec='seconds')}`\n"
        f"{rationale}"
    )


def _render_grid_config(result: GridConfigResult) -> dict[str, Any]:
    scope = result.symbol if result.symbol else "default tier"
    desc_lines = [
        f"**Symbol**: {scope}",
        f"**Spacing**: `{result.spacing_percentage}%`",
        f"**Levels above**: {result.levels_above}",
        f"**Levels below**: {result.levels_below}",
        f"**Order size**: `${result.order_size_usd:,.2f}`",
    ]
    return {
        "title": f"Grid config — {scope}",
        "description": "\n".join(desc_lines),
        "color": COLOR_INFO,
        "fields": [],
    }


def _render_help(result: HelpResult) -> dict[str, Any]:
    commands = [e for e in result.entries if e.category == "command"]
    queries = [e for e in result.entries if e.category == "query"]
    fields: list[tuple[str, str]] = []
    if commands:
        fields.append(("Commands", _help_list(commands)))
    if queries:
        fields.append(("Queries", _help_list(queries)))
    if not fields:
        return {
            "title": "Available commands and queries",
            "description": "_No help entries available._",
            "color": COLOR_INFO,
            "fields": [],
        }
    return {
        "title": "Available commands and queries",
        "description": f"{len(commands)} commands • {len(queries)} queries",
        "color": COLOR_INFO,
        "fields": fields,
    }


def _help_list(entries: list[HelpEntry]) -> str:
    lines = [f"`{e.kind}` — {e.description}" for e in entries]
    joined = "\n".join(lines)
    return _truncate(joined, _MAX_FIELD_VALUE_CHARS)


# --------------------------------------------------------------------- #
# Internal helpers                                                      #
# --------------------------------------------------------------------- #


def _take_with_overflow[T](items: list[T], limit: int) -> tuple[list[T], int]:
    """Return ``(first_n, count_dropped)`` so renderers can show "N more"."""
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
