from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from .env_utils import load_msz_env_files
from .telegram_index import FolderHeading, format_index, normalize_heading_text


def _runtime_dir() -> Path:
    return Path(os.getenv("HEROKU_RUNTIME_DIR", "runtime")).expanduser().resolve()


def _load_env_files() -> None:
    load_msz_env_files()


def _heroku_imports():
    heroku_dir = Path(__file__).resolve().parents[1] / "heroku_bot"
    if str(heroku_dir) not in sys.path:
        sys.path.insert(0, str(heroku_dir))
    from config import load_settings
    from export_topic_list import _parse_export_link, _resolve_start_message_id
    from telegram_client import TelegramService

    return load_settings, _parse_export_link, _resolve_start_message_id, TelegramService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an editable text-heading folder index from a Telegram topic.")
    parser.add_argument("topic_link", help="Private Telegram topic link, e.g. https://t.me/c/<chat>/<topic>/<message>")
    parser.add_argument("--out", default="", help="Output .txt index path.")
    parser.add_argument("--config", default=os.getenv("HEROKU_CONFIG_PATH", "heroku_bot/config.yaml"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-delay-sec", type=float, default=1.0)
    parser.add_argument("--onwards", action="store_true", help="Start from the linked message instead of topic root.")
    return parser


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120].strip(" .") or "telegram_topic"


def _default_output_path(parsed, topic_title: str | None = None) -> Path:
    runtime_dir = _runtime_dir()
    out_dir = runtime_dir / "telegram_indexes"
    out_dir.mkdir(parents=True, exist_ok=True)
    if topic_title:
        return out_dir / f"{_safe_filename(topic_title)}.txt"
    topic_id = parsed.topic_id if parsed.topic_id is not None else parsed.message_id
    return out_dir / f"telegram_folder_index_{abs(parsed.chat_id)}_{topic_id}.txt"


async def _linked_message_title(telegram, chat_id: int, message_id: int) -> str | None:
    try:
        message = await telegram.get_message(chat_id, message_id)
    except Exception:
        return None
    for attr in ("forum_topic_created", "forum_topic_edited"):
        event = getattr(message, attr, None) if message is not None else None
        title = getattr(event, "title", None) if event is not None else None
        if title:
            return normalize_heading_text(str(title))
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return normalize_heading_text(text) or None


async def run(args: argparse.Namespace) -> int:
    load_settings, parse_export_link, resolve_start_message_id, TelegramService = _heroku_imports()
    settings, _ = load_settings(Path(args.config))
    parsed = parse_export_link(args.topic_link)
    if not parsed.is_topic:
        raise ValueError("Telegram folder index generation requires a private forum topic link.")
    start_message_id = resolve_start_message_id(parsed, args.onwards)

    telegram = TelegramService(settings, logger=logging.getLogger("telegram_folder_index"), receive_updates=False)
    headings: list[FolderHeading] = []
    await telegram.start()
    try:
        topic_title = await telegram.get_forum_topic_title(parsed.chat_id, parsed.topic_id)
        if not topic_title and parsed.message_id != parsed.topic_id:
            topic_title = await _linked_message_title(telegram, parsed.chat_id, parsed.message_id)
        output = Path(args.out).expanduser() if args.out else _default_output_path(parsed, topic_title)
        if not output.is_absolute():
            output = Path.cwd() / output
        if topic_title:
            print(f"Topic title: {topic_title}", flush=True)
        print(f"Listing Telegram topic messages from {start_message_id}...", flush=True)
        message_ids = await telegram.list_topic_message_ids(
            parsed.chat_id,
            parsed.topic_id,
            start_from_message_id=start_message_id,
            batch_size=args.batch_size,
        )
        ordered_ids = sorted(set(message_ids))
        print(f"Found {len(ordered_ids)} topic messages. Fetching text messages...", flush=True)
        for start in range(0, len(ordered_ids), args.batch_size):
            chunk = ordered_ids[start : start + args.batch_size]
            messages = await telegram.get_messages_bulk(parsed.chat_id, chunk)
            by_id = {message.id: message for message in messages}
            for message_id in chunk:
                message = by_id.get(message_id)
                if message is None:
                    continue
                text = normalize_heading_text(getattr(message, "text", "") or "")
                if not text:
                    continue
                headings.append(FolderHeading(message_id=message.id, level=1, name=text, enabled=True))
            if start + args.batch_size < len(ordered_ids) and args.batch_delay_sec > 0:
                await asyncio.sleep(args.batch_delay_sec)
    finally:
        await telegram.stop()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        format_index(
            args.topic_link,
            headings,
            start_message_id=start_message_id,
            topic_title=topic_title or "",
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(headings)} text headings to: {output}")
    return 0


def main() -> None:
    _load_env_files()
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
