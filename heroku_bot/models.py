from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CloneStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class MessageKind(str, Enum):
    TEXT = "text"
    DIRECT_MEDIA = "direct_media"
    VIDEO_LEECH = "video_leech"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ClassificationResult:
    kind: MessageKind
    reason: str


@dataclass
class CloneJob:
    id: int
    mapping_key: str
    source_chat_id: int
    source_topic_id: int
    source_message_id: int
    destination_chat_id: int
    destination_topic_id: int
    status: CloneStatus
    retry_count: int
    last_error: Optional[str]
    destination_message_id: Optional[int]
    bot_command_message_id: Optional[int]
    bot_media_message_id: Optional[int]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TopicMessageFingerprint:
    message_id: int
    fingerprint: str
