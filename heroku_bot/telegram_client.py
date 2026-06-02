from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
from pathlib import Path
from time import monotonic, time
from typing import Awaitable, Callable, TypeVar

from pyrogram import Client, filters, raw
from pyrogram.errors import FloodWait, RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import Chat, Message, User

from config import AppSettings, MappingConfig
from hyper_download import HyperTGDownload


T = TypeVar("T")

from pyrogram.session.session import Session, TLObject


if not getattr(Session.invoke, "_topic_cloner_sleep_patch", False):
    _original_session_invoke = Session.invoke

    async def _patched_session_invoke(
        self,
        query: TLObject,
        retries: int = Session.MAX_RETRIES,
        timeout: float = Session.WAIT_TIMEOUT,
        sleep_threshold: float = Session.SLEEP_THRESHOLD,
    ):
        # Pyrogram's media download code hardcodes small sleep thresholds for
        # GetFile calls. When Telegram asks for a larger wait, Pyrogram logs the
        # FloodWait inside get_file and returns an empty file. Raise only low
        # non-negative thresholds to the client setting; preserve negative
        # thresholds because Pyrogram uses them to mean "raise immediately".
        client_threshold = getattr(getattr(self, "client", None), "sleep_threshold", None)
        if client_threshold is not None and sleep_threshold >= 0:
            sleep_threshold = max(sleep_threshold, client_threshold)
        return await _original_session_invoke(
            self,
            query,
            retries=retries,
            timeout=timeout,
            sleep_threshold=sleep_threshold,
        )

    _patched_session_invoke._topic_cloner_sleep_patch = True
    Session.invoke = _patched_session_invoke


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
        self.helper_clients: dict[int, Client] = {}
        self.helper_loads: dict[int, int] = {}
        self.hyper_threads = int(os.getenv("HYPER_THREADS", "0") or 0)
        self.hyper_dump_chat = self._parse_chat_id(
            os.getenv("HYPER_DUMP_CHAT", "").strip()
            or os.getenv("LEECH_DUMP_CHAT", "").strip()
        )
        self.helper_tokens = self._parse_env_list("HELPER_TOKENS")
        self.helper_session_strings = self._helper_session_strings()
        self.use_main_session_as_helper = self._read_bool_env("TG_USE_MAIN_SESSION_AS_HELPER", True)
        self.helper_client_kinds: dict[int, str] = {}
        self._owned_helper_client_ids: set[int] = set()
        self.flood_wait_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None
        self.app = Client(
            name="telegram-topic-cloner",
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            session_string=settings.session_string,
            in_memory=True,
            no_updates=not receive_updates,
            sleep_threshold=86400,
            **self._transmission_options(),
        )

    async def start(self) -> User:
        await self.app.start()
        await self._start_helper_clients()
        return await self.read_call("get_me", self.app.get_me)

    async def stop(self) -> None:
        try:
            await self.app.stop()
        except Exception:
            self.logger.exception("shutdown error", extra={"event": "shutdown_error"})
        for index, client in list(self.helper_clients.items()):
            if index not in self._owned_helper_client_ids:
                continue
            try:
                await client.stop()
            except Exception:
                self.logger.exception("helper shutdown error", extra={"event": "shutdown_error"})
        self.helper_clients.clear()
        self.helper_loads.clear()
        self.helper_client_kinds.clear()
        self._owned_helper_client_ids.clear()

    async def _start_helper_clients(self) -> None:
        if self.helper_clients:
            return

        next_index = 1
        if self.use_main_session_as_helper:
            self.helper_clients[next_index] = self.app
            self.helper_loads[next_index] = 0
            self.helper_client_kinds[next_index] = "main_user_session"
            next_index += 1

        helper_tasks = []
        for session_string in self.helper_session_strings:
            if session_string == self.settings.session_string and self.use_main_session_as_helper:
                continue
            helper_tasks.append(self._start_helper_user_session(next_index, session_string))
            next_index += 1

        for token in self.helper_tokens:
            helper_tasks.append(self._start_helper_bot(next_index, token))
            next_index += 1

        if helper_tasks:
            await asyncio.gather(*helper_tasks)

        if self.helper_clients:
            self.logger.info(
                "telegram helper clients started",
                extra={
                    "event": "telegram_helper_clients_started",
                    "helper_count": len(self.helper_clients),
                    "helper_user_sessions": self.helper_user_session_count,
                    "helper_bots": self.helper_bot_count,
                    "main_session_helper": self.main_session_helper_enabled,
                    "hyper_threads": self.hyper_threads,
                    "hyper_dump_chat": self.hyper_dump_chat,
                },
            )

    @property
    def helper_user_session_count(self) -> int:
        return sum(1 for kind in self.helper_client_kinds.values() if kind in {"main_user_session", "user_session"})

    @property
    def helper_bot_count(self) -> int:
        return sum(1 for kind in self.helper_client_kinds.values() if kind == "bot")

    @property
    def main_session_helper_enabled(self) -> bool:
        return any(kind == "main_user_session" for kind in self.helper_client_kinds.values())

    async def _start_helper_user_session(self, index: int, session_string: str) -> None:
        client = Client(
            name=f"telegram-topic-helper-user-{index}",
            api_id=self.settings.api_id,
            api_hash=self.settings.api_hash,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
            sleep_threshold=86400,
            **self._transmission_options(),
        )
        try:
            await client.start()
        except Exception:
            self.logger.exception(
                "failed to start helper user session",
                extra={"event": "helper_user_session_start_failed", "helper_index": index},
            )
            return

        self.helper_clients[index] = client
        self.helper_loads[index] = 0
        self.helper_client_kinds[index] = "user_session"
        self._owned_helper_client_ids.add(index)

    async def _start_helper_bot(self, index: int, token: str) -> None:
        client = Client(
            name=f"telegram-topic-helper-{index}",
            api_id=self.settings.api_id,
            api_hash=self.settings.api_hash,
            bot_token=token,
            in_memory=True,
            no_updates=True,
            sleep_threshold=86400,
            **self._transmission_options(),
        )
        try:
            await client.start()
        except Exception:
            self.logger.exception(
                "failed to start helper bot",
                extra={"event": "helper_bot_start_failed", "helper_index": index},
            )
            return

        self.helper_clients[index] = client
        self.helper_loads[index] = 0
        self.helper_client_kinds[index] = "bot"
        self._owned_helper_client_ids.add(index)

    @staticmethod
    def _parse_env_list(name: str) -> list[str]:
        return [
            value
            for value in re.split(r"[\s,;]+", os.getenv(name, "").strip())
            if value
        ]

    @classmethod
    def _helper_session_strings(cls) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for name in ("TG_HELPER_SESSION_STRINGS", "HELPER_SESSION_STRINGS", "USER_SESSION_STRINGS"):
            for session_string in cls._parse_env_list(name):
                if session_string in seen:
                    continue
                seen.add(session_string)
                values.append(session_string)
        return values

    @staticmethod
    def _read_bool_env(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            return default
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _transmission_options() -> dict[str, int]:
        if "max_concurrent_transmissions" in inspect.signature(Client.__init__).parameters:
            return {"max_concurrent_transmissions": 100}
        return {}

    @staticmethod
    def _parse_chat_id(value: str) -> int | str | None:
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return value

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
                callback = self.flood_wait_callback
                if callback is not None:
                    try:
                        await callback(
                            {
                                "operation": operation,
                                "wait_seconds": wait_seconds,
                                "wait_until": time() + wait_seconds,
                            }
                        )
                    except Exception:
                        self.logger.debug(
                            "flood wait callback failed",
                            exc_info=True,
                            extra={"event": "flood_wait_callback_failed", "operation": operation},
                        )
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

    async def copy_message_to_topic(
        self,
        chat_id: int | str,
        from_chat_id: int | str,
        topic_id: int,
        message_id: int,
    ):
        await self.ensure_peer_cached(chat_id)
        return await self.write_call(
            "copy_message_to_topic",
            lambda: self.app.copy_message(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_to_message_id=topic_id,
            ),
        )

    async def download_media_to_path(
        self,
        message: Message,
        target_path: Path | str,
        progress=None,
        download_mode: str = "auto",
    ) -> str:
        download_mode = (download_mode or "auto").strip().lower()
        if download_mode not in {"hyper", "auto", "normal"}:
            raise ValueError("download_mode must be one of: hyper, auto, normal")

        progress_handler = self._wrap_progress_callback(progress)
        if download_mode == "hyper" and not self.helper_clients:
            raise RuntimeError(
                "Hyper Telegram download requested, but no helper clients are available. "
                "Set TG_USE_MAIN_SESSION_AS_HELPER=true, TG_HELPER_SESSION_STRINGS, or HELPER_TOKENS in heroku_bot/.env."
            )

        if download_mode in {"hyper", "auto"} and self.helper_clients:
            hyper = HyperTGDownload(
                self.app,
                self.helper_clients,
                self.helper_loads,
                hyper_threads=self.hyper_threads,
                logger=self.logger,
            )
            try:
                result = await hyper.download_media(
                    message,
                    file_name=str(target_path),
                    progress=progress_handler,
                    dump_chat=self.hyper_dump_chat,
                )
            except Exception as exc:
                if download_mode == "hyper":
                    raise RuntimeError(f"Hyper Telegram download failed: {exc}") from exc
                self.logger.warning(
                    "hyper download failed, falling back to main client",
                    exc_info=True,
                    extra={"event": "hyper_download_fallback"},
                )
            else:
                if isinstance(result, str):
                    return self._resolve_downloaded_path(result)
                if download_mode == "hyper":
                    raise RuntimeError("Hyper Telegram download returned no file.")
                self.logger.warning(
                    "hyper download returned no file, falling back to main client",
                    extra={"event": "hyper_download_empty"},
                )
            finally:
                await hyper.aclose()

        result = await self.write_call(
            "download_media",
            lambda: self.app.download_media(
                message,
                file_name=str(target_path),
                progress=progress_handler,
            ),
        )
        if not isinstance(result, str):
            raise RuntimeError("Unexpected download result type")

        return self._resolve_downloaded_path(result)

    @staticmethod
    def _resolve_downloaded_path(result: str) -> str:
        downloaded_path = Path(result)
        if downloaded_path.is_dir():
            files = [p for p in downloaded_path.rglob("*") if p.is_file() and p.suffix.lower() != ".temp"]
            if not files:
                files = [p for p in downloaded_path.rglob("*") if p.is_file()]
            if not files:
                raise RuntimeError(
                    f"Media download failed: no file found inside {downloaded_path}"
                )
            # Prefer the most recently modified file in case multiple files were created.
            downloaded_path = max(files, key=TelegramService._safe_mtime)

        if downloaded_path.suffix.lower() == ".temp":
            final_path = downloaded_path.with_suffix("")
            if final_path.exists():
                downloaded_path = final_path
            else:
                try:
                    downloaded_path.rename(final_path)
                    downloaded_path = final_path
                except OSError:
                    pass

        try:
            size = downloaded_path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"Media download failed: cannot stat {downloaded_path}") from exc
        if size <= 0:
            downloaded_path.unlink(missing_ok=True)
            raise RuntimeError(f"Media download failed: downloaded file is empty ({downloaded_path})")

        return str(downloaded_path)

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _wrap_progress_callback(self, progress):
        if progress is None:
            return None

        loop = self.app.loop
        logger = self.logger
        last_report_at = 0.0

        def _log_progress_error(future) -> None:
            try:
                future.result()
            except Exception:
                logger.debug("progress callback failed", exc_info=True)

        def _handler(current, total, *args):
            nonlocal last_report_at
            now = monotonic()
            if current < total and now - last_report_at < 0.35:
                return
            last_report_at = now

            try:
                result = progress(current, total, *args)
            except Exception:
                logger.debug("progress callback failed", exc_info=True)
                return

            if not inspect.isawaitable(result):
                return

            if loop.is_closed():
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                return

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                future = asyncio.run_coroutine_threadsafe(result, loop)
                future.add_done_callback(_log_progress_error)
                return

            if running_loop is loop:
                task = loop.create_task(result)
                task.add_done_callback(_log_progress_error)
                return

            future = asyncio.run_coroutine_threadsafe(result, loop)
            future.add_done_callback(_log_progress_error)

        return _handler

    async def send_file_to_topic(
        self,
        chat_id: int,
        topic_id: int,
        file_path: Path,
        caption: str | None = None,
    ) -> Message:
        if not file_path.exists() or not file_path.is_file():
            raise RuntimeError(f"Invalid file path for upload: {file_path}")

        await self.ensure_peer_cached(chat_id)
        return await self.write_call(
            "send_file_to_topic",
            lambda: self.app.send_document(
                chat_id=chat_id,
                document=str(file_path),
                caption=caption,
                reply_to_message_id=topic_id,
            ),
        )

    async def send_downloaded_media_to_topic(
        self,
        chat_id: int,
        topic_id: int,
        source_message: Message,
        file_path: Path,
        caption: str | None = None,
        caption_entities=None,
        progress=None,
    ) -> Message:
        if not file_path.exists() or not file_path.is_file():
            raise RuntimeError(f"Invalid file path for upload: {file_path}")

        await self.ensure_peer_cached(chat_id)

        try:
            if source_message.photo:
                return await self.write_call(
                    "send_downloaded_photo_to_topic",
                    lambda: self.app.send_photo(
                        chat_id=chat_id,
                        photo=str(file_path),
                        caption=caption,
                        caption_entities=caption_entities,
                        reply_to_message_id=topic_id,
                        progress=self._wrap_progress_callback(progress),
                    ),
                )

            if source_message.video:
                duration, width, height = await self._video_upload_metadata(file_path, source_message.video)
                thumb = await self._generate_video_thumbnail(file_path, duration)
                self.logger.info(
                    "uploading downloaded video: duration=%s width=%s height=%s source_duration=%s file=%s",
                    duration,
                    width,
                    height,
                    int(getattr(source_message.video, "duration", 0) or 0),
                    file_path,
                    extra={
                        "event": "video_upload_metadata",
                        "file": str(file_path),
                        "duration": duration,
                        "width": width,
                        "height": height,
                        "source_duration": int(getattr(source_message.video, "duration", 0) or 0),
                    },
                )
                return await self.write_call(
                    "send_downloaded_video_to_topic",
                    lambda: self.app.send_video(
                        chat_id=chat_id,
                        video=str(file_path),
                        caption=caption,
                        caption_entities=caption_entities,
                        reply_to_message_id=topic_id,
                        duration=duration,
                        width=width,
                        height=height,
                        thumb=str(thumb) if thumb else None,
                        file_name=file_path.name,
                        supports_streaming=True,
                        progress=self._wrap_progress_callback(progress),
                    ),
                )

            if source_message.animation:
                duration, width, height = await self._video_upload_metadata(file_path, source_message.animation)
                thumb = await self._generate_video_thumbnail(file_path, duration)
                self.logger.info(
                    "uploading downloaded animation: duration=%s width=%s height=%s source_duration=%s file=%s",
                    duration,
                    width,
                    height,
                    int(getattr(source_message.animation, "duration", 0) or 0),
                    file_path,
                    extra={
                        "event": "animation_upload_metadata",
                        "file": str(file_path),
                        "duration": duration,
                        "width": width,
                        "height": height,
                        "source_duration": int(getattr(source_message.animation, "duration", 0) or 0),
                    },
                )
                return await self.write_call(
                    "send_downloaded_animation_to_topic",
                    lambda: self.app.send_animation(
                        chat_id=chat_id,
                        animation=str(file_path),
                        caption=caption,
                        caption_entities=caption_entities,
                        reply_to_message_id=topic_id,
                        duration=duration,
                        width=width,
                        height=height,
                        thumb=str(thumb) if thumb else None,
                        file_name=file_path.name,
                        progress=self._wrap_progress_callback(progress),
                    ),
                )

            if source_message.audio:
                duration = int(getattr(source_message.audio, "duration", 0) or 0)
                return await self.write_call(
                    "send_downloaded_audio_to_topic",
                    lambda: self.app.send_audio(
                        chat_id=chat_id,
                        audio=str(file_path),
                        caption=caption,
                        caption_entities=caption_entities,
                        reply_to_message_id=topic_id,
                        duration=duration,
                        performer=getattr(source_message.audio, "performer", None),
                        title=getattr(source_message.audio, "title", None),
                        file_name=file_path.name,
                        progress=self._wrap_progress_callback(progress),
                    ),
                )

            if source_message.voice:
                duration = int(getattr(source_message.voice, "duration", 0) or 0)
                return await self.write_call(
                    "send_downloaded_voice_to_topic",
                    lambda: self.app.send_voice(
                        chat_id=chat_id,
                        voice=str(file_path),
                        caption=caption,
                        caption_entities=caption_entities,
                        reply_to_message_id=topic_id,
                        duration=duration,
                        progress=self._wrap_progress_callback(progress),
                    ),
                )

            if source_message.sticker:
                return await self.write_call(
                    "send_downloaded_sticker_to_topic",
                    lambda: self.app.send_sticker(
                        chat_id=chat_id,
                        sticker=str(file_path),
                        reply_to_message_id=topic_id,
                        progress=self._wrap_progress_callback(progress),
                    ),
                )
        except Exception:
            self.logger.warning(
                "typed media upload failed, falling back to document upload",
                exc_info=True,
            )

        return await self.write_call(
            "send_downloaded_document_to_topic",
            lambda: self.app.send_document(
                chat_id=chat_id,
                document=str(file_path),
                caption=caption,
                caption_entities=caption_entities,
                reply_to_message_id=topic_id,
                file_name=file_path.name,
                progress=self._wrap_progress_callback(progress),
            ),
        )

    async def _video_upload_metadata(self, file_path: Path, source_media) -> tuple[int, int, int]:
        source_duration = int(getattr(source_media, "duration", 0) or 0)
        source_width = int(getattr(source_media, "width", 0) or 0)
        source_height = int(getattr(source_media, "height", 0) or 0)
        probed_duration, probed_width, probed_height = await self._probe_video_metadata(file_path)
        return (
            probed_duration or source_duration,
            probed_width or source_width,
            probed_height or source_height,
        )

    async def _generate_video_thumbnail(self, file_path: Path, duration: int) -> Path | None:
        if os.getenv("GENERATE_VIDEO_THUMBNAILS", "true").strip().lower() in {"0", "false", "no", "off"}:
            return None

        ffmpeg_binary = await self._first_available_binary(self._ffmpeg_candidates())
        if not ffmpeg_binary:
            self.logger.warning(
                "ffmpeg is not available; video thumbnail generation is disabled. "
                "Install imageio-ffmpeg or the Heroku apt buildpack with an Aptfile containing ffmpeg.",
                extra={"event": "ffmpeg_missing", "candidates": self._ffmpeg_candidates()},
            )
            return None

        thumb_path = file_path.with_name(f"{file_path.stem}.tg-thumb.jpg")
        timestamps = self._thumbnail_timestamps(duration)

        for timestamp in timestamps:
            if thumb_path.exists():
                thumb_path.unlink(missing_ok=True)
            if await self._extract_video_thumbnail(file_path, thumb_path, timestamp, ffmpeg_binary):
                self.logger.info(
                    "generated video frame thumbnail",
                    extra={"event": "video_thumbnail_generated", "path": str(thumb_path), "timestamp": timestamp},
                )
                return thumb_path

        self.logger.warning(
            "could not generate a usable video frame thumbnail; upload will use Telegram default",
            extra={"event": "video_thumbnail_generation_failed", "file": str(file_path)},
        )
        return None

    @staticmethod
    def _thumbnail_timestamps(duration: int) -> list[float]:
        if duration <= 0:
            duration = 3
        midpoint = max(duration // 2, 1)
        if duration > 12:
            return [
                float(midpoint),
                max(duration * 0.35, 1.0),
                max(duration * 0.65, 1.0),
                max(duration * 0.8, 1.0),
            ]
        return [float(midpoint), max(duration * 0.75, 1.0), 1.0]

    async def _extract_video_thumbnail(
        self,
        file_path: Path,
        thumb_path: Path,
        timestamp: float,
        ffmpeg_binary: str,
    ) -> bool:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.2f}",
                "-i",
                str(file_path),
                "-vf",
                "scale='min(480,iw)':-2",
                "-q:v",
                "2",
                "-frames:v",
                "1",
                "-f",
                "mjpeg",
                "-y",
                str(thumb_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except (FileNotFoundError, TimeoutError, asyncio.TimeoutError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return False
        except Exception:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.logger.debug("video thumbnail generation failed", exc_info=True)
            return False

        if process.returncode != 0:
            if stderr:
                self.logger.debug("ffmpeg thumbnail failed: %s", stderr.decode(errors="ignore").strip())
            return False

        try:
            if not thumb_path.exists() or thumb_path.stat().st_size <= 0:
                return False
            return await asyncio.to_thread(self._is_usable_video_frame_thumb, thumb_path)
        except OSError:
            return False

    @staticmethod
    def _is_usable_video_frame_thumb(thumb_path: Path) -> bool:
        try:
            from PIL import Image, ImageStat
        except ImportError:
            return True

        try:
            with Image.open(thumb_path) as image:
                grayscale = image.convert("L")
                stat = ImageStat.Stat(grayscale)
                mean = float(stat.mean[0])
                extrema = grayscale.getextrema()
                contrast = float(extrema[1] - extrema[0]) if extrema else 0.0
        except OSError:
            return False

        return mean >= 8.0 and contrast >= 5.0 and thumb_path.stat().st_size <= 200 * 1024

    async def _binary_available(self, binary: str) -> bool:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=5)
        except (FileNotFoundError, TimeoutError, asyncio.TimeoutError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return False
        except Exception:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.logger.debug("binary availability check failed", exc_info=True)
            return False
        return process.returncode == 0

    async def _probe_video_metadata(self, file_path: Path) -> tuple[int, int, int]:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self._ffprobe_binary(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(file_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        except (FileNotFoundError, TimeoutError, asyncio.TimeoutError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return 0, 0, 0
        except Exception:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.logger.debug("video metadata probe failed", exc_info=True)
            return 0, 0, 0

        if process.returncode != 0 or not stdout:
            if stderr:
                self.logger.debug("ffprobe failed: %s", stderr.decode(errors="ignore").strip())
            return 0, 0, 0

        try:
            payload = json.loads(stdout.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return 0, 0, 0

        width = 0
        height = 0
        video_stream = None
        for stream in payload.get("streams", []):
            if stream.get("codec_type") != "video":
                continue
            video_stream = stream
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            break

        duration = self._duration_from_ffprobe_payload(payload, video_stream)
        return duration, width, height

    @classmethod
    def _duration_from_ffprobe_payload(cls, payload: dict, video_stream: dict | None) -> int:
        candidates = [
            payload.get("format", {}).get("duration"),
        ]
        if video_stream:
            candidates.extend(
                [
                    video_stream.get("duration"),
                    video_stream.get("tags", {}).get("DURATION"),
                    video_stream.get("tags", {}).get("duration"),
                ]
            )

            duration_ts = video_stream.get("duration_ts")
            time_base = video_stream.get("time_base")
            if duration_ts and time_base:
                candidates.append(cls._duration_from_time_base(duration_ts, time_base))

        for value in candidates:
            duration = cls._coerce_duration(value)
            if duration > 0:
                return duration
        return 0

    @staticmethod
    def _duration_from_time_base(duration_ts, time_base) -> float | None:
        try:
            numerator, denominator = str(time_base).split("/", 1)
            return float(duration_ts) * float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _coerce_duration(value) -> int:
        if value in (None, ""):
            return 0

        if isinstance(value, (int, float)):
            return max(1, round(float(value))) if float(value) > 0 else 0

        text = str(value).strip()
        if not text:
            return 0

        try:
            parsed = float(text)
            return max(1, round(parsed)) if parsed > 0 else 0
        except ValueError:
            pass

        match = re.match(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$", text)
        if not match:
            return 0

        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        total = hours * 3600 + minutes * 60 + seconds
        return max(1, round(total)) if total > 0 else 0

    @staticmethod
    def _ffmpeg_binary() -> str:
        return TelegramService._ffmpeg_candidates()[0]

    @staticmethod
    def _ffmpeg_candidates() -> list[str]:
        candidates: list[str] = []
        configured = os.getenv("FFMPEG_BINARY", "").strip()
        if configured:
            candidates.append(configured)

        try:
            import imageio_ffmpeg

            candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            pass

        if binary := shutil.which("ffmpeg"):
            candidates.append(binary)

        candidates.append("ffmpeg")
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                deduped.append(candidate)
                seen.add(candidate)
        return deduped

    async def _first_available_binary(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            if await self._binary_available(candidate):
                return candidate
            self.logger.debug(
                "ffmpeg candidate is not executable",
                extra={"event": "ffmpeg_candidate_unavailable", "binary": candidate},
            )
        return None

    @staticmethod
    def _ffprobe_candidates() -> list[str]:
        candidates: list[str] = []
        configured = os.getenv("FFPROBE_BINARY", "").strip()
        if configured:
            candidates.append(configured)
        if binary := shutil.which("ffprobe"):
            candidates.append(binary)
        candidates.append("ffprobe")
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                deduped.append(candidate)
                seen.add(candidate)
        return deduped

    @staticmethod
    def _ffprobe_binary() -> str:
        configured = os.getenv("FFPROBE_BINARY", "").strip()
        if configured:
            return configured
        if binary := shutil.which("ffprobe"):
            return binary
        ffprobe_candidates = TelegramService._ffprobe_candidates()
        return ffprobe_candidates[0] if ffprobe_candidates else "ffprobe"

    async def get_chat(self, chat_id: int | str) -> Chat:
        await self.ensure_peer_cached(chat_id)
        return await self.read_call("get_chat", lambda: self.app.get_chat(chat_id))

    async def get_forum_topic_title(self, chat_id: int, topic_id: int) -> str | None:
        try:
            peer = await self.resolve_input_peer(chat_id)
            channel_id = getattr(peer, "channel_id", None)
            access_hash = getattr(peer, "access_hash", None)
            if channel_id is None or access_hash is None:
                return None

            result = await self.read_call(
                "get_forum_topic_title",
                lambda: self.app.invoke(
                    raw.functions.channels.GetForumTopicsByID(
                        channel=raw.types.InputChannel(
                            channel_id=channel_id,
                            access_hash=access_hash,
                        ),
                        topics=[topic_id],
                    )
                ),
            )
            for topic in getattr(result, "topics", []) or []:
                candidate_ids = {
                    getattr(topic, "top_message", None),
                    getattr(topic, "id", None),
                    getattr(topic, "topic_id", None),
                }
                if topic_id not in candidate_ids:
                    continue
                title = getattr(topic, "title", None)
                if title:
                    return str(title)
        except Exception:
            self.logger.debug(
                "forum topic title lookup failed",
                exc_info=True,
                extra={"event": "forum_topic_title_lookup_failed", "chat_id": chat_id, "topic_id": topic_id},
            )
        try:
            anchor = await self.get_message(chat_id, topic_id)
            for attr in ("forum_topic_created", "forum_topic_edited"):
                event = getattr(anchor, attr, None) if anchor is not None else None
                if event is None:
                    continue
                for title_attr in ("title", "name"):
                    title = getattr(event, title_attr, None)
                    if title:
                        return str(title)
            text = (getattr(anchor, "text", None) or getattr(anchor, "caption", None) or "").strip() if anchor else ""
            return text or None
        except Exception:
            self.logger.debug(
                "forum topic anchor title fallback failed",
                exc_info=True,
                extra={"event": "forum_topic_anchor_title_lookup_failed", "chat_id": chat_id, "topic_id": topic_id},
            )
        return None

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
        progress_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
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
            if progress_callback is not None:
                try:
                    await progress_callback(
                        {
                            "found_message_ids": len(message_ids),
                            "last_message_id": batch_ids[-1],
                        }
                    )
                except Exception:
                    self.logger.debug(
                        "topic message id progress callback failed",
                        exc_info=True,
                        extra={"event": "topic_message_id_progress_callback_failed"},
                    )
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
