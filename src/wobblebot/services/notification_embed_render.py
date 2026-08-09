"""Render proactive notifications as per-event Discord embeds.

The push-side twin of ``discord_embed_render`` (the v1.0 query-side
renderer): a ``match`` over the typed ``NotificationEvent`` union, one
bespoke embed per event, no fallthrough. Rows without a typed event —
pre-slice history, plus the deliberately-generic raise sites
(heartbeat alerts, maintenance) — render through the legacy
title/message/context-fields path that used to live inline in
``cli/operator``.

Color semantics (per the P3 blueprint): green = trading activity you
wanted (fills, clean session end, successful command), red = stop the
presses (loss cap, failed withdrawal, dirty exit), amber = money
moved (withdrawal submitted — the loudest event the harvester emits),
blue = informational lifecycle.
"""

from __future__ import annotations

from typing import Any

from wobblebot.ports.notification_events import (
    CommandResultEvent,
    FillEvent,
    HarvestProposalEvent,
    LossCapEvent,
    SessionEndEvent,
    SessionStartEvent,
    WithdrawalFailedEvent,
    WithdrawalSubmittedEvent,
)
from wobblebot.ports.notifier import Notification

# Discord embed colors (mirror of adapters/discord_transport.py —
# duplicated because services cannot import adapters, same as
# discord_embed_render).
COLOR_INFO = 0x3498DB
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF39C12
COLOR_ERROR = 0xE74C3C

_LEVEL_TO_COLOR = {
    "info": COLOR_INFO,
    "warning": COLOR_WARNING,
    "error": COLOR_ERROR,
    "critical": COLOR_ERROR,
}


def render_notification_embed(notification: Notification, row_id: int) -> dict[str, Any]:
    """Convert a persisted notification to ``send_embed`` kwargs.

    Typed rows dispatch to the per-event renderer; everything else
    takes the legacy generic path. Both include the ``level``/``id``
    footer the operator relies on for cross-referencing.
    """
    footer = f"level={notification.level} • id={row_id}"
    event = notification.event
    if event is None:
        return {
            "title": notification.title,
            "description": notification.message,
            "color": _LEVEL_TO_COLOR.get(notification.level, COLOR_INFO),
            "fields": render_context_fields(notification.context),
            "footer": footer,
        }
    match event:
        case SessionStartEvent():
            embed = _render_session_start(event)
        case FillEvent():
            embed = _render_fill(event)
        case LossCapEvent():
            embed = _render_loss_cap(event)
        case SessionEndEvent():
            embed = _render_session_end(event)
        case HarvestProposalEvent():
            embed = _render_harvest_proposal(event)
        case WithdrawalFailedEvent():
            embed = _render_withdrawal_failed(event)
        case WithdrawalSubmittedEvent():
            embed = _render_withdrawal_submitted(event)
        case CommandResultEvent():
            embed = _render_command_result(event)
    embed["footer"] = footer
    return embed


def render_context_fields(context: dict[str, Any], max_fields: int = 8) -> list[tuple[str, str]]:
    """Legacy path: a context dict as (name, value) embed fields.

    Discord caps embeds at 25 fields and 1024 chars per value; we
    self-limit to ``max_fields`` and truncate long values so a verbose
    context dict doesn't blow up the embed.
    """
    fields: list[tuple[str, str]] = []
    for idx, (key, value) in enumerate(context.items()):
        if idx >= max_fields:
            break
        text = str(value)
        if len(text) > 200:
            text = text[:197] + "..."
        fields.append((str(key), text))
    return fields


# --------------------------------------------------------------------- #
# Per-event renderers                                                   #
# --------------------------------------------------------------------- #


def _render_session_start(event: SessionStartEvent) -> dict[str, Any]:
    runtime = (
        f"{event.max_runtime_seconds / 60:.0f}m cap"
        if event.max_runtime_seconds is not None
        else "unlimited"
    )
    return {
        "title": f"▶ Live session started — {len(event.symbols)} symbol(s)",
        "description": ", ".join(event.symbols),
        "color": COLOR_INFO,
        "fields": [
            ("Portfolio value", f"${event.starting_value_usd:,.2f}"),
            ("Free USD", f"${event.starting_usd:,.2f}"),
            ("Loss cap", f"${event.max_session_loss_usd:,.2f}"),
            ("Tick / runtime", f"{event.tick_seconds:g}s / {runtime}"),
        ],
    }


