from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
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
from topic_utils import (
    build_private_topic_link,
    parse_private_chat_message_link,
    parse_private_topic_link,
)


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
    source_topic_id: int | None
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
        skipped: int,
        payload: dict[str, Any],
        status_callback: Optional["StatusCallback"],
        message_type: str | None = None,
        file_name: str | None = None,
    ) -> None:
        self.source_message_id = source_message_id
        self.index = index
        self.total_messages = total_messages
        self.success = success
        self.failed = failed
        self.skipped = skipped
        self.payload = payload
        self.status_callback = status_callback
        self.message_type = message_type
        self.file_name = file_name

        self._download_started_at = time.time()
        self._upload_started_at: float | None = None
        self._download_snapshot = TransferSnapshot("download", 0, 0, 0.0, None)
        self._upload_snapshot = TransferSnapshot("upload", 0, 0, 0.0, None)
        self._last_progress: dict[str, tuple[int, float, float]] = {}
        self._last_emit_at = 0.0
        self._last_stage = ""

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
        active_stage = "upload" if self._upload_snapshot.current > 0 else "download"
        stage_changed = active_stage != self._last_stage
        if not force and not stage_changed and now - self._last_emit_at < 0.5:
            return
        self._last_emit_at = now
        self._last_stage = active_stage

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
            skipped=self.skipped,
            current_message_id=self.source_message_id,
            transfer_stage=active_stage,
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
            current_message_type=self.message_type,
            current_file_name=self.file_name,
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


class SkippedMessageError(RuntimeError):
    """Raised for Telegram messages that should be skipped without aborting a clone."""


def _is_uncopyable_message_error(exc: Exception) -> bool:
    return isinstance(exc, ValueError) and "can't copy this message" in str(exc).lower()


def _unsupported_message_reason(message: Any | None) -> str:
    if message is None:
        return "source message is unavailable"
    if getattr(message, "empty", False):
        return "source message is empty or deleted"
    service = getattr(message, "service", None)
    if service is not None:
        return f"service message skipped: {service}"
    classification = classify_message(message)
    if classification.kind == MessageKind.UNSUPPORTED:
        return classification.reason
    return "message has no downloadable media"


def _is_skippable_unsupported_message(message: Any | None) -> bool:
    if message is None or getattr(message, "empty", False):
        return True
    if getattr(message, "service", None) is not None:
        return True
    return classify_message(message).kind == MessageKind.UNSUPPORTED


def _message_media_type(message: Any | None) -> str:
    if message is None or getattr(message, "empty", False):
        return "Unknown"
    if getattr(message, "video", None):
        return "Video"
    if getattr(message, "animation", None):
        return "Video"
    document = getattr(message, "document", None)
    if document:
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
        file_name = str(getattr(document, "file_name", "") or "").lower()
        if mime_type == "application/pdf" or file_name.endswith(".pdf"):
            return "PDF"
        if mime_type.startswith("video/"):
            return "Video"
        return "Document"
    if getattr(message, "photo", None):
        return "Photo"
    if getattr(message, "audio", None):
        return "Audio"
    if getattr(message, "voice", None):
        return "Voice"
    if getattr(message, "sticker", None):
        return "Sticker"
    if getattr(message, "text", None):
        return "Text"
    return "Unsupported"


def _message_file_name(message: Any | None) -> str | None:
    if message is None:
        return None
    for attr in ("document", "video", "animation", "audio"):
        media = getattr(message, attr, None)
        file_name = str(getattr(media, "file_name", "") or "").strip() if media else ""
        if file_name:
            return file_name
    return None


def _sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(value or ""))
    cleaned = cleaned.replace("\x00", "")
    return cleaned.strip().strip(".")


def _split_name_and_extension(file_name: str) -> tuple[str, str]:
    path = Path(file_name)
    extension = "".join(path.suffixes)
    if extension and file_name.endswith(extension):
        stem = file_name[: -len(extension)]
    else:
        stem = file_name
        extension = ""
    return stem or "file", extension


