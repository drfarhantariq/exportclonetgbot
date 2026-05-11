import asyncio
import json
import re
import shlex
from pathlib import Path
from time import monotonic
from typing import Any

from aiofiles.os import remove

from .. import bot_loop, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import send_message
from .mirror_leech import Mirror


def _parse_args(message_text: str) -> tuple[str, int]:
    parts = shlex.split(message_text or "")
    up_dest = ""
    chunk_size = 5
    index = 1
    while index < len(parts):
        token = parts[index]
        if token == "-up" and index + 1 < len(parts):
            up_dest = parts[index + 1].strip()
            index += 2
            continue
        if token == "-c" and index + 1 < len(parts):
            try:
                parsed = int(parts[index + 1])
                if parsed > 0:
                    chunk_size = min(parsed, 20)
            except ValueError:
                pass
            index += 2
            continue
        index += 1
    if not up_dest:
        raise ValueError("Provide destination with -up chat_id|topic_id")
    return _normalize_up_dest(up_dest), chunk_size


def _parse_private_telegram_link(up_dest: str) -> str | None:
    # Supports:
    # - https://t.me/c/<chat>/<topic>/<message>
    # - https://t.me/c/<chat>/<message>
    # - tg://openmessage?chat_id=-100...&message_id=...
    raw = up_dest.strip()
    topic_match = re.match(
        r"^https?://t\.me/c/(\d+)/(\d+)/(\d+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if topic_match:
        chat_part = topic_match.group(1)
        topic_part = topic_match.group(2)
        return f"-100{chat_part}|{topic_part}"

    message_match = re.match(
        r"^https?://t\.me/c/(\d+)/(\d+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if message_match:
        chat_part = message_match.group(1)
        return f"-100{chat_part}"

    tg_open_match = re.match(
        r"^tg://openmessage\?chat_id=(-?\d+)&message_id=\d+$",
        raw,
        flags=re.IGNORECASE,
    )
    if tg_open_match:
        chat_id = tg_open_match.group(1)
        return chat_id
    return None


def _normalize_up_dest(up_dest: str) -> str:
    parsed = _parse_private_telegram_link(up_dest)
    if parsed is not None:
        return parsed
    return up_dest.strip()


def _extract_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries = payload.get("entries")
    if isinstance(entries, list):
        normalized: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", "")).strip().lower()
            if kind == "text":
                text_value = str(item.get("value") or item.get("text") or "").strip()
                if text_value:
                    normalized.append({"type": "text", "value": text_value})
            elif kind == "link":
                link_value = str(item.get("value") or item.get("url") or item.get("link") or "").strip()
                if link_value:
                    normalized.append({"type": "link", "value": link_value})
        return normalized
    raise ValueError("JSON file does not contain a valid 'entries' array.")


def _build_segments(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current_links: list[str] = []
    for entry in entries:
        kind = entry["type"]
        value = entry["value"]
        if kind == "link":
            current_links.append(value)
            continue
        if current_links:
            segments.append({"type": "links", "items": current_links})
            current_links = []
        segments.append({"type": "text", "value": value})
    if current_links:
        segments.append({"type": "links", "items": current_links})
    return segments


def _parse_topic_destination(up_dest: str) -> tuple[int | str, int | None]:
    raw = up_dest.strip()
    if "|" in raw:
        chat_part, topic_part = raw.split("|", 1)
        chat = int(chat_part) if chat_part.lstrip("-").isdigit() else chat_part
        topic = int(topic_part) if topic_part.lstrip("-").isdigit() else None
        return chat, topic
    chat = int(raw) if raw.lstrip("-").isdigit() else raw
    return chat, None


async def _wait_for_chunk_completion(mid_task_pairs: list[tuple[int, asyncio.Task]], timeout_sec: int = 3600) -> None:
    deadline = monotonic() + timeout_sec
    observed = {mid: False for mid, _ in mid_task_pairs}
    while True:
        all_done = True
        async with task_dict_lock:
            active_mids = set(task_dict.keys())
        for mid, task in mid_task_pairs:
            if mid in active_mids:
                observed[mid] = True
                all_done = False
                continue
            if observed[mid]:
                continue
            if not task.done():
                all_done = False
        if all_done:
            await asyncio.gather(*(task for _, task in mid_task_pairs), return_exceptions=True)
            return
        if monotonic() > deadline:
            raise TimeoutError("Timed out while waiting for leech chunk to complete.")
        await asyncio.sleep(2)


async def _safe_status_edit(status_message, text: str) -> None:
    try:
        await status_message.edit(text)
    except Exception:
        pass


def _format_status_text(
    *,
    stage: str,
    segment_index: int,
    total_segments: int,
    sent_text: int,
    total_text: int,
    processed_links: int,
    total_links: int,
    chunk_info: str = "",
) -> str:
    lines = [
        "<b>Topic Clone JSON</b>",
        f"Stage: {stage}",
        f"Segments: {segment_index}/{total_segments}",
        f"Text sent: {sent_text}/{total_text}",
        f"Links processed: {processed_links}/{total_links}",
    ]
    if chunk_info:
        lines.append(f"Chunk: {chunk_info}")
    return "\n".join(lines)


async def _process_link_segment(
    client,
    trigger_message,
    up_dest: str,
    links: list[str],
    chunk_size: int,
    status_callback=None,
) -> int:
    command_name = BotCommands.LeechCommand[0]
    processed = 0
    for start in range(0, len(links), chunk_size):
        chunk = links[start : start + chunk_size]
        if status_callback is not None:
            await status_callback(
                {
                    "chunk_start": start + 1,
                    "chunk_end": start + len(chunk),
                    "chunk_total": len(links),
                }
            )
        launched: list[tuple[int, asyncio.Task]] = []
        for link in chunk:
            cmd_line = f"/{command_name} {link} -up {up_dest}"
            cmd_msg = await send_message(trigger_message, cmd_line)
            cmd_msg = await client.get_messages(chat_id=trigger_message.chat.id, message_ids=cmd_msg.id)
            if trigger_message.from_user:
                cmd_msg.from_user = trigger_message.from_user
            else:
                cmd_msg.sender_chat = trigger_message.sender_chat
            task = bot_loop.create_task(Mirror(client, cmd_msg, is_leech=True).new_event())
            launched.append((cmd_msg.id, task))
        await _wait_for_chunk_completion(launched)
        processed += len(chunk)
        if status_callback is not None:
            await status_callback(
                {
                    "chunk_completed": processed,
                    "chunk_total": len(links),
                }
            )
    return processed


async def _load_payload_from_reply(message) -> dict[str, Any]:
    reply = message.reply_to_message
    if reply is None:
        raise ValueError("Reply to a JSON export file or JSON text.")
    if reply.document is not None:
        downloaded = await reply.download()
        try:
            text = Path(downloaded).read_text(encoding="utf-8")
        finally:
            try:
                await remove(downloaded)
            except Exception:
                pass
    else:
        text = reply.text or ""
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object.")
    return payload


async def topic_clone_json(client, message):
    if Config.DISABLE_LEECH:
        await send_message(message, "The Leech command is currently disabled.")
        return
    try:
        up_dest, chunk_size = _parse_args(message.text or "")
        payload = await _load_payload_from_reply(message)
        entries = _extract_entries(payload)
        if not entries:
            raise ValueError("No entries found in JSON.")
        segments = _build_segments(entries)
        dest_chat, dest_topic = _parse_topic_destination(up_dest)
    except Exception as exc:
        await send_message(
            message,
            f"Topic clone setup failed: {exc}\n\n"
            f"Usage: /{BotCommands.TopicCloneJsonCommand[0]} -up chat_id|topic_id [-c 5] "
            "or -up https://t.me/c/<chat>/<topic>/<message> as a reply to export JSON.",
        )
        return

    total_segments = len(segments)
    total_text = sum(1 for segment in segments if segment["type"] == "text")
    total_links = sum(len(segment["items"]) for segment in segments if segment["type"] == "links")
    status = await send_message(
        message,
        _format_status_text(
            stage="Starting",
            segment_index=0,
            total_segments=total_segments,
            sent_text=0,
            total_text=total_text,
            processed_links=0,
            total_links=total_links,
            chunk_info=f"size {chunk_size}",
        ),
    )
    sent_text = 0
    processed_links = 0
    try:
        for index, segment in enumerate(segments, start=1):
            if segment["type"] == "text":
                await _safe_status_edit(
                    status,
                    _format_status_text(
                        stage="Sending text",
                        segment_index=index,
                        total_segments=total_segments,
                        sent_text=sent_text,
                        total_text=total_text,
                        processed_links=processed_links,
                        total_links=total_links,
                    )
                )
                await TgClient.bot.send_message(
                    chat_id=dest_chat,
                    text=segment["value"],
                    disable_web_page_preview=True,
                    message_thread_id=dest_topic,
                )
                sent_text += 1
                await _safe_status_edit(
                    status,
                    _format_status_text(
                        stage="Text sent",
                        segment_index=index,
                        total_segments=total_segments,
                        sent_text=sent_text,
                        total_text=total_text,
                        processed_links=processed_links,
                        total_links=total_links,
                    )
                )
                continue
            links = segment["items"]
            await _safe_status_edit(
                status,
                _format_status_text(
                    stage="Processing links",
                    segment_index=index,
                    total_segments=total_segments,
                    sent_text=sent_text,
                    total_text=total_text,
                    processed_links=processed_links,
                    total_links=total_links,
                    chunk_info=f"0/{len(links)} in current segment",
                )
            )

            async def _on_chunk(update: dict[str, Any]) -> None:
                chunk_done = int(update.get("chunk_completed", 0))
                chunk_total = int(update.get("chunk_total", len(links)))
                chunk_start = int(update.get("chunk_start", 0))
                chunk_end = int(update.get("chunk_end", 0))
                chunk_info = (
                    f"{chunk_start}-{chunk_end}/{chunk_total} queued"
                    if chunk_start and chunk_end
                    else f"{chunk_done}/{chunk_total} done in current segment"
                )
                await _safe_status_edit(
                    status,
                    _format_status_text(
                        stage="Processing links",
                        segment_index=index,
                        total_segments=total_segments,
                        sent_text=sent_text,
                        total_text=total_text,
                        processed_links=processed_links + chunk_done,
                        total_links=total_links,
                        chunk_info=chunk_info,
                    )
                )

            segment_processed = await _process_link_segment(
                client,
                message,
                up_dest,
                links,
                chunk_size,
                status_callback=_on_chunk,
            )
            processed_links += segment_processed
    except Exception as exc:
        await _safe_status_edit(
            status,
            _format_status_text(
                stage=f"Failed: {exc}",
                segment_index=total_segments,
                total_segments=total_segments,
                sent_text=sent_text,
                total_text=total_text,
                processed_links=processed_links,
                total_links=total_links,
            )
        )
        return

    await _safe_status_edit(
        status,
        _format_status_text(
            stage="Completed",
            segment_index=total_segments,
            total_segments=total_segments,
            sent_text=sent_text,
            total_text=total_text,
            processed_links=processed_links,
            total_links=total_links,
        )
    )

