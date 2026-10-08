"""Independent outbound notification daemon. No commands, LLM or financial keys.

Activation is opt-in via the delivery Compose profile. Missing config/token exits
2; storage failure exits 1. No external send occurs until explicitly started.
"""

import argparse
import asyncio
import logging
import os
from datetime import timedelta
from pathlib import Path

import httpx

from wobblebot.adapters.discord_delivery import DiscordDelivery
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.cli._common import (
    CONFIG_LOAD_ERRORS,
    add_config_args,
    config_load_exit,
    emit_heartbeat,
    install_signal_handlers,
    load_operator_env,
    missing_section_exit,
)
from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.runtime import load_resolved_config
from wobblebot.ports.exceptions import StorageError
from wobblebot.services.delivery import forward_notifications
from wobblebot.services.health_response import HealthObserver

_LOGGER = logging.getLogger(__name__)


async def run(config: WobbleBotConfig, token: str) -> int:
    """Bounded sends in a separate process from the conversational operator."""
    assert config.delivery is not None and config.operator is not None
    channel = config.operator.auth.outbound_channel_id
    storage = SQLiteStorageAdapter(config.delivery.operator_db)
    stop = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop, logger=_LOGGER)
    cadence = config.schedules.get_or_default("delivery_poll", timedelta(seconds=2)).total_seconds()
    if cadence <= 0:
        _LOGGER.error("delivery_poll must be positive to run the delivery daemon")
        return 2
    try:
        await storage.connect()
        async with httpx.AsyncClient() as client:
            transport = DiscordDelivery(client, token)

            async def send_loop() -> None:
                while not stop.is_set():
                    await emit_heartbeat(storage, "cli/delivery")
                    await forward_notifications(storage, transport, channel)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=cadence)
                    except TimeoutError:
                        continue

            async def health_loop() -> None:
                interval = config.schedules.get_or_default(
                    "health_response", timedelta(seconds=30)
                ).total_seconds()
                if interval <= 0:
                    await stop.wait()
                    return
                observer = HealthObserver(config, storage)
                while not stop.is_set():
                    async with asyncio.timeout(30):
                        await observer.poll()
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                    except TimeoutError:
                        continue

            async with asyncio.TaskGroup() as group:
                group.create_task(send_loop(), name="delivery-sender")
                group.create_task(health_loop(), name="independent-health-observer")
    except ExceptionGroup:
        _LOGGER.error("Independent delivery/health task failed; exiting for external supervision")
        return 1
    except StorageError:
        _LOGGER.error("Delivery storage unavailable")
        return 1
    finally:
        await storage.close()
    return 0


def main() -> int:  # pylint: disable=too-many-return-statements
    # Each operator-fixable precondition has a distinct clean exit.
    """Validate configuration before constructing any outbound client."""
    configure_logging()
    load_operator_env()
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_args(parser)
    args = parser.parse_args()
    try:
        config = load_resolved_config(args.config, args.profile)
    except CONFIG_LOAD_ERRORS as exc:
        return config_load_exit(exc)
    if config.delivery is None:
        return missing_section_exit(_LOGGER, "delivery")
    if config.operator is None:
        return missing_section_exit(_LOGGER, "operator")
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token or not config.operator.auth.outbound_channel_id.isdecimal():
        _LOGGER.error("Delivery requires Discord credentials and a numeric outbound channel")
        return 2
    if config.operator.auth.outbound_channel_id not in config.operator.auth.allowed_channel_ids:
        _LOGGER.error("Delivery channel must be in operator's authorized channel list")
        return 2
    if Path(config.delivery.operator_db).resolve() != Path(config.operator.operator_db).resolve():
        _LOGGER.error("Delivery must consume the configured operator database")
        return 2
    if config.delivery.operator_db == ":memory:":
        _LOGGER.error("Delivery requires durable operator storage")
        return 2
    configure_logging(rotating_file_path=config.delivery.log_file_path)
    try:
        return asyncio.run(run(config, token))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
