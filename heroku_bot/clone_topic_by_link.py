from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
import time
from typing import Any, Awaitable, Callable, Optional
from urllib import error, request

from pyrogram import Client, filters
from pyrogram.errors import ChatForwardsRestricted

from config import ConfigError, load_settings
from message_classifier import (
    classify_message,
    extract_caption_payload,
    extract_text_payload,
)
from models import MessageKind
from telegram_client import TelegramService
from topic_utils import parse_private_topic_link


HEROKU_RUNTIME_DIR = Path(os.getenv("HEROKU_RUNTIME_DIR", "heroku_runtime")).expanduser().resolve()
HEROKU_STATE_DIR = HEROKU_RUNTIME_DIR / "state"
MONGODB_DATA_API_URL = os.getenv("MONGODB_DATA_API_URL", "").strip()
MONGODB_DATA_API_KEY = os.getenv("MONGODB_DATA_API_KEY", "").strip()
MONGODB_DATA_SOURCE = os.getenv("MONGODB_DATA_SOURCE", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "").strip()
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "bot_state").strip()


@dataclass(frozen=True)
class CloneEndpoints:
    source_chat_id: int
    source_topic_id: int
    source_start_message_id: int
    destination_chat_id: int
    destination_topic_id: int


def _get_readable_file_size(size_in_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(size_in_bytes or 0)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f}{units[unit_index]}"


def _get_readable_time(seconds: float) -> str:
    total = int(max(seconds, 0))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return "".join(parts)


@dataclass
class TransferSnapshot:
    stage: str
    current: int
    total: int
    speed_bps: float
    eta_seconds: float | None


