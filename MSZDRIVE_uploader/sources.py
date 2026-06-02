from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from .msz_api import infer_extension_from_signature, norm_rel


@dataclass(frozen=True)
class SourceItem:
    path: Path
    rel_path: str
    cleanup: bool = False


def _safe_name(name: str) -> str:
    cleaned = " ".join(str(name).replace("\x00", "").split())
    return Path(cleaned).name.strip()


def _has_extension(name: str) -> bool:
    suffix = Path(name).suffix
    return bool(suffix and "/" not in suffix and "\\" not in suffix and not any(char.isspace() for char in suffix))


def _truncate_name(name: str, limit: int = 180) -> str:
    if len(name) <= limit:
        return name
    suffix = Path(name).suffix
    stem = Path(name).stem if suffix else name
    stem_limit = max(1, limit - len(suffix))
    return stem[:stem_limit].rstrip(" .") + suffix


def _media_extension(message: object, media: object | None) -> str:
    mime_type = str(getattr(media, "mime_type", "") or "").strip()
    if mime_type:
        extension = mimetypes.guess_extension(mime_type)
        if extension:
            if extension == ".jpe":
                return ".jpg"
            return extension
    if getattr(message, "photo", None):
        return ".jpg"
    if getattr(message, "video", None) or getattr(message, "animation", None) or getattr(message, "video_note", None):
        return ".mp4"
    if getattr(message, "audio", None):
        return ".mp3"
    if getattr(message, "voice", None):
        return ".ogg"
    return ".bin"


async def iter_local(path: Path) -> AsyncIterator[SourceItem]:
    path = path.expanduser().resolve()
    if path.is_file():
        yield SourceItem(path=path, rel_path=path.name, cleanup=False)
        return
    if not path.is_dir():
        raise FileNotFoundError(path)

    files = sorted((item for item in path.rglob("*") if item.is_file()), key=lambda p: str(p).lower())
    for file_path in files:
        yield SourceItem(path=file_path, rel_path=norm_rel(str(file_path.relative_to(path))), cleanup=False)


async def list_gdrive_folder(url: str, staging_dir: Path):
    try:
        import gdown
    except ImportError as exc:
        raise RuntimeError("gdown is required for --source gdrive. Install requirements first.") from exc

    staging_dir.mkdir(parents=True, exist_ok=True)
    output_dir = staging_dir / "gdrive"
    output_dir.mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(
        gdown.download_folder,
        url=url,
        output=str(output_dir),
        quiet=False,
        use_cookies=False,
        skip_download=True,
    )


async def iter_gdrive(url: str, staging_dir: Path) -> AsyncIterator[SourceItem]:
    try:
        import gdown
    except ImportError as exc:
        raise RuntimeError("gdown is required for --source gdrive. Install requirements first.") from exc

    files = await list_gdrive_folder(url, staging_dir)
    for drive_file in files:
        rel_path = norm_rel(getattr(drive_file, "path", "") or Path(getattr(drive_file, "local_path")).name)
        yield await download_gdrive_file(drive_file, rel_path, staging_dir)


async def download_gdrive_file(drive_file: object, rel_path: str, staging_dir: Path) -> SourceItem:
    try:
        import gdown
    except ImportError as exc:
        raise RuntimeError("gdown is required for --source gdrive. Install requirements first.") from exc

    local_path = Path(getattr(drive_file, "local_path"))
    if not local_path.is_absolute():
        local_path = staging_dir / "gdrive" / rel_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists() and local_path.stat().st_size > 0:
        print(f"Reusing existing download: {local_path}", flush=True)
        downloaded_path = local_path
    else:
        download_output = str(local_path if local_path.suffix else local_path.parent)
        downloaded = await asyncio.to_thread(
            gdown.download,
            url="https://drive.google.com/uc?id=" + str(getattr(drive_file, "id")),
            output=download_output,
            quiet=False,
            use_cookies=False,
        )
        if not downloaded:
            raise RuntimeError(f"Google Drive download failed: {rel_path}")
        downloaded_path = Path(downloaded)
    if not Path(rel_path).suffix and downloaded_path.suffix:
        rel_path = norm_rel(str(Path(rel_path).with_suffix(downloaded_path.suffix)))
    return SourceItem(path=downloaded_path, rel_path=rel_path, cleanup=True)


