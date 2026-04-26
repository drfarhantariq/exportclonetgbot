from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from pyrogram.types import Message, MessageEntity

from models import ClassificationResult, MessageKind


def _is_video_document(message: Message) -> bool:
    if not message.document:
        return False
    mime_type = (message.document.mime_type or "").lower()
    return mime_type.startswith("video/")


def classify_message(message: Message) -> ClassificationResult:
    if getattr(message, "empty", False):
        return ClassificationResult(MessageKind.UNSUPPORTED, "message is empty or deleted")

    if message.service is not None:
        return ClassificationResult(MessageKind.UNSUPPORTED, "service messages are skipped")

    if message.video or message.animation or _is_video_document(message):
        return ClassificationResult(MessageKind.VIDEO_LEECH, "video-like media routed through leech bot")

    if message.photo or message.audio or message.voice or message.sticker:
        return ClassificationResult(MessageKind.DIRECT_MEDIA, "direct cached media clone")

    if message.document and not _is_video_document(message):
        return ClassificationResult(MessageKind.DIRECT_MEDIA, "direct non-video document clone")

    if message.text:
        return ClassificationResult(MessageKind.TEXT, "plain text clone")

    return ClassificationResult(MessageKind.UNSUPPORTED, "unsupported message type")


def extract_text_payload(message: Message) -> tuple[str, Optional[list[MessageEntity]], bool]:
    return (
        message.text or "",
        message.entities,
        bool(message.web_page is None),
    )


def extract_caption_payload(message: Message) -> tuple[str | None, Optional[list[MessageEntity]]]:
    return (message.caption, message.caption_entities)


def extract_reusable_file_id(message: Message) -> Optional[str]:
    if message.photo:
        return message.photo.file_id
    if message.video:
        return message.video.file_id
    if message.animation:
        return message.animation.file_id
    if message.audio:
        return message.audio.file_id
    if message.voice:
        return message.voice.file_id
    if message.sticker:
        return message.sticker.file_id
    if message.document:
        return message.document.file_id
    return None


def normalize_sync_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip())


def build_sync_fingerprint(message: Message) -> str:
    classification = classify_message(message)
    payload: dict[str, object] = {
        "kind": classification.kind.value,
        "text": normalize_sync_text(message.text),
        "caption": normalize_sync_text(message.caption),
    }

    if message.video:
        payload.update(
            {
                "media": "video",
                "duration": message.video.duration or 0,
                "width": message.video.width or 0,
                "height": message.video.height or 0,
                "mime_type": (message.video.mime_type or "").lower(),
            }
        )
    elif message.animation:
        payload.update(
            {
                "media": "animation",
                "duration": message.animation.duration or 0,
                "width": message.animation.width or 0,
                "height": message.animation.height or 0,
                "mime_type": (message.animation.mime_type or "").lower(),
            }
        )
    elif message.photo:
        payload.update(
            {
                "media": "photo",
                "width": message.photo.width or 0,
                "height": message.photo.height or 0,
            }
        )
    elif message.audio:
        payload.update(
            {
                "media": "audio",
                "duration": message.audio.duration or 0,
                "title": normalize_sync_text(message.audio.title),
                "performer": normalize_sync_text(message.audio.performer),
                "file_name": normalize_sync_text(message.audio.file_name),
            }
        )
    elif message.voice:
        payload.update(
            {
                "media": "voice",
                "duration": message.voice.duration or 0,
            }
        )
    elif message.sticker:
        payload.update(
            {
                "media": "sticker",
                "emoji": normalize_sync_text(message.sticker.emoji),
                "set_name": normalize_sync_text(message.sticker.set_name),
            }
        )
    elif message.document:
        payload.update(
            {
                "media": "document",
                "mime_type": (message.document.mime_type or "").lower(),
                "file_name": normalize_sync_text(message.document.file_name),
            }
        )
    else:
        payload["media"] = "text"

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