class WzmlTransferReporter:
    def __init__(
        self,
        *,
        source_message_id: int,
        index: int,
        total_messages: int,
        success: int,
        failed: int,
        payload: dict[str, Any],
        status_callback: Optional["StatusCallback"],
    ) -> None:
        self.source_message_id = source_message_id
        self.index = index
        self.total_messages = total_messages
        self.success = success
        self.failed = failed
        self.payload = payload
        self.status_callback = status_callback

        self._download_started_at = time.time()
        self._upload_started_at: float | None = None
        self._download_snapshot = TransferSnapshot("download", 0, 0, 0.0, None)
        self._upload_snapshot = TransferSnapshot("upload", 0, 0, 0.0, None)
        self._last_progress: dict[str, tuple[int, float, float]] = {}
        self._last_emit_at = 0.0

    def _snapshot(self, stage: str, current: int, total: int) -> TransferSnapshot:
        now = time.time()
        started_at = self._download_started_at if stage == "download" else (self._upload_started_at or now)
        if stage not in self._last_progress:
            self._last_progress[stage] = (current, now, 0.0)
            return TransferSnapshot(stage=stage, current=current, total=total, speed_bps=0.0, eta_seconds=None)

        previous_current, previous_at, previous_speed = self._last_progress[stage]
        delta_bytes = max(current - previous_current, 0)
        delta_time = max(now - previous_at, 1e-3)
        instant_speed = delta_bytes / delta_time if delta_bytes else previous_speed
        if previous_speed > 0 and instant_speed > 0:
            speed_bps = previous_speed * 0.65 + instant_speed * 0.35
        elif instant_speed > 0:
            speed_bps = instant_speed
        else:
            elapsed = max(now - started_at, 1e-3)
            speed_bps = current / elapsed
        self._last_progress[stage] = (current, now, speed_bps)
        eta_seconds: float | None
        if speed_bps > 0 and total > 0 and current <= total:
            eta_seconds = max((total - current) / speed_bps, 0.0)
        else:
            eta_seconds = None
        return TransferSnapshot(stage=stage, current=current, total=total, speed_bps=speed_bps, eta_seconds=eta_seconds)

    async def _emit(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit_at < 1.0:
            return
        self._last_emit_at = now

        dl = self._download_snapshot
        up = self._upload_snapshot
        dl_eta = _get_readable_time(dl.eta_seconds) if dl.eta_seconds is not None else "-"
        up_eta = _get_readable_time(up.eta_seconds) if up.eta_seconds is not None else "-"

        print(
            f"[{self.index}/{self.total_messages}] msg {self.source_message_id} "
            f"DL {dl.current}/{dl.total} ({_get_readable_file_size(dl.speed_bps)}/s eta={dl_eta}) | "
            f"UP {up.current}/{up.total} ({_get_readable_file_size(up.speed_bps)}/s eta={up_eta})"
        )

        await _save_clone_status(
            self.status_callback,
            "running",
            self.payload,
            current_index=self.index,
            total_messages=self.total_messages,
            success=self.success,
            failed=self.failed,
            current_message_id=self.source_message_id,
            transfer_stage="upload" if up.current > 0 else "download",
            download_current=dl.current,
            download_total=dl.total,
            download_speed_bps=dl.speed_bps,
            download_speed=f"{_get_readable_file_size(dl.speed_bps)}/s",
            download_eta=dl_eta,
            upload_current=up.current,
            upload_total=up.total,
            upload_speed_bps=up.speed_bps,
            upload_speed=f"{_get_readable_file_size(up.speed_bps)}/s",
            upload_eta=up_eta,
        )

    async def on_download_progress(self, current: int, total: int, *args: Any) -> None:
        self._download_snapshot = self._snapshot("download", int(current), int(total))
        await self._emit()

    async def on_upload_progress(self, current: int, total: int, *args: Any) -> None:
        if self._upload_started_at is None:
            self._upload_started_at = time.time()
        self._upload_snapshot = self._snapshot("upload", int(current), int(total))
        await self._emit()

    async def complete(self) -> None:
        await self._emit(force=True)


def _parse_ids_csv(raw: str) -> list[int]:
    values: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(f"Invalid message id: {token}") from exc
        if value <= 0:
            raise ValueError(f"Message id must be > 0: {value}")
        values.append(value)

    # Keep order deterministic and remove duplicates.
    return sorted(set(values))


def _ensure_runtime_dirs() -> None:
    HEROKU_STATE_DIR.mkdir(parents=True, exist_ok=True)


StatusCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def _save_clone_status(
    status_callback: Optional[StatusCallback],
    phase: str,
    payload: dict[str, Any],
    **extra: Any,
) -> None:
    if status_callback is None:
        return
    state = dict(payload)
    state["phase"] = phase
    state["updated_at"] = time.time()
    if "started_at" not in state and phase == "running":
        state["started_at"] = state["updated_at"]
    state.update(extra)
    await status_callback(state)


def _snapshot_path(name: str) -> Path:
    _ensure_runtime_dirs()
    return HEROKU_STATE_DIR / f"{name}.json"


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    _ensure_runtime_dirs()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_admin_ids(raw: str) -> set[int]:
    values: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.add(int(token))
        except ValueError as exc:
            raise ValueError(f"Invalid bot admin id: {token}") from exc
    return values


class MongoStateStore:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        data_source: str,
        database: str,
        collection: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.data_source = data_source
        self.database = database
        self.collection = collection

    @property
    def enabled(self) -> bool:
        return bool(
            self.base_url and self.api_key and self.data_source and self.database and self.collection
        )

    def _request_json(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(
                "MongoDB Data API settings are incomplete. Set MONGODB_DATA_API_URL, "
                "MONGODB_DATA_API_KEY, MONGODB_DATA_SOURCE, MONGODB_DATABASE, and "
                "MONGODB_COLLECTION."
            )

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/{action}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "apiKey": self.api_key,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MongoDB Data API request failed: {message}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"MongoDB Data API unavailable: {exc}") from exc

        return json.loads(raw) if raw.strip() else {}

    async def save(self, key: str, payload: dict[str, Any]) -> None:
        document = {
            "_id": key,
            "kind": "clone",
            "payload": payload,
            "updated_at": asyncio.get_running_loop().time(),
        }

        await asyncio.to_thread(
            self._request_json,
            "updateOne",
            {
                "dataSource": self.data_source,
                "database": self.database,
                "collection": self.collection,
                "filter": {"_id": key},
                "update": {"$set": document},
                "upsert": True,
            },
        )

    async def load(self, key: str) -> dict[str, Any] | None:
        response = await asyncio.to_thread(
            self._request_json,
            "findOne",
            {
                "dataSource": self.data_source,
                "database": self.database,
                "collection": self.collection,
                "filter": {"_id": key},
            },
        )
        document = response.get("document")
        if not isinstance(document, dict):
            return None
        payload = document.get("payload")
        if not isinstance(payload, dict):
            return None
        return payload


def _parse_topic_link_or_fail(label: str, link: str):
    stripped = link.strip()
    if not stripped:
        raise ValueError(f"{label} link is required")
    try:
        return parse_private_topic_link(stripped)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {label} link format. Expected: https://t.me/c/<chat>/<topic>/<message>"
        ) from exc


