"""Operator interaction daemon — Stage 5.6 (ADR-013).

Long-running CLI that ties together every Phase 5 component:
``DiscordTransport`` (5.2) for Gateway I/O, ``OllamaAssistantAdapter``
(5.3) for intent parsing, ``OperatorService`` (5.4) for query
answering, ``SqliteNotifierAdapter`` (5.5) for outbound forwarding,
plus ``conversation_turns`` (5.6.A) + ``OperatorConfig`` (5.6.B).

Three concurrent concerns: (1) **notification forwarder** drains
``notifications WHERE forwarded=0`` and posts color-coded embeds;
(2) **conversation flow** builds ``ConversationContext``, calls
``AssistantPort.parse_intent``, routes by intent variant (Command →
confirm embed + pending row; Query → answer; Conversational/Unparseable
→ reply); (3) **confirmation flow** transitions
``awaiting_confirmation`` → ``approved``/``rejected`` via reaction
handler + in-memory message_id→pending_id map.

Per ADR-013 decision 3: cli/operator NEVER calls
``OperatorService.dispatch_command`` directly. Commands cross
``pending_commands`` so cli/live's ADR-002 firewall (the
``WHERE status='approved'`` poll, Stage 5.4) is the only path from
intent to engine.

Run as a module: ``python -m wobblebot.cli.operator``
(``--config /path/to/settings.yml`` to override the YAML path).
"""

# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from wobblebot.adapters.anthropic_assistant import AnthropicAssistantAdapter
from wobblebot.adapters.discord_transport import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    CONFIRM_EMOJI,
    REJECT_EMOJI,
    DiscordTransport,
    DiscordTransportConfig,
    DiscordTransportError,
    InboundMessage,
    ReactionEvent,
)
from wobblebot.adapters.google import GoogleAssistantAdapter
from wobblebot.adapters.mock_exchange import MockExchangeAdapter
from wobblebot.adapters.ollama_assistant import OllamaAssistantAdapter
from wobblebot.adapters.openai import OpenAIAssistantAdapter
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    add_config_args,
    emit_heartbeat,
    load_operator_env,
    run_poll_loop,
    safe_shutdown,
)
from wobblebot.config.cli import OperatorConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.prompts import Prompt, load_prompt
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.assistant import (
    AssistantPort,
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
    SymbolStateSnapshot,
)
from wobblebot.ports.exceptions import (
    AssistantError,
    OperatorError,
    StorageError,
)
from wobblebot.ports.operator import (
    IntentCommand,
    IntentConversational,
    IntentQuery,
    IntentUnparseable,
    OperatorIntent,
    PendingCommand,
)
from wobblebot.ports.storage import StoragePort
from wobblebot.services.grid_engine import GridEngine
from wobblebot.services.llm_cost_gate import SessionCostTracker
from wobblebot.services.operator_service import OperatorService

_LOGGER = logging.getLogger("wobblebot.cli.operator")


# --------------------------------------------------------------------- #
# Notification forwarder                                                #
# --------------------------------------------------------------------- #

_LEVEL_TO_COLOR = {
    "info": COLOR_INFO,
    "warning": COLOR_WARNING,
    "error": COLOR_ERROR,
    "critical": COLOR_ERROR,
}


async def _forward_pending_notifications(
    *,
    storage: StoragePort,
    transport: DiscordTransport,
    channel_id: str,
) -> int:
    """Drain ``notifications WHERE forwarded=0``; post each to Discord.

    Per-row failures (Discord post fails, mark-forwarded fails) are
    logged and the loop continues — losing forward progress on one
    row beats stopping the whole daemon. Returns the count of rows
    successfully forwarded.
    """
    try:
        rows = await storage.get_notifications(forwarded=False)
    except StorageError as exc:
        _LOGGER.warning("forwarder: get_notifications failed", extra={"error": str(exc)})
        return 0
    forwarded = 0
    for row in rows:
        if row.id is None:  # defensive; persisted rows always have an id
            continue
        try:
            await transport.send_embed(
                channel_id,
                title=row.notification.title,
                description=row.notification.message,
                color=_LEVEL_TO_COLOR.get(row.notification.level, COLOR_INFO),
                fields=_render_context_fields(row.notification.context),
                footer=f"level={row.notification.level} • id={row.id}",
            )
            await storage.mark_notification_forwarded(row.id, Timestamp(dt=datetime.now(UTC)))
            forwarded += 1
        except (DiscordTransportError, StorageError) as exc:
            _LOGGER.warning(
                "forwarder: per-row forward failed; will retry next poll",
                extra={
                    "notification_id": row.id,
                    "level": row.notification.level,
                    "error": str(exc),
                },
            )
    return forwarded


