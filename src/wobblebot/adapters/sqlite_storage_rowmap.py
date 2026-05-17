"""Row-to-domain mapping helpers for ``SQLiteStorageAdapter``.

Extracted from ``sqlite_storage.py`` to keep the adapter module under
the project's per-file line budget (1000 lines, pylint
``too-many-lines``). These are pure functions: SQLite row in, domain
or port-layer value object out. They never touch the connection, never
write, and have no side effects.

Two pairs of helpers also live here for the MoE per-expert audit trail
(``serialize_expert_opinions`` / ``deserialize_expert_opinions``) so
the JSON conversion stays alongside the row mapping that consumes it.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import aiosqlite
from pydantic import TypeAdapter

from wobblebot.domain.llm_cost import LLMCallRecord
from wobblebot.domain.models import NewsItem, Order, PriceSnapshot, Trade
from wobblebot.domain.value_objects import Amount, OrderSide, Price, Symbol, Timestamp
from wobblebot.ports.advisor import AdvisorRecommendation, AdvisorSuggestion, AppliedSuggestion
from wobblebot.ports.assistant import ConversationTurn
from wobblebot.ports.harvester import TransferProposal, TransferResult
from wobblebot.ports.notifier import Notification, PersistedNotification
from wobblebot.ports.operator import (
    CommandResult,
    OperatorCommand,
    OperatorIntent,
    PendingCommand,
)

# Module-level TypeAdapter — Pydantic discriminator resolution is the
# only way to materialize the right OperatorCommand variant from a
# serialized dict. Cheap to construct once.
_COMMAND_ADAPTER: TypeAdapter[OperatorCommand] = TypeAdapter(OperatorCommand)
_INTENT_ADAPTER: TypeAdapter[OperatorIntent] = TypeAdapter(OperatorIntent)


def row_to_order(row: aiosqlite.Row) -> Order:
    return Order(
        id=UUID(row["id"]),
        exchange_id=row["exchange_id"],
        symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
        side=OrderSide(row["side"]),
        price=Price(amount=Decimal(row["price_amount"]), currency=row["price_currency"]),
        amount=Amount(value=Decimal(row["amount_value"]), asset=row["amount_asset"]),
        status=row["status"],
        filled_amount=Decimal(row["filled_amount"]),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
        updated_at=(
            Timestamp(dt=datetime.fromisoformat(row["updated_at"])) if row["updated_at"] else None
        ),
    )


def row_to_trade(row: aiosqlite.Row) -> Trade:
    return Trade(
        id=row["id"],
        order_id=row["order_id"],
        symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
        side=OrderSide(row["side"]),
        price=Price(amount=Decimal(row["price_amount"]), currency=row["price_currency"]),
        amount=Amount(value=Decimal(row["amount_value"]), asset=row["amount_asset"]),
        fee=Decimal(row["fee"]),
        cost=Decimal(row["cost"]),
        executed_at=Timestamp(dt=datetime.fromisoformat(row["executed_at"])),
    )


def row_to_price_snapshot(row: aiosqlite.Row) -> PriceSnapshot:
    return PriceSnapshot(
        symbol=Symbol(base=row["symbol_base"], quote=row["symbol_quote"]),
        price=Price(amount=Decimal(row["price_amount"]), currency=row["price_currency"]),
        observed_at=Timestamp(dt=datetime.fromisoformat(row["observed_at"])),
    )


def row_to_news_item(row: aiosqlite.Row) -> NewsItem:
    return NewsItem(
        source=row["source"],
        external_id=row["external_id"],
        published_at=Timestamp(dt=datetime.fromisoformat(row["published_at"])),
        headline=row["headline"],
        body=row["body"] or "",
        sentiment_score=row["sentiment_score"],
        mentioned_coins=json.loads(row["mentioned_coins"]),
        fetched_at=Timestamp(dt=datetime.fromisoformat(row["fetched_at"])),
    )


def row_to_advisor_suggestion(row: aiosqlite.Row) -> AdvisorSuggestion:
    expert_opinions_raw = row["expert_opinions"] if "expert_opinions" in row.keys() else "[]"
    return AdvisorSuggestion(
        recommendation=AdvisorRecommendation(
            recommendation_id=row["recommendation_id"],
            timestamp=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
            role=row["role"],
            recommendations=json.loads(row["recommendations"]),
            rationale=row["rationale"],
            confidence=row["confidence"],
            expert_opinions=deserialize_expert_opinions(
                expert_opinions_raw,
                fallback_timestamp=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
            ),
        ),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
        input_summary=json.loads(row["input_summary"]),
        model_name=row["model_name"],
    )


def serialize_expert_opinions(opinions: list[AdvisorRecommendation]) -> str:
    """Serialize per-expert opinions for the audit-trail column.

    Stored as a JSON array of dicts (one per expert). The forensic
    fields are: role, confidence, recommendations, rationale. The
    ``recommendation_id`` and ``timestamp`` per-expert get dropped on
    purpose — they're synthetic UUIDs / per-call wall-clocks that
    carry no semantic information once the aggregated row exists. If
    we ever need them, the column is JSON so a future migration can
    add them back without a schema change.
    """
    return json.dumps(
        [
            {
                "role": op.role,
                "confidence": op.confidence,
                "recommendations": op.recommendations,
                "rationale": op.rationale,
            }
            for op in opinions
        ]
    )


def deserialize_expert_opinions(
    raw: str,
    *,
    fallback_timestamp: Timestamp,
) -> list[AdvisorRecommendation]:
    """Reverse :func:`serialize_expert_opinions`.

    We reconstruct ``AdvisorRecommendation`` instances with synthetic
    ``recommendation_id`` (``"opinion-<idx>"``) and the parent row's
    timestamp — neither field was persisted per-opinion. Read-side
    consumers (``tools/show_suggestions.py``) care about role /
    confidence / recommendations / rationale, not the synthetic IDs.
    """
    if not raw:
        return []
    payload = json.loads(raw)
    return [
        AdvisorRecommendation(
            recommendation_id=f"opinion-{idx}",
            timestamp=fallback_timestamp,
            role=entry["role"],
            recommendations=entry.get("recommendations") or {},
            rationale=entry["rationale"],
            confidence=entry["confidence"],
        )
        for idx, entry in enumerate(payload)
    ]


def row_to_applied_suggestion(row: aiosqlite.Row) -> AppliedSuggestion:
    return AppliedSuggestion(
        recommendation_id=row["recommendation_id"],
        applied_at=Timestamp(dt=datetime.fromisoformat(row["applied_at"])),
        symbol=row["symbol"],
        applied_keys=json.loads(row["applied_keys"]),
        rejected_keys=json.loads(row["rejected_keys"]),
        model_name=row["model_name"],
        rationale=row["rationale"],
    )


def row_to_transfer_proposal(row: aiosqlite.Row) -> TransferProposal:
    return TransferProposal(
        proposal_id=row["proposal_id"],
        direction=row["direction"],
        asset=row["asset"],
        amount=Decimal(row["amount"]),
        rationale=row["rationale"],
        current_exchange_balance=Decimal(row["current_exchange_balance"]),
        target_exchange_balance=Decimal(row["target_exchange_balance"]),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
    )


def row_to_transfer_result(row: aiosqlite.Row) -> TransferResult:
    return TransferResult(
        proposal_id=row["proposal_id"],
        transaction_id=row["transaction_id"],
        status=row["status"],
        executed_amount=Decimal(row["executed_amount"]),
        direction=row["direction"],
        asset=row["asset"],
        timestamp=Timestamp(dt=datetime.fromisoformat(row["timestamp"])),
    )


def row_to_conversation_turn(row: aiosqlite.Row) -> ConversationTurn:
    """Materialize a ``ConversationTurn`` from a ``conversation_turns`` row.

    ``intent_json`` is NULL for assistant turns and for operator turns
    whose AssistantPort parse failed; both materialize as
    ``intent=None``. Operator turns with a parsed intent run through
    the discriminator-aware ``TypeAdapter`` to rebuild the right
    ``OperatorIntent`` variant.
    """
    intent_raw = row["intent_json"]
    intent = _INTENT_ADAPTER.validate_json(intent_raw) if intent_raw else None
    return ConversationTurn(
        id=UUID(row["id"]),
        channel_id=row["channel_id"],
        user_id=row["user_id"],
        role=row["role"],
        content=row["content"],
        intent=intent,
        timestamp=Timestamp(dt=datetime.fromisoformat(row["timestamp"])),
    )


def row_to_notification(row: aiosqlite.Row) -> PersistedNotification:
    """Materialize a ``PersistedNotification`` from a ``notifications`` row.

    Builds the inner :class:`Notification` from the persisted columns,
    then wraps it with the row-level ``id`` / ``forwarded`` /
    ``forwarded_at`` / ``created_at`` fields.
    """
    notification = Notification(
        level=row["level"],
        title=row["title"],
        message=row["message"],
        timestamp=Timestamp(dt=datetime.fromisoformat(row["timestamp"])),
        context=json.loads(row["context_json"]),
    )
    return PersistedNotification(
        id=row["id"],
        notification=notification,
        forwarded=bool(row["forwarded"]),
        forwarded_at=(
            Timestamp(dt=datetime.fromisoformat(row["forwarded_at"]))
            if row["forwarded_at"]
            else None
        ),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
    )


def row_to_pending_command(row: aiosqlite.Row) -> PendingCommand:
    """Materialize a ``PendingCommand`` from a ``pending_commands`` row.

    ``command_json`` and ``result_json`` are stored as JSON strings so
    schema evolution (new command kinds, new result fields) doesn't
    force a SQLite migration. Discriminator resolution rebuilds the
    typed ``OperatorCommand`` variant on read.
    """
    command_payload = json.loads(row["command_json"])
    command = _COMMAND_ADAPTER.validate_python(command_payload)
    result_raw = row["result_json"]
    result: CommandResult | None = None
    if result_raw:
        result = CommandResult.model_validate_json(result_raw)
    return PendingCommand(
        id=UUID(row["id"]),
        command=command,
        status=row["status"],
        channel_id=row["channel_id"],
        requesting_user_id=row["requesting_user_id"],
        confirming_user_id=row["confirming_user_id"],
        confirmed_at=(
            Timestamp(dt=datetime.fromisoformat(row["confirmed_at"]))
            if row["confirmed_at"]
            else None
        ),
        dispatched_at=(
            Timestamp(dt=datetime.fromisoformat(row["dispatched_at"]))
            if row["dispatched_at"]
            else None
        ),
        result=result,
        ttl_expires_at=Timestamp(dt=datetime.fromisoformat(row["ttl_expires_at"])),
        created_at=Timestamp(dt=datetime.fromisoformat(row["created_at"])),
    )


def row_to_llm_call_record(row: aiosqlite.Row) -> LLMCallRecord:
    """Materialize an ``LLMCallRecord`` from an ``llm_calls`` row.

    All non-null columns map directly; ``tokens_reasoning`` and
    ``request_id`` and ``error_kind`` round-trip as ``None`` when the
    row stored NULL. ``cost_usd`` is stored as a TEXT string to
    preserve Decimal precision; convert back via ``Decimal`` ctor.
    """
    return LLMCallRecord(
        id=UUID(row["id"]),
        timestamp=Timestamp(dt=datetime.fromisoformat(row["timestamp"])),
        role=row["role"],
        provider=row["provider"],
        model=row["model"],
        tokens_in=int(row["tokens_in"]),
        tokens_out=int(row["tokens_out"]),
        tokens_reasoning=(
            int(row["tokens_reasoning"]) if row["tokens_reasoning"] is not None else None
        ),
        cost_usd=Decimal(row["cost_usd"]),
        request_id=row["request_id"],
        success=bool(row["success"]),
        error_kind=row["error_kind"],
    )