def _prompt_topic_link(label: str, initial_value: str) -> str:
    if initial_value.strip():
        return initial_value.strip()

    while True:
        value = input(
            f"Enter {label} topic link (https://t.me/c/<chat>/<topic>/<message>): "
        ).strip()
        if not value:
            print(f"{label} topic link is required.")
            continue
        try:
            _parse_topic_link_or_fail(label, value)
            return value
        except ValueError as exc:
            print(str(exc))


def _resolve_endpoints(
    source_link: str,
    destination_link: str,
    start_id_override: int,
) -> CloneEndpoints:
    source = _parse_topic_link_or_fail("source", source_link)
    destination = _parse_topic_link_or_fail("destination", destination_link)

    start_message_id = start_id_override if start_id_override > 0 else source.message_id
    if start_message_id <= 0:
        raise ValueError("start-id must be > 0")

    return CloneEndpoints(
        source_chat_id=source.chat_id,
        source_topic_id=source.topic_id,
        source_start_message_id=start_message_id,
        destination_chat_id=destination.chat_id,
        destination_topic_id=destination.topic_id,
    )


async def _clone_message_with_hidden_sender(
    telegram: TelegramService,
    endpoints: CloneEndpoints,
    source_message: Any,
    reporter: WzmlTransferReporter | None = None,
) -> bool:
    if source_message is None or getattr(source_message, "empty", False):
        raise RuntimeError("Source message is missing")

    classification = classify_message(source_message)
    if classification.kind == MessageKind.TEXT:
        text, entities, disable_preview = extract_text_payload(source_message)
        await telegram.send_text_to_topic(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
            text=text,
            entities=entities,
            disable_web_page_preview=disable_preview,
        )
        return True

    if classification.kind == MessageKind.DIRECT_MEDIA:
        await _download_and_upload_message(
            telegram,
            source_message,
            endpoints,
            reporter=reporter,
        )
        return True

    return False