def _render_context_fields(context: dict[str, Any], max_fields: int = 8) -> list[tuple[str, str]]:
    """Render a context dict as Discord embed fields (name, value pairs).

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


async def _forwarder_loop(
    *,
    storage: StoragePort,
    transport: DiscordTransport,
    channel_id: str,
    poll_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """Background task: poll + forward + sleep, until ``stop_event`` is set."""
    _LOGGER.info(
        "notification forwarder started",
        extra={"channel_id": channel_id, "poll_seconds": poll_seconds},
    )

    async def _one_cycle() -> None:
        # Stage 8.4.E follow-up — cli/operator's forwarder loop is the
        # most-frequent recurring task in the daemon (default 2s);
        # using it as the heartbeat anchor means the /health view sees
        # liveness regardless of whether any notifications were drained.
        await emit_heartbeat(storage, "cli/operator")
        await _forward_pending_notifications(
            storage=storage, transport=transport, channel_id=channel_id
        )

    try:
        await run_poll_loop(_one_cycle, interval_seconds=poll_seconds, stop_event=stop_event)
    finally:
        _LOGGER.info("notification forwarder stopped")


# --------------------------------------------------------------------- #
# TTL expirer for pending_commands                                      #
# --------------------------------------------------------------------- #


async def _expire_stale_pending_commands(storage: StoragePort) -> int:
    """Mark expired any ``awaiting_confirmation`` row past its TTL.

    Per ADR-013 decision 3 the operator's ✅/❌ reaction is the only
    way an awaiting_confirmation row becomes approved/rejected. If
    the operator walks away (or the daemon was offline during
    posting), the row's ``ttl_expires_at`` is the safety net — this
    expirer transitions matches to ``expired`` so the audit table
    doesn't accumulate stale awaiting rows forever.

    Per-row failures (storage update fails) are logged and the loop
    continues — losing one expiration beats stopping the daemon.
    Returns the count of rows successfully expired.
    """
    try:
        rows = await storage.get_pending_commands(status="awaiting_confirmation")
    except StorageError as exc:
        _LOGGER.warning("ttl_expirer: get_pending_commands failed", extra={"error": str(exc)})
        return 0
    now = datetime.now(UTC)
    expired_count = 0
    for row in rows:
        if row.ttl_expires_at.dt > now:
            continue  # not yet expired
        updated = row.model_copy(update={"status": "expired"})
        try:
            await storage.save_pending_command(updated)
            expired_count += 1
            _LOGGER.info(
                "pending command expired",
                extra={
                    "pending_id": str(row.id),
                    "command_kind": row.command.kind,
                    "ttl_expires_at": row.ttl_expires_at.dt.isoformat(),
                },
            )
        except StorageError as exc:
            _LOGGER.warning(
                "ttl_expirer: per-row update failed",
                extra={"pending_id": str(row.id), "error": str(exc)},
            )
    return expired_count


async def _ttl_expirer_loop(
    *,
    storage: StoragePort,
    poll_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """Background task: scan + expire + sleep, until ``stop_event`` is set."""
    _LOGGER.info("ttl expirer started", extra={"poll_seconds": poll_seconds})

    async def _one_cycle() -> None:
        await _expire_stale_pending_commands(storage)

    try:
        await run_poll_loop(_one_cycle, interval_seconds=poll_seconds, stop_event=stop_event)
    finally:
        _LOGGER.info("ttl expirer stopped")


# --------------------------------------------------------------------- #
# Conversation context assembly                                         #
# --------------------------------------------------------------------- #


async def _compose_engine_state_snapshot(
    *,
    live_storage: StoragePort | None,
    active_symbols: tuple[str, ...] = (),
) -> EngineStateSnapshot:
    """Build a best-effort engine state snapshot for the assistant prompt.

    Reads from live.db (open orders + latest balance) when available;
    fills in zeros / empty lists when not. Per the Stage 5.6 v1
    limitation noted in stage-5.1-design.md decision 8: pause state
    is in-memory in cli/live's engine and not visible from
    cli/operator, so all active symbols are reported as ``"active"``.
    """
    now = Timestamp(dt=datetime.now(UTC))
    if live_storage is None:
        return EngineStateSnapshot(
            snapshot_at=now,
            symbols=[],
            total_usd_balance=0.0,
            session_pnl=0.0,
            session_runtime_seconds=0.0,
        )
    symbols: list[SymbolStateSnapshot] = []
    for symbol_str in active_symbols:
        try:
            opens = await live_storage.get_open_orders()
            # No Symbol-from-string parsing here; the engine_state
            # snapshot's symbol field is the plain string form anyway.
            open_for_symbol = sum(1 for o in opens if str(o.symbol) == symbol_str)
        except StorageError:
            open_for_symbol = 0
        symbols.append(
            SymbolStateSnapshot(
                symbol=symbol_str,
                state="active",
                open_order_count=open_for_symbol,
            )
        )
    try:
        balances = await live_storage.get_latest_balance_snapshot()
        usd_total = next((float(b.total) for b in balances if b.asset.upper() == "USD"), 0.0)
    except StorageError:
        usd_total = 0.0
    return EngineStateSnapshot(
        snapshot_at=now,
        symbols=symbols,
        total_usd_balance=usd_total,
        session_pnl=0.0,
        session_runtime_seconds=0.0,
    )


# --------------------------------------------------------------------- #
# Conversation flow — message handler                                   #
# --------------------------------------------------------------------- #


async def _handle_inbound_message(  # pylint: disable=too-many-arguments,too-many-locals
    message: InboundMessage,
    *,
    operator_storage: StoragePort,
    live_storage: StoragePort | None,
    assistant: AssistantPort,
    operator_service: OperatorService,
    transport: DiscordTransport,
    outbound_channel_id: str,
    context_window_turns: int,
    confirm_ttl_seconds: int,
    pending_message_map: dict[str, UUID],
) -> None:
    """Parse an inbound operator message + route the resulting intent.

    Persists the operator turn (with parsed intent), then dispatches:
      - IntentCommand: writes a PendingCommand row (awaiting_confirmation)
        and posts a confirm embed; the reaction handler will transition
        to approved/rejected.
      - IntentQuery: calls operator_service.answer_query and posts an
        embed with the structured result.
      - IntentConversational: posts the reply_text as a plain message.
      - IntentUnparseable: posts the reason as a "couldn't parse" reply.

    Per-step failures are logged and surfaced to Discord; the daemon
    continues. Assistant errors mark the operator turn with intent=None
    and post an apology.
    """
    operator_turn = ConversationTurn(
        id=uuid4(),
        channel_id=message.channel_id,
        user_id=message.user_id,
        role="operator",
        content=message.content,
        intent=None,
        timestamp=message.timestamp,
    )
    try:
        await operator_storage.save_conversation_turn(operator_turn)
    except StorageError as exc:
        _LOGGER.error(
            "failed to persist inbound operator turn; aborting parse",
            extra={"channel_id": message.channel_id, "error": str(exc)},
        )
        return

    snapshot = await _compose_engine_state_snapshot(live_storage=live_storage)
    try:
        recent = await operator_storage.get_conversation_turns(
            message.channel_id, message.user_id, limit=context_window_turns
        )
    except StorageError as exc:
        _LOGGER.warning(
            "failed to read recent turns; proceeding without history",
            extra={"channel_id": message.channel_id, "error": str(exc)},
        )
        recent = []
    # Strip the just-saved operator turn from the history — assistant
    # gets it as ``current_message`` instead of as a prior turn.
    prior_turns = tuple(t for t in recent if t.id != operator_turn.id)

    context = ConversationContext(
        current_message=message.content,
        channel_id=message.channel_id,
        user_id=message.user_id,
        recent_turns=prior_turns,
        engine_state_snapshot=snapshot,
    )

    try:
        intent = await assistant.parse_intent(context)
    except AssistantError as exc:
        _LOGGER.error(
            "assistant parse failed",
            extra={"channel_id": message.channel_id, "error": str(exc)},
        )
        await _safe_send_message(
            transport,
            outbound_channel_id,
            f"Sorry, I couldn't process that. ({type(exc).__name__})",
        )
        return

    # Re-save the operator turn with the parsed intent attached.
    parsed_turn = operator_turn.model_copy(update={"intent": intent})
    try:
        await operator_storage.save_conversation_turn(parsed_turn)
    except StorageError as exc:
        _LOGGER.warning(
            "failed to upsert operator turn with parsed intent",
            extra={"turn_id": str(operator_turn.id), "error": str(exc)},
        )

    await _route_intent(
        intent=intent,
        channel_id=message.channel_id,
        user_id=message.user_id,
        operator_storage=operator_storage,
        operator_service=operator_service,
        transport=transport,
        outbound_channel_id=outbound_channel_id,
        confirm_ttl_seconds=confirm_ttl_seconds,
        pending_message_map=pending_message_map,
    )


async def _route_intent(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    intent: OperatorIntent,
    channel_id: str,
    user_id: str,
    operator_storage: StoragePort,
    operator_service: OperatorService,
    transport: DiscordTransport,
    outbound_channel_id: str,
    confirm_ttl_seconds: int,
    pending_message_map: dict[str, UUID],
) -> None:
    """Dispatch the parsed intent to the right handler."""
    match intent:
        case IntentCommand():
            await _handle_command_intent(
                intent=intent,
                channel_id=channel_id,
                user_id=user_id,
                operator_storage=operator_storage,
                transport=transport,
                outbound_channel_id=outbound_channel_id,
                confirm_ttl_seconds=confirm_ttl_seconds,
                pending_message_map=pending_message_map,
            )
        case IntentQuery():
            await _handle_query_intent(
                intent=intent,
                channel_id=channel_id,
                user_id=user_id,
                operator_storage=operator_storage,
                operator_service=operator_service,
                transport=transport,
                outbound_channel_id=outbound_channel_id,
            )
        case IntentConversational():
            await _handle_conversational(
                intent=intent,
                channel_id=channel_id,
                user_id=user_id,
                operator_storage=operator_storage,
                transport=transport,
                outbound_channel_id=outbound_channel_id,
            )
        case IntentUnparseable():
            await _handle_unparseable(
                intent=intent,
                channel_id=channel_id,
                user_id=user_id,
                operator_storage=operator_storage,
                transport=transport,
                outbound_channel_id=outbound_channel_id,
            )
        case _:
            _LOGGER.error("unknown intent variant", extra={"intent_kind": type(intent).__name__})


async def _handle_command_intent(  # pylint: disable=too-many-arguments
    *,
    intent: IntentCommand,
    channel_id: str,
    user_id: str,
    operator_storage: StoragePort,
    transport: DiscordTransport,
    outbound_channel_id: str,
    confirm_ttl_seconds: int,
    pending_message_map: dict[str, UUID],
) -> None:
    """Persist a PendingCommand + post a confirm embed."""
    now = datetime.now(UTC)
    pending = PendingCommand(
        id=uuid4(),
        command=intent.command,
        status="awaiting_confirmation",
        channel_id=channel_id,
        requesting_user_id=user_id,
        ttl_expires_at=Timestamp(
            dt=now.replace(microsecond=0) + timedelta(seconds=confirm_ttl_seconds)
        ),
        created_at=Timestamp(dt=now),
    )
    try:
        await operator_storage.save_pending_command(pending)
    except StorageError as exc:
        _LOGGER.error(
            "failed to persist pending command; abandoning",
            extra={"command_kind": intent.command.kind, "error": str(exc)},
        )
        await _safe_send_message(
            transport,
            outbound_channel_id,
            "Failed to record the command. Try again in a moment.",
        )
        return

    summary = _summarize_command(intent)
    try:
        confirm_message_id = await transport.send_confirmation(
            outbound_channel_id, summary=summary, ref_id=str(pending.id)
        )
    except DiscordTransportError as exc:
        _LOGGER.error(
            "failed to post confirmation embed",
            extra={"pending_id": str(pending.id), "error": str(exc)},
        )
        return
    pending_message_map[confirm_message_id] = pending.id

    assistant_reply = ConversationTurn(
        id=uuid4(),
        channel_id=channel_id,
        user_id=user_id,
        role="assistant",
        content=f"Posted confirmation for {summary}",
        intent=None,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    await _safe_save_turn(operator_storage, assistant_reply)


def _summarize_command(intent: IntentCommand) -> str:
    """Render a concise one-line summary of a Command intent for the embed."""
    command = intent.command
    if command.kind in ("pause", "resume", "cancel_open_orders"):
        symbol = getattr(command, "symbol", None)
        symbol_str = str(symbol) if symbol is not None else "all symbols"
        return f"`{command.kind}` on {symbol_str}"
    return f"`{command.kind}`"


async def _handle_query_intent(  # pylint: disable=too-many-arguments
    *,
    intent: IntentQuery,
    channel_id: str,
    user_id: str,
    operator_storage: StoragePort,
    operator_service: OperatorService,
    transport: DiscordTransport,
    outbound_channel_id: str,
) -> None:
    """Answer a Query via OperatorService and post an embed."""
    try:
        result = await operator_service.answer_query(intent.query)
    except OperatorError as exc:
        _LOGGER.error(
            "query dispatch failed",
            extra={"query_kind": intent.query.kind, "error": str(exc)},
        )
        await _safe_send_message(
            transport,
            outbound_channel_id,
            f"Query failed: {exc}",
        )
        return

    try:
        await transport.send_embed(
            outbound_channel_id,
            title=f"Query: {intent.query.kind}",
            description=_summarize_query_result(result),
            color=COLOR_SUCCESS,
        )
    except DiscordTransportError as exc:
        _LOGGER.error("failed to post query result", extra={"error": str(exc)})

    assistant_reply = ConversationTurn(
        id=uuid4(),
        channel_id=channel_id,
        user_id=user_id,
        role="assistant",
        content=_summarize_query_result(result),
        intent=None,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    await _safe_save_turn(operator_storage, assistant_reply)


def _summarize_query_result(result: Any) -> str:
    """One-line summary of a QueryResult for the embed description.

    Generic: just dumps the model as JSON. The Discord-side richer
    rendering is a future enhancement; for v1 the JSON is enough.
    """
    try:
        dumped: str = result.model_dump_json(indent=2)
        return dumped[:1800]
    except Exception:  # pylint: disable=broad-exception-caught
        return repr(result)[:1800]


async def _handle_conversational(  # pylint: disable=too-many-arguments
    *,
    intent: IntentConversational,
    channel_id: str,
    user_id: str,
    operator_storage: StoragePort,
    transport: DiscordTransport,
    outbound_channel_id: str,
) -> None:
    """Post the reply_text as a plain message + save the assistant turn."""
    await _safe_send_message(transport, outbound_channel_id, intent.reply_text)
    assistant_reply = ConversationTurn(
        id=uuid4(),
        channel_id=channel_id,
        user_id=user_id,
        role="assistant",
        content=intent.reply_text,
        intent=None,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    await _safe_save_turn(operator_storage, assistant_reply)


async def _handle_unparseable(  # pylint: disable=too-many-arguments
    *,
    intent: IntentUnparseable,
    channel_id: str,
    user_id: str,
    operator_storage: StoragePort,
    transport: DiscordTransport,
    outbound_channel_id: str,
) -> None:
    """Surface the assistant's "I didn't understand" reason to Discord."""
    reply = f"I couldn't parse that: {intent.reason}"
    await _safe_send_message(transport, outbound_channel_id, reply)
    assistant_reply = ConversationTurn(
        id=uuid4(),
        channel_id=channel_id,
        user_id=user_id,
        role="assistant",
        content=reply,
        intent=None,
        timestamp=Timestamp(dt=datetime.now(UTC)),
    )
    await _safe_save_turn(operator_storage, assistant_reply)