def _apply_filename_affixes_to_name(file_name: str, prefix: str, suffix: str) -> str:
    safe_original = _sanitize_filename_component(file_name) or "file"
    stem, extension = _split_name_and_extension(safe_original)
    safe_prefix = _sanitize_filename_component(prefix)
    safe_suffix = _sanitize_filename_component(suffix)
    updated_stem = f"{safe_prefix}{stem}{safe_suffix}".strip() or stem
    return f"{updated_stem}{extension}"


def _build_upload_file_path(
    downloaded_file: Path,
    source_message: Any,
    filename_prefix: str,
    filename_suffix: str,
) -> Path:
    if not filename_prefix and not filename_suffix:
        return downloaded_file

    source_name = _message_file_name(source_message) or downloaded_file.name
    target_name = _apply_filename_affixes_to_name(source_name, filename_prefix, filename_suffix)
    target_path = downloaded_file.with_name(target_name)
    if target_path == downloaded_file:
        return downloaded_file

    unique_target = target_path
    counter = 1
    while unique_target.exists():
        stem, extension = _split_name_and_extension(target_name)
        unique_target = downloaded_file.with_name(f"{stem}_{counter}{extension}")
        counter += 1

    downloaded_file.rename(unique_target)
    return unique_target


def _apply_text_affixes(text: str, prefix: str, suffix: str) -> str:
    raw = str(text or "")
    if not prefix and not suffix:
        return raw
    return f"{prefix}{raw}{suffix}"


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


def _parse_source_link_or_fail(link: str):
    stripped = link.strip()
    if not stripped:
        raise ValueError("source link is required")
    try:
        return parse_private_topic_link(stripped)
    except ValueError:
        pass
    try:
        return parse_private_chat_message_link(stripped)
    except ValueError as exc:
        raise ValueError(
            "Invalid source link format. Expected: "
            "https://t.me/c/<chat>/<topic>/<message> or https://t.me/c/<chat>/<message>"
        ) from exc


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
        expected = "https://t.me/c/<chat>/<topic>/<message>"
        if label == "source":
            expected += " or https://t.me/c/<chat>/<message>"
        prompt_label = "source link" if label == "source" else f"{label} topic link"
        value = input(f"Enter {prompt_label} ({expected}): ").strip()
        if not value:
            print(f"{prompt_label.capitalize()} is required.")
            continue
        try:
            if label == "source":
                _parse_source_link_or_fail(value)
            else:
                _parse_topic_link_or_fail(label, value)
            return value
        except ValueError as exc:
            print(str(exc))


def _resolve_endpoints(
    source_link: str,
    destination_link: str,
    start_id_override: int,
) -> CloneEndpoints:
    source = _parse_source_link_or_fail(source_link)
    destination = _parse_topic_link_or_fail("destination", destination_link)

    start_message_id = start_id_override if start_id_override > 0 else source.message_id
    if start_message_id <= 0:
        raise ValueError("start-id must be > 0")

    return CloneEndpoints(
        source_chat_id=source.chat_id,
        source_topic_id=getattr(source, "topic_id", None),
        source_start_message_id=start_message_id,
        destination_chat_id=destination.chat_id,
        destination_topic_id=destination.topic_id,
    )


async def _clone_message_with_hidden_sender(
    telegram: TelegramService,
    endpoints: CloneEndpoints,
    source_message: Any,
    text_prefix: str = "",
    text_suffix: str = "",
    reporter: WzmlTransferReporter | None = None,
) -> Any | None:
    if source_message is None or getattr(source_message, "empty", False):
        raise RuntimeError("Source message is missing")

    classification = classify_message(source_message)
    if classification.kind == MessageKind.TEXT:
        text, entities, disable_preview = extract_text_payload(source_message)
        text = _apply_text_affixes(text, text_prefix, text_suffix)
        return await telegram.send_text_to_topic(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
            text=text,
            entities=entities,
            disable_web_page_preview=disable_preview,
        )

    # For media/documents, prefer Telegram's native copy path first. Only fall
    # back to download+upload later when copy_message raises
    # CHAT_FORWARDS_RESTRICTED.
    return None


