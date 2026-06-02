from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .env_utils import load_msz_env_files
from .gdrive_upload import GDRIVE_FOLDER_MIME, GoogleDriveResumableUploader
from .msz_api import MszApiClient, MszEntry, ensure_disk_space, natural_sort_key, norm_rel
from .msz_to_gdrive import _looks_like_msz_id, _safe_local_path


def _runtime_dir() -> Path:
    return Path(os.getenv("HEROKU_RUNTIME_DIR", "runtime")).expanduser().resolve()


def _load_env_files() -> None:
    load_msz_env_files()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _heroku_imports():
    heroku_dir = Path(__file__).resolve().parents[1] / "heroku_bot"
    if str(heroku_dir) not in sys.path:
        sys.path.insert(0, str(heroku_dir))
    from config import load_settings
    from export_topic_list import _parse_export_link
    from telegram_client import TelegramService

    return load_settings, _parse_export_link, TelegramService


@dataclass(frozen=True)
class SourceFile:
    source_id: str
    rel_path: str
    size: int | None
    source_entry: Any

    @property
    def name(self) -> str:
        return Path(self.rel_path).name


class TelegramUploadState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._read()
        self.data.setdefault("version", 1)
        self.data.setdefault("files", {})
        self.data.setdefault("folders", {})

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = _utc_now()
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def file_key(source_type: str, source_id: str, rel_path: str, size: int | None, chat_id: int, topic_id: int) -> str:
        return f"{source_type}|{source_id}|{norm_rel(rel_path)}|{size if size is not None else 'unknown'}|{chat_id}|{topic_id}"

    @staticmethod
    def folder_key(source_type: str, folder_path: str, chat_id: int, topic_id: int) -> str:
        return f"{source_type}|folder|{norm_rel(folder_path)}|{chat_id}|{topic_id}"

    def uploaded_file(self, key: str) -> bool:
        record = self.data["files"].get(key)
        return isinstance(record, dict) and record.get("status") == "uploaded"

    def failed_keys(self) -> set[str]:
        return {
            key
            for key, record in self.data.get("files", {}).items()
            if isinstance(record, dict) and record.get("status") == "failed"
        }

    def folder_message_id(self, key: str) -> int | None:
        record = self.data["folders"].get(key)
        if not isinstance(record, dict):
            return None
        try:
            return int(record.get("message_id"))
        except (TypeError, ValueError):
            return None

    def mark_folder(self, key: str, folder_path: str, message_id: int) -> None:
        self.data["folders"][key] = {
            "folder_path": norm_rel(folder_path),
            "message_id": message_id,
            "updated_at": _utc_now(),
        }
        self.save()

    def mark_file(self, key: str, status: str, **payload: Any) -> None:
        previous = self.data["files"].get(key, {})
        attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
        self.data["files"][key] = {
            **(previous if isinstance(previous, dict) else {}),
            **payload,
            "status": status,
            "attempts": attempts + (1 if status == "uploading" else 0),
            "updated_at": _utc_now(),
        }
        self.save()


def _append_failed_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["failed_at"] = _utc_now()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


