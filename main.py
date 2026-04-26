from __future__ import annotations

import argparse
import asyncio
import logging

logging.getLogger("pyrogram.crypto.aes").setLevel(logging.ERROR)

from pyrogram import idle

from bot_leech import BotLeechService
from boss_key import BossKeyController
from clone_worker import CloneWorker
from config import ConfigError, load_settings, resolve_runtime_flags
from db import StateStore
from history_scanner import HistoryScanner
from logging_utils import configure_logging
from router import MappingRouter
from telegram_client import TelegramService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clone Telegram topic messages into mapped destination topics using a user account session."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML mapping config. Default: config.yaml",
    )
    parser.add_argument(
        "--clone-existing",
        action="store_true",
        help="Scan and enqueue existing source-topic history before processing.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep watching configured source topics for new messages after backlog processing.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process the current queue and exit without watch mode.",
    )
    parser.add_argument(
        "--reset-mapping-state",
        action="store_true",
        help="Delete saved SQLite clone state for the mappings in the current config before running.",
    )
    return parser


async def validate_startup(
    telegram: TelegramService,
    router: MappingRouter,
    logger: logging.Logger,
) -> None:
    me = await telegram.read_call("get_me", telegram.app.get_me)
    logger.info(
        "session validated",
        extra={
            "event": "session_validated",
            "user_id": me.id,
            "username": me.username,
        },
    )

    bot_user = await telegram.get_bot_user()
    if bot_user.id != telegram.settings.leech_bot_id:
        raise RuntimeError(
            f"LEECH_BOT_ID mismatch: expected {telegram.settings.leech_bot_id}, got {bot_user.id}"
        )
    await telegram.get_chat(telegram.settings.leech_bot_username)
    await telegram.get_bot_history(limit=1)

    logger.info(
        "bot validated",
        extra={
            "event": "bot_validated",
            "bot_id": bot_user.id,
            "bot_username": bot_user.username,
        },
    )

    for mapping in router.enabled_mappings:
        await telegram.validate_mapping(mapping)
        logger.info(
            "mapping validated",
            extra={
                "event": "mapping_validated",
                "mapping_key": mapping.key,
            },
        )


def build_watch_callback(router: MappingRouter, store: StateStore, logger: logging.Logger):
    async def on_source_message(_, message):
        for mapping in router.match_message(message):
            inserted = await store.enqueue_job(mapping, message.id)
            if inserted:
                logger.info(
                    "queued watched message",
                    extra={
                        "event": "watch_queued_message",
                        "mapping_key": mapping.key,
                        "source_message_id": message.id,
                    },
                )

    return on_source_message


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        settings, mappings = load_settings(args.config)
        flags = resolve_runtime_flags(args, settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    configure_logging(settings.log_level, settings.log_file_path)
    logger = logging.getLogger("telegram_cloner")
    logger.info(
        "startup validation",
        extra={
            "event": "startup",
            "config_path": str(settings.config_path),
            "database_path": str(settings.database_path),
            "clone_existing": flags.clone_existing,
            "watch": flags.watch,
            "dry_run": settings.dry_run,
            "mapping_count": len([mapping for mapping in mappings if mapping.enabled]),
        },
    )

    router = MappingRouter(mappings)
    store = StateStore(settings.database_path, logging.getLogger("telegram_cloner.db"))
    telegram = TelegramService(
        settings,
        logging.getLogger("telegram_cloner.telegram"),
        receive_updates=flags.watch,
    )
    leech = BotLeechService(
        settings,
        telegram,
        store,
        logging.getLogger("telegram_cloner.bot_leech"),
    )
    scanner = HistoryScanner(
        settings,
        telegram,
        store,
        logging.getLogger("telegram_cloner.history"),
    )
    worker = CloneWorker(
        settings,
        telegram,
        store,
        router,
        leech,
        logging.getLogger("telegram_cloner.worker"),
    )

    watch_handler = None
    worker_task: asyncio.Task | None = None
    idle_task: asyncio.Task | None = None
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        logger.warning("shutdown requested", extra={"event": "shutdown_requested"})
        worker.stop()
        shutdown_event.set()

    boss_key = BossKeyController(
        settings,
        logging.getLogger("telegram_cloner.boss_key"),
        on_trigger=lambda: loop.call_soon_threadsafe(request_shutdown),
    )

    try:
        await store.connect()
        if args.reset_mapping_state:
            reset_count = await store.reset_mappings(router.enabled_mappings)
            logger.warning(
                "mapping state reset",
                extra={
                    "event": "mapping_state_reset",
                    "mapping_count": len(router.enabled_mappings),
                    "reset_rows": reset_count,
                },
            )

        recovered_count = await store.recover_processing_jobs()
        logger.info(
            "recovered processing jobs",
            extra={
                "event": "recovered_processing_jobs",
                "count": recovered_count,
            },
        )

        await telegram.start()
        await validate_startup(telegram, router, logger)
        boss_key.start()

        recovered_missed_forwards = await worker.recover_missed_forwards_from_bot_history()
        logger.info(
            "pre-processing missed-forward recovery complete",
            extra={
                "event": "preprocessing_missed_forward_recovery_complete",
                "recovered_count": recovered_missed_forwards,
            },
        )

        if flags.watch:
            watch_handler = telegram.add_watch_handler(
                router.source_chat_ids,
                build_watch_callback(router, store, logging.getLogger("telegram_cloner.watch")),
            )
            logger.info(
                "watch mode enabled",
                extra={
                    "event": "watch_mode_enabled",
                    "source_chat_count": len(router.source_chat_ids),
                },
            )

        if flags.clone_existing:
            await scanner.scan_all(router.enabled_mappings)

        worker_task = asyncio.create_task(worker.run(watch_mode=flags.watch))

        if flags.watch:
            idle_task = asyncio.create_task(idle())
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            done, pending = await asyncio.wait(
                {worker_task, idle_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if shutdown_task in done or idle_task in done:
                worker.stop()

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info("worker stopped during shutdown", extra={"event": "worker_stopped_during_shutdown"})
        counts = await store.get_counts()
        logger.info("shutdown", extra={"event": "shutdown", "counts": counts})
        return 0
    finally:
        boss_key.stop()

        if worker_task is not None and not worker_task.done():
            worker.stop()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass

        if watch_handler is not None:
            telegram.remove_watch_handler(watch_handler)

        await telegram.stop()
        await store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        print("Stopped.")
        raise SystemExit(130)
