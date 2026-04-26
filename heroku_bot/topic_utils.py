from __future__ import annotations

import re
from dataclasses import dataclass

from pyrogram.types import Message


PRIVATE_TOPIC_LINK_RE = re.compile(
    r"^https?://t\.me/c/(?P<chat>\d+)/(?P<topic>\d+)/(?P<message>\d+)$"
)
PRIVATE_CHAT_MESSAGE_LINK_RE = re.compile(
    r"^https?://t\.me/c/(?P<chat>\d+)/(?P<message>\d+)$"
)


@dataclass(frozen=True)
class ParsedPrivateTopicLink:
    chat_id: int
    topic_id: int
    message_id: int


@dataclass(frozen=True)
class ParsedPrivateChatMessageLink:
    chat_id: int
    message_id: int


def chat_id_to_c_format(chat_id: int) -> int:
    text = str(chat_id)
    if text.startswith("-100"):
        return int(text[4:])
    return abs(chat_id)


def c_format_to_chat_id(chat_id: int) -> int:
    return int(f"-100{chat_id}")


def build_private_topic_link(chat_id: int, topic_id: int, message_id: int) -> str:
    return f"https://t.me/c/{chat_id_to_c_format(chat_id)}/{topic_id}/{message_id}"


def build_private_message_link(chat_id: int, message_id: int) -> str:
    return f"https://t.me/c/{chat_id_to_c_format(chat_id)}/{message_id}"


def parse_private_topic_link(link: str) -> ParsedPrivateTopicLink:
    match = PRIVATE_TOPIC_LINK_RE.match(link.strip())
    if not match:
        raise ValueError(f"Unsupported private topic link format: {link}")

    return ParsedPrivateTopicLink(
        chat_id=c_format_to_chat_id(int(match.group("chat"))),
        topic_id=int(match.group("topic")),
        message_id=int(match.group("message")),
    )


def parse_private_chat_message_link(link: str) -> ParsedPrivateChatMessageLink:
    match = PRIVATE_CHAT_MESSAGE_LINK_RE.match(link.strip())
    if not match:
        raise ValueError(f"Unsupported private message link format: {link}")

    return ParsedPrivateChatMessageLink(
        chat_id=c_format_to_chat_id(int(match.group("chat"))),
        message_id=int(match.group("message")),
    )


def belongs_to_topic(message: Message | None, topic_id: int) -> bool:
    if message is None:
        return False

    if getattr(message, "reply_to_top_message_id", None) == topic_id:
        return True

    if getattr(message, "reply_to_message_id", None) == topic_id:
        return True

    if getattr(message, "id", None) == topic_id:
        return True

    return False
