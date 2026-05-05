from __future__ import annotations

import argparse
import asyncio
import html
import inspect
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple
from urllib import error, request

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from config import ConfigError, load_settings
from telegram_client import TelegramService
from topic_utils import (
    build_private_message_link,
    build_private_topic_link,
    parse_private_chat_message_link,
    parse_private_topic_link,
)


class ParsedExportLink(NamedTuple):
    chat_id: int
    message_id: int
    topic_id: int | None

    @property
    def is_topic(self) -> bool:
        return self.topic_id is not None


DEFAULT_UPLOAD_TOPIC_LINK = "https://t.me/c/3541699273/38603/38604"
TELEGRAM_MESSAGE_SAFE_CHARS = 3800
HEROKU_RUNTIME_DIR = Path(os.getenv("HEROKU_RUNTIME_DIR", "heroku_runtime")).expanduser().resolve()
HEROKU_STATE_DIR = HEROKU_RUNTIME_DIR / "state"
HEROKU_EXPORTS_DIR = HEROKU_RUNTIME_DIR / "exports"
MONGODB_DATA_API_URL = os.getenv("MONGODB_DATA_API_URL", "").strip()
MONGODB_DATA_API_KEY = os.getenv("MONGODB_DATA_API_KEY", "").strip()
MONGODB_DATA_SOURCE = os.getenv("MONGODB_DATA_SOURCE", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "").strip()
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "bot_state").strip()

ExportStatusCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _parse_export_link(link: str) -> ParsedExportLink:
    stripped = link.strip()

    try:
        parsed_topic = parse_private_topic_link(stripped)
        return ParsedExportLink(
            chat_id=parsed_topic.chat_id,
            topic_id=parsed_topic.topic_id,
            message_id=parsed_topic.message_id,
        )
    except ValueError:
        pass

    try:
        parsed_message = parse_private_chat_message_link(stripped)
        return ParsedExportLink(
            chat_id=parsed_message.chat_id,
            topic_id=None,
            message_id=parsed_message.message_id,
        )
    except ValueError as exc:
        raise ValueError(
            "Unsupported private link format. Use one of: "
            "https://t.me/c/<chat>/<topic>/<message> or https://t.me/c/<chat>/<message>"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export a Telegram topic or private channel chronologically into a txt file. "
            "Media messages become links, text messages become text entries."
        )
    )
    parser.add_argument(
        "--topic-link",
        default="",
        help=(
            "Private link format: https://t.me/c/<chat>/<topic>/<message> "
            "or https://t.me/c/<chat>/<message>"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output txt file path. Default: topic_list_<chat>_<topic>.txt",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Messages fetched per bulk request (default: 20, safer for flood waits)",
    )
    parser.add_argument(
        "--batch-delay-sec",
        type=float,
        default=2.0,
        help="Delay between bulk requests in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--upload-topic-link",
        default=DEFAULT_UPLOAD_TOPIC_LINK,
        help=(
            "Topic link where the generated txt file will be uploaded "
            f"(default: {DEFAULT_UPLOAD_TOPIC_LINK})"
        ),
    )
    parser.add_argument(
        "--caption-file-names",
        action="store_true",
        help=(
            "For video links only, append '-n <caption>' when a caption exists "
            "(default: disabled)"
        ),
    )
    parser.add_argument(
        "--onwards",
        action="store_true",
        help=(
            "For forum topic links, start from the linked message instead of the "
            "topic root and export through the end of the topic"
        ),
    )
    parser.add_argument(
        "--header",
        default="INDEX 👆",
        help="Header text for /index output (default: INDEX 👆)",
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


def _ensure_txt_output_path(path: Path) -> Path:
    if path.suffix.lower() == ".txt":
        return path
    if path.suffix:
        return path.with_suffix(".txt")
    return path.with_name(f"{path.name}.txt")


def _ensure_runtime_dirs() -> None:
    HEROKU_STATE_DIR.mkdir(parents=True, exist_ok=True)
    HEROKU_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _runtime_output_path(name: str) -> Path:
    _ensure_runtime_dirs()
    return _ensure_txt_output_path(HEROKU_EXPORTS_DIR / name)


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
            "kind": "export",
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


def _prompt_topic_link(initial_value: str) -> str:
    if initial_value.strip():
        return initial_value.strip()

    while True:
        value = input(
            "Enter private topic or message link "
            "(https://t.me/c/<chat>/<topic>/<message> or https://t.me/c/<chat>/<message>): "
        ).strip()
        if not value:
            print("Topic link is required.")
            continue

        try:
            _parse_export_link(value)
            return value
        except ValueError as exc:
            print(f"Invalid link format: {exc}")


def _prompt_output_path(initial_value: str) -> str:
    if initial_value.strip():
        return initial_value.strip()

    value = input(
        "Output txt path (press Enter for the default export name): "
    ).strip()
    return value


def _resolve_start_message_id(parsed: ParsedExportLink, onwards: bool) -> int:
    if parsed.is_topic and onwards:
        return parsed.message_id
    if parsed.is_topic:
        return parsed.topic_id
    return parsed.message_id


def _default_output_path(parsed: ParsedExportLink, onwards: bool) -> Path:
    if parsed.is_topic:
        if onwards and parsed.message_id != parsed.topic_id:
            return _runtime_output_path(
                f"topic_list_{abs(parsed.chat_id)}_{parsed.topic_id}_from_{parsed.message_id}.txt"
            )
        return _runtime_output_path(f"topic_list_{abs(parsed.chat_id)}_{parsed.topic_id}.txt")

    return _runtime_output_path(f"channel_list_{abs(parsed.chat_id)}_from_{parsed.message_id}.txt")


def _is_pdf_message(message) -> bool:
    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    file_name = (getattr(document, "file_name", "") or "").lower()
    return mime_type == "application/pdf" or file_name.endswith(".pdf")


def _is_image_message(message) -> bool:
    if getattr(message, "photo", None) is not None:
        return True

    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    return mime_type.startswith("image/")


def _is_video_message(message) -> bool:
    if getattr(message, "video", None) is not None:
        return True

    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    return mime_type.startswith("video/")


def _is_html_message(message) -> bool:
    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    file_name = (getattr(document, "file_name", "") or "").lower()
    return (
        mime_type in {"text/html", "application/xhtml+xml"}
        or file_name.endswith(".html")
        or file_name.endswith(".htm")
    )


def _is_archive_message(message) -> bool:
    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    file_name = (getattr(document, "file_name", "") or "").lower()

    archive_mime_types = {
        "application/zip",
        "application/x-zip-compressed",
        "application/rar",
        "application/vnd.rar",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/x-tar",
        "application/gzip",
        "application/x-gzip",
        "application/x-bzip2",
        "application/x-xz",
        "application/apkg",
    }
    archive_extensions = (
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".apkg",
    )

    return mime_type in archive_mime_types or file_name.endswith(archive_extensions)


def _message_has_target_media(message) -> bool:
    return (
        _is_video_message(message)
        or _is_pdf_message(message)
        or _is_image_message(message)
        or _is_html_message(message)
        or _is_archive_message(message)
    )


def _message_text(message) -> str:
    text = (getattr(message, "text", None) or "").strip()
    return text


def _message_caption(message) -> str:
    caption = (getattr(message, "caption", None) or "").strip()
    return caption


def build_index_message(
    entries: dict[str, str] | list[tuple[str, str]],
    header: str = "INDEX 👆",
) -> str:
    if isinstance(entries, dict):
        normalized_entries = list(entries.items())
    else:
        normalized_entries = list(entries)

    lines = [f"<b>{html.escape(header.strip() or 'INDEX 👆')}</b>"]
    for display_text, link in normalized_entries:
        text = html.escape(str(display_text).strip())
        url = html.escape(str(link).strip(), quote=True)
        if not text or not url:
            continue
        lines.append(f'<a href="{url}">{text}</a>')

    return "\n".join(lines).rstrip() + "\n"


def _build_index_message(
    entries: dict[str, str] | list[tuple[str, str]],
    header: str = "INDEX 👆",
) -> str:
    return build_index_message(entries, header=header)


def _build_topic_index_entries(messages, chat_id: int, topic_id: int) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []

    for message in messages:
        text = _normalize_text_line(_message_text(message))
        if not text:
            continue

        entries.append(
            (
                text,
                build_private_topic_link(chat_id, topic_id, message.id),
            )
        )

    return entries


async def _send_topic_index_message(
    bot: Client,
    chat_id: int,
    topic_id: int,
    text: str,
) -> None:
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": ParseMode.HTML,
        "disable_web_page_preview": True,
    }

    try:
        supports_message_thread_id = "message_thread_id" in inspect.signature(bot.send_message).parameters
    except (TypeError, ValueError):
        supports_message_thread_id = False

    if supports_message_thread_id:
        kwargs["message_thread_id"] = topic_id
    else:
        kwargs["reply_to_message_id"] = topic_id

    await bot.send_message(**kwargs)


def _normalize_text_line(text: str) -> str:
    collapsed = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return collapsed


def _format_entries(entries: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    current_link_block_chars = 0

    for index, (entry_type, value) in enumerate(entries):
        if entry_type == "text":
            text_line = _normalize_text_line(value)
            if not text_line:
                continue
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(text_line)
            current_link_block_chars = 0
            continue

        projected_chars = current_link_block_chars + len(value) + 1
        if current_link_block_chars > 0 and projected_chars > TELEGRAM_MESSAGE_SAFE_CHARS:
            # Start a new chunk before exceeding Telegram message text limits.
            lines.append("")
            current_link_block_chars = 0

        lines.append(value)
        current_link_block_chars += len(value) + 1

        is_next_link = index + 1 < len(entries) and entries[index + 1][0] == "link"
        if not is_next_link:
            current_link_block_chars = 0

    return "\n".join(lines).rstrip() + "\n"


async def _notify_export_status(
    callback: ExportStatusCallback | None,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        await callback(payload)
    except Exception:
        logging.getLogger("topic_export").debug("export status callback failed", exc_info=True)


async def run_export(
    topic_link: str,
    config_path: str,
    out_path: str,
    batch_size: int,
    batch_delay_sec: float,
    upload_topic_link: str,
    caption_file_names: bool,
    onwards: bool,
    status_callback: ExportStatusCallback | None = None,
) -> Path:
    try:
        settings, _ = load_settings(config_path)
    except ConfigError as exc:
        raise RuntimeError(f"Configuration error: {exc}") from exc

    parsed = _parse_export_link(topic_link)
    if batch_size <= 0:
        raise RuntimeError("batch-size must be > 0")
    if batch_delay_sec < 0:
        raise RuntimeError("batch-delay-sec must be >= 0")

    if out_path:
        candidate = Path(out_path).expanduser()
        if not candidate.is_absolute():
            candidate = HEROKU_EXPORTS_DIR / candidate
        output = _ensure_txt_output_path(candidate.resolve())
    else:
        output = _default_output_path(parsed, onwards)

    parsed_upload_topic = parse_private_topic_link(upload_topic_link.strip())
    start_message_id = _resolve_start_message_id(parsed, onwards)

    telegram = TelegramService(settings, logger=logging.getLogger("topic_export"), receive_updates=False)

    async def _handle_flood_wait(payload: dict[str, Any]) -> None:
        await _notify_export_status(
            status_callback,
            {
                "phase": "running",
                "stage": "flood_wait",
                "flood_wait_operation": payload.get("operation"),
                "flood_wait_seconds": payload.get("wait_seconds"),
                "flood_wait_until": payload.get("wait_until"),
            },
        )

    try:
        telegram.flood_wait_callback = _handle_flood_wait
        await _notify_export_status(
            status_callback,
            {
                "phase": "running",
                "stage": "starting",
                "source_chat_id": parsed.chat_id,
                "source_topic_id": parsed.topic_id,
                "start_message_id": start_message_id,
                "output": str(output),
                "upload_chat_id": parsed_upload_topic.chat_id,
                "upload_topic_id": parsed_upload_topic.topic_id,
            },
        )
        await telegram.start()
        await _notify_export_status(status_callback, {"phase": "running", "stage": "listing_message_ids"})

        if parsed.is_topic:
            message_ids = await telegram.list_topic_message_ids(
                chat_id=parsed.chat_id,
                topic_id=parsed.topic_id,
                start_from_message_id=start_message_id,
                batch_size=batch_size,
            )
        else:
            message_ids = await telegram.list_chat_message_ids(
                chat_id=parsed.chat_id,
                start_from_message_id=parsed.message_id,
                batch_size=batch_size,
            )

        ordered_ids = sorted(set(message_ids))
        total_messages = len(ordered_ids)
        messages = []
        await _notify_export_status(
            status_callback,
            {
                "phase": "running",
                "stage": "fetching_messages",
                "total_messages": total_messages,
                "fetched_messages": 0,
            },
        )

        for start in range(0, total_messages, batch_size):
            chunk = ordered_ids[start : start + batch_size]
            chunk_messages = await telegram.get_messages_bulk(parsed.chat_id, chunk)
            by_id = {message.id: message for message in chunk_messages}
            for message_id in chunk:
                message = by_id.get(message_id)
                if message is not None:
                    messages.append(message)

            fetched_messages = min(start + len(chunk), total_messages)
            await _notify_export_status(
                status_callback,
                {
                    "phase": "running",
                    "stage": "fetching_messages",
                    "total_messages": total_messages,
                    "fetched_messages": fetched_messages,
                    "current_message_id": chunk[-1] if chunk else None,
                },
            )

            has_more = start + batch_size < total_messages
            if has_more and batch_delay_sec > 0:
                await asyncio.sleep(batch_delay_sec)

        entries: list[tuple[str, str]] = []
        media_links = 0
        text_entries = 0
        await _notify_export_status(
            status_callback,
            {
                "phase": "running",
                "stage": "processing_messages",
                "total_messages": total_messages,
                "fetched_messages": len(messages),
                "processed_messages": 0,
                "media_links": media_links,
                "text_entries": text_entries,
            },
        )
        for index, message in enumerate(messages, start=1):
            if _message_has_target_media(message):
                if parsed.is_topic:
                    link = build_private_topic_link(parsed.chat_id, parsed.topic_id, message.id)
                else:
                    link = build_private_message_link(parsed.chat_id, message.id)

                if caption_file_names and _is_video_message(message):
                    caption = _normalize_text_line(_message_caption(message))
                    if caption:
                        entries.append(("link", f"{link} -n {caption}"))
                        media_links += 1
                        await _notify_export_status(
                            status_callback,
                            {
                                "phase": "running",
                                "stage": "processing_messages",
                                "total_messages": total_messages,
                                "fetched_messages": len(messages),
                                "processed_messages": index,
                                "media_links": media_links,
                                "text_entries": text_entries,
                                "current_message_id": message.id,
                            },
                        )
                        continue

                entries.append(("link", link))
                media_links += 1
                await _notify_export_status(
                    status_callback,
                    {
                        "phase": "running",
                        "stage": "processing_messages",
                        "total_messages": total_messages,
                        "fetched_messages": len(messages),
                        "processed_messages": index,
                        "media_links": media_links,
                        "text_entries": text_entries,
                        "current_message_id": message.id,
                    },
                )
                continue

            text = _message_text(message)
            if text:
                entries.append(("text", text))
                text_entries += 1

            await _notify_export_status(
                status_callback,
                {
                    "phase": "running",
                    "stage": "processing_messages",
                    "total_messages": total_messages,
                    "fetched_messages": len(messages),
                    "processed_messages": index,
                    "media_links": media_links,
                    "text_entries": text_entries,
                    "current_message_id": message.id,
                },
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        await _notify_export_status(
            status_callback,
            {
                "phase": "running",
                "stage": "writing_file",
                "total_messages": total_messages,
                "fetched_messages": len(messages),
                "processed_messages": len(messages),
                "media_links": media_links,
                "text_entries": text_entries,
                "output": str(output),
            },
        )
        output.write_text(_format_entries(entries), encoding="utf-8")

        await _notify_export_status(
            status_callback,
            {
                "phase": "running",
                "stage": "uploading_file",
                "total_messages": total_messages,
                "fetched_messages": len(messages),
                "processed_messages": len(messages),
                "media_links": media_links,
                "text_entries": text_entries,
                "output": str(output),
            },
        )
        await telegram.send_document_to_topic(
            chat_id=parsed_upload_topic.chat_id,
            topic_id=parsed_upload_topic.topic_id,
            document_path=output,
            caption=f"Exported file: {output.name}",
        )
        await _notify_export_status(
            status_callback,
            {
                "phase": "completed",
                "stage": "completed",
                "total_messages": total_messages,
                "fetched_messages": len(messages),
                "processed_messages": len(messages),
                "media_links": media_links,
                "text_entries": text_entries,
                "output": str(output),
            },
        )
        return output
    finally:
        await telegram.stop()


async def run_index(
    topic_link: str,
    config_path: str,
    batch_size: int,
    batch_delay_sec: float,
    onwards: bool,
    bot: Client,
    header: str = "INDEX 👆",
) -> int:
    try:
        settings, _ = load_settings(config_path)
    except ConfigError as exc:
        raise RuntimeError(f"Configuration error: {exc}") from exc

    parsed = _parse_export_link(topic_link)
    if not parsed.is_topic:
        raise RuntimeError("Index generation requires a private forum topic link.")
    if batch_size <= 0:
        raise RuntimeError("batch-size must be > 0")
    if batch_delay_sec < 0:
        raise RuntimeError("batch-delay-sec must be >= 0")

    start_message_id = _resolve_start_message_id(parsed, onwards)
    telegram = TelegramService(settings, logger=logging.getLogger("topic_index"), receive_updates=False)

    try:
        await telegram.start()
        messages = await telegram.list_topic_messages(
            chat_id=parsed.chat_id,
            topic_id=parsed.topic_id,
            start_from_message_id=start_message_id,
            batch_size=batch_size,
            inter_batch_delay_sec=batch_delay_sec,
        )

        entries = _build_topic_index_entries(messages, parsed.chat_id, parsed.topic_id)
        if not entries:
            raise RuntimeError("No text messages were found in the specified topic.")

        await _send_topic_index_message(
            bot=bot,
            chat_id=parsed.chat_id,
            topic_id=parsed.topic_id,
            text=_build_index_message(entries, header=header),
        )
        return len(entries)
    finally:
        await telegram.stop()


def _build_export_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "topic_link": args.topic_link,
        "config_path": args.config,
        "out_path": args.out,
        "batch_size": args.batch_size,
        "batch_delay_sec": args.batch_delay_sec,
        "upload_topic_link": args.upload_topic_link,
        "caption_file_names": args.caption_file_names,
        "onwards": args.onwards,
    }


def _build_index_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "topic_link": args.topic_link,
        "config_path": args.config,
        "batch_size": args.batch_size,
        "batch_delay_sec": args.batch_delay_sec,
        "onwards": args.onwards,
        "header": args.header,
    }


def _index_usage_text() -> str:
    return (
        "Usage: /index <topic_link>\n"
        "Or: /index --topic-link <link> [--config <file>] [--batch-size N] "
        "[--batch-delay-sec S] [--onwards] [--header <text>]\n\n"
        "Scans the linked topic for text messages only and sends a clickable HTML index "
        "back into the same topic."
    )


def _export_bot_help_text() -> str:
    return (
        "Available commands:\n\n"
        "/export --topic-link <link> [--out <file>] [--batch-size N] "
        "[--batch-delay-sec S] [--upload-topic-link <link>] [--caption-file-names] [--onwards]\n"
        "/export last or /export resume\n\n"
        f"{_index_usage_text()}\n\n"
        "/status - Show the saved export profile"
    )


def _normalize_index_command(command_text: str) -> str:
    stripped = command_text.strip()
    if not stripped or stripped.startswith("-"):
        return stripped
    return f"--topic-link {stripped}"


async def _run_export_bot(args: argparse.Namespace) -> int:
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
        name="export-topic-bot",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    async def _authorized(message) -> bool:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        return isinstance(user_id, int) and user_id in admin_ids

    async def _run_spec(message, payload: dict[str, Any], label: str) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        await store.save(f"export:{label}", payload)
        _write_json_file(_snapshot_path(f"export_{label}"), payload)
        status = await message.reply_text("Export started.")
        try:
            output = await run_export(
                topic_link=payload["topic_link"],
                config_path=payload["config_path"],
                out_path=payload["out_path"],
                batch_size=int(payload["batch_size"]),
                batch_delay_sec=float(payload["batch_delay_sec"]),
                upload_topic_link=payload["upload_topic_link"],
                caption_file_names=bool(payload["caption_file_names"]),
                onwards=bool(payload["onwards"]),
            )
        except Exception as exc:
            await status.edit_text(f"Export failed: {exc}")
            raise
        else:
            await status.edit_text(f"Export complete: {output}")

    async def _run_index_spec(message, payload: dict[str, Any]) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        status = await message.reply_text("Index generation started.")
        try:
            count = await run_index(
                topic_link=payload["topic_link"],
                config_path=payload["config_path"],
                batch_size=int(payload["batch_size"]),
                batch_delay_sec=float(payload["batch_delay_sec"]),
                onwards=bool(payload["onwards"]),
                bot=bot,
                header=str(payload.get("header") or "INDEX 👆"),
            )
        except Exception as exc:
            await status.edit_text(f"Index failed: {exc}")
            raise
        else:
            await status.edit_text(f"Index complete: {count} links sent.")

    @bot.on_message(filters.private & filters.command(["start", "help"], prefixes="/"))
    async def help_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        await message.reply_text(_export_bot_help_text())

    @bot.on_message(filters.private & filters.command("export", prefixes="/"))
    async def export_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        if command_text.lower() in {"last", "resume"}:
            payload = await store.load("export:last") or _read_json_file(_snapshot_path("export_last"))
            if payload is None:
                await message.reply_text("No saved export profile found.")
                return
        else:
            try:
                parsed = build_parser().parse_args(shlex.split(command_text))
            except SystemExit:
                await message.reply_text(
                    "Usage: /export --topic-link <link> [--out <file>] [--batch-size N] [--batch-delay-sec S] [--upload-topic-link <link>] [--caption-file-names] [--onwards]"
                )
                return
            payload = _build_export_payload(parsed)

        await _run_spec(message, payload, "last")

    @bot.on_message(filters.private & filters.command("status", prefixes="/"))
    async def status_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        payload = await store.load("export:last") or _read_json_file(_snapshot_path("export_last"))
        if payload is None:
            await message.reply_text("No saved export state.")
            return
        await message.reply_text(json.dumps(payload, indent=2, sort_keys=True))

    @bot.on_message(filters.private & filters.command("index", prefixes="/"))
    async def index_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        try:
            parsed = build_parser().parse_args(shlex.split(_normalize_index_command(command_text)))
        except SystemExit:
            await message.reply_text(_index_usage_text())
            return

        if not parsed.topic_link:
            await message.reply_text(_index_usage_text())
            return

        payload = _build_index_payload(parsed)
        await _run_index_spec(message, payload)

    await bot.start()
    print("Export bot is running.")
    await asyncio.Event().wait()
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.bot_mode:
        try:
            asyncio.run(_run_export_bot(args))
        except KeyboardInterrupt:
            print("Export bot stopped by user.")
            return 130
        except Exception as exc:
            print(f"Export bot failed: {exc}")
            return 1
        return 0

    topic_link = _prompt_topic_link(args.topic_link)
    out_path = _prompt_output_path(args.out)

    try:
        output = asyncio.run(
            run_export(
                topic_link,
                args.config,
                out_path,
                args.batch_size,
                args.batch_delay_sec,
                args.upload_topic_link,
                args.caption_file_names,
                args.onwards,
            )
        )
    except KeyboardInterrupt:
        print("Export cancelled by user.")
        return 130
    except Exception as exc:
        print(f"Export failed: {exc}")
        return 1

    print(f"Export complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
