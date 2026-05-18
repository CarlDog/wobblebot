"""Recalibrate CLI — scale operator-tunable USD knobs by balance ratio (Stage 7.6.B).

Operator usage::

    python -m wobblebot.cli.recalibrate --target-balance 10        # dry-run
    python -m wobblebot.cli.recalibrate --target-balance 50 --commit
    python -m wobblebot.cli.recalibrate --target-balance 10 --current-balance 99.92
    python -m wobblebot.cli.recalibrate --target-balance 10 --config /path/to/custom.yml

**Default current balance comes from a live Kraken read** via the
read-only ``KRAKEN_API_KEY`` (same path ``cli/status`` uses). Operators
can override via ``--current-balance`` for what-if analysis without
hitting the API — useful when designing a scale-down before the
balance has actually moved.

Dry-run is the default. The CLI prints a per-knob delta table:

    balance:           $99.92 → $10.00 (scale 0.1001)
    -------------------------------------------------------
    grid.default.order_size_usd            $10.0 → $1.00
    safety.max_total_exposure_usd          $100.0 → $10.01
    harvester.surplus_threshold_usd        $500.0 → $50.04
    ...

``--commit`` runs the proposed deltas through
``services/settings_rewriter.apply_dotted_overrides`` which writes the
file atomically (temp file + rename) and round-trips ``ruamel.yaml`` to
preserve every comment and quoting style.

Per ADR-012's auto-tuning gate: this is operator-initiated, not
LLM-initiated, so the auto-apply bounds don't apply. The gate exists
to defend against LLM proposals slipping through, not against the
operator's own intent.

Exit codes:
    0 — dry-run success (proposal printed) OR commit success.
    1 — Kraken balance read failed.
    2 — config / credential / argparse error (operator-recoverable).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.cli._common import add_config_args, load_operator_env
from wobblebot.config.kraken import KrakenConfig
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import ExchangeError
from wobblebot.services.calibrator import RecalibrationProposal, recalibrate
from wobblebot.services.settings_rewriter import (
    SettingsRewriteError,
    apply_dotted_overrides,
)

_LOGGER = logging.getLogger("wobblebot.cli.recalibrate")


async def _read_kraken_usd_balance() -> Decimal | None:
    """Read live USD balance via the read-only key. Returns ``None`` on
    read failure (logged); caller decides whether that's fatal."""
    try:
        kraken_config = KrakenConfig.from_env()
    except ValueError as exc:
        _LOGGER.error(
            "missing Kraken read-only credentials",
            extra={"error": str(exc)},
        )
        return None
    adapter = KrakenAdapter(config=kraken_config)
    try:
        balance = await adapter.get_balance("USD")
    except ExchangeError as exc:
        _LOGGER.error(
            "Kraken balance read failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None
    finally:
        await adapter.aclose()
    if balance is None:
        # No USD in the account is reportable as zero, not as missing
        # data — `balance` is None only if the asset isn't present in
        # the BalanceEx response, which for USD on Kraken means $0.
        return Decimal("0")
    return balance.total


def _print_proposal(proposal: RecalibrationProposal) -> None:
    """Render the proposal as a human-readable delta table."""
    if not proposal.changes:
        _LOGGER.info(
            "no changes — proposed values match current values "
            "(possibly because current_balance == target_balance, "
            "or rounding collapsed every delta to zero)"
        )
        return
    header = (
        f"balance: ${proposal.current_balance} → ${proposal.target_balance} "
        f"(scale {proposal.scale_factor:.6f})"
    )
    _LOGGER.info(header)
    _LOGGER.info("-" * max(len(header), 78))
    longest_path = max(len(c.yaml_path) for c in proposal.changes)
    for change in proposal.changes:
        _LOGGER.info(
            "  %s  $%-10s → $%-10s",
            change.yaml_path.ljust(longest_path),
            str(change.current_value),
            str(change.proposed_value),
        )


def _proposal_to_overrides(
    proposal: RecalibrationProposal,
) -> dict[str, Decimal]:
    """Flatten the proposal's changes into the rewriter's dict shape."""
    return {c.yaml_path: c.proposed_value for c in proposal.changes}


# pylint: disable=too-many-return-statements
# Each return represents a distinct CLI exit path: bad creds, zero
# balance, scale-input refusal, dry-run success, no-op commit, rewriter
# refusal, rewriter ENOENT, no-on-disk-change, success. Collapsing
# them through a single variable + final return would hurt readability.
async def _run(
    *,
    config: WobbleBotConfig,
    target_balance: Decimal,
    current_balance_override: Decimal | None,
    commit: bool,
    config_path: Path,
) -> int:
    # Resolve current balance: explicit override OR live Kraken read.
    if current_balance_override is not None:
        current_balance = current_balance_override
        _LOGGER.info(
            "using operator-supplied current balance",
            extra={"current_balance_usd": str(current_balance)},
        )
    else:
        balance = await _read_kraken_usd_balance()
        if balance is None:
            return 1
        if balance <= Decimal("0"):
            _LOGGER.error(
                "current Kraken USD balance is zero or negative — cannot "
                "compute a meaningful scale factor. Pass --current-balance "
                "explicitly if you want to recalibrate from a hypothetical "
                "baseline.",
                extra={"balance_usd": str(balance)},
            )
            return 1
        current_balance = balance
        _LOGGER.info(
            "current Kraken USD balance",
            extra={"current_balance_usd": str(current_balance)},
        )

    try:
        proposal = recalibrate(
            current_balance=current_balance,
            target_balance=target_balance,
            current_config=config,
        )
    except ValueError as exc:
        _LOGGER.error("recalibrate refused", extra={"error": str(exc)})
        return 2

    _print_proposal(proposal)

    if not commit:
        _LOGGER.info("dry-run only — pass --commit to rewrite settings.yml")
        return 0

    if not proposal.changes:
        _LOGGER.info("nothing to commit (no changes proposed)")
        return 0

    try:
        diff = apply_dotted_overrides(
            config_path,
            overrides=_proposal_to_overrides(proposal),
        )
    except SettingsRewriteError as exc:
        _LOGGER.error("rewrite refused", extra={"error": str(exc)})
        return 2
    except FileNotFoundError as exc:
        _LOGGER.error("settings file not found", extra={"path": str(exc)})
        return 2

    if not diff:
        _LOGGER.info("no on-disk changes — file was already in target shape")
        return 0
    _LOGGER.info("settings.yml rewritten", extra={"path": str(config_path)})
    # Stream the diff to stdout so operator can pipe / inspect / paste
    # into a commit message. Logger goes to stderr.
    sys.stdout.write(diff)
    return 0


def _parse_balance(raw: str, label: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(
            f"invalid {label}: {raw!r}; expected a decimal number"
        ) from exc
    if value <= Decimal("0"):
        raise argparse.ArgumentTypeError(f"{label} must be positive; got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    parser.add_argument(
        "--target-balance",
        type=lambda s: _parse_balance(s, "target balance"),
        required=True,
        help="USD balance the new config should be tuned for. Required.",
    )
    parser.add_argument(
        "--current-balance",
        type=lambda s: _parse_balance(s, "current balance"),
        default=None,
        help=(
            "Override the live Kraken USD balance read. Useful for "
            "what-if analysis without hitting the API."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Rewrite settings.yml in-place with the proposed deltas. "
            "Without --commit the CLI is read-only (dry-run prints "
            "the diff but doesn't touch the file)."
        ),
    )
    parser.add_argument("--log-format", choices=("plain", "json"), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_operator_env()
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_resolved_config(
            config_path=args.config,
            profile_name=args.profile,
            cli_overrides={},
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    log_format: Any = args.log_format if args.log_format is not None else "plain"
    configure_logging(log_format=log_format)

    # Resolve the on-disk path the rewriter will mutate. When --config
    # is unset, runtime falls back to config/settings.yml (or the
    # example). For --commit we need to know the path explicitly.
    config_path = args.config if args.config is not None else Path("config") / "settings.yml"

    return asyncio.run(
        _run(
            config=config,
            target_balance=args.target_balance,
            current_balance_override=args.current_balance,
            commit=args.commit,
            config_path=config_path,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