class TransferLogger:
    def __init__(self, prefix: str, label: str, file_name: str, total_size: int | None) -> None:
        self.prefix = prefix
        self.label = label
        self.file_name = file_name
        self.total_size = total_size
        self.started_at = time.monotonic()
        self.last_log_at = 0.0
        self.last_done = -1

    def __call__(self, done: int, total_size: int | None = None, *args) -> None:
        total = total_size or self.total_size
        now = time.monotonic()
        complete = total is not None and done >= total
        enough_time = now - self.last_log_at >= 5.0
        enough_bytes = self.last_done < 0 or done - self.last_done >= 25 * 1024 * 1024
        if not (complete or enough_time or enough_bytes):
            return
        self.last_log_at = now
        self.last_done = done
        elapsed = max(0.001, now - self.started_at)
        speed_bps = max(0, done) / elapsed
        eta = ((total - done) / speed_bps) if total and speed_bps > 0 and done < total else None
        if total:
            percent = min(100.0, done / total * 100)
            print(
                f"{self.prefix} {self.label}: {self.file_name} "
                f"{_format_bytes(done)} / {_format_bytes(total)} ({percent:.1f}%) "
                f"at {_format_bytes(int(speed_bps))}/s, ETA {_format_duration(eta)}",
                flush=True,
            )
        else:
            print(
                f"{self.prefix} {self.label}: {self.file_name} {_format_bytes(done)} "
                f"at {_format_bytes(int(speed_bps))}/s, ETA --",
                flush=True,
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload Google Drive or MSZ Drive files to a Telegram topic.")
    parser.add_argument("source", nargs="?", default="", help="Google Drive folder/file URL/id, or MSZ URL/id/path.")
    parser.add_argument("dest", nargs="?", default="", help="Telegram topic link. Defaults to TELEGRAM_TARGET_TOPIC_LINK.")
    parser.add_argument("--source-type", choices=("gdrive", "msz"), required=True)
    parser.add_argument("--telegram-topic-link", default=os.getenv("TELEGRAM_TARGET_TOPIC_LINK", ""))
    parser.add_argument("--gdrive-token-pickle", default=os.getenv("GDRIVE_TOKEN_PICKLE", "token.pickle"))
    parser.add_argument("--msz-source-path", default=os.getenv("MSZ_SOURCE_PATH", ""))
    parser.add_argument("--msz-source-id", default=os.getenv("MSZ_SOURCE_ID", ""))
    parser.add_argument("--msz-source-url", default=os.getenv("MSZ_SOURCE_URL", ""))
    parser.add_argument("--base-url", default=os.getenv("MSZ_BASE_URL", "https://cloud.medicalstudyzone.com"))
    parser.add_argument("--api-token", default=os.getenv("MSZ_API_TOKEN", ""))
    parser.add_argument("--config", default=os.getenv("HEROKU_CONFIG_PATH", "heroku_bot/config.yaml"))
    parser.add_argument("--runtime-dir", default=str(_runtime_dir()))
    parser.add_argument("--state-file", default="")
    parser.add_argument("--failed-log", default="")
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--retry-failed-only", action="store_true")
    parser.add_argument("--delete-failed-downloads", action="store_true")
    parser.set_defaults(no_resume=True)
    parser.add_argument("--no-resume", dest="no_resume", action="store_true", help="Ignore previous transfer state. Default.")
    parser.add_argument("--resume", dest="no_resume", action="store_false", help="Resume from previous successful transfer state.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-downloads", action="store_true")
    return parser


def _telegram_dest(args: argparse.Namespace) -> str:
    return args.telegram_topic_link.strip() or args.dest.strip() or os.getenv("TELEGRAM_TARGET_TOPIC_LINK", "").strip()


def _safe_local_path(staging_dir: Path, rel_path: str) -> Path:
    parts = [Path(part).name for part in norm_rel(rel_path).split("/") if part.strip()]
    return staging_dir.joinpath(*(parts or ["download.bin"]))


def _folder_path(rel_path: str) -> str:
    parent = norm_rel(str(Path(rel_path).parent))
    return "" if parent == "." else parent


def _folders_for_path(folder_path: str) -> list[tuple[str, str]]:
    segments = [part.strip() for part in norm_rel(folder_path).split("/") if part.strip()]
    folders: list[tuple[str, str]] = []
    current: list[str] = []
    for segment in segments:
        current.append(segment)
        folders.append((norm_rel("/".join(current)), segment))
    return folders


def _caption_for_file(path: Path) -> str:
    return path.name


def _reply_kwargs(topic_id: int) -> dict[str, Any]:
    try:
        from pyrogram.types import ReplyParameters

        return {"reply_parameters": ReplyParameters(message_id=topic_id)}
    except Exception:
        return {"reply_to_message_id": topic_id}


def _media_kind_for_file(path: Path) -> str:
    mime_type = (mimetypes.guess_type(path.name)[0] or "").lower()
    suffix = path.suffix.lower()
    if mime_type.startswith("image/") and suffix not in {".svg", ".svgz"}:
        return "photo"
    if mime_type.startswith("video/") or suffix in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
        return "video"
    if mime_type.startswith("audio/") or suffix in {".mp3", ".m4a", ".flac", ".wav", ".aac"}:
        return "audio"
    return "document"


def _source_rel_from_msz(root: MszEntry, entry: MszEntry) -> str:
    if root.is_file:
        return entry.name
    root_name = Path(root.name).name or "MSZ Source"
    return norm_rel(f"{root_name}/{MszApiClient.rel_path_under(root, entry)}")


def _gdrive_source_files(args: argparse.Namespace) -> tuple[GoogleDriveResumableUploader, list[SourceFile]]:
    gdrive = GoogleDriveResumableUploader(Path(args.gdrive_token_pickle))
    root_id = GoogleDriveResumableUploader.extract_folder_id(args.source)
    if not root_id:
        raise ValueError("Provide a Google Drive folder/file URL or id.")
    files = [
        SourceFile(
            source_id=item["id"],
            rel_path=norm_rel(rel_path),
            size=int(item["size"]) if str(item.get("size", "")).isdigit() else None,
            source_entry=item,
        )
        for item, rel_path in gdrive.iter_files_under(root_id)
        if item.get("mimeType") != GDRIVE_FOLDER_MIME
    ]
    return gdrive, files


def _resolve_msz_source_args(args: argparse.Namespace) -> tuple[str, str, str]:
    source_url = args.msz_source_url.strip()
    source_id = args.msz_source_id.strip()
    source_path = args.msz_source_path.strip()
    source = args.source.strip()
    if source and not (source_url or source_id or source_path):
        if source.startswith(("http://", "https://")):
            source_url = source
        elif _looks_like_msz_id(source):
            source_id = source
        else:
            source_path = source
    return source_path, source_id, source_url


def _msz_source_files(args: argparse.Namespace) -> tuple[MszApiClient, list[SourceFile]]:
    source_path, source_id, source_url = _resolve_msz_source_args(args)
    if args.source and not (source_path or source_id or source_url):
        source_url = args.source if args.source.startswith(("http://", "https://")) else ""
        source_id = "" if source_url else args.source
    if not (source_path or source_id or source_url):
        raise ValueError("Provide an MSZ source URL/id/path, or set MSZ_SOURCE_PATH/MSZ_SOURCE_ID/MSZ_SOURCE_URL.")
    msz = MszApiClient(args.base_url, args.api_token or os.getenv("MSZ_API_TOKEN", ""))

    def _log(message: str) -> None:
        print(f"[msz] {message}", flush=True)

    root, entries = msz.resolve_source_entries(source_path=source_path, source_id=source_id, source_url=source_url, log=_log)
    files = [
        SourceFile(
            source_id=str(entry.id),
            rel_path=_source_rel_from_msz(root, entry),
            size=entry.size,
            source_entry=entry,
        )
        for entry in entries
    ]
    return msz, files


async def _send_folder_heading(
    telegram,
    *,
    source_type: str,
    chat_id: int,
    topic_id: int,
    folder_path: str,
    folder_name: str,
    state: TelegramUploadState,
    no_resume: bool,
    sent_folder_keys: set[str],
) -> int:
    key = TelegramUploadState.folder_key(source_type, folder_path, chat_id, topic_id)
    if key in sent_folder_keys:
        existing = state.folder_message_id(key)
        return existing or 0
    existing = None if no_resume else state.folder_message_id(key)
    if existing:
        sent_folder_keys.add(key)
        return existing
    message = await telegram.send_text_to_topic(
        chat_id,
        topic_id,
        folder_name,
        entities=None,
        disable_web_page_preview=True,
    )
    state.mark_folder(key, folder_path, int(message.id))
    sent_folder_keys.add(key)
    print(f"[folder] {folder_path}", flush=True)
    return int(message.id)


async def _send_local_file(telegram, chat_id: int, topic_id: int, local_path: Path, progress) -> int:
    await telegram.ensure_peer_cached(chat_id)
    caption = _caption_for_file(local_path)
    kind = _media_kind_for_file(local_path)
    progress_callback = telegram._wrap_progress_callback(progress)
    if kind == "photo":
        message = await telegram.write_call(
            "send_drive_photo_to_topic",
            lambda: telegram.app.send_photo(
                chat_id=chat_id,
                photo=str(local_path),
                caption=caption,
                progress=progress_callback,
                **_reply_kwargs(topic_id),
            ),
        )
    elif kind == "video":
        duration, width, height = await telegram._probe_video_metadata(local_path)
        thumb = await telegram._generate_video_thumbnail(local_path, duration)
        message = await telegram.write_call(
            "send_drive_video_to_topic",
            lambda: telegram.app.send_video(
                chat_id=chat_id,
                video=str(local_path),
                caption=caption,
                duration=duration,
                width=width,
                height=height,
                thumb=str(thumb) if thumb else None,
                file_name=local_path.name,
                supports_streaming=True,
                progress=progress_callback,
                **_reply_kwargs(topic_id),
            ),
        )
    elif kind == "audio":
        message = await telegram.write_call(
            "send_drive_audio_to_topic",
            lambda: telegram.app.send_audio(
                chat_id=chat_id,
                audio=str(local_path),
                caption=caption,
                file_name=local_path.name,
                progress=progress_callback,
                **_reply_kwargs(topic_id),
            ),
        )
    else:
        message = await telegram.write_call(
            "send_drive_document_to_topic",
            lambda: telegram.app.send_document(
                chat_id=chat_id,
                document=str(local_path),
                caption=caption,
                file_name=local_path.name,
                progress=progress_callback,
                **_reply_kwargs(topic_id),
            ),
        )
    return int(message.id)


async def run(args: argparse.Namespace) -> int:
    target_link = _telegram_dest(args)
    if not target_link:
        raise ValueError("Provide a Telegram topic destination link as dest or --telegram-topic-link.")

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    staging_dir = runtime_dir / "drive_to_telegram_staging"
    state_file = Path(args.state_file) if args.state_file else runtime_dir / "state" / "drive_to_telegram_state.json"
    failed_log = Path(args.failed_log) if args.failed_log else runtime_dir / "logs" / "drive_to_telegram_failed.jsonl"
    staging_dir.mkdir(parents=True, exist_ok=True)

    if args.source_type == "gdrive":
        source_client, files = _gdrive_source_files(args)
        download_label = "GDrive download"
    else:
        source_client, files = _msz_source_files(args)
        download_label = "MSZ download"

    files = sorted(files, key=lambda item: natural_sort_key(item.rel_path))
    load_settings, parse_export_link, TelegramService = _heroku_imports()
    settings, _ = load_settings(args.config)
    parsed = parse_export_link(target_link)
    if not parsed.is_topic:
        raise ValueError("Telegram destination must be a private forum topic link.")

    state = TelegramUploadState(state_file)
    failed_keys = state.failed_keys()
    telegram = TelegramService(settings, logger=logging.getLogger("drive_to_telegram"), receive_updates=False)

    total = uploaded = skipped = failed = 0
    sent_folder_keys: set[str] = set()
    await telegram.start()
    try:
        for source_file in files:
            total += 1
            prefix = f"[{total}/{len(files)}]"
            key = TelegramUploadState.file_key(
                args.source_type,
                source_file.source_id,
                source_file.rel_path,
                source_file.size,
                parsed.chat_id,
                parsed.topic_id,
            )
            local_path = _safe_local_path(staging_dir, source_file.rel_path)
            try:
                if args.retry_failed_only and key not in failed_keys:
                    skipped += 1
                    print(f"{prefix} Skipping not-failed file: {source_file.rel_path}", flush=True)
                    continue
                if not args.no_resume and state.uploaded_file(key):
                    skipped += 1
                    print(f"{prefix} Skipping already uploaded: {source_file.rel_path}", flush=True)
                    continue
                folder_path = _folder_path(source_file.rel_path)
                if args.dry_run:
                    print(f"{prefix} DRY RUN: folders={folder_path or '(root)'} file={source_file.rel_path}", flush=True)
                    skipped += 1
                    continue
                for folder_full_path, folder_name in _folders_for_path(folder_path):
                    await _send_folder_heading(
                        telegram,
                        source_type=args.source_type,
                        chat_id=parsed.chat_id,
                        topic_id=parsed.topic_id,
                        folder_path=folder_full_path,
                        folder_name=folder_name,
                        state=state,
                        no_resume=args.no_resume,
                        sent_folder_keys=sent_folder_keys,
                    )

                ensure_disk_space(staging_dir, source_file.size or 0)
                if not local_path.exists() or (source_file.size is not None and local_path.stat().st_size != source_file.size):
                    print(f"{prefix} Downloading: {source_file.rel_path}", flush=True)
                    download_progress = TransferLogger(prefix, download_label, source_file.name, source_file.size)
                    if args.source_type == "gdrive":
                        await asyncio.to_thread(
                            source_client.download_file,
                            source_file.source_id,
                            local_path,
                            progress_callback=download_progress,
                        )
                    else:
                        await asyncio.to_thread(
                            source_client.download_file,
                            source_file.source_entry,
                            local_path,
                            max_retries=args.max_retries,
                            progress_callback=download_progress,
                        )
                    download_progress(local_path.stat().st_size, source_file.size)
                else:
                    print(f"{prefix} Reusing existing download: {local_path}", flush=True)

                state.mark_file(
                    key,
                    "uploading",
                    source_type=args.source_type,
                    source_id=source_file.source_id,
                    rel_path=source_file.rel_path,
                    size=source_file.size,
                    local_path=str(local_path),
                )
                print(f"{prefix} Uploading to Telegram: {source_file.rel_path}", flush=True)
                upload_progress = TransferLogger(prefix, "Telegram upload", local_path.name, local_path.stat().st_size)
                message_id = await _send_local_file(telegram, parsed.chat_id, parsed.topic_id, local_path, upload_progress)
                upload_progress(local_path.stat().st_size, local_path.stat().st_size)
                state.mark_file(
                    key,
                    "uploaded",
                    source_type=args.source_type,
                    source_id=source_file.source_id,
                    rel_path=source_file.rel_path,
                    size=source_file.size,
                    telegram_message_id=message_id,
                )
                uploaded += 1
                if not args.keep_downloads:
                    await asyncio.to_thread(local_path.unlink, missing_ok=True)
            except Exception as exc:
                failed += 1
                state.mark_file(
                    key,
                    "failed",
                    source_type=args.source_type,
                    source_id=source_file.source_id,
                    rel_path=source_file.rel_path,
                    size=source_file.size,
                    error=str(exc),
                )
                _append_failed_log(
                    failed_log,
                    {
                        "source_type": args.source_type,
                        "source_id": source_file.source_id,
                        "rel_path": source_file.rel_path,
                        "size": source_file.size,
                        "local_path": str(local_path),
                        "error": str(exc),
                    },
                )
                print(f"{prefix} ERROR: {source_file.rel_path}: {exc}", flush=True)
                if args.delete_failed_downloads and local_path.exists():
                    await asyncio.to_thread(local_path.unlink, missing_ok=True)
                if not args.continue_on_error:
                    print("Stopping after first error. Use --continue-on-error to keep going.", flush=True)
                    break
    finally:
        await telegram.stop()
        if not args.keep_downloads and (failed == 0 or args.delete_failed_downloads):
            shutil.rmtree(staging_dir, ignore_errors=True)

    print(f"Done. Total={total} Uploaded={uploaded} Skipped={skipped} Failed={failed}")
    print(f"State file: {state_file}")
    print(f"Failed log: {failed_log}")
    return 1 if failed else 0


def main() -> None:
    _load_env_files()
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