# --------------------------------------------------------------------- #
# Confirmation flow — reaction handler                                  #
# --------------------------------------------------------------------- #


async def _handle_reaction(
    event: ReactionEvent,
    *,
    operator_storage: StoragePort,
    pending_message_map: dict[str, UUID],
) -> None:
    """Transition a PendingCommand based on a ✅ / ❌ reaction.

    Lookups go through the in-memory ``pending_message_map``. If the
    map doesn't have the message_id (daemon restarted; reaction is
    on something other than a confirmation embed; etc.), the event
    is ignored. Per ADR-013 the persisted pending_commands row's TTL
    is the long-term safety net — an abandoned awaiting_confirmation
    row expires on its own.
    """
    if event.action != "add":
        return  # only add transitions
    pending_id = pending_message_map.get(event.message_id)
    if pending_id is None:
        return  # not a confirmation reaction

    try:
        pending = await operator_storage.get_pending_command(pending_id)
    except StorageError as exc:
        _LOGGER.error(
            "reaction handler: get_pending_command failed",
            extra={"pending_id": str(pending_id), "error": str(exc)},
        )
        return
    if pending is None:
        return  # row already gone
    if pending.status != "awaiting_confirmation":
        return  # already transitioned (idempotency vs duplicate reaction)

    now = Timestamp(dt=datetime.now(UTC))
    if event.emoji == CONFIRM_EMOJI:
        updated = pending.model_copy(
            update={
                "status": "approved",
                "confirming_user_id": event.user_id,
                "confirmed_at": now,
            }
        )
    elif event.emoji == REJECT_EMOJI:
        updated = pending.model_copy(
            update={
                "status": "rejected",
                "confirming_user_id": event.user_id,
                "confirmed_at": now,
            }
        )
    else:
        return  # other emoji; ignore

    try:
        await operator_storage.save_pending_command(updated)
        _LOGGER.info(
            "pending command transitioned by operator reaction",
            extra={
                "pending_id": str(pending_id),
                "new_status": updated.status,
                "confirming_user_id": event.user_id,
            },
        )
    except StorageError as exc:
        _LOGGER.error(
            "reaction handler: save_pending_command failed",
            extra={"pending_id": str(pending_id), "error": str(exc)},
        )


