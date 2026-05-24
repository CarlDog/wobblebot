"""End-to-end Discord-bot probe via webhook.

Posts test messages into the wobblebot channel through a Discord
webhook (so the operator sees the conversation live in-channel) and
captures the bot's response by polling ``operator.db``'s
``conversation_turns`` table.

Drives the EXACT code path the live bot runs on every operator
message -- inbound message -> DiscordTransport -> cli/operator's
handler -> assistant.parse_intent -> answer_query/dispatch_command ->
embed render -> channel post. The only thing it doesn't exercise is
operator-driven [OK]/[X] reactions for command confirmations (the
webhook can't react).

**Prerequisites:**

1. Create a webhook in the wobblebot channel:
   Channel Settings -> Integrations -> Webhooks -> New Webhook ->
   copy the URL.
2. Extract the webhook ID from the URL -- the long number between
   ``/webhooks/`` and the next ``/``.
3. Add that ID to ``operator.auth.allowed_user_ids`` in
   ``config/settings.yml`` so the running cli/operator daemon
   accepts inbound messages from the webhook author.
4. Restart cli/operator to load the new allowlist.
5. Save the webhook URL as ``WOBBLEBOT_DISCORD_TEST_WEBHOOK_URL`` in
   ``.env`` (or pass via ``--webhook-url``).

**Use when:**

- Verifying that prompt edits land as expected in the live bot.
- Catching multi-turn routing drift that only shows up after
  several refinement turns.
- Running a regression battery before promoting a prompt change.

Run as: ``python tools/probe_discord_bot.py``
Override the battery via ``--messages "msg1;;msg2;;..."``.

**State written:** every probe message + the bot's response lands
in the production ``conversation_turns`` table. Use a dedicated
test channel + ``test`` user-id allowlist if you don't want probe
traffic in the operator's real history.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

import httpx
from dotenv import load_dotenv

DEFAULT_BATTERY: tuple[str, ...] = (
    "status",
    "how are things?",
    "show me what's available",
    "any news?",
    "give me a brief",
    "status report for the past 4 hours",
    "catch me up",
    "show recent fills",
    "now filter to ETH",
    "ok back to BTC",
    "what about the past 6 hours",
    "buy more bitcoin",
    "pause XRP",
    "thanks",
)

DEFAULT_OPERATOR_DB = "data/wobblebot-operator.db"
POLL_TIMEOUT_SEC = 120
POLL_INTERVAL_SEC = 2


def post_message(webhook_url: str, content: str) -> None:
    response = httpx.post(webhook_url, json={"content": content}, timeout=10)
    response.raise_for_status()


def snapshot_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT id FROM conversation_turns")}


def find_operator_turn(
    conn: sqlite3.Connection, content: str, exclude_ids: set[str]
) -> dict | None:
    rows = conn.execute(
        """
        SELECT id, role, content, intent_json, timestamp
        FROM conversation_turns
        WHERE role = 'operator' AND content = ?
        ORDER BY timestamp DESC
        LIMIT 5
        """,
        (content,),
    ).fetchall()
    for r in rows:
        if r[0] not in exclude_ids:
            return {
                "id": r[0],
                "content": r[2],
                "intent_json": r[3],
                "timestamp": r[4],
            }
    return None


def find_new_assistant_turns(
    conn: sqlite3.Connection, after_iso_ts: str, exclude_ids: set[str]
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, content, intent_json, timestamp
        FROM conversation_turns
        WHERE role = 'assistant' AND timestamp > ?
        ORDER BY timestamp ASC
        """,
        (after_iso_ts,),
    ).fetchall()
    return [
        {"id": r[0], "content": r[1], "timestamp": r[3]} for r in rows if r[0] not in exclude_ids
    ]


