from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from pyrogram import Client, filters, raw
from pyrogram.errors import FloodWait, RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import Chat, Message, User

from config import AppSettings, MappingConfig


T = TypeVar("T")


class TelegramService:
    def __init__(
        self,
        settings: AppSettings,
        logger: logging.Logger,
        *,
        receive_updates: bool,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.app = Client(
            name="telegram-topic-cloner",
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            session_string=settings.session_string,
            in_memory=True,
            no_updates=not receive_updates,
        )

    async def start(self) -> User:
        await self.app.start()
        return await self.read_call("get_me", self.app.get_me)

    async def stop(self) -> None:
        try:
            await self.app.stop()
        except Exception:
            self.logger.exception("shutdown error", extra={"event": "shutdown_error"})

    async def read_call(self, operation: str, func: Callable[[], Awaitable[T]]) -> T:
        return await self._call(operation, func, apply_action_delay=False)

    async def write_call(self, operation: str, func: Callable[[], Awaitable[T]]) -> T:
        return await self._call(operation, func, apply_action_delay=True)

    async def _call(
        self,
        operation: str,
        func: Callable[[], Awaitable[T]],
        *,
        apply_action_delay: bool,
    ) -> T:
        attempt = 1
        while True:
            try:
                result = await func()
                if apply_action_delay and self.settings.action_delay_sec > 0:
                    await asyncio.sleep(self.settings.action_delay_sec)
                return result
            except FloodWait as exc:
                wait_seconds = float(getattr(exc, "value", 0) or 0) + self.settings.flood_wait_buffer_sec
                self.logger.warning(
                    "flood wait",
                    extra={
                        "event": "flood_wait",
                        "operation": operation,
                        "wait_seconds": wait_seconds,
                    },
                )
                await asyncio.sleep(wait_seconds)
            except RPCError as exc:
                if not self._is_retryable_rpc_error(exc) or attempt >= self.settings.retry_limit:
                    raise
                backoff = min(2 ** attempt, 30)
                self.logger.warning(
                    "retryable rpc error",
                    extra={
                        "event": "retryable_rpc_error",
                        "operation": operation,
                        "attempt": attempt,
                        "backoff_seconds": backoff,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(backoff)
                attempt += 1
            except (OSError, TimeoutError) as exc:
                if attempt >= self.settings.retry_limit:
                    raise
                backoff = min(2 ** attempt, 30)
                self.logger.warning(
                    "retryable network error",
                    extra={
                        "event": "retryable_network_error",
                        "operation": operation,
                        "attempt": attempt,
                        "backoff_seconds": backoff,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(backoff)
                attempt += 1

    @staticmethod
    def _is_retryable_rpc_error(exc: RPCError) -> bool:
        message = str(exc).lower()
        transient_fragments = (
            "timeout",
            "temporarily",
            "internal",
            "server",
            "worker busy",
            "connection",
            "reset",
        )
        return any(fragment in message for fragment in transient_fragments)

    @staticmethod
    def _is_channel_chat_id(chat_id: int | str) -> bool:
        return isinstance(chat_id, int) and str(chat_id).startswith("-100")

    @staticmethod
    def _raw_channel_id(chat_id: int) -> int:
        return int(str(chat_id)[4:])

    async def ensure_peer_cached(self, chat_id: int | str) -> None:
        if not self._is_channel_chat_id(chat_id):
            return

        try:
            await self.read_call(
                "storage_get_peer_by_id",
                lambda: self.app.storage.get_peer_by_id(chat_id),
            )
            return
        except KeyError:
            pass

        raw_channel_id = self._raw_channel_id(chat_id)
        self.logger.info(
            "warming large channel peer",
            extra={
                "event": "warm_large_channel_peer",
                "chat_id": chat_id,
                "raw_channel_id": raw_channel_id,
            },
        )

        await self.read_call(
            "seed_large_channel_peer",
            lambda: self.app.invoke(
                raw.functions.channels.GetChannels(
                    id=[
                        raw.types.InputChannel(
                            channel_id=raw_channel_id,
                            access_hash=0,
                        )
                    ]
                )
            ),
        )
        await self.read_call(
            "storage_get_peer_by_id_after_seed",
            lambda: self.app.storage.get_peer_by_id(chat_id),
        )

    async def resolve_input_peer(self, chat_id: int | str):
        await self.ensure_peer_cached(chat_id)
        return await self.read_call("resolve_peer", lambda: self.app.resolve_peer(chat_id))

    async def get_chat(self, chat_id: int | str) -> Chat:
        await self.ensure_peer_cached(chat_id)
        return await self.read_call("get_chat", lambda: self.app.get_chat(chat_id))

    async def get_bot_user(self) -> User:
        return await self.read_call("get_bot_user", lambda: self.app.get_users(self.settings.leech_bot_username))

    async def get_message(self, chat_id: int | str, message_id: int) -> Message | None:
        await self.ensure_peer_cached(chat_id)
        message = await self.read_call(
            "get_message",
            lambda: self.app.get_messages(chat_id, message_id, replies=0),
        )
        if isinstance(message, list):
            return message[0] if message else None
        return message

    async def get_messages_bulk(self, chat_id: int | str, message_ids: list[int]) -> list[Message]:
        if not message_ids:
            return []

        await self.ensure_peer_cached(chat_id)
        messages = await self.read_call(
            "get_messages_bulk",
            lambda: self.app.get_messages(chat_id, message_ids, replies=0),
        )
        if not isinstance(messages, list):
            messages = [messages] if messages else []
        return [message for message in messages if message is not None and not getattr(message, "empty", False)]

    async def get_topic_anchor(self, chat_id: int, topic_id: int) -> Message:
        message = await self.get_message(chat_id, topic_id)
        if message is None or getattr(message, "empty", False):
            raise RuntimeError(f"Topic anchor message {topic_id} was not found in {chat_id}")
        return message

    async def validate_mapping(self, mapping: MappingConfig) -> None:
        await self.get_chat(mapping.source_chat_id)
        await self.get_chat(mapping.destination_chat_id)
        await self.get_topic_anchor(mapping.source_chat_id, mapping.source_topic_id)
        await self.get_topic_anchor(mapping.destination_chat_id, mapping.destination_topic_id)

    async def list_topic_message_ids(
        self,
        chat_id: int,
        topic_id: int,
        start_from_message_id: int = 0,
        batch_size: int = 100,
    ) -> list[int]:
        peer = await self.resolve_input_peer(chat_id)
        offset_id = 0
        min_id = max(start_from_message_id - 1, 0)
        message_ids: list[int] = []
        seen_ids: set[int] = set()

        while True:
            replies = await self.read_call(
                "get_topic_replies",
                lambda: self.app.invoke(
                    raw.functions.messages.GetReplies(
                        peer=peer,
                        msg_id=topic_id,
                        offset_id=offset_id,
                        offset_date=0,
                        add_offset=0,
                        limit=batch_size,
                        max_id=0,
                        min_id=min_id,
                        hash=0,
                    )
                ),
            )

            batch_ids = []
            for item in getattr(replies, "messages", []):
                message_id = getattr(item, "id", None)
                if not isinstance(message_id, int):
                    continue
                if message_id == topic_id:
                    continue
                if start_from_message_id and message_id < start_from_message_id:
                    continue
                if message_id in seen_ids:
                    continue
                batch_ids.append(message_id)
                seen_ids.add(message_id)

            if not batch_ids:
                break

            message_ids.extend(batch_ids)
            next_offset = min(batch_ids)
            if next_offset == offset_id:
                break
            offset_id = next_offset

            if len(batch_ids) < batch_size:
                break

        return message_ids

    async def list_topic_messages(
        self,
        chat_id: int,
        topic_id: int,
        start_from_message_id: int = 0,
        batch_size: int = 100,
        inter_batch_delay_sec: float = 0.0,
    ) -> list[Message]:
        message_ids = await self.list_topic_message_ids(
            chat_id,
            topic_id,
            start_from_message_id=start_from_message_id,
            batch_size=batch_size,
        )
        ordered_ids = sorted(set(message_ids))
        messages: list[Message] = []

        for start in range(0, len(ordered_ids), batch_size):
            chunk = ordered_ids[start : start + batch_size]
            chunk_messages = await self.get_messages_bulk(chat_id, chunk)
            by_id = {message.id: message for message in chunk_messages}
            for message_id in chunk:
                message = by_id.get(message_id)
                if message is not None:
                    messages.append(message)

            has_more = start + batch_size < len(ordered_ids)
            if has_more and inter_batch_delay_sec > 0:
                await asyncio.sleep(inter_batch_delay_sec)

        return messages

    async def list_chat_message_ids(
        self,
        chat_id: int,
        start_from_message_id: int = 0,
        batch_size: int = 100,
    ) -> list[int]:
        peer = await self.resolve_input_peer(chat_id)
        offset_id = 0
        min_id = max(start_from_message_id - 1, 0)
        message_ids: list[int] = []
        seen_ids: set[int] = set()

        while True:
            history = await self.read_call(
                "get_chat_history_raw",
                lambda: self.app.invoke(
                    raw.functions.messages.GetHistory(
                        peer=peer,
                        offset_id=offset_id,
                        offset_date=0,
                        add_offset=0,
                        limit=batch_size,
                        max_id=0,
                        min_id=min_id,
                        hash=0,
                    )
                ),
            )

            batch_ids = []
            for item in getattr(history, "messages", []):
                message_id = getattr(item, "id", None)
                if not isinstance(message_id, int):
                    continue
                if start_from_message_id and message_id < start_from_message_id:
                    continue
                if message_id in seen_ids:
                    continue
                batch_ids.append(message_id)
                seen_ids.add(message_id)

            if not batch_ids:
                break

            message_ids.extend(batch_ids)
            next_offset = min(batch_ids)
            if next_offset == offset_id:
                break
            offset_id = next_offset

            if len(batch_ids) < batch_size:
                break

        return message_ids

    async def list_chat_messages(
        self,
        chat_id: int,
        start_from_message_id: int = 0,
        batch_size: int = 100,
        inter_batch_delay_sec: float = 0.0,
    ) -> list[Message]:
        message_ids = await self.list_chat_message_ids(
            chat_id,
            start_from_message_id=start_from_message_id,
            batch_size=batch_size,
        )
        ordered_ids = sorted(set(message_ids))
        messages: list[Message] = []

        for start in range(0, len(ordered_ids), batch_size):
            chunk = ordered_ids[start : start + batch_size]
            chunk_messages = await self.get_messages_bulk(chat_id, chunk)
            by_id = {message.id: message for message in chunk_messages}
            for message_id in chunk:
                message = by_id.get(message_id)
                if message is not None:
                    messages.append(message)

            has_more = start + batch_size < len(ordered_ids)
            if has_more and inter_batch_delay_sec > 0:
                await asyncio.sleep(inter_batch_delay_sec)

        return messages

    async def send_text_to_topic(
        self,
        chat_id: int,
        topic_id: int,
        text: str,
        entities,
        disable_web_page_preview: bool,
    ) -> Message:
        await self.ensure_peer_cached(chat_id)
        return await self.write_call(
            "send_text_to_topic",
            lambda: self.app.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                reply_to_message_id=topic_id,
                disable_web_page_preview=disable_web_page_preview,
            ),
        )

    async def send_cached_media_to_topic(
        self,
        chat_id: int,
        topic_id: int,
        file_id: str,
        caption: str | None,
        caption_entities,
    ) -> Message:
        await self.ensure_peer_cached(chat_id)
        return await self.write_call(
            "send_cached_media_to_topic",
            lambda: self.app.send_cached_media(
                chat_id=chat_id,
                file_id=file_id,
                caption=caption,
                caption_entities=caption_entities,
                reply_to_message_id=topic_id,
            ),
        )

    async def send_document_to_topic(
        self,
        chat_id: int,
        topic_id: int,
        document_path: Path,
        caption: str | None = None,
    ) -> Message:
        await self.ensure_peer_cached(chat_id)
        return await self.write_call(
            "send_document_to_topic",
            lambda: self.app.send_document(
                chat_id=chat_id,
                document=str(document_path),
                caption=caption,
                reply_to_message_id=topic_id,
            ),
        )

    async def send_message_to_bot(self, text: str) -> Message:
        return await self.write_call(
            "send_message_to_bot",
            lambda: self.app.send_message(self.settings.leech_bot_username, text),
        )

    async def get_bot_history(self, limit: int = 25, offset_id: int = 0) -> list[Message]:
        async def collect_history() -> list[Message]:
            messages: list[Message] = []
            async for message in self.app.get_chat_history(
                self.settings.leech_bot_username,
                limit=limit,
                offset_id=offset_id,
            ):
                messages.append(message)
            return messages

        return await self.read_call("get_bot_history", collect_history)

    async def click_message_button(
        self,
        message: Message,
        preferred_labels: list[str] | None = None,
    ) -> bool:
        reply_markup = getattr(message, "reply_markup", None)
        inline_keyboard = getattr(reply_markup, "inline_keyboard", None)
        if not inline_keyboard:
            return False

        normalized_preferences = [item.strip().lower() for item in (preferred_labels or []) if item.strip()]

        target_row: int | None = None
        target_col: int | None = None

        if normalized_preferences:
            for row_index, row in enumerate(inline_keyboard):
                for col_index, button in enumerate(row):
                    label = (getattr(button, "text", "") or "").strip().lower()
                    if not label:
                        continue
                    if any(pref == label or pref in label for pref in normalized_preferences):
                        target_row = row_index
                        target_col = col_index
                        break
                if target_row is not None:
                    break

        if target_row is None or target_col is None:
            target_row = 0
            target_col = 0

        await self.write_call(
            "click_message_button",
            lambda: message.click(target_row, target_col),
        )
        return True

    def add_watch_handler(self, source_chat_ids: list[int], callback) -> MessageHandler:
        handler = MessageHandler(callback, filters.chat(source_chat_ids))
        self.app.add_handler(handler, group=1)
        return handler

    def remove_watch_handler(self, handler: MessageHandler) -> None:
        self.app.remove_handler(handler, group=1)