# --------------------------------------------------------------------- #
# Safe-send helpers                                                     #
# --------------------------------------------------------------------- #


async def _safe_send_message(transport: DiscordTransport, channel_id: str, content: str) -> None:
    """``transport.send_message`` with errors logged + swallowed."""
    try:
        await transport.send_message(channel_id, content)
    except DiscordTransportError as exc:
        _LOGGER.error("send_message failed", extra={"error": str(exc)})


async def _safe_save_turn(storage: StoragePort, turn: ConversationTurn) -> None:
    """``save_conversation_turn`` with errors logged + swallowed."""
    try:
        await storage.save_conversation_turn(turn)
    except StorageError as exc:
        _LOGGER.error(
            "failed to save conversation turn",
            extra={"turn_id": str(turn.id), "error": str(exc)},
        )


# --------------------------------------------------------------------- #
# Lifecycle / wiring                                                    #
# --------------------------------------------------------------------- #


def _build_assistant(  # pylint: disable=too-many-return-statements
    operator_cfg: OperatorConfig,
    config: WobbleBotConfig,
    operator_storage: SQLiteStorageAdapter,
    prompt: Prompt,
) -> AssistantPort | None:
    """Construct the configured ``AssistantPort`` adapter.

    Dispatches on ``operator_cfg.assistant.provider``. Returns
    ``None`` and logs a startup error when the cloud path is
    misconfigured — caller must `return 2` after closing storage.

    Phase 5 ships Ollama. Phase 6 Stage 6.2 adds Anthropic.
    Stages 6.3/6.4 will add OpenAI / Google.
    """
    asst_cfg = operator_cfg.assistant
    if asst_cfg.provider == "ollama":
        return OllamaAssistantAdapter(
            model=asst_cfg.model,
            prompt=prompt,
            base_url=asst_cfg.base_url,
            temperature=asst_cfg.temperature,
            max_tokens=asst_cfg.max_tokens,
            timeout_seconds=asst_cfg.timeout_seconds,
        )
    if asst_cfg.provider == "anthropic":
        if config.llm is None:
            _LOGGER.error(
                "operator.assistant.provider='anthropic' but settings.yml "
                "has no `llm:` block; Phase 6 / ADR-014 requires cost-cap "
                "config for cloud providers."
            )
            return None
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            _LOGGER.error(
                "ANTHROPIC_API_KEY missing from environment; required when "
                "operator.assistant.provider=='anthropic'."
            )
            return None
        return AnthropicAssistantAdapter(
            model=asst_cfg.model,
            prompt=prompt,
            api_key=api_key,
            storage=operator_storage,
            session_tracker=SessionCostTracker(),
            cost_config=config.llm.cost,
            retry_config=config.llm.retry,
            temperature=asst_cfg.temperature,
            max_tokens=asst_cfg.max_tokens,
            timeout_seconds=asst_cfg.timeout_seconds,
        )
    if asst_cfg.provider == "openai":
        if config.llm is None:
            _LOGGER.error(
                "operator.assistant.provider='openai' but settings.yml "
                "has no `llm:` block; Phase 6 / ADR-014 requires cost-cap "
                "config for cloud providers."
            )
            return None
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            _LOGGER.error(
                "OPENAI_API_KEY missing from environment; required when "
                "operator.assistant.provider=='openai'."
            )
            return None
        organization = os.environ.get("OPENAI_ORGANIZATION") or None
        return OpenAIAssistantAdapter(
            model=asst_cfg.model,
            prompt=prompt,
            api_key=api_key,
            organization=organization,
            storage=operator_storage,
            session_tracker=SessionCostTracker(),
            cost_config=config.llm.cost,
            retry_config=config.llm.retry,
            temperature=asst_cfg.temperature,
            max_tokens=asst_cfg.max_tokens,
            timeout_seconds=asst_cfg.timeout_seconds,
        )
    if asst_cfg.provider == "google":
        if config.llm is None:
            _LOGGER.error(
                "operator.assistant.provider='google' but settings.yml "
                "has no `llm:` block; Phase 6 / ADR-014 requires cost-cap "
                "config for cloud providers."
            )
            return None
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            _LOGGER.error(
                "GOOGLE_API_KEY missing from environment; required when "
                "operator.assistant.provider=='google'."
            )
            return None
        return GoogleAssistantAdapter(
            model=asst_cfg.model,
            prompt=prompt,
            api_key=api_key,
            storage=operator_storage,
            session_tracker=SessionCostTracker(),
            cost_config=config.llm.cost,
            retry_config=config.llm.retry,
            temperature=asst_cfg.temperature,
            max_tokens=asst_cfg.max_tokens,
            timeout_seconds=asst_cfg.timeout_seconds,
        )
    # Should never trip: AssistantLLMConfig.provider is a Literal[
    # "ollama", "anthropic", "openai", "google"] for Stage 6.4 (every
    # Phase 6 provider closed); widening it without adding the matching
    # branch would surface here at runtime.
    _LOGGER.error("unknown assistant provider", extra={"provider": asst_cfg.provider})
    return None