async def _download_and_upload_message(
    telegram: TelegramService,
    source_message: Any,
    endpoints: CloneEndpoints,
    filename_prefix: str = "",
    filename_suffix: str = "",
    text_prefix: str = "",
    text_suffix: str = "",
    reporter: WzmlTransferReporter | None = None,
) -> Any:
    if source_message is None or getattr(source_message, "empty", False):
        raise RuntimeError("Source message is missing")

    classification = classify_message(source_message)
    caption, caption_entities = extract_caption_payload(source_message)

    # Protected text-only messages cannot be downloaded; repost text directly.
    if classification.kind == MessageKind.TEXT:
        text, entities, disable_preview = extract_text_payload(source_message)
        text = _apply_text_affixes(text, text_prefix, text_suffix)
        return await telegram.send_text_to_topic(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
            text=text,
            entities=entities,
            disable_web_page_preview=disable_preview,
        )

    if not getattr(source_message, "media", None):
        reason = _unsupported_message_reason(source_message)
        if classification.kind == MessageKind.UNSUPPORTED:
            raise SkippedMessageError(reason)
        raise RuntimeError(f"Restricted message has no downloadable media: {reason}")

    with tempfile.TemporaryDirectory() as temp_dir:
        download_path = Path(temp_dir)
        media = (
            getattr(source_message, source_message.media.value)
            if getattr(source_message, "media", None)
            else None
        )
        media_file_name = getattr(media, "file_name", "") if media is not None else ""
        if media_file_name:
            safe_media_file_name = Path(str(media_file_name).replace("\x00", "")).name.strip()
            target_file = download_path / (safe_media_file_name or f"{source_message.id}.bin")
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

        downloaded_file = await _resolve_downloaded_media_file(download_result)
        upload_file = _build_upload_file_path(
            downloaded_file,
            source_message,
            filename_prefix=filename_prefix,
            filename_suffix=filename_suffix,
        )

        sent_message = await telegram.send_downloaded_media_to_topic(
            chat_id=endpoints.destination_chat_id,
            topic_id=endpoints.destination_topic_id,
            source_message=source_message,
            file_path=upload_file,
            caption=caption,
            caption_entities=caption_entities,
            progress=reporter.on_upload_progress if reporter else None,
        )
        if reporter is not None:
            await reporter.complete()
        return sent_message


async def _resolve_downloaded_media_file(download_result: str | Path) -> Path:
    downloaded_file = Path(download_result)

    if downloaded_file.is_dir():
        def _candidate_files() -> list[Path]:
            files = [p for p in downloaded_file.rglob("*") if p.is_file() and p.suffix.lower() != ".temp"]
            if files:
                return files
            return [p for p in downloaded_file.rglob("*") if p.is_file()]

        files = await asyncio.to_thread(_candidate_files)
        if not files:
            raise RuntimeError(f"Failed to find downloaded file inside {downloaded_file}")
        downloaded_file = max(files, key=_safe_mtime)

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

    for attempt in range(3):
        try:
            if downloaded_file.exists() and downloaded_file.is_file():
                size = downloaded_file.stat().st_size
                if size > 0:
                    return downloaded_file
                if attempt == 2:
                    downloaded_file.unlink(missing_ok=True)
                    raise RuntimeError(f"Downloaded media file is empty: {downloaded_file}")
        except OSError as exc:
            if attempt == 2:
                raise RuntimeError(f"Downloaded media path is invalid: {downloaded_file}") from exc
        await asyncio.sleep(0.2)

    raise RuntimeError(f"Downloaded media path is invalid: {downloaded_file}")


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


