from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client, raw
from pyrogram.errors import FloodWait


PRIVATE_TOPIC_LINK_RE = re.compile(
    r"^https?://t\.me/c/(?P<chat>\d+)/(?P<topic>\d+)/(?P<message>\d+)$"
)


def c_format_to_chat_id(chat_id: int) -> int:
    return int(f"-100{chat_id}")


def chat_id_to_c_format(chat_id: int) -> int:
    text = str(chat_id)
    if text.startswith("-100"):
        return int(text[4:])
    return abs(chat_id)


def build_private_topic_link(chat_id: int, topic_id: int, message_id: int) -> str:
    return f"https://t.me/c/{chat_id_to_c_format(chat_id)}/{topic_id}/{message_id}"


def parse_private_topic_link(link: str) -> tuple[int, int, int]:
    match = PRIVATE_TOPIC_LINK_RE.match(link.strip())
    if not match:
        raise ValueError(f"Unsupported private topic link format: {link}")

    return (
        c_format_to_chat_id(int(match.group("chat"))),
        int(match.group("topic")),
        int(match.group("message")),
    )


def read_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export one Telegram topic chronologically into a txt file. "
            "Video/PDF/Image/HTML messages are exported as links. "
            "Text messages are exported as text."
        )
    )
    parser.add_argument(
        "--topic-link",
        default="",
        help="Example first-message link: https://t.me/c/<chat>/<topic>/<message>",
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
        help="Messages fetched per bulk request (default: 20)",
    )
    parser.add_argument(
        "--batch-delay-sec",
        type=float,
        default=2.0,
        help="Delay between bulk requests in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--reply-batch-size",
        type=int,
        default=100,
        help="Reply scan batch size for topic history (default: 100)",
    )
    return parser


def prompt_topic_link(initial_value: str) -> str:
    if initial_value.strip():
        return initial_value.strip()

    while True:
        value = input(
            "Enter example first-message topic link "
            "(https://t.me/c/<chat>/<topic>/<message>): "
        ).strip()
        if not value:
            print("Topic link is required.")
            continue

        try:
            parse_private_topic_link(value)
            return value
        except ValueError as exc:
            print(f"Invalid link format: {exc}")


def prompt_output_path(initial_value: str) -> str:
    if initial_value.strip():
        return initial_value.strip()

    return input(
        "Output txt path (press Enter for default topic_list_<chat>_<topic>.txt): "
    ).strip()


async def call_with_flood_wait(operation: str, func, flood_wait_buffer_sec: float = 2.0):
    while True:
        try:
            return await func()
        except FloodWait as exc:
            wait_seconds = float(getattr(exc, "value", 0) or 0) + flood_wait_buffer_sec
            print(
                f"[{operation}] Flood wait: sleeping {wait_seconds:.1f}s to avoid abuse."
            )
            await asyncio.sleep(wait_seconds)


async def list_topic_message_ids(
    app: Client,
    chat_id: int,
    topic_id: int,
    start_from_message_id: int,
    reply_batch_size: int,
) -> list[int]:
    peer = await call_with_flood_wait("resolve_peer", lambda: app.resolve_peer(chat_id))

    offset_id = 0
    min_id = max(start_from_message_id - 1, 0)
    message_ids: list[int] = []
    seen_ids: set[int] = set()

    while True:
        replies = await call_with_flood_wait(
            "get_topic_replies",
            lambda: app.invoke(
                raw.functions.messages.GetReplies(
                    peer=peer,
                    msg_id=topic_id,
                    offset_id=offset_id,
                    offset_date=0,
                    add_offset=0,
                    limit=reply_batch_size,
                    max_id=0,
                    min_id=min_id,
                    hash=0,
                )
            ),
        )

        batch_ids: list[int] = []
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

        if len(batch_ids) < reply_batch_size:
            break

    return sorted(set(message_ids))


async def get_messages_bulk(app: Client, chat_id: int, message_ids: list[int]):
    if not message_ids:
        return []

    messages = await call_with_flood_wait(
        "get_messages_bulk",
        lambda: app.get_messages(chat_id, message_ids, replies=0),
    )

    if not isinstance(messages, list):
        messages = [messages] if messages else []

    return [m for m in messages if m is not None and not getattr(m, "empty", False)]


def is_pdf_message(message) -> bool:
    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    file_name = (getattr(document, "file_name", "") or "").lower()
    return mime_type == "application/pdf" or file_name.endswith(".pdf")


def is_image_message(message) -> bool:
    if getattr(message, "photo", None) is not None:
        return True

    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    return mime_type.startswith("image/")


def is_video_message(message) -> bool:
    if getattr(message, "video", None) is not None:
        return True

    document = getattr(message, "document", None)
    if document is None:
        return False

    mime_type = (getattr(document, "mime_type", "") or "").lower()
    return mime_type.startswith("video/")


def is_html_message(message) -> bool:
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


def message_has_target_link(message) -> bool:
    return (
        is_video_message(message)
        or is_pdf_message(message)
        or is_image_message(message)
        or is_html_message(message)
    )


def message_text(message) -> str:
    return (getattr(message, "text", None) or "").strip()


def normalize_text_line(text: str) -> str:
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


TELEGRAM_MESSAGE_SAFE_CHARS = 3800


def format_entries(entries: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    current_link_block_chars = 0

    for index, (entry_type, value) in enumerate(entries):
        if entry_type == "text":
            text_line = normalize_text_line(value)
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


async def run_export(
    topic_link: str,
    out_path: str,
    batch_size: int,
    batch_delay_sec: float,
    reply_batch_size: int,
) -> Path:
    if batch_size <= 0:
        raise RuntimeError("batch-size must be > 0")
    if batch_delay_sec < 0:
        raise RuntimeError("batch-delay-sec must be >= 0")
    if reply_batch_size <= 0:
        raise RuntimeError("reply-batch-size must be > 0")

    chat_id, topic_id, start_message_id = parse_private_topic_link(topic_link)

    output = Path(out_path).expanduser().resolve() if out_path else Path(
        f"topic_list_{abs(chat_id)}_{topic_id}.txt"
    ).resolve()

    load_dotenv()
    api_id = int(read_required_env("TG_API_ID"))
    api_hash = read_required_env("TG_API_HASH")
    session_string = read_required_env("TG_SESSION_STRING")

    app = Client(
        name="telegram-topic-exporter",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )

    await app.start()
    try:
        message_ids = await list_topic_message_ids(
            app,
            chat_id,
            topic_id,
            start_message_id,
            reply_batch_size,
        )

        entries: list[tuple[str, str]] = []
        for start in range(0, len(message_ids), batch_size):
            chunk_ids = message_ids[start : start + batch_size]
            chunk_messages = await get_messages_bulk(app, chat_id, chunk_ids)
            by_id = {message.id: message for message in chunk_messages}

            for message_id in chunk_ids:
                message = by_id.get(message_id)
                if message is None:
                    continue

                if message_has_target_link(message):
                    entries.append(("link", build_private_topic_link(chat_id, topic_id, message.id)))
                    continue

                text = message_text(message)
                if text:
                    entries.append(("text", text))

            has_more = start + batch_size < len(message_ids)
            if has_more and batch_delay_sec > 0:
                await asyncio.sleep(batch_delay_sec)

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(format_entries(entries), encoding="utf-8")
        return output
    finally:
        await app.stop()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    topic_link = prompt_topic_link(args.topic_link)
    out_path = prompt_output_path(args.out)

    try:
        output = asyncio.run(
            run_export(
                topic_link,
                out_path,
                args.batch_size,
                args.batch_delay_sec,
                args.reply_batch_size,
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