async def _main_async(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    config: WobbleBotConfig,
) -> int:
    if config.operator is None:
        _LOGGER.error("settings.yml is missing the `operator:` section")
        return 2
    operator_cfg = config.operator

    if operator_cfg.auth.outbound_channel_id not in operator_cfg.auth.allowed_channel_ids:
        _LOGGER.error(
            "operator.auth.outbound_channel_id must be in allowed_channel_ids",
            extra={
                "outbound_channel_id": operator_cfg.auth.outbound_channel_id,
                "allowed_channel_ids": sorted(operator_cfg.auth.allowed_channel_ids),
            },
        )
        return 2

    # Open the operator daemon's primary DB (pending_commands +
    # notifications + conversation_turns all live here).
    operator_storage = SQLiteStorageAdapter(operator_cfg.operator_db)
    try:
        await operator_storage.connect()
    except StorageError as exc:
        _LOGGER.error(
            "failed to open operator db",
            extra={"path": operator_cfg.operator_db, "error": str(exc)},
        )
        return 2

    # Optional: open live.db (for queries that need order / balance data)
    live_storage: SQLiteStorageAdapter | None = None
    if operator_cfg.live_db is not None:
        live_storage = SQLiteStorageAdapter(operator_cfg.live_db)
        try:
            await live_storage.connect()
        except StorageError as exc:
            _LOGGER.warning(
                "failed to open live db; queries needing it will return empty",
                extra={"path": operator_cfg.live_db, "error": str(exc)},
            )
            live_storage = None

    # Operator assistant LLM
    try:
        prompt = load_prompt(Path(operator_cfg.assistant.prompt_file))
    except (FileNotFoundError, ValueError) as exc:
        _LOGGER.error(
            "failed to load operator prompt",
            extra={"path": operator_cfg.assistant.prompt_file, "error": str(exc)},
        )
        await operator_storage.close()
        return 2
    assistant = _build_assistant(operator_cfg, config, operator_storage, prompt)
    if assistant is None:
        await operator_storage.close()
        return 2

    # Operator service for query answering. cli/operator has no engine,
    # so it constructs a stand-in via the existing OperatorService class
    # against the live_storage; pause/dispatch commands route through
    # pending_commands (cli/live handles them).
    # v1 limitation: status queries return symbols as 'active' because
    # pause state lives in cli/live's in-memory engine, not in storage.
    stub_engine = GridEngine(
        MockExchangeAdapter(starting_balances={}, starting_prices={}),
        live_storage or operator_storage,
        config.grid,
        config.safety,
    )
    operator_service = OperatorService(
        engine=stub_engine,
        storage=live_storage or operator_storage,
        active_symbols=(),
        grid_config=config.grid,
        session_started_at=Timestamp(dt=datetime.now(UTC)),
    )

    # Discord transport
    transport = DiscordTransport(
        DiscordTransportConfig(
            bot_token_env_var=operator_cfg.auth.bot_token_env_var,
            allowed_user_ids=operator_cfg.auth.allowed_user_ids,
            allowed_channel_ids=operator_cfg.auth.allowed_channel_ids,
        )
    )

    # In-memory confirm_message_id → pending_id map. Persisted state
    # survives restart (TTL covers); the map gets rebuilt as new
    # confirmations are posted.
    pending_message_map: dict[str, UUID] = {}

    async def _on_message(msg: InboundMessage) -> None:
        await _handle_inbound_message(
            msg,
            operator_storage=operator_storage,
            live_storage=live_storage,
            assistant=assistant,
            operator_service=operator_service,
            transport=transport,
            outbound_channel_id=operator_cfg.auth.outbound_channel_id,
            context_window_turns=operator_cfg.context_window_turns,
            confirm_ttl_seconds=operator_cfg.confirm_ttl_seconds,
            pending_message_map=pending_message_map,
        )

    async def _on_reaction(evt: ReactionEvent) -> None:
        await _handle_reaction(
            evt,
            operator_storage=operator_storage,
            pending_message_map=pending_message_map,
        )

    transport.on_message(_on_message)
    transport.on_reaction(_on_reaction)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    forwarder_task = asyncio.create_task(
        _forwarder_loop(
            storage=operator_storage,
            transport=transport,
            channel_id=operator_cfg.auth.outbound_channel_id,
            poll_seconds=operator_cfg.forwarder_poll_seconds,
            stop_event=stop_event,
        ),
        name="operator-forwarder",
    )
    ttl_expirer_task = asyncio.create_task(
        _ttl_expirer_loop(
            storage=operator_storage,
            poll_seconds=operator_cfg.ttl_expirer_poll_seconds,
            stop_event=stop_event,
        ),
        name="operator-ttl-expirer",
    )

    _LOGGER.info(
        "operator daemon starting",
        extra={
            "outbound_channel_id": operator_cfg.auth.outbound_channel_id,
            "allowed_user_ids": sorted(operator_cfg.auth.allowed_user_ids),
            "allowed_channel_ids": sorted(operator_cfg.auth.allowed_channel_ids),
            "context_window_turns": operator_cfg.context_window_turns,
            "confirm_ttl_seconds": operator_cfg.confirm_ttl_seconds,
        },
    )

    exit_code = 0
    try:
        # discord.py's Client.start blocks until the connection terminates.
        # SIGINT triggers transport.close() via the signal handler.
        gateway_task = asyncio.create_task(transport.start(), name="operator-gateway")
        await stop_event.wait()
        await transport.close()
        await gateway_task
    except DiscordTransportError as exc:
        _LOGGER.error("discord transport failed; exiting", extra={"error": str(exc)})
        exit_code = 1
    finally:
        stop_event.set()

        async def _cancel_background_tasks() -> None:
            for task in (forwarder_task, ttl_expirer_task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        async def _close_assistant() -> None:
            aclose = getattr(assistant, "aclose", None)
            if aclose is not None:
                await aclose()

        phases: list[tuple[str, Any]] = [
            ("cancel_background_tasks", _cancel_background_tasks),
            ("close_assistant", _close_assistant),
            ("close_operator_storage", operator_storage.close),
        ]
        if live_storage is not None:
            phases.append(("close_live_storage", live_storage.close))
        await safe_shutdown(phases, logger=_LOGGER)
    return exit_code


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """SIGINT/SIGTERM → set stop_event. Windows asyncio falls back to KeyboardInterrupt."""

    def _set_stop() -> None:
        _LOGGER.info("signal received; initiating clean shutdown")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            return


def _build_overrides(_args: argparse.Namespace) -> dict[str, Any]:
    """Translate CLI flags into a config override dict. v1 has no flags."""
    return {}


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="wobblebot.cli.operator",
        description="Discord-backed operator interaction daemon (ADR-013).",
    )
    add_config_args(parser)
    args = parser.parse_args()
    load_operator_env()
    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides=_build_overrides(args),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log_format = config.operator.log_format if config.operator is not None else "plain"
    configure_logging(level="INFO", log_format=log_format)
    # Catch KeyboardInterrupt at the top so Ctrl+C produces a clean
    # exit-code-0 line instead of a CancelledError traceback —
    # mirrors the pattern cli/live and cli/web already use.
    try:
        rc = asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt at top level; exiting clean")
        rc = 0
    # Force-exit so non-daemon library threads (httpx pool, discord.py
    # heartbeat, etc.) can't keep the interpreter alive after the
    # asyncio loop has finished. safe_shutdown in the finally block
    # already ran the data-integrity cleanups; this just bypasses
    # Python's wait-for-non-daemon-threads phase. Matches the
    # 2026-05-23 cli/web hotfix pattern (commit e3a11ce).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main())