async def _clone_restricted_message(
    telegram: TelegramService,
    endpoints: CloneEndpoints,
    source_message: Any,
    filename_prefix: str = "",
    filename_suffix: str = "",
    text_prefix: str = "",
    text_suffix: str = "",
    reporter: WzmlTransferReporter | None = None,
) -> Any:
    classification = classify_message(source_message)

    if classification.kind == MessageKind.TEXT:
        text, entities, disable_preview = extract_text_payload(source_message)
        text = _apply_text_affixes(text, text_prefix, text_suffix)
        return await telegram.send_text_to_topic(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
            text=text,
            entities=entities,
            disable_web_page_preview=disable_preview,
        )

    return await _download_and_upload_message(
        telegram,
        source_message,
        endpoints,
        filename_prefix=filename_prefix,
        filename_suffix=filename_suffix,
        text_prefix=text_prefix,
        text_suffix=text_suffix,
        reporter=reporter,
    )


def _build_destination_message_link(endpoints: CloneEndpoints, destination_message_id: int | None) -> str | None:
    if not destination_message_id:
        return None
    return build_private_topic_link(
        endpoints.destination_chat_id,
        endpoints.destination_topic_id,
        destination_message_id,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clone messages from a source Telegram topic or channel to a destination "
            "topic using private Telegram links."
        )
    )
    parser.add_argument(
        "--source-link",
        default="",
        help=(
            "Source topic or channel message link: "
            "https://t.me/c/<chat>/<topic>/<message> or https://t.me/c/<chat>/<message>"
        ),
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
        "--filename-prefix",
        default="",
        help="Prefix added to uploaded media filenames (default: none)",
    )
    parser.add_argument(
        "--filename-suffix",
        default="",
        help="Suffix added to uploaded media filenames (default: none)",
    )
    parser.add_argument(
        "--text-prefix",
        default="",
        help="Prefix added to cloned text messages (default: none)",
    )
    parser.add_argument(
        "--text-suffix",
        default="",
        help="Suffix added to cloned text messages (default: none)",
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
    filename_prefix: str,
    filename_suffix: str,
    text_prefix: str,
    text_suffix: str,
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
    elif endpoints.source_topic_id is None:
        source_ids = await telegram.list_chat_message_ids(
            chat_id=endpoints.source_chat_id,
            start_from_message_id=endpoints.source_start_message_id,
            batch_size=batch_size,
        )
        source_ids = sorted(set(source_ids))
        if limit > 0:
            source_ids = source_ids[:limit]
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
            skipped=0,
            current_message_id=None,
        )
        return (0, 0)

    success = 0
    failed = 0
    skipped = 0
    total_messages = len(source_ids)
    progress_state: dict[str, Any] = {
        "current_index": 0,
        "total_messages": total_messages,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "current_message_id": None,
    }

    previous_flood_wait_callback = telegram.flood_wait_callback

    async def _report_flood_wait(wait: dict[str, object]) -> None:
        wait_seconds = float(wait.get("wait_seconds") or 0)
        wait_until = float(wait.get("wait_until") or (time.time() + wait_seconds))
        await _save_clone_status(
            status_callback,
            "running",
            payload,
            **progress_state,
            flood_wait_operation=str(wait.get("operation") or "telegram"),
            flood_wait_seconds=wait_seconds,
            flood_wait_until=wait_until,
        )

    telegram.flood_wait_callback = _report_flood_wait
    await _save_clone_status(
        status_callback,
        "running",
        payload,
        current_index=0,
        total_messages=total_messages,
        success=0,
        failed=0,
        skipped=0,
        current_message_id=None,
    )

    try:
        for index, source_message_id in enumerate(source_ids, start=1):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Clone cancelled by user")

            source_message = None
            destination_message = None
            used_restricted_media_fallback = False
            try:
                progress_state.update(
                    {
                        "current_index": index,
                        "total_messages": total_messages,
                        "success": success,
                        "failed": failed,
                        "skipped": skipped,
                        "current_message_id": source_message_id,
                    }
                )
                if not dry_run and source_message is None:
                    try:
                        source_message = await telegram.get_message(
                            endpoints.source_chat_id,
                            source_message_id,
                        )
                    except Exception:
                        logging.getLogger("topic_clone").debug(
                            "source message metadata lookup failed",
                            exc_info=True,
                            extra={
                                "event": "source_message_metadata_lookup_failed",
                                "message_id": source_message_id,
                            },
                        )

                current_message_type = _message_media_type(source_message)
                current_file_name = _message_file_name(source_message)
                if current_file_name and (filename_prefix or filename_suffix):
                    current_file_name = _apply_filename_affixes_to_name(
                        current_file_name,
                        filename_prefix,
                        filename_suffix,
                    )
                progress_state.update(
                    {
                        "current_message_type": current_message_type,
                        "current_file_name": current_file_name,
                    }
                )
                await _save_clone_status(
                    status_callback,
                    "running",
                    payload,
                    current_index=index,
                    total_messages=total_messages,
                    success=success,
                    failed=failed,
                    skipped=skipped,
                    current_message_id=source_message_id,
                    current_message_type=current_message_type,
                    current_file_name=current_file_name,
                )

                if dry_run:
                    print(
                        f"[DRY RUN] {index}/{total_messages} copy "
                        f"{endpoints.source_chat_id}:{source_message_id} -> "
                        f"{endpoints.destination_chat_id}:{endpoints.destination_topic_id}"
                    )
                else:
                    copied_with_hidden_sender = None
                    if hide_sender_name:
                        if source_message is None:
                            source_message = await telegram.get_message(
                                endpoints.source_chat_id,
                                source_message_id,
                            )
                            current_message_type = _message_media_type(source_message)
                            current_file_name = _message_file_name(source_message)
                            if current_file_name and (filename_prefix or filename_suffix):
                                current_file_name = _apply_filename_affixes_to_name(
                                    current_file_name,
                                    filename_prefix,
                                    filename_suffix,
                                )
                        reporter = WzmlTransferReporter(
                            source_message_id=source_message_id,
                            index=index,
                            total_messages=total_messages,
                            success=success,
                            failed=failed,
                            skipped=skipped,
                            payload=payload,
                            status_callback=status_callback,
                            message_type=current_message_type,
                            file_name=current_file_name,
                        )
                        copied_with_hidden_sender = await _clone_message_with_hidden_sender(
                            telegram,
                            endpoints,
                            source_message,
                            text_prefix=text_prefix,
                            text_suffix=text_suffix,
                            reporter=reporter,
                        )

                    if copied_with_hidden_sender is not None:
                        destination_message = copied_with_hidden_sender
                    else:
                        force_customization = bool(
                            filename_prefix or filename_suffix or text_prefix or text_suffix
                        )
                        force_text_repost = bool(text_prefix or text_suffix) and current_message_type == "Text"
                        if force_text_repost and source_message is None:
                            source_message = await telegram.get_message(
                                endpoints.source_chat_id,
                                source_message_id,
                            )
                            current_message_type = _message_media_type(source_message)
                            current_file_name = _message_file_name(source_message)
                            if current_file_name and (filename_prefix or filename_suffix):
                                current_file_name = _apply_filename_affixes_to_name(
                                    current_file_name,
                                    filename_prefix,
                                    filename_suffix,
                                )
                        if force_text_repost and _is_skippable_unsupported_message(source_message):
                            raise SkippedMessageError(_unsupported_message_reason(source_message))
                        if force_text_repost:
                            reporter = WzmlTransferReporter(
                                source_message_id=source_message_id,
                                index=index,
                                total_messages=total_messages,
                                success=success,
                                failed=failed,
                                skipped=skipped,
                                payload=payload,
                                status_callback=status_callback,
                                message_type=current_message_type,
                                file_name=current_file_name,
                            )
                            destination_message = await _clone_restricted_message(
                                telegram,
                                endpoints,
                                source_message,
                                filename_prefix=filename_prefix,
                                filename_suffix=filename_suffix,
                                text_prefix=text_prefix,
                                text_suffix=text_suffix,
                                reporter=reporter,
                            )
                        elif force_customization:
                            used_restricted_media_fallback = True
                            if source_message is None:
                                source_message = await telegram.get_message(
                                    endpoints.source_chat_id,
                                    source_message_id,
                                )
                            if _is_skippable_unsupported_message(source_message):
                                raise SkippedMessageError(_unsupported_message_reason(source_message))
                            reporter = WzmlTransferReporter(
                                source_message_id=source_message_id,
                                index=index,
                                total_messages=total_messages,
                                success=success,
                                failed=failed,
                                skipped=skipped,
                                payload=payload,
                                status_callback=status_callback,
                                message_type=current_message_type,
                                file_name=current_file_name,
                            )
                            destination_message = await _clone_restricted_message(
                                telegram,
                                endpoints,
                                source_message,
                                filename_prefix=filename_prefix,
                                filename_suffix=filename_suffix,
                                text_prefix=text_prefix,
                                text_suffix=text_suffix,
                                reporter=reporter,
                            )
                        else:
                            try:
                                destination_message = await telegram.copy_message_to_topic(
                                    chat_id=endpoints.destination_chat_id,
                                    from_chat_id=endpoints.source_chat_id,
                                    topic_id=endpoints.destination_topic_id,
                                    message_id=source_message_id,
                                )
                            except Exception as exc:
                                if not isinstance(exc, ChatForwardsRestricted) and not _is_uncopyable_message_error(exc):
                                    raise
                                used_restricted_media_fallback = True
                                if source_message is None:
                                    source_message = await telegram.get_message(
                                        endpoints.source_chat_id,
                                        source_message_id,
                                    )
                                if _is_skippable_unsupported_message(source_message):
                                    raise SkippedMessageError(_unsupported_message_reason(source_message))
                                reporter = WzmlTransferReporter(
                                    source_message_id=source_message_id,
                                    index=index,
                                    total_messages=total_messages,
                                    success=success,
                                    failed=failed,
                                    skipped=skipped,
                                    payload=payload,
                                    status_callback=status_callback,
                                    message_type=current_message_type,
                                    file_name=current_file_name,
                                )
                                destination_message = await _clone_restricted_message(
                                    telegram,
                                    endpoints,
                                    source_message,
                                    filename_prefix=filename_prefix,
                                    filename_suffix=filename_suffix,
                                    text_prefix=text_prefix,
                                    text_suffix=text_suffix,
                                    reporter=reporter,
                                )
                    print(f"[{index}/{total_messages}] cloned source message {source_message_id}")
                success += 1
                destination_message_id = getattr(destination_message, "id", None)
                last_successful_message_link = _build_destination_message_link(
                    endpoints,
                    int(destination_message_id) if isinstance(destination_message_id, int) else None,
                )
                progress_state.update(
                    {
                        "success": success,
                        "skipped": skipped,
                        "last_processed_source_message_id": source_message_id,
                        "last_successful_source_message_id": source_message_id,
                        "last_successful_destination_message_id": destination_message_id,
                        "last_successful_message_link": last_successful_message_link,
                        "resume_after_source_message_id": source_message_id,
                    }
                )
                await _save_clone_status(
                    status_callback,
                    "running",
                    payload,
                    current_index=index,
                    total_messages=total_messages,
                    success=success,
                    failed=failed,
                    skipped=skipped,
                    current_message_id=source_message_id,
                    last_processed_source_message_id=source_message_id,
                    last_successful_source_message_id=source_message_id,
                    last_successful_destination_message_id=destination_message_id,
                    last_successful_message_link=last_successful_message_link,
                    resume_after_source_message_id=source_message_id,
                    current_message_type=current_message_type,
                    current_file_name=current_file_name,
                )
            except asyncio.CancelledError:
                raise
            except SkippedMessageError as exc:
                skipped += 1
                progress_state["skipped"] = skipped
                logging.getLogger("topic_clone").info(
                    "skipped unsupported source message",
                    extra={
                        "event": "clone_message_skipped",
                        "message_id": source_message_id,
                        "reason": str(exc),
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
                    skipped=skipped,
                    current_message_id=source_message_id,
                    last_processed_source_message_id=source_message_id,
                    resume_after_source_message_id=source_message_id,
                    skipped_reason=str(exc),
                    current_message_type=_message_media_type(source_message),
                    current_file_name=_message_file_name(source_message),
                )
                print(f"[{index}/{total_messages}] skipped source message {source_message_id}: {exc}")
            except Exception as exc:
                failed += 1
                progress_state["failed"] = failed
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
                    skipped=skipped,
                    current_message_id=source_message_id,
                    error=str(exc),
                    current_message_type=_message_media_type(source_message),
                    current_file_name=_message_file_name(source_message),
                )
                print(f"[{index}/{total_messages}] failed for message {source_message_id}: {exc}")
                if not continue_on_error:
                    raise

            has_more = index < total_messages
            if has_more and not dry_run:
                next_delay_sec = delay_sec
                if used_restricted_media_fallback:
                    next_delay_sec = max(
                        next_delay_sec,
                        telegram.settings.restricted_media_cooldown_sec,
                    )
                if next_delay_sec > 0:
                    await asyncio.sleep(next_delay_sec)
    finally:
        telegram.flood_wait_callback = previous_flood_wait_callback

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
        "filename_prefix": args.filename_prefix,
        "filename_suffix": args.filename_suffix,
        "text_prefix": args.text_prefix,
        "text_suffix": args.text_suffix,
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
    if endpoints.source_topic_id is None:
        source_topic_title = None
        destination_topic_title = await telegram.get_forum_topic_title(
            endpoints.destination_chat_id,
            endpoints.destination_topic_id,
        )
    else:
        source_topic_title, destination_topic_title = await asyncio.gather(
            telegram.get_forum_topic_title(endpoints.source_chat_id, endpoints.source_topic_id),
            telegram.get_forum_topic_title(endpoints.destination_chat_id, endpoints.destination_topic_id),
        )

    return {
        "source_chat_id": endpoints.source_chat_id,
        "source_chat_title": _chat_display_name(source_chat),
        "source_topic_id": endpoints.source_topic_id,
        "source_topic_title": source_topic_title
        or (f"Topic {endpoints.source_topic_id}" if endpoints.source_topic_id is not None else "Channel messages"),
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
    filename_prefix: str = "",
    filename_suffix: str = "",
    text_prefix: str = "",
    text_suffix: str = "",
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
        if endpoints.source_topic_id is not None:
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
            filename_prefix=filename_prefix,
            filename_suffix=filename_suffix,
            text_prefix=text_prefix,
            text_suffix=text_suffix,
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
                "filename_prefix": filename_prefix,
                "filename_suffix": filename_suffix,
                "text_prefix": text_prefix,
                "text_suffix": text_suffix,
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
                filename_prefix=str(payload.get("filename_prefix", "") or ""),
                filename_suffix=str(payload.get("filename_suffix", "") or ""),
                text_prefix=str(payload.get("text_prefix", "") or ""),
                text_suffix=str(payload.get("text_suffix", "") or ""),
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
                    "Usage: /clone --source-link <link> --destination-link <link> [--start-id N] [--limit N] [--delay-sec S] [--batch-size N] [--message-ids 1,2,3] [--dry-run] [--continue-on-error] [--hide-sender-name] [--filename-prefix TEXT] [--filename-suffix TEXT] [--text-prefix TEXT] [--text-suffix TEXT]"
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
                filename_prefix=args.filename_prefix,
                filename_suffix=args.filename_suffix,
                text_prefix=args.text_prefix,
                text_suffix=args.text_suffix,
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