def format_intent(intent_json: str | None) -> str:  # pylint: disable=too-many-return-statements
    if not intent_json:
        return "(no intent yet -- parse failed or still running)"
    try:
        intent = json.loads(intent_json)
        kind = str(intent.get("kind", "?"))
        if kind == "command":
            cmd = intent.get("command", {})
            sym = cmd.get("symbol")
            sym_str = f" symbol={sym}" if sym is not None else ""
            return f"command:{cmd.get('kind')}{sym_str}"
        if kind == "query":
            q = intent.get("query", {})
            qkind = q.get("kind")
            extras: list[str] = []
            if (lb := q.get("lookback_hours")) is not None:
                extras.append(f"lookback={lb}")
            if (sym := q.get("symbol")) is not None:
                extras.append(f"symbol={sym}")
            tag = f"[{','.join(extras)}]" if extras else ""
            return f"query:{qkind}{tag}"
        if kind == "conversational":
            return "conversational"
        if kind == "unparseable":
            reason = intent.get("reason", "")[:120]
            return f"unparseable: {reason}"
        return kind
    except (json.JSONDecodeError, TypeError):
        return intent_json[:80]


def probe_one(
    conn: sqlite3.Connection,
    webhook_url: str,
    msg: str,
    idx: int,
    total: int,
) -> None:
    print(f"\n========== [{idx}/{total}] >>> {msg}")
    sys.stdout.flush()

    ids_before = snapshot_ids(conn)
    try:
        post_message(webhook_url, msg)
    except httpx.HTTPError as exc:
        print(f"  POST failed: {exc}")
        return

    # Stage 1: wait for our operator turn to appear
    op_turn: dict | None = None
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        op_turn = find_operator_turn(conn, msg, ids_before)
        if op_turn is not None:
            break
        time.sleep(POLL_INTERVAL_SEC)
    if op_turn is None:
        print(f"  TIMEOUT: operator turn never appeared in {POLL_TIMEOUT_SEC}s")
        return

    # Stage 2: wait for intent_json to be populated
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT intent_json FROM conversation_turns WHERE id = ?",
            (op_turn["id"],),
        ).fetchone()
        if row and row[0]:
            op_turn["intent_json"] = row[0]
            break
        time.sleep(POLL_INTERVAL_SEC)

    # Stage 3: wait for an assistant turn newer than our operator turn
    op_ts = op_turn["timestamp"]
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    assistant_rows: list[dict] = []
    while time.monotonic() < deadline:
        assistant_rows = find_new_assistant_turns(conn, op_ts, ids_before)
        if assistant_rows:
            break
        time.sleep(POLL_INTERVAL_SEC)

    print(f"  PARSED: {format_intent(op_turn['intent_json'])}")
    if not assistant_rows:
        print("  (no assistant reply yet -- likely AssistantError; check daemon logs)")
        return
    for r in assistant_rows:
        content = r["content"]
        if len(content) > 500:
            content = content[:500] + " ...(truncated)"
        print(f"  BOT: {content}")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools.probe_discord_bot",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help=(
            "Webhook URL. Defaults to $WOBBLEBOT_DISCORD_TEST_WEBHOOK_URL "
            "(loaded from .env). Treat as a credential."
        ),
    )
    parser.add_argument(
        "--messages",
        type=str,
        default=None,
        help=(
            "Custom battery as a ';;'-joined string. " "Default: the bundled regression battery."
        ),
    )
    parser.add_argument(
        "--operator-db",
        type=str,
        default=DEFAULT_OPERATOR_DB,
        help=f"Path to operator.db (default: {DEFAULT_OPERATOR_DB}).",
    )
    args = parser.parse_args()

    load_dotenv()
    webhook_url = args.webhook_url or os.environ.get("WOBBLEBOT_DISCORD_TEST_WEBHOOK_URL")
    if not webhook_url:
        print(
            "error: webhook URL not provided. Pass --webhook-url or set "
            "WOBBLEBOT_DISCORD_TEST_WEBHOOK_URL in .env.",
            file=sys.stderr,
        )
        return 2

    if args.messages:
        messages = [m.strip() for m in args.messages.split(";;") if m.strip()]
    else:
        messages = list(DEFAULT_BATTERY)

    conn = sqlite3.connect(args.operator_db)
    try:
        for idx, msg in enumerate(messages, 1):
            probe_one(conn, webhook_url, msg, idx, len(messages))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
