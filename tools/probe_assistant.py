"""Probe the operator-assistant LLM's intent-routing accuracy.

Calls ``OllamaAssistantAdapter.parse_intent`` directly against the
configured Ollama model with a battery of test phrasings and prints
each parse. Bypasses storage, Discord, and the OperatorService --
the goal is to evaluate the prompt + model's routing decisions in
isolation. Fast iteration (~10s per message vs ~30s through the
full Discord round-trip).

**Use when:**

- Editing ``config/prompts/operator.md`` and you want to know whether
  the routing examples are being honored before deploying.
- Swapping models (``operator.assistant.model``) and you want a
  quick accuracy check.
- Investigating a routing regression without the noise of the full
  daemon pipeline.

**Use ``tools/probe_discord_bot.py`` instead when** you want to
verify the full chain (operator turn persistence + intent routing
+ embed rendering + Discord posting) -- at the cost of needing a
running ``cli/operator`` daemon and a webhook.

Run as: ``python tools/probe_assistant.py``
Override the message battery via ``--messages "msg1;;msg2;;..."``.

No external state mutated. No Discord traffic. No DB writes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wobblebot.adapters.ollama_assistant import OllamaAssistantAdapter
from wobblebot.config.prompts import load_prompt
from wobblebot.config.runtime import load_resolved_config
from wobblebot.domain.value_objects import Timestamp
from wobblebot.ports.assistant import (
    ConversationContext,
    ConversationTurn,
    EngineStateSnapshot,
    SymbolStateSnapshot,
)

# The default battery covers every routing surface the prompt
# advertises. Edit / extend as new prompt examples land. Each entry
# is (category, message); the harness prints both for context.
DEFAULT_BATTERY: tuple[tuple[str, str], ...] = (
    ("simple_query", "status"),
    ("simple_query", "open orders"),
    ("simple_query", "harvester_status"),
    ("phrasing", "how are things?"),
    ("phrasing", "what's going on"),
    ("phrasing", "show me what's available"),
    ("phrasing", "what can you do"),
    ("phrasing", "any news?"),
    ("phrasing", "what's the harvester doing"),
    ("phrasing", "show grid"),
    ("brief", "give me a brief"),
    ("brief", "morning update"),
    ("brief", "catch me up"),
    ("brief", "status report"),
    ("brief", "status report for the past 4 hours"),
    ("brief", "what's new since last check"),
    ("command", "pause BTC"),
    ("command", "resume BTC/USD"),
    ("command", "pause everything"),
    ("command", "stop the bot"),
    ("command", "cancel open orders on BTC"),
    ("edge", "buy more bitcoin"),
    ("edge", "what's the weather"),
    ("edge", "pause XRP"),
    ("edge", "thanks"),
    ("edge", "good night"),
    ("numeric", "status report for the last 2 hours"),
    ("numeric", "fills in the past 6 hours"),
    ("numeric", "news from the past 12 hours"),
)


def make_snapshot() -> EngineStateSnapshot:
    """A reasonable engine-state snapshot for grounding parses.

    Hardcoded BTC/USD active so parses against "pause BTC" /
    "show grid" succeed; "pause XRP" should still come back as
    unparseable since XRP isn't in the snapshot's symbol set.
    """
    return EngineStateSnapshot(
        snapshot_at=Timestamp(dt=datetime.now(UTC)),
        symbols=[
            SymbolStateSnapshot(symbol="BTC/USD", state="active", open_order_count=5),
        ],
        total_usd_balance=79.92,
        session_pnl=0.0,
        session_runtime_seconds=60.0,
        recent_fill_count=2,
        harvester_band="hold",
    )


def format_intent(intent) -> str:  # type: ignore[no-untyped-def]  # pylint: disable=too-many-return-statements
    """Render a parsed intent as a short one-liner for visual scanning."""
    kind = intent.kind
    if kind == "command":
        sym = getattr(intent.command, "symbol", None)
        sym_str = f" symbol={sym}" if sym is not None else ""
        return f"command:{intent.command.kind}{sym_str}"
    if kind == "query":
        q = intent.query
        query_extras: list[str] = []
        lb = getattr(q, "lookback_hours", None)
        if lb is not None:
            query_extras.append(f"lookback={lb}")
        sym = getattr(q, "symbol", None)
        if sym is not None:
            query_extras.append(f"symbol={sym}")
        tag = f"[{','.join(query_extras)}]" if query_extras else ""
        return f"query:{q.kind}{tag}"
    if kind == "conversational":
        return f"conversational: {intent.reply_text[:80]}"
    if kind == "unparseable":
        return f"unparseable: {intent.reason[:120]}"
    return str(kind)


async def main_async(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    messages: list[str],
    include_multi_turn: bool,
    model_override: str | None,
    prompt_file_override: str | None,
    force_json: bool,
    bypass_suitability_check: bool,
) -> int:
    config = load_resolved_config(config_path=None, profile_name=None, cli_overrides={})
    operator_cfg = config.operator
    if operator_cfg is None:
        print("error: settings.yml is missing the operator block", file=sys.stderr)
        return 2
    if operator_cfg.assistant.provider != "ollama":
        print(
            f"warning: assistant.provider is {operator_cfg.assistant.provider!r}; "
            "this probe only knows how to construct the Ollama adapter.",
            file=sys.stderr,
        )
        return 2

    prompt_path = Path(prompt_file_override) if prompt_file_override else Path(
        operator_cfg.assistant.prompt_file
    )
    prompt = load_prompt(prompt_path)
    model = model_override or operator_cfg.assistant.model
    print(f"# probe model: {model}")
    print(f"# prompt file: {prompt_path} ({len(prompt.body)} chars)")
    if force_json:
        print("# force_json: ON (overrides is_thinking_model heuristic)")
    adapter = OllamaAssistantAdapter(
        model=model,
        prompt=prompt,
        base_url=operator_cfg.assistant.base_url,
        temperature=operator_cfg.assistant.temperature,
        max_tokens=operator_cfg.assistant.max_tokens,
        timeout_seconds=180.0,
        force_json=force_json,
        bypass_suitability_check=bypass_suitability_check,
    )
    snapshot = make_snapshot()

    for msg in messages:
        ctx = ConversationContext(
            current_message=msg,
            channel_id="C-probe",
            user_id="U-probe",
            recent_turns=(),
            engine_state_snapshot=snapshot,
        )
        try:
            intent = await adapter.parse_intent(ctx)
            parsed = format_intent(intent)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            parsed = f"ERROR: {type(exc).__name__}: {exc}"
        print(f">>> {msg}")
        print(f"    {parsed}")
        print()

    if include_multi_turn:
        # Reproduces the 2026-05-24 multi-turn drift defect (now
        # mitigated by the prompt's "trust the catalog" guidance +
        # null-default coercion in operator_intents.py). Re-run this
        # block whenever the prompt or schema changes to catch
        # regressions.
        print("=" * 60)
        print("MULTI-TURN: fills query, refine, refine, refine")
        print("=" * 60)
        history = [
            ("show recent fills", "Recent fills -- all symbols (24h): **2** fills"),
            ("now filter to ETH", "Recent fills -- ETH/USD (24h): _No fills_"),
            ("ok back to BTC", "Recent fills -- BTC/USD (24h): **2** fills"),
        ]
        turns: list[ConversationTurn] = []
        for user_msg, assistant_reply in history:
            ts = Timestamp(dt=datetime.now(UTC))
            turns.append(
                ConversationTurn(
                    id=uuid4(),
                    channel_id="C-probe",
                    user_id="U-probe",
                    role="operator",
                    content=user_msg,
                    intent=None,
                    timestamp=ts,
                )
            )
            turns.append(
                ConversationTurn(
                    id=uuid4(),
                    channel_id="C-probe",
                    user_id="U-probe",
                    role="assistant",
                    content=assistant_reply,
                    intent=None,
                    timestamp=ts,
                )
            )
        follow_up = "what about the past 6 hours"
        ctx = ConversationContext(
            current_message=follow_up,
            channel_id="C-probe",
            user_id="U-probe",
            recent_turns=tuple(turns),
            engine_state_snapshot=snapshot,
        )
        try:
            intent = await adapter.parse_intent(ctx)
            print(f">>> {follow_up}")
            print(f"    {format_intent(intent)}")
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            print(f">>> {follow_up}")
            print(f"    ERROR: {type(exc).__name__}: {exc}")

    await adapter._client.aclose()  # pylint: disable=protected-access
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools.probe_assistant",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--messages",
        type=str,
        default=None,
        help=(
            "Custom message battery as a ';;'-joined string. "
            "Default: the bundled battery covering every prompt routing case."
        ),
    )
    parser.add_argument(
        "--skip-multi-turn",
        action="store_true",
        help="Skip the multi-turn drift reproduction block at the end.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=(
            "Override the configured operator.assistant.model (e.g. "
            "'gemma4:e4b-it-q8_0'). Useful for A/B comparison across "
            "Ollama-served models without editing settings.yml or "
            "restarting cli/operator. Default: use the configured model."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        type=str,
        default=None,
        help=(
            "Override the system prompt path (default: "
            "operator.assistant.prompt_file from settings). Used to "
            "evaluate compact prompt variants against small reasoning "
            "models. The file must still have role=operator in its YAML "
            "frontmatter."
        ),
    )
    parser.add_argument(
        "--force-json",
        action="store_true",
        help=(
            "Force Ollama 'format=json' even for thinking-model name "
            "patterns. The 2026-05-25 diagnostic showed newer reasoning "
            "models (phi4-reasoning) emit clean JSON under format=json "
            "rather than the assumed empty-{}. Use this flag to evaluate "
            "whether a candidate's blocked classification is still valid."
        ),
    )
    parser.add_argument(
        "--bypass-suitability-check",
        action="store_true",
        help=(
            "Skip the KNOWN_INCOMPATIBLE_FOR_ASSISTANT hard-block at "
            "adapter construction. Required to evaluate models on that "
            "list under the new compact-prompt + force_json fixes. The "
            "blocklist remains in effect for production cli/operator."
        ),
    )
    args = parser.parse_args()

    if args.messages:
        messages = [m.strip() for m in args.messages.split(";;") if m.strip()]
    else:
        messages = [m for _, m in DEFAULT_BATTERY]

    return asyncio.run(
        main_async(
            messages,
            not args.skip_multi_turn,
            args.model,
            args.prompt_file,
            args.force_json,
            args.bypass_suitability_check,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