def _render_fill(event: FillEvent) -> dict[str, Any]:
    counters = (
        f"{event.counters_placed} counter(s) placed"
        if event.counters_placed
        else "no counters placed"
    )
    return {
        "title": f"✅ Fill — {event.symbol}",
        "description": f"{event.fills} order(s) filled; {counters}.",
        "color": COLOR_SUCCESS,
        "fields": [("Tick", str(event.tick))],
    }


def _render_loss_cap(event: LossCapEvent) -> dict[str, Any]:
    return {
        "title": "🛑 Loss cap tripped — session ending",
        "description": (
            f"Session PnL **${event.session_pnl_usd:,.2f}** breached the "
            f"-${event.limit_usd:,.2f} cap at tick {event.tick}. cli/live is "
            "cancelling open orders and stopping."
        ),
        "color": COLOR_ERROR,
        "fields": [],
    }


def _render_session_end(event: SessionEndEvent) -> dict[str, Any]:
    clean = event.exit_code == 0
    hours, rem = divmod(int(event.duration_seconds), 3600)
    minutes, seconds = divmod(rem, 60)
    pnl = f"${event.session_pnl_usd:,.4f}" if event.session_pnl_usd is not None else "unknown"
    ending_value = (
        f"${event.ending_value_usd:,.2f}" if event.ending_value_usd is not None else "unknown"
    )
    cancels = f"{event.open_orders_cancelled} cancelled"
    if event.open_orders_cancel_failed:
        cancels += f", **{event.open_orders_cancel_failed} FAILED**"
    return {
        "title": (f"⏹ Session ended {'cleanly' if clean else f'(exit {event.exit_code})'}"),
        "description": (f"{event.ticks} tick(s) over {hours}h {minutes:02d}m {seconds:02d}s."),
        "color": COLOR_SUCCESS if clean else COLOR_ERROR,
        "fields": [
            ("Session PnL", pnl),
            ("Value", f"${event.starting_value_usd:,.2f} → {ending_value}"),
            ("Open orders", cancels),
        ],
    }


def _render_harvest_proposal(event: HarvestProposalEvent) -> dict[str, Any]:
    return {
        "title": f"💡 Harvester proposal — {event.direction} {event.amount} {event.asset}",
        "description": (
            f"{event.rationale}\n"
            f"Hypothetical until executed: `cli/harvest --execute {event.proposal_id}`."
        ),
        "color": COLOR_INFO,
        "fields": [
            ("Proposal", event.proposal_id),
            (
                "Exchange balance",
                f"${event.current_exchange_balance:,.2f} → "
                f"${event.target_exchange_balance:,.2f} target",
            ),
        ],
    }


def _render_withdrawal_failed(event: WithdrawalFailedEvent) -> dict[str, Any]:
    return {
        "title": f"❌ Withdrawal failed — {event.amount} {event.asset}",
        "description": (
            f"Kraken rejected proposal {event.proposal_id}: {event.error} "
            f"({event.error_type}). **No money moved.**"
        ),
        "color": COLOR_ERROR,
        "fields": [("Destination", event.destination)],
    }


def _render_withdrawal_submitted(event: WithdrawalSubmittedEvent) -> dict[str, Any]:
    return {
        "title": f"💸 Withdrawal submitted — {event.amount} {event.asset}",
        "description": (
            f"Kraken accepted proposal {event.proposal_id}. " "**Money has left the exchange.**"
        ),
        "color": COLOR_WARNING,
        "fields": [
            ("refid", event.transaction_id),
            ("Destination", event.destination),
            ("Status", event.status),
        ],
    }


def _render_command_result(event: CommandResultEvent) -> dict[str, Any]:
    scope = f" {event.symbol}" if event.symbol else ""
    ok = event.success
    return {
        "title": (
            f"{'✅' if ok else '❌'} Command "
            f"{'executed' if ok else 'FAILED'} — {event.command_kind}{scope}"
        ),
        "description": event.message,
        "color": COLOR_SUCCESS if ok else COLOR_ERROR,
        "fields": [],
    }


__all__ = ("render_context_fields", "render_notification_embed")
