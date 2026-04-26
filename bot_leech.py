from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

from pyrogram.types import Message

from config import AppSettings, MappingConfig
from db import StateStore
from models import CloneJob
from telegram_client import TelegramService
from topic_utils import build_private_topic_link


class BotLeechError(RuntimeError):
    pass


BotStatusState = Literal["active", "inactive", "unknown"]


class BotLeechService:
    MAX_RESUMABLE_COMMAND_AGE_SEC = 120

    def __init__(
        self,
        settings: AppSettings,
        telegram: TelegramService,
        store: StateStore,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.telegram = telegram
        self.store = store
        self.logger = logger
        self._prefetch_watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._prefetched_media_message_ids: dict[int, int] = {}
        self._restart_lock = asyncio.Lock()
        self._last_restart_at: float = 0.0

    async def prefetch_leech_command(
        self,
        job: CloneJob,
        mapping: MappingConfig,
        source_message: Message,
        on_upload_started: Callable[[], Awaitable[None]] | None = None,
    ) -> int | None:
        if job.bot_command_message_id:
            command_message = await self.telegram.get_message(
                self.settings.leech_bot_username,
                job.bot_command_message_id,
            )
            if (
                command_message is not None
                and not getattr(command_message, "empty", False)
                and self._is_recent_command(command_message)
            ):
                self._start_prefetch_watch(
                    job=job,
                    mapping=mapping,
                    source_message=source_message,
                    command_message=command_message,
                    on_upload_started=on_upload_started,
                )
                return job.bot_command_message_id

        source_link = build_private_topic_link(
            mapping.source_chat_id,
            mapping.source_topic_id,
            source_message.id,
        )
        command_message = await self.telegram.send_message_to_bot(f"/leech {source_link}")
        await self.store.set_bot_command_message_id(job.id, command_message.id)
        self._start_prefetch_watch(
            job=job,
            mapping=mapping,
            source_message=source_message,
            command_message=command_message,
            on_upload_started=on_upload_started,
        )
        self.logger.info(
            "prefetched bot command sent",
            extra={
                "event": "bot_command_prefetched",
                "mapping_key": mapping.key,
                "source_message_id": source_message.id,
                "bot_command_message_id": command_message.id,
                "source_link": source_link,
            },
        )
        return command_message.id

    async def request_leech_media(
        self,
        job: CloneJob,
        mapping: MappingConfig,
        source_message: Message,
        on_upload_started: Callable[[], Awaitable[None]] | None = None,
    ) -> list[Message]:
        source_link = build_private_topic_link(
            mapping.source_chat_id,
            mapping.source_topic_id,
            source_message.id,
        )

        cached_prefetched_media_id = self._prefetched_media_message_ids.get(job.id)
        if cached_prefetched_media_id is None:
            cached_prefetched_media_id = await self.store.get_bot_media_message_id(job.id)
        if cached_prefetched_media_id:
            prefetched_message = await self.telegram.get_message(
                self.settings.leech_bot_username,
                cached_prefetched_media_id,
            )
            if prefetched_message is not None and not getattr(prefetched_message, "empty", False):
                related_batch = await self._collect_related_media_batch(
                    after_message_id=job.bot_command_message_id or 0,
                    sent_at=None,
                    expected_source_message=source_message,
                    seed_media_message=prefetched_message,
                )
                self.logger.info(
                    "using prefetched bot media",
                    extra={
                        "event": "bot_media_prefetched_reused",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                        "bot_media_message_id": prefetched_message.id,
                        "media_count": len(related_batch),
                    },
                )
                return related_batch

        prefetch_watch_task = self._prefetch_watch_tasks.get(job.id)
        if prefetch_watch_task is not None and not prefetch_watch_task.done():
            try:
                await prefetch_watch_task
            except Exception:
                pass
            cached_prefetched_media_id = self._prefetched_media_message_ids.get(job.id)
            if cached_prefetched_media_id is None:
                cached_prefetched_media_id = await self.store.get_bot_media_message_id(job.id)
            if cached_prefetched_media_id:
                prefetched_message = await self.telegram.get_message(
                    self.settings.leech_bot_username,
                    cached_prefetched_media_id,
                )
                if prefetched_message is not None and not getattr(prefetched_message, "empty", False):
                    related_batch = await self._collect_related_media_batch(
                        after_message_id=job.bot_command_message_id or 0,
                        sent_at=None,
                        expected_source_message=source_message,
                        seed_media_message=prefetched_message,
                    )
                    self.logger.info(
                        "using prefetched bot media after wait",
                        extra={
                            "event": "bot_media_prefetched_reused_after_wait",
                            "mapping_key": mapping.key,
                            "source_message_id": source_message.id,
                            "bot_media_message_id": prefetched_message.id,
                            "media_count": len(related_batch),
                        },
                    )
                    return related_batch

        command = f"/leech {source_link}"
        max_attempts = max(1, self.settings.bot_releech_retry_limit + 1)
        last_error: str | None = None
        reused_existing_command = False
        active_command_message_id = job.bot_command_message_id

        for attempt in range(1, max_attempts + 1):
            command_message: Message | None = None
            if not reused_existing_command and active_command_message_id:
                command_message = await self.telegram.get_message(
                    self.settings.leech_bot_username,
                    active_command_message_id,
                )
                if command_message is not None and not getattr(command_message, "empty", False):
                    reused_existing_command = True
                    existing_media = await self._find_existing_uploaded_media_after_command(
                        after_message_id=command_message.id,
                        sent_at=command_message.date,
                        expected_source_message=source_message,
                    )
                    if existing_media is not None:
                        await self.store.set_bot_media_message_id(job.id, existing_media.id)
                        related_batch = await self._collect_related_media_batch(
                            after_message_id=command_message.id,
                            sent_at=command_message.date,
                            expected_source_message=source_message,
                            seed_media_message=existing_media,
                        )
                        self.logger.info(
                            "recovered uploaded media from bot history",
                            extra={
                                "event": "bot_history_media_recovered",
                                "mapping_key": mapping.key,
                                "source_message_id": source_message.id,
                                "bot_command_message_id": command_message.id,
                                "bot_media_message_id": existing_media.id,
                                "media_count": len(related_batch),
                            },
                        )
                        return related_batch

                    if not self._is_recent_command(command_message):
                        self.logger.info(
                            "stored bot command is stale, issuing fresh leech command",
                            extra={
                                "event": "bot_command_stale_refresh",
                                "mapping_key": mapping.key,
                                "source_message_id": source_message.id,
                                "bot_command_message_id": command_message.id,
                                "max_age_sec": self.MAX_RESUMABLE_COMMAND_AGE_SEC,
                            },
                        )
                        reused_existing_command = False
                        active_command_message_id = None
                        command_message = None
                        continue

                    self.logger.info(
                        "resuming prefetched bot command",
                        extra={
                            "event": "bot_command_resumed",
                            "mapping_key": mapping.key,
                            "source_message_id": source_message.id,
                            "bot_command_message_id": command_message.id,
                            "source_link": source_link,
                        },
                    )

            if command_message is None:
                command_message = await self.telegram.send_message_to_bot(command)
                await self.store.set_bot_command_message_id(job.id, command_message.id)
                active_command_message_id = command_message.id

                self.logger.info(
                    "bot command sent",
                    extra={
                        "event": "bot_command_sent",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                        "bot_command_message_id": command_message.id,
                        "source_link": source_link,
                        "retry_count": attempt - 1,
                    },
                )

            try:
                media_message = await self._wait_for_uploaded_media(
                    after_message_id=command_message.id,
                    sent_at=command_message.date,
                    expected_source_message=source_message,
                    on_upload_started=on_upload_started,
                )
                await self.store.set_bot_media_message_id(job.id, media_message.id)
                related_batch = await self._collect_related_media_batch(
                    after_message_id=command_message.id,
                    sent_at=command_message.date,
                    expected_source_message=source_message,
                    seed_media_message=media_message,
                )

                self.logger.info(
                    "bot media detected",
                    extra={
                        "event": "bot_media_detected",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                        "bot_media_message_id": media_message.id,
                        "media_count": len(related_batch),
                        "retry_count": attempt - 1,
                    },
                )
                return related_batch
            except BotLeechError as exc:
                last_error = str(exc)
                reused_existing_command = False
                if attempt >= max_attempts or not self._is_retryable_failure(last_error):
                    raise

                self.logger.warning(
                    "bot leech attempt failed, resending command",
                    extra={
                        "event": "bot_releech_retry",
                        "mapping_key": mapping.key,
                        "source_message_id": source_message.id,
                        "retry_count": attempt,
                        "error": last_error,
                    },
                )
                await asyncio.sleep(max(self.settings.action_delay_sec, self.settings.bot_releech_retry_delay_sec))
                # Force issuing a fresh /leech on next attempt after a stalled or restarted bot task.
                active_command_message_id = None

        raise BotLeechError(last_error or "Bot leech failed without an explicit error")

    async def _find_existing_uploaded_media_after_command(
        self,
        after_message_id: int,
        sent_at: datetime | None,
        expected_source_message: Message,
        *,
        page_size: int = 100,
        max_messages_to_scan: int = 2000,
    ) -> Message | None:
        scanned = 0
        offset_id = 0

        while scanned < max_messages_to_scan:
            batch = await self.telegram.get_bot_history(limit=page_size, offset_id=offset_id)
            if not batch:
                return None

            for message in batch:
                if message.id <= after_message_id:
                    return None

                from_user = getattr(message, "from_user", None)
                if from_user is None or from_user.id != self.settings.leech_bot_id:
                    continue

                if sent_at is not None and message.date is not None and message.date < sent_at:
                    continue

                if not self._is_uploaded_media(message):
                    continue

                if not self._matches_expected_media(message, expected_source_message):
                    continue

                return message

            scanned += len(batch)
            offset_id = batch[-1].id

        return None

    async def _collect_related_media_batch(
        self,
        after_message_id: int,
        sent_at: datetime | None,
        expected_source_message: Message,
        seed_media_message: Message,
    ) -> list[Message]:
        if not self._looks_like_split_part(seed_media_message, expected_source_message):
            return [seed_media_message]

        collected: dict[int, Message] = {seed_media_message.id: seed_media_message}
        loop = asyncio.get_running_loop()
        settle_window_sec = max(6.0, self.settings.action_delay_sec * 3)
        stable_since = loop.time()

        while True:
            history = await self.telegram.get_bot_history(limit=50)
            found_new = False

            for message in history:
                if message.id <= after_message_id:
                    continue

                from_user = getattr(message, "from_user", None)
                if from_user is None or from_user.id != self.settings.leech_bot_id:
                    continue

                if sent_at is not None and message.date is not None and message.date < sent_at:
                    continue

                if not self._is_uploaded_media(message):
                    continue

                if not self._matches_expected_media(message, expected_source_message):
                    continue

                if message.id not in collected:
                    collected[message.id] = message
                    found_new = True

            if found_new:
                stable_since = loop.time()
            elif loop.time() - stable_since >= settle_window_sec:
                break

            await asyncio.sleep(max(1.5, self.settings.action_delay_sec))

        ordered = [collected[item_id] for item_id in sorted(collected)]
        return ordered

    def _start_prefetch_watch(
        self,
        job: CloneJob,
        mapping: MappingConfig,
        source_message: Message,
        command_message: Message,
        on_upload_started: Callable[[], Awaitable[None]] | None,
    ) -> None:
        task = self._prefetch_watch_tasks.get(job.id)
        if task is not None and not task.done():
            return

        self._prefetch_watch_tasks[job.id] = asyncio.create_task(
            self._watch_prefetched_media(
                job=job,
                mapping=mapping,
                source_message=source_message,
                command_message=command_message,
                on_upload_started=on_upload_started,
            )
        )

    async def _watch_prefetched_media(
        self,
        job: CloneJob,
        mapping: MappingConfig,
        source_message: Message,
        command_message: Message,
        on_upload_started: Callable[[], Awaitable[None]] | None,
    ) -> None:
        source_link = build_private_topic_link(
            mapping.source_chat_id,
            mapping.source_topic_id,
            source_message.id,
        )
        command = f"/leech {source_link}"
        max_attempts = max(1, self.settings.bot_releech_retry_limit + 1)
        active_command_message = command_message

        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    media_message = await self._wait_for_uploaded_media(
                        after_message_id=active_command_message.id,
                        sent_at=active_command_message.date,
                        expected_source_message=source_message,
                        on_upload_started=on_upload_started,
                    )
                    self._prefetched_media_message_ids[job.id] = media_message.id
                    await self.store.set_bot_media_message_id(job.id, media_message.id)
                    self.logger.info(
                        "prefetched bot media ready",
                        extra={
                            "event": "prefetched_bot_media_ready",
                            "mapping_key": mapping.key,
                            "source_message_id": source_message.id,
                            "bot_command_message_id": active_command_message.id,
                            "bot_media_message_id": media_message.id,
                            "retry_count": attempt - 1,
                        },
                    )
                    return
                except BotLeechError as exc:
                    error_text = str(exc)
                    if attempt >= max_attempts or not self._is_retryable_failure(error_text):
                        raise

                    self.logger.warning(
                        "prefetch stalled after bot restart, resending command",
                        extra={
                            "event": "prefetch_releech_retry",
                            "mapping_key": mapping.key,
                            "source_message_id": source_message.id,
                            "retry_count": attempt,
                            "error": error_text,
                        },
                    )
                    await asyncio.sleep(max(self.settings.action_delay_sec, self.settings.bot_releech_retry_delay_sec))
                    active_command_message = await self.telegram.send_message_to_bot(command)
                    await self.store.set_bot_command_message_id(job.id, active_command_message.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "prefetched bot media watch failed",
                extra={
                    "event": "prefetched_bot_media_watch_failed",
                    "mapping_key": mapping.key,
                    "source_message_id": source_message.id,
                    "bot_command_message_id": command_message.id,
                    "error": str(exc),
                },
            )
        finally:
            current = self._prefetch_watch_tasks.get(job.id)
            if current is asyncio.current_task():
                self._prefetch_watch_tasks.pop(job.id, None)

    async def _wait_for_uploaded_media(
        self,
        after_message_id: int,
        sent_at: datetime | None,
        expected_source_message: Message,
        on_upload_started: Callable[[], Awaitable[None]] | None = None,
    ) -> Message:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.bot_response_timeout_sec
        last_activity_at = loop.time()
        seen_snapshots: dict[int, str] = {}
        upload_callback_fired = False
        active_stall_probe_count = 0

        try:
            while loop.time() < deadline:
                history = await self.telegram.get_bot_history(limit=30)
                candidates: list[Message] = []
                detected_error: str | None = None
                saw_activity = False

                for message in history:
                    if message.id <= after_message_id:
                        continue

                    from_user = getattr(message, "from_user", None)
                    if from_user is None or from_user.id != self.settings.leech_bot_id:
                        continue

                    if sent_at is not None and message.date is not None and message.date < sent_at:
                        continue

                    if not self._is_related_to_command(message, after_message_id):
                        continue

                    snapshot = self._message_snapshot(message)
                    previous_snapshot = seen_snapshots.get(message.id)
                    if previous_snapshot != snapshot:
                        seen_snapshots[message.id] = snapshot
                        saw_activity = True

                    if (
                        not upload_callback_fired
                        and on_upload_started is not None
                        and self._message_indicates_upload_started(message)
                    ):
                        upload_callback_fired = True
                        await on_upload_started()

                    if self._is_uploaded_media(message):
                        if self._matches_expected_media(message, expected_source_message):
                            candidates.append(message)
                        continue

                    error_message = self._extract_bot_error(message)
                    if error_message and detected_error is None:
                        detected_error = error_message

                if candidates:
                    candidates.sort(key=lambda item: item.id)
                    return candidates[0]

                if detected_error:
                    raise BotLeechError(detected_error)

                if saw_activity:
                    last_activity_at = loop.time()
                    active_stall_probe_count = 0

                stall_seconds = loop.time() - last_activity_at
                if stall_seconds >= self.settings.bot_stall_timeout_sec:
                    status_state = await self._probe_bot_status()
                    if status_state == "active":
                        active_stall_probe_count += 1
                        if active_stall_probe_count >= 2:
                            await self._restart_bot_and_confirm(
                                reason=(
                                    "upload appears stuck despite active status "
                                    f"for ~{int(stall_seconds)}s"
                                )
                            )
                            raise BotLeechError("Bot restart requested after prolonged active upload stall")
                        last_activity_at = loop.time()
                        continue

                    await self._restart_bot_and_confirm(
                        reason=f"upload stalled for ~{int(stall_seconds)}s with status={status_state}"
                    )
                    if status_state == "inactive":
                        raise BotLeechError("Bot reported no active tasks after the upload stalled")

                    raise BotLeechError(
                        f"Bot task stalled for {int(stall_seconds)} seconds without new progress"
                    )

                await asyncio.sleep(max(2.0, self.settings.action_delay_sec))
        except asyncio.CancelledError:
            raise

        raise BotLeechError(
            f"Timed out waiting for uploaded media from {self.settings.leech_bot_username}"
        )

    async def _restart_bot_and_confirm(self, reason: str) -> None:
        async with self._restart_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            cooldown_sec = max(20.0, self.settings.action_delay_sec * 8)
            if now - self._last_restart_at < cooldown_sec:
                self.logger.info(
                    "bot restart skipped due to cooldown",
                    extra={
                        "event": "bot_restart_cooldown",
                        "reason": reason,
                    },
                )
                return

            command_message = await self.telegram.send_message_to_bot("/restart")
            self.logger.warning(
                "bot restart command sent",
                extra={
                    "event": "bot_restart_sent",
                    "bot_command_message_id": command_message.id,
                    "reason": reason,
                },
            )

            deadline = loop.time() + max(20.0, self.settings.bot_status_response_timeout_sec)
            clicked = False

            while loop.time() < deadline:
                history = await self.telegram.get_bot_history(limit=20)
                for message in history:
                    if message.id <= command_message.id:
                        continue

                    from_user = getattr(message, "from_user", None)
                    if from_user is None or from_user.id != self.settings.leech_bot_id:
                        continue

                    clicked = await self.telegram.click_message_button(
                        message,
                        preferred_labels=["yes", "✅ yes", "confirm", "restart"],
                    )
                    if clicked:
                        self._last_restart_at = loop.time()
                        self.logger.warning(
                            "bot restart confirmed",
                            extra={
                                "event": "bot_restart_confirmed",
                                "bot_command_message_id": command_message.id,
                                "reason": reason,
                            },
                        )
                        await asyncio.sleep(max(self.settings.action_delay_sec, 2.0))
                        return

                await asyncio.sleep(max(1.5, self.settings.action_delay_sec))

            self.logger.warning(
                "bot restart confirmation not found",
                extra={
                    "event": "bot_restart_confirmation_missing",
                    "bot_command_message_id": command_message.id,
                    "reason": reason,
                },
            )

    async def _probe_bot_status(self) -> BotStatusState:
        command = self.settings.bot_status_command.strip()
        if not command:
            return "unknown"

        command_message = await self.telegram.send_message_to_bot(command)
        self.logger.info(
            "bot status probe sent",
            extra={
                "event": "bot_status_probe_sent",
                "bot_command_message_id": command_message.id,
                "status_command": command,
            },
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.bot_status_response_timeout_sec

        while loop.time() < deadline:
            history = await self.telegram.get_bot_history(limit=15)
            saw_response = False

            for message in history:
                if message.id <= command_message.id:
                    continue

                from_user = getattr(message, "from_user", None)
                if from_user is None or from_user.id != self.settings.leech_bot_id:
                    continue

                saw_response = True
                state = self._classify_status_message(message)
                if state == "unknown":
                    continue

                saw_response = True
                self.logger.info(
                    "bot status probe result",
                    extra={
                        "event": "bot_status_probe_result",
                        "bot_command_message_id": command_message.id,
                        "status_state": state,
                    },
                )
                return state

            if saw_response:
                self.logger.info(
                    "bot status probe returned unclassified response",
                    extra={
                        "event": "bot_status_probe_unclassified",
                        "bot_command_message_id": command_message.id,
                    },
                )
                return "unknown"

            await asyncio.sleep(max(2.0, self.settings.action_delay_sec))

        self.logger.warning(
            "bot status probe timed out",
            extra={
                "event": "bot_status_probe_timed_out",
                "bot_command_message_id": command_message.id,
            },
        )
        return "unknown"

    @staticmethod
    def _is_uploaded_media(message: Message) -> bool:
        return bool(message.video or message.animation or message.document)

    def _is_recent_command(self, message: Message) -> bool:
        message_date = getattr(message, "date", None)
        if message_date is None:
            return False

        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)

        age_seconds = (datetime.now(timezone.utc) - message_date).total_seconds()
        return age_seconds <= self.MAX_RESUMABLE_COMMAND_AGE_SEC

    @staticmethod
    def _extract_bot_error(message: Message) -> str | None:
        text = (message.text or message.caption or "").strip().lower()
        if not text:
            return None

        status_markers = (
            "task by",
            "processed",
            "status",
            "speed",
            "time",
            "engine",
            "out mode",
            "in mode",
        )
        if any(marker in text for marker in status_markers):
            return None

        error_patterns = (
            r"\bfailed\b",
            r"\bunable\b",
            r"\bnot found\b",
            r"\binvalid\b",
            r"\bexception\b",
            r"\bprivate link\b",
            r"\bnot accessible\b",
            r"\bno active bot task\b",
            r"\bno active task\b",
            r"\bbot restarted\b",
            r"\btask lost\b",
            r"\berror:\b",
            r"\berror occurred\b",
            r"\bdownload failed\b",
            r"\bupload failed\b",
        )
        if any(re.search(pattern, text) for pattern in error_patterns):
            return text[:500]
        return None

    @staticmethod
    def _is_retryable_failure(error_text: str) -> bool:
        normalized = error_text.lower()
        retryable_fragments = (
            "timed out waiting",
            "no active bot task",
            "no active task",
            "restart",
            "restarted",
            "stalled",
            "lost",
            "upload",
            "connection",
            "temporarily",
            "timeout",
        )
        non_retryable_fragments = (
            "private link",
            "not accessible",
            "not found",
            "invalid",
            "you don't have access",
        )
        if any(fragment in normalized for fragment in non_retryable_fragments):
            return False
        return any(fragment in normalized for fragment in retryable_fragments)

    @staticmethod
    def _message_snapshot(message: Message) -> str:
        text = message.text or ""
        caption = message.caption or ""
        media_bits = [
            "video" if message.video else "",
            "animation" if message.animation else "",
            "document" if message.document else "",
            "audio" if message.audio else "",
            "photo" if message.photo else "",
        ]
        return "|".join(
            [
                str(message.id),
                text,
                caption,
                "".join(media_bits),
                str(getattr(message, "edit_date", "") or ""),
                str(getattr(message, "date", "") or ""),
            ]
        )

    @staticmethod
    def _classify_status_message(message: Message) -> BotStatusState:
        text = (message.text or message.caption or "").strip().lower()
        if not text:
            return "unknown"

        if "no active bot task" in text or "no active task" in text:
            return "inactive"

        active_fragments = (
            "processed",
            "status",
            "speed",
            "time",
            "upload",
            "download",
            "task by",
            "bot stats",
        )
        if any(fragment in text for fragment in active_fragments):
            return "active"

        return "unknown"

    @staticmethod
    def _message_indicates_upload_started(message: Message) -> bool:
        text = (message.text or message.caption or "").strip().lower()
        if not text:
            return False
        explicit_markers = (
            "status -> upload",
            "status - upload",
            "status: upload",
            "status => upload",
        )
        return any(marker in text for marker in explicit_markers) or (
            "status" in text and "upload" in text
        )

    @staticmethod
    def _is_related_to_command(message: Message, command_message_id: int) -> bool:
        reply_to_message_id = getattr(message, "reply_to_message_id", None)
        if reply_to_message_id is None:
            return True
        return reply_to_message_id == command_message_id

    @staticmethod
    def _matches_expected_media(message: Message, source_message: Message) -> bool:
        if BotLeechService._looks_like_split_part(message, source_message):
            return True

        source_media = source_message.video or source_message.animation or source_message.document
        target_media = message.video or message.animation or message.document
        if source_media is None or target_media is None:
            return True

        source_size = getattr(source_media, "file_size", None)
        target_size = getattr(target_media, "file_size", None)
        if source_size is None or target_size is None:
            return True

        tolerance_bytes = 16 * 1024
        return abs(int(source_size) - int(target_size)) <= tolerance_bytes

    @staticmethod
    def _looks_like_split_part(message: Message, source_message: Message) -> bool:
        source_media = source_message.video or source_message.animation or source_message.document
        target_media = message.video or message.animation or message.document
        source_name = (getattr(source_media, "file_name", "") or "").strip().lower()
        target_name = (getattr(target_media, "file_name", "") or "").strip().lower()

        if not target_name:
            return False

        split_markers = (".part", "part0", ".001", ".002", ".003")
        if not any(marker in target_name for marker in split_markers):
            return False

        if not source_name:
            return True

        source_stem = source_name
        if "." in source_name:
            source_stem = source_name.rsplit(".", 1)[0]
        normalized_stem = source_stem.replace(" ", "").replace("_", "").replace("-", "")
        normalized_target = target_name.replace(" ", "").replace("_", "").replace("-", "")
        return normalized_stem in normalized_target
