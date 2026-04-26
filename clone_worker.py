from __future__ import annotations

import asyncio
import logging

from pyrogram.errors import ChatForwardsRestricted
from pyrogram.types import Message

from bot_leech import BotLeechError, BotLeechService
from config import AppSettings
from db import StateStore
from message_classifier import (
    classify_message,
    extract_caption_payload,
    extract_reusable_file_id,
    extract_text_payload,
)
from models import CloneJob, MessageKind
from router import MappingRouter
from telegram_client import TelegramService
from topic_utils import belongs_to_topic


class CloneWorker:
    MAX_INFLIGHT_BOT_JOBS = 2

    def __init__(
        self,
        settings: AppSettings,
        telegram: TelegramService,
        store: StateStore,
        router: MappingRouter,
        bot_leech: BotLeechService,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.telegram = telegram
        self.store = store
        self.router = router
        self.bot_leech = bot_leech
        self.logger = logger
        self._stop_event = asyncio.Event()
        self._prefetch_lock = asyncio.Lock()
        self._prefetched_job_ids: set[int] = set()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self, watch_mode: bool) -> None:
        try:
            while not self._stop_event.is_set():
                job = await self.store.get_next_job(self.settings.retry_limit)
                if job is None:
                    if not watch_mode:
                        return
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=self.settings.poll_idle_sec)
                    except TimeoutError:
                        continue
                    return

                await self._process_job(job)
        except asyncio.CancelledError:
            self.logger.info("worker cancelled", extra={"event": "worker_cancelled"})
            return

    async def recover_missed_forwards_from_bot_history(self) -> int:
        recoverable_jobs = await self.store.list_recoverable_bot_jobs(self.settings.retry_limit)
        if not recoverable_jobs:
            return 0

        recovered_count = 0
        for job in recoverable_jobs:
            if self._stop_event.is_set():
                break

            try:
                await self._process_job(job)
                recovered_count += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

        self.logger.info(
            "missed-forward recovery pass finished",
            extra={
                "event": "missed_forward_recovery_finished",
                "candidate_count": len(recoverable_jobs),
                "recovered_count": recovered_count,
            },
        )
        return recovered_count

    async def _process_job(self, job: CloneJob) -> None:
        mapping = self.router.mapping_for_job(
            job.source_chat_id,
            job.source_topic_id,
            job.destination_chat_id,
            job.destination_topic_id,
        )
        if mapping is None:
            await self.store.mark_done(job.id, None, "skipped: mapping no longer exists")
            return

        await self.store.mark_processing(job.id)

        try:
            source_message = await self.telegram.get_message(job.source_chat_id, job.source_message_id)
            if source_message is None or getattr(source_message, "empty", False):
                await self.store.mark_done(job.id, None, "skipped: source message missing")
                return

            if not belongs_to_topic(source_message, mapping.source_topic_id):
                await self.store.mark_done(job.id, None, "skipped: source message not in configured topic")
                return

            classification = classify_message(source_message)
            if classification.kind == MessageKind.UNSUPPORTED:
                await self.store.mark_done(job.id, None, f"skipped: {classification.reason}")
                return

            if self.settings.dry_run:
                await self.store.mark_done(job.id, None, f"dry-run: {classification.reason}")
                self.logger.info(
                    "dry run clone",
                    extra={
                        "event": "dry_run_clone",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                        "kind": classification.kind.value,
                    },
                )
                return

            destination_message = await self._clone_message(
                mapping.destination_chat_id,
                mapping.destination_topic_id,
                source_message,
                classification.kind,
                job,
            )
            await self.store.mark_done(job.id, destination_message.id)

            self.logger.info(
                "destination send success",
                extra={
                    "event": "destination_send_success",
                    "mapping_key": mapping.key,
                    "source_message_id": source_message.id,
                    "destination_message_id": destination_message.id,
                    "kind": classification.kind.value,
                },
            )

            if self.settings.enable_bot_prefetch:
                await self._prefetch_next_bot_job()
        except asyncio.CancelledError:
            self.logger.info(
                "current clone cancelled",
                extra={
                    "event": "clone_cancelled",
                    "mapping_key": mapping.key,
                    "source_message_id": job.source_message_id,
                },
            )
            raise
        except Exception as exc:
            await self.store.mark_failed(job.id, str(exc))
            self.logger.exception(
                "clone failed",
                extra={
                    "event": "clone_failed",
                    "mapping_key": mapping.key,
                    "source_message_id": job.source_message_id,
                    "retry_count": job.retry_count + 1,
                    "error": str(exc),
                },
            )

    async def _clone_message(
        self,
        destination_chat_id: int,
        destination_topic_id: int,
        source_message: Message,
        kind: MessageKind,
        job: CloneJob,
    ) -> Message:
        mapping = self.router.mapping_for_job(
            job.source_chat_id,
            job.source_topic_id,
            job.destination_chat_id,
            job.destination_topic_id,
        )
        if mapping is None:
            raise RuntimeError("Mapping disappeared while cloning")

        if kind == MessageKind.TEXT:
            text, entities, disable_preview = extract_text_payload(source_message)
            return await self.telegram.send_text_to_topic(
                destination_chat_id,
                destination_topic_id,
                text=text,
                entities=entities,
                disable_web_page_preview=disable_preview,
            )

        if kind == MessageKind.DIRECT_MEDIA:
            file_id = extract_reusable_file_id(source_message)
            if not file_id:
                raise RuntimeError("No reusable file_id found for supported media message")
            caption, caption_entities = extract_caption_payload(source_message)
            try:
                return await self.telegram.send_cached_media_to_topic(
                    destination_chat_id,
                    destination_topic_id,
                    file_id=file_id,
                    caption=caption,
                    caption_entities=caption_entities,
                )
            except ChatForwardsRestricted:
                self.logger.warning(
                    "direct media clone blocked, falling back to leech bot",
                    extra={
                        "event": "direct_media_leech_fallback",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                    },
                )
                return await self._clone_via_bot(
                    destination_chat_id=destination_chat_id,
                    destination_topic_id=destination_topic_id,
                    source_message=source_message,
                    job=job,
                    mapping=mapping,
                )

        if kind == MessageKind.VIDEO_LEECH:
            return await self._clone_via_bot(
                destination_chat_id=destination_chat_id,
                destination_topic_id=destination_topic_id,
                source_message=source_message,
                job=job,
                mapping=mapping,
            )

        raise RuntimeError(f"Unhandled message kind: {kind.value}")

    async def _clone_via_bot(
        self,
        destination_chat_id: int,
        destination_topic_id: int,
        source_message: Message,
        job: CloneJob,
        mapping,
    ) -> Message:
        bot_media_messages = await self.bot_leech.request_leech_media(
            job=job,
            mapping=mapping,
            source_message=source_message,
            on_upload_started=lambda: self._prefetch_next_bot_job(),
        )
        if not bot_media_messages:
            raise BotLeechError("Leech bot responded without uploaded media")

        destination_result: Message | None = None
        for index, bot_media_message in enumerate(bot_media_messages):
            file_id = extract_reusable_file_id(bot_media_message)
            if not file_id:
                raise BotLeechError("Leech bot responded without reusable uploaded media")

            caption, caption_entities = (None, None)
            if index == 0:
                caption, caption_entities = extract_caption_payload(source_message)

            destination_result = await self.telegram.send_cached_media_to_topic(
                destination_chat_id,
                destination_topic_id,
                file_id=file_id,
                caption=caption,
                caption_entities=caption_entities,
            )

        if destination_result is None:
            raise BotLeechError("Leech bot upload forwarding produced no destination message")
        return destination_result

    async def _prefetch_next_bot_job(self) -> None:
        try:
            if not self.settings.enable_bot_prefetch or self._stop_event.is_set():
                return

            async with self._prefetch_lock:
                inflight_bot_jobs = await self.store.count_inflight_bot_jobs()
                if inflight_bot_jobs >= self.MAX_INFLIGHT_BOT_JOBS:
                    return

                next_job = await self.store.get_next_unprefetched_job(self.settings.retry_limit)
                if next_job is None:
                    return

                if next_job.id in self._prefetched_job_ids or next_job.bot_command_message_id:
                    return

                mapping = self.router.mapping_for_job(
                    next_job.source_chat_id,
                    next_job.source_topic_id,
                    next_job.destination_chat_id,
                    next_job.destination_topic_id,
                )
                if mapping is None:
                    return

                source_message = await self.telegram.get_message(
                    next_job.source_chat_id,
                    next_job.source_message_id,
                )
                if source_message is None or getattr(source_message, "empty", False):
                    return

                if not belongs_to_topic(source_message, mapping.source_topic_id):
                    return

                classification = classify_message(source_message)
                if classification.kind != MessageKind.VIDEO_LEECH:
                    return

                await self.bot_leech.prefetch_leech_command(
                    job=next_job,
                    mapping=mapping,
                    source_message=source_message,
                    on_upload_started=lambda: self._prefetch_next_bot_job(),
                )
                self._prefetched_job_ids.add(next_job.id)
                self.logger.info(
                    "next bot job prefetched",
                    extra={
                        "event": "bot_job_prefetched",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "bot prefetch skipped after error",
                extra={
                    "event": "bot_prefetch_skipped",
                    "error": str(exc),
                },
            )