def _caption_text(message: object) -> str:
    caption = getattr(message, "caption", "") or ""
    return _safe_name(caption)


def telegram_media_filename(message: object, *, use_caption: bool = False) -> str:
    media = getattr(message, getattr(getattr(message, "media", None), "value", ""), None)
    if use_caption:
        caption_name = _caption_text(message)
        if caption_name:
            if not _has_extension(caption_name):
                caption_name += _media_extension(message, media)
            return _truncate_name(caption_name)
    file_name = _safe_name(getattr(media, "file_name", "") if media is not None else "")
    if file_name:
        if not _has_extension(file_name):
            file_name += _media_extension(message, media)
        return _truncate_name(file_name)
    message_id = getattr(message, "id", "message")
    if getattr(message, "photo", None):
        return f"{message_id}.jpg"
    if getattr(message, "video", None):
        return f"{message_id}.mp4"
    if getattr(message, "audio", None):
        return f"{message_id}.mp3"
    if getattr(message, "voice", None):
        return f"{message_id}.ogg"
    if getattr(message, "animation", None):
        return f"{message_id}.mp4"
    if getattr(message, "document", None):
        return f"{message_id}.bin"
    return f"{message_id}.bin"


def message_has_downloadable_media(message: object) -> bool:
    return any(
        getattr(message, attr, None)
        for attr in ("audio", "document", "photo", "animation", "video", "voice", "video_note")
    )


async def iter_telegram_topic(
    topic_link: str,
    staging_dir: Path,
    config_path: Path,
    batch_size: int,
    batch_delay_sec: float,
    start_from_message_id: int = 0,
) -> AsyncIterator[SourceItem]:
    heroku_dir = Path(__file__).resolve().parents[1] / "heroku_bot"
    if heroku_dir.exists() and str(heroku_dir) not in sys.path:
        sys.path.insert(0, str(heroku_dir))

    from config import load_settings
    from export_topic_list import _parse_export_link
    from telegram_client import TelegramService

    parsed = _parse_export_link(topic_link)
    settings, _ = load_settings(config_path)
    telegram = TelegramService(settings, logger=logging.getLogger("msz_sources"), receive_updates=False)
    staging_dir.mkdir(parents=True, exist_ok=True)

    await telegram.start()
    try:
        if parsed.is_topic:
            message_ids = await telegram.list_topic_message_ids(
                parsed.chat_id,
                parsed.topic_id,
                start_from_message_id=start_from_message_id or parsed.message_id,
                batch_size=batch_size,
            )
        else:
            message_ids = await telegram.list_chat_message_ids(
                parsed.chat_id,
                start_from_message_id=start_from_message_id or parsed.message_id,
                batch_size=batch_size,
            )

        ordered_ids = sorted(set(message_ids))
        for start in range(0, len(ordered_ids), batch_size):
            chunk = ordered_ids[start : start + batch_size]
            messages = await telegram.get_messages_bulk(parsed.chat_id, chunk)
            by_id = {message.id: message for message in messages}
            for message_id in chunk:
                message = by_id.get(message_id)
                if message is None or not message_has_downloadable_media(message):
                    continue
                file_name = telegram_media_filename(message)
                target_path = staging_dir / f"{message_id}_{file_name}"
                result = await telegram.download_media_to_path(message, target_path)
                downloaded = Path(result)
                rel_name = file_name
                if "." not in rel_name:
                    inferred = infer_extension_from_signature(downloaded)
                    if inferred:
                        rel_name += inferred
                yield SourceItem(path=downloaded, rel_path=rel_name, cleanup=True)

            if start + batch_size < len(ordered_ids) and batch_delay_sec > 0:
                await asyncio.sleep(batch_delay_sec)
    finally:
        await telegram.stop()


async def cleanup_item(item: SourceItem) -> None:
    if not item.cleanup:
        return
    try:
        item.path.unlink(missing_ok=True)
    except OSError:
        pass
    parent = item.path.parent
    while parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        if parent == parent.parent:
            break