async def _download_and_upload_message(
    telegram: TelegramService,
    source_message: Any,
    endpoints: CloneEndpoints,
    reporter: WzmlTransferReporter | None = None,
) -> None:
    if source_message is None or getattr(source_message, "empty", False):
        raise RuntimeError("Source message is missing")

    classification = classify_message(source_message)
    caption, caption_entities = extract_caption_payload(source_message)

    # Protected text-only messages cannot be downloaded; repost text directly.
    if classification.kind == MessageKind.TEXT:
        text, entities, disable_preview = extract_text_payload(source_message)
        await telegram.send_text_to_topic(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
            text=text,
            entities=entities,
            disable_web_page_preview=disable_preview,
        )
        return

    if not getattr(source_message, "media", None):
        raise RuntimeError(
            f"Restricted message type is unsupported for fallback upload: {classification.kind.value}"
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        download_path = Path(temp_dir)
        media = (
            getattr(source_message, source_message.media.value)
            if getattr(source_message, "media", None)
            else None
        )
        media_file_name = getattr(media, "file_name", "") if media is not None else ""
        if media_file_name:
            target_file = download_path / Path(media_file_name).name
        else:
            fallback_ext = ".bin"
            if getattr(source_message, "photo", None):
                fallback_ext = ".jpg"
            elif getattr(source_message, "video", None):
                fallback_ext = ".mp4"
            elif getattr(source_message, "audio", None):
                fallback_ext = ".mp3"
            elif getattr(source_message, "voice", None):
                fallback_ext = ".ogg"
            target_file = download_path / f"{source_message.id}{fallback_ext}"
        download_result = await telegram.download_media_to_path(
            source_message,
            target_file,
            progress=reporter.on_download_progress if reporter else None,
        )
        if not download_result:
            raise RuntimeError("Failed to download restricted media")

        downloaded_file = Path(download_result)
        if downloaded_file.is_dir():
            files = [p for p in downloaded_file.rglob("*") if p.is_file() and p.suffix.lower() != ".temp"]
            if not files:
                files = [p for p in downloaded_file.rglob("*") if p.is_file()]
            if not files:
                raise RuntimeError(
                    f"Failed to find downloaded file inside {downloaded_file}"
                )
            downloaded_file = max(files, key=lambda p: p.stat().st_mtime)

        if downloaded_file.suffix.lower() == ".temp":
            without_temp = downloaded_file.with_suffix("")
            if without_temp.exists():
                downloaded_file = without_temp
            else:
                try:
                    downloaded_file.rename(without_temp)
                    downloaded_file = without_temp
                except OSError:
                    pass

        if not downloaded_file.exists() or not downloaded_file.is_file():
            raise RuntimeError(f"Downloaded media path is invalid: {downloaded_file}")
        if downloaded_file.stat().st_size <= 0:
            downloaded_file.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded media file is empty: {downloaded_file}")

        await telegram.send_downloaded_media_to_topic(
            chat_id=endpoints.destination_chat_id,
            topic_id=endpoints.destination_topic_id,
            source_message=source_message,
            file_path=downloaded_file,
            caption=caption,
            caption_entities=caption_entities,
            progress=reporter.on_upload_progress if reporter else None,
        )
        if reporter is not None:
            await reporter.complete()


async def _clone_restricted_message(
    telegram: TelegramService,
    endpoints: CloneEndpoints,
    source_message: Any,
    reporter: WzmlTransferReporter | None = None,
) -> None:
    classification = classify_message(source_message)

    if classification.kind == MessageKind.TEXT:
        text, entities, disable_preview = extract_text_payload(source_message)
        await telegram.send_text_to_topic(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
            text=text,
            entities=entities,
            disable_web_page_preview=disable_preview,
        )
        return

    await _download_and_upload_message(telegram, source_message, endpoints, reporter=reporter)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clone messages from a source Telegram topic to a destination topic using "
            "private topic links."
        )
    )
    parser.add_argument(
        "--source-link",
        default="",
        help="Source topic link: https://t.me/c/<chat>/<topic>/<message>",
    )
    parser.add_argument(
        "--destination-link",
        default="",
        help="Destination topic link: https://t.me/c/<chat>/<topic>/<message>",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--start-id",
        type=int,
        default=0,
        help=(
            "Source message id to start from. Default is the message id embedded in "
            "--source-link."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of messages to clone (0 = all from start-id)",
    )
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=0.35,
        help="Delay between cloned messages in seconds (default: 0.35)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Bulk fetch batch size while listing source messages (default: 50)",
    )
    parser.add_argument(
        "--message-ids",
        default="",
        help=(
            "Clone only these source message IDs (comma-separated). "
            "When set, --start-id and --limit are ignored."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be cloned without sending anything",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue cloning when a message fails",
    )
    parser.add_argument(
        "--hide-sender-name",
        action="store_true",
        help=(
            "Clone by reposting supported messages (text/media) so original sender "
            "attribution is not shown"
        ),
    )
    parser.add_argument(
        "--bot-mode",
        action="store_true",
        help="Run as a Telegram bot for Heroku control instead of the CLI flow",
    )
    parser.add_argument(
        "--bot-token",
        default="",
        help="Bot token for bot mode. Defaults to HEROKU_BOT_TOKEN.",
    )
    parser.add_argument(
        "--bot-admin-ids",
        default="",
        help=(
            "Comma-separated Telegram user IDs allowed to control the bot. "
            "Defaults to BOT_ADMIN_USER_IDS."
        ),
    )
    return parser



async def _clone_topic_messages(
    telegram: TelegramService,
    endpoints: CloneEndpoints,
    *,
    limit: int,
    delay_sec: float,
    batch_size: int,
    explicit_message_ids: list[int],
    dry_run: bool,
    continue_on_error: bool,
    hide_sender_name: bool,
    payload: dict[str, Any],
    status_callback: Optional[StatusCallback] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> tuple[int, int]:
    if batch_size <= 0:
        raise RuntimeError("batch-size must be > 0")
    if delay_sec < 0:
        raise RuntimeError("delay-sec must be >= 0")
    if limit < 0:
        raise RuntimeError("limit must be >= 0")

    if explicit_message_ids:
        source_ids = explicit_message_ids
    else:
        source_ids = await telegram.list_topic_message_ids(
            chat_id=endpoints.source_chat_id,
            topic_id=endpoints.source_topic_id,
            start_from_message_id=endpoints.source_start_message_id,
            batch_size=batch_size,
        )
        source_ids = sorted(set(source_ids))
        if limit > 0:
            source_ids = source_ids[:limit]

    if not source_ids:
        await _save_clone_status(
            status_callback,
            "running",
            payload,
            current_index=0,
            total_messages=0,
            success=0,
            failed=0,
            current_message_id=None,
        )
        return (0, 0)

    success = 0
    failed = 0
    total_messages = len(source_ids)
    await _save_clone_status(
        status_callback,
        "running",
        payload,
        current_index=0,
        total_messages=total_messages,
        success=0,
        failed=0,
        current_message_id=None,
    )

    for index, source_message_id in enumerate(source_ids, start=1):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("Clone cancelled by user")

        source_message = None
        try:
            if dry_run:
                print(
                    f"[DRY RUN] {index}/{total_messages} copy "
                    f"{endpoints.source_chat_id}:{source_message_id} -> "
                    f"{endpoints.destination_chat_id}:{endpoints.destination_topic_id}"
                )
            else:
                copied_with_hidden_sender = False
                if hide_sender_name:
                    source_message = await telegram.get_message(
                        endpoints.source_chat_id,
                        source_message_id,
                    )
                    reporter = WzmlTransferReporter(
                        source_message_id=source_message_id,
                        index=index,
                        total_messages=total_messages,
                        success=success,
                        failed=failed,
                        payload=payload,
                        status_callback=status_callback,
                    )
                    copied_with_hidden_sender = await _clone_message_with_hidden_sender(
                        telegram,
                        endpoints,
                        source_message,
                        reporter=reporter,
                    )

                if not copied_with_hidden_sender:
                    try:
                        await telegram.copy_message_to_topic(
                            chat_id=endpoints.destination_chat_id,
                            from_chat_id=endpoints.source_chat_id,
                            topic_id=endpoints.destination_topic_id,
                            message_id=source_message_id,
                        )
                    except ChatForwardsRestricted:
                        if source_message is None:
                            source_message = await telegram.get_message(
                                endpoints.source_chat_id,
                                source_message_id,
                            )
                        reporter = WzmlTransferReporter(
                            source_message_id=source_message_id,
                            index=index,
                            total_messages=total_messages,
                            success=success,
                            failed=failed,
                            payload=payload,
                            status_callback=status_callback,
                        )
                        await _clone_restricted_message(
                            telegram,
                            endpoints,
                            source_message,
                            reporter=reporter,
                        )
                print(f"[{index}/{total_messages}] cloned source message {source_message_id}")
            success += 1
            await _save_clone_status(
                status_callback,
                "running",
                payload,
                current_index=index,
                total_messages=total_messages,
                success=success,
                failed=failed,
                current_message_id=source_message_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            logging.getLogger("topic_clone").exception(
                "clone message failed",
                extra={
                    "event": "clone_message_failure",
                    "message_id": source_message_id,
                    "error": str(exc),
                },
            )
            await _save_clone_status(
                status_callback,
                "running",
                payload,
                current_index=index,
                total_messages=total_messages,
                success=success,
                failed=failed,
                current_message_id=source_message_id,
                error=str(exc),
            )
            print(f"[{index}/{total_messages}] failed for message {source_message_id}: {exc}")
            if not continue_on_error:
                raise

        has_more = index < total_messages
        if has_more and not dry_run and delay_sec > 0:
            await asyncio.sleep(delay_sec)

    return (success, failed)


def _build_clone_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source_link": args.source_link,
        "destination_link": args.destination_link,
        "config_path": args.config,
        "start_id": args.start_id,
        "limit": args.limit,
        "delay_sec": args.delay_sec,
        "batch_size": args.batch_size,
        "message_ids": args.message_ids,
        "dry_run": args.dry_run,
        "continue_on_error": args.continue_on_error,
        "hide_sender_name": args.hide_sender_name,
    }


def _chat_display_name(chat) -> str:
    for attr in ("title", "username", "first_name"):
        value = getattr(chat, attr, None)
        if value:
            return str(value)
    return str(getattr(chat, "id", "unknown"))


async def _build_endpoint_labels(
    telegram: TelegramService,
    endpoints: CloneEndpoints,
) -> dict[str, Any]:
    source_chat, destination_chat = await asyncio.gather(
        telegram.get_chat(endpoints.source_chat_id),
        telegram.get_chat(endpoints.destination_chat_id),
    )
    source_topic_title, destination_topic_title = await asyncio.gather(
        telegram.get_forum_topic_title(endpoints.source_chat_id, endpoints.source_topic_id),
        telegram.get_forum_topic_title(endpoints.destination_chat_id, endpoints.destination_topic_id),
    )

    return {
        "source_chat_id": endpoints.source_chat_id,
        "source_chat_title": _chat_display_name(source_chat),
        "source_topic_id": endpoints.source_topic_id,
        "source_topic_title": source_topic_title or f"Topic {endpoints.source_topic_id}",
        "destination_chat_id": endpoints.destination_chat_id,
        "destination_chat_title": _chat_display_name(destination_chat),
        "destination_topic_id": endpoints.destination_topic_id,
        "destination_topic_title": destination_topic_title or f"Topic {endpoints.destination_topic_id}",
    }


async def run_clone(
    source_link: str,
    destination_link: str,
    config_path: str,
    start_id: int,
    limit: int,
    delay_sec: float,
    batch_size: int,
    message_ids: str,
    dry_run: bool,
    continue_on_error: bool,
    hide_sender_name: bool,
    status_callback: Optional[StatusCallback] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> tuple[int, int]:
    try:
        settings, _ = load_settings(config_path)
    except ConfigError as exc:
        raise RuntimeError(f"Configuration error: {exc}") from exc

    endpoints = _resolve_endpoints(source_link, destination_link, start_id)
    explicit_ids = _parse_ids_csv(message_ids) if message_ids.strip() else []

    telegram = TelegramService(settings, logger=logging.getLogger("topic_clone"), receive_updates=False)

    try:
        await telegram.start()

        # Validate source and destination topics before the clone loop.
        await telegram.get_topic_anchor(endpoints.source_chat_id, endpoints.source_topic_id)
        await telegram.get_topic_anchor(endpoints.destination_chat_id, endpoints.destination_topic_id)
        endpoint_labels = await _build_endpoint_labels(telegram, endpoints)

        return await _clone_topic_messages(
            telegram,
            endpoints,
            limit=limit,
            delay_sec=delay_sec,
            batch_size=batch_size,
            explicit_message_ids=explicit_ids,
            dry_run=dry_run,
            continue_on_error=continue_on_error,
            hide_sender_name=hide_sender_name,
            payload={
                "source_link": source_link,
                "destination_link": destination_link,
                **endpoint_labels,
                "config_path": config_path,
                "start_id": start_id,
                "limit": limit,
                "delay_sec": delay_sec,
                "batch_size": batch_size,
                "message_ids": message_ids,
                "dry_run": dry_run,
                "continue_on_error": continue_on_error,
                "hide_sender_name": hide_sender_name,
            },
            status_callback=status_callback,
            cancel_event=cancel_event,
        )
    finally:
        await telegram.stop()


async def _run_clone_bot(args: argparse.Namespace) -> int:
    _ensure_runtime_dirs()

    bot_token = (args.bot_token or os.getenv("HEROKU_BOT_TOKEN", "")).strip()
    if not bot_token:
        raise RuntimeError("Bot mode requires HEROKU_BOT_TOKEN or --bot-token")

    admin_ids = _parse_admin_ids(args.bot_admin_ids or os.getenv("BOT_ADMIN_USER_IDS", ""))
    if not admin_ids:
        raise RuntimeError("Bot mode requires BOT_ADMIN_USER_IDS or --bot-admin-ids")

    api_id = int(os.getenv("TG_API_ID", "0") or 0)
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if api_id == 0 or not api_hash:
        raise RuntimeError("Bot mode requires TG_API_ID and TG_API_HASH")

    store = MongoStateStore(
        MONGODB_DATA_API_URL,
        MONGODB_DATA_API_KEY,
        MONGODB_DATA_SOURCE,
        MONGODB_DATABASE,
        MONGODB_COLLECTION,
    )

    bot = Client(
        name="clone-topic-bot",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    active_clone_task: asyncio.Task | None = None
    active_cancel_event: asyncio.Event | None = None

    async def _authorized(message) -> bool:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        return isinstance(user_id, int) and user_id in admin_ids

    async def _run_spec(message, payload: dict[str, Any], label: str) -> None:
        nonlocal active_clone_task, active_cancel_event
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        active_cancel_event = asyncio.Event()
        active_clone_task = asyncio.current_task()

        async def _save_state(phase: str, extra: dict[str, Any] | None = None) -> None:
            state = dict(payload)
            state["phase"] = phase
            state["updated_at"] = time.time()
            state["payload"] = payload
            if phase == "running":
                state["started_at"] = state.get("started_at", state["updated_at"])
            if extra:
                state.update(extra)
            try:
                await store.save(f"clone:{label}", state)
            except Exception:
                _write_json_file(_snapshot_path(f"clone_{label}"), state)

        await _save_state("queued")
        status = await message.reply_text("Clone started.")
        try:
            success, failed = await run_clone(
                source_link=payload["source_link"],
                destination_link=payload["destination_link"],
                config_path=payload["config_path"],
                start_id=int(payload["start_id"]),
                limit=int(payload["limit"]),
                delay_sec=float(payload["delay_sec"]),
                batch_size=int(payload["batch_size"]),
                message_ids=payload["message_ids"],
                dry_run=bool(payload["dry_run"]),
                continue_on_error=bool(payload["continue_on_error"]),
                hide_sender_name=bool(payload["hide_sender_name"]),
                status_callback=lambda state: _save_state("running", state),
                cancel_event=active_cancel_event,
            )
        except asyncio.CancelledError:
            await _save_state("cancelled", {"error": "Cancelled by user"})
            await status.edit_text("Clone cancelled.")
            raise
        except Exception as exc:
            await _save_state("failed", {"error": str(exc)})
            await status.edit_text(f"Clone failed: {exc}")
            raise
        else:
            await _save_state("completed", {"success": success, "failed": failed})
            await status.edit_text(f"Clone complete. Success: {success}, Failed: {failed}")
        finally:
            active_clone_task = None
            active_cancel_event = None

        return

    @bot.on_message(filters.private & filters.command("clone", prefixes="/"))
    async def clone_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        if command_text.lower() in {"last", "resume"}:
            payload = await store.load("clone:last") or _read_json_file(_snapshot_path("clone_last"))
            if payload is None:
                await message.reply_text("No saved clone profile found.")
                return
        else:
            try:
                parsed = _build_parser().parse_args(shlex.split(command_text))
            except SystemExit:
                await message.reply_text(
                    "Usage: /clone --source-link <link> --destination-link <link> [--start-id N] [--limit N] [--delay-sec S] [--batch-size N] [--message-ids 1,2,3] [--dry-run] [--continue-on-error] [--hide-sender-name]"
                )
                return
            payload = _build_clone_payload(parsed)

        await _run_spec(message, payload, "last")

    @bot.on_message(filters.private & filters.command("status", prefixes="/"))
    async def status_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        payload = await store.load("clone:last") or _read_json_file(_snapshot_path("clone_last"))
        if payload is None:
            await message.reply_text("No saved clone state.")
            return
        await message.reply_text(json.dumps(payload, indent=2, sort_keys=True))

    @bot.on_message(filters.private & filters.command("cancel", prefixes="/"))
    async def cancel_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        if active_cancel_event is None or active_cancel_event.is_set():
            await message.reply_text("No active clone task to cancel.")
            return
        active_cancel_event.set()
        if active_clone_task is not None and not active_clone_task.done():
            active_clone_task.cancel()
        await message.reply_text("Clone cancellation requested.")

    @bot.on_message(filters.private & filters.command("restart", prefixes="/"))
    async def restart_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        await message.reply_text("Restarting bot now...")
        try:
            await bot.stop()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable, *sys.argv])

    await bot.start()
    print("Clone bot is running.")
    await asyncio.Event().wait()
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.bot_mode:
        try:
            asyncio.run(_run_clone_bot(args))
        except KeyboardInterrupt:
            print("Clone bot stopped by user.")
            return 130
        except Exception as exc:
            print(f"Clone bot failed: {exc}")
            return 1
        return 0

    source_link = _prompt_topic_link("source", args.source_link)
    destination_link = _prompt_topic_link("destination", args.destination_link)

    try:
        success, failed = asyncio.run(
            run_clone(
                source_link=source_link,
                destination_link=destination_link,
                config_path=args.config,
                start_id=args.start_id,
                limit=args.limit,
                delay_sec=args.delay_sec,
                batch_size=args.batch_size,
                message_ids=args.message_ids,
                dry_run=args.dry_run,
                continue_on_error=args.continue_on_error,
                hide_sender_name=args.hide_sender_name,
            )
        )
    except KeyboardInterrupt:
        print("Clone cancelled by user.")
        return 130
    except Exception as exc:
        print(f"Clone failed: {exc}")
        return 1

    print(f"Clone complete. Success: {success}, Failed: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
