from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .browser_upload import MszBrowserUploader
from .gdrive_upload import GoogleDriveResumableUploader
from .msz_api import MszApiClient, ensure_disk_space, infer_extension_from_signature, norm_rel
from .msz_upload import DEFAULT_API_MAX_BYTES, _find_remote_match, _remote_parent_folder
from .sources import message_has_downloadable_media, telegram_media_filename
from .telegram_index import UNSORTED_FOLDER, assign_media_folder, folder_paths_by_heading, parse_index


def _runtime_dir() -> Path:
    return Path(os.getenv("HEROKU_RUNTIME_DIR", "runtime")).expanduser().resolve()


def _load_env_files() -> None:
    for env_path in (Path("MSZDRIVE_uploader/.env"), Path(".env"), Path("heroku_bot/.env")):
        if env_path.exists():
            load_dotenv(env_path, override=False)


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
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


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


class TelegramDriveState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._read()
        self.data.setdefault("version", 1)
        self.data.setdefault("files", {})

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
    def key(message_id: int, target: str, destination_path: str, size: int | None) -> str:
        return f"{message_id}|{target}|{norm_rel(destination_path)}|{size if size is not None else 'unknown'}"

    def is_uploaded(self, message_id: int, target: str, destination_path: str, size: int | None) -> bool:
        record = self.data["files"].get(self.key(message_id, target, destination_path, size))
        return isinstance(record, dict) and record.get("status") == "uploaded"

    def mark(self, message_id: int, target: str, destination_path: str, size: int | None, status: str, **extra) -> None:
        key = self.key(message_id, target, destination_path, size)
        previous = self.data["files"].get(key, {})
        attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
        self.data["files"][key] = {
            "status": status,
            "message_id": message_id,
            "target": target,
            "destination_path": norm_rel(destination_path),
            "size": size,
            "attempts": attempts + (1 if status == "uploading" else 0),
            "updated_at": _utc_now(),
            **extra,
        }
        self.save()

    def failed_keys(self) -> set[str]:
        return {
            key
            for key, record in self.data.get("files", {}).items()
            if isinstance(record, dict) and record.get("status") == "failed"
        }


def _append_failed_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["failed_at"] = _utc_now()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload Telegram topic media to MSZ Drive, Google Drive, or both.")
    parser.add_argument("--index", required=True, help="Editable text folder index generated by telegram_folder_index.")
    parser.add_argument("--target", choices=("msz", "gdrive", "both"), required=True)
    parser.add_argument("--target-folder", default=os.getenv("MSZ_TARGET_FOLDER", ""), help="MSZ target folder/path.")
    parser.add_argument("--gdrive-folder-id", default=os.getenv("GDRIVE_FOLDER_ID", ""))
    parser.add_argument("--gdrive-token-pickle", default=os.getenv("GDRIVE_TOKEN_PICKLE", "token.pickle"))
    parser.add_argument("--base-url", default=os.getenv("MSZ_BASE_URL", "https://cloud.medicalstudyzone.com"))
    parser.add_argument("--api-token", default=os.getenv("MSZ_API_TOKEN", ""))
    parser.add_argument("--email", default=os.getenv("MSZ_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("MSZ_PASSWORD", ""))
    parser.add_argument("--api-max-bytes", type=int, default=int(os.getenv("MSZ_API_MAX_BYTES", DEFAULT_API_MAX_BYTES)))
    parser.add_argument("--browser-state-file", default="")
    parser.add_argument("--browser-folder-url", default=os.getenv("MSZ_BROWSER_FOLDER_URL", ""))
    parser.add_argument("--browser-headed", action="store_true")
    parser.add_argument("--chromium-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""))
    parser.add_argument("--config", default=os.getenv("HEROKU_CONFIG_PATH", "heroku_bot/config.yaml"))
    parser.add_argument("--runtime-dir", default=str(_runtime_dir()))
    parser.add_argument("--state-file", default="")
    parser.add_argument("--failed-log", default="")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-delay-sec", type=float, default=1.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument(
        "--tg-download",
        choices=("hyper", "auto", "normal"),
        default=os.getenv("TG_DOWNLOAD_MODE", "hyper"),
        help="Telegram media download mode. hyper requires HELPER_TOKENS; auto falls back to normal.",
    )
    parser.add_argument("--retry-failed-only", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--keep-downloads", action="store_true")
    parser.add_argument("--delete-failed-downloads", action="store_true")
    parser.add_argument(
        "--above",
        action="store_true",
        help="Assign media before each enabled text heading to that heading folder instead of media after it.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _media_object(message: object) -> object | None:
    media_name = getattr(getattr(message, "media", None), "value", "")
    media = getattr(message, media_name, None) if media_name else None
    if media is not None:
        return media
    for attr in ("document", "video", "audio", "voice", "animation", "photo", "video_note"):
        media = getattr(message, attr, None)
        if media is not None:
            return media
    return None


def _media_size(message: object) -> int | None:
    media = _media_object(message)
    value = getattr(media, "file_size", None) if media is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _telegram_text(message: object) -> str:
    return " ".join(part.strip() for part in str(getattr(message, "text", "") or "").splitlines() if part.strip())


def _destination_paths(target_folder: str, active_folder: str, file_name: str) -> tuple[str, str]:
    folder = assign_media_folder(0, active_folder)
    rel = norm_rel(f"{folder}/{file_name}")
    msz_rel = norm_rel(f"{target_folder}/{rel}") if target_folder else rel
    return msz_rel, rel


def _destination_parent(destination: str) -> str:
    parent = norm_rel(str(Path(destination).parent))
    return "" if parent == "." else parent


def _topic_root_from_headings(headings) -> str:
    for heading in headings:
        if heading.enabled and heading.level == 1:
            return norm_rel(heading.name)
    return ""


def _root_heading_paths(paths_by_heading: dict[int, str], root_folder: str) -> dict[int, str]:
    root_folder = norm_rel(root_folder)
    if not root_folder:
        return paths_by_heading
    rooted: dict[int, str] = {}
    for message_id, folder_path in paths_by_heading.items():
        folder_path = norm_rel(folder_path)
        if folder_path == root_folder or folder_path.startswith(root_folder + "/"):
            rooted[message_id] = folder_path
        else:
            rooted[message_id] = norm_rel(f"{root_folder}/{folder_path}")
    return rooted


def _topic_root(folder_index) -> str:
    return norm_rel(getattr(folder_index, "topic_title", "") or _topic_root_from_headings(folder_index.headings))


def _ensure_filename(message: object, downloaded: Path) -> str:
    file_name = telegram_media_filename(message)
    if not Path(file_name).suffix:
        inferred = infer_extension_from_signature(downloaded)
        if inferred:
            file_name += inferred
    return Path(file_name).name


def _prefetch_existing_gdrive_uploads(
    *,
    gdrive: GoogleDriveResumableUploader | None,
    root_folder_id: str,
    message_id: int,
    target_paths: dict[str, str],
    size: int | None,
    pending_targets: list[str],
    state: TelegramDriveState,
    prefix: str,
) -> list[str]:
    if gdrive is None or "gdrive" not in pending_targets:
        return pending_targets
    destination = target_paths["gdrive"]
    parent_id = gdrive.ensure_folder_path(root_folder_id, _destination_parent(destination))
    existing = gdrive.existing_file_matches(parent_id, Path(destination).name, size)
    if not existing:
        return pending_targets
    state.mark(message_id, "gdrive", destination, size, "uploaded", remote_id=existing.get("id"), preexisting=True)
    print(f"{prefix} Skipping existing Google Drive file before download: {destination}", flush=True)
    return [target for target in pending_targets if target != "gdrive"]


def _message_downloadable(message: object | None) -> bool:
    return message is not None and message_has_downloadable_media(message)


def _media_folder_assignments(
    messages: list[object],
    paths_by_heading: dict[int, str],
    heading_ids: set[int],
    *,
    above: bool,
) -> list[tuple[object, str]]:
    assignments: list[tuple[object, str]] = []
    if not above:
        active_folder = UNSORTED_FOLDER
        for message in messages:
            message_id = getattr(message, "id", None)
            if message_id in paths_by_heading:
                active_folder = paths_by_heading[message_id]
                print(f"[folder] {message_id}: {active_folder}", flush=True)
                continue
            if message_id in heading_ids:
                continue
            if _message_downloadable(message):
                assignments.append((message, active_folder))
        return assignments

    pending_media: list[object] = []
    for message in messages:
        message_id = getattr(message, "id", None)
        if message_id in paths_by_heading:
            folder = paths_by_heading[message_id]
            print(f"[folder above] {message_id}: {folder} ({len(pending_media)} media)", flush=True)
            assignments.extend((media, folder) for media in pending_media)
            pending_media = []
            continue
        if message_id in heading_ids:
            continue
        if _message_downloadable(message):
            pending_media.append(message)

    if pending_media:
        print(f"[folder above] {len(pending_media)} trailing media had no following heading; using {UNSORTED_FOLDER}", flush=True)
        assignments.extend((media, UNSORTED_FOLDER) for media in pending_media)
    return assignments


async def _upload_msz(
    *,
    local_path: Path,
    destination_path: str,
    size: int,
    args: argparse.Namespace,
    client: MszApiClient,
    browser_holder: dict[str, MszBrowserUploader | None],
    browser_state_file: Path,
) -> object | None:
    if size < args.api_max_bytes:
        return await asyncio.to_thread(client.upload_file, local_path, destination_path, args.max_retries)

    browser = browser_holder.get("browser")
    if browser is None:
        browser = MszBrowserUploader(
            base_url=args.base_url,
            email=args.email or os.getenv("MSZ_EMAIL", ""),
            password=args.password or os.getenv("MSZ_PASSWORD", ""),
            storage_state_path=browser_state_file,
            headless=not args.browser_headed,
            executable_path=args.chromium_executable,
            folder_url=args.browser_folder_url,
            verbose=True,
        )
        browser_holder["browser"] = browser
    await browser.upload(local_path, _remote_parent_folder(destination_path) or norm_rel(args.target_folder))
    return None


async def run(args: argparse.Namespace) -> int:
    folder_index = parse_index(Path(args.index))
    root_folder = _topic_root(folder_index)
    paths_by_heading = _root_heading_paths(folder_paths_by_heading(folder_index.headings), root_folder)
    heading_ids = {heading.message_id for heading in folder_index.headings}
    if args.target in {"msz", "both"} and not args.target_folder:
        raise ValueError("--target-folder is required for --target msz or --target both.")
    if args.target in {"gdrive", "both"} and not args.gdrive_folder_id:
        raise ValueError("--gdrive-folder-id or GDRIVE_FOLDER_ID is required for --target gdrive or --target both.")

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    staging_dir = runtime_dir / "telegram_to_drive_staging"
    state_file = Path(args.state_file) if args.state_file else runtime_dir / "state" / "telegram_to_drive_state.json"
    failed_log = Path(args.failed_log) if args.failed_log else runtime_dir / "logs" / "telegram_to_drive_failed.jsonl"
    browser_state_file = (
        Path(args.browser_state_file)
        if args.browser_state_file
        else runtime_dir / "state" / "msz_browser_state.json"
    )
    staging_dir.mkdir(parents=True, exist_ok=True)

    load_settings, parse_export_link, TelegramService = _heroku_imports()
    settings, _ = load_settings(args.config)
    parsed = parse_export_link(folder_index.topic_link)
    if not parsed.is_topic:
        raise ValueError("The index topic_link must be a private forum topic link.")
    start_message_id = folder_index.start_message_id or parsed.topic_id

    state = TelegramDriveState(state_file)
    msz = None if args.dry_run or args.target == "gdrive" else MszApiClient(args.base_url, args.api_token or os.getenv("MSZ_API_TOKEN", ""))
    gdrive = None
    if not args.dry_run and args.target in {"gdrive", "both"}:
        gdrive = GoogleDriveResumableUploader(Path(args.gdrive_token_pickle))
    browser_holder: dict[str, MszBrowserUploader | None] = {"browser": None}

    telegram = TelegramService(settings, logger=logging.getLogger("telegram_to_drive"), receive_updates=False)
    total = uploaded = skipped = failed = 0
    active_folder = UNSORTED_FOLDER
    await telegram.start()
    try:
        print(f"Telegram download mode: {args.tg_download}", flush=True)
        print(f"Helper clients started: {len(telegram.helper_clients)}", flush=True)
        print(f"Helper user sessions: {getattr(telegram, 'helper_user_session_count', 0)}", flush=True)
        print(f"Helper bots started: {getattr(telegram, 'helper_bot_count', 0)}", flush=True)
        print(
            f"Main user session used for hyper: {'yes' if getattr(telegram, 'main_session_helper_enabled', False) else 'no'}",
            flush=True,
        )
        print(f"Hyper threads: {telegram.hyper_threads}", flush=True)
        print(f"Hyper dump chat: {telegram.hyper_dump_chat or 'none'}", flush=True)
        if args.tg_download == "hyper" and not telegram.helper_clients:
            print(
                "Hyper Telegram download requested, but no helper clients are available. "
                "Set TG_USE_MAIN_SESSION_AS_HELPER=true, TG_HELPER_SESSION_STRINGS, or HELPER_TOKENS in heroku_bot/.env.",
                flush=True,
            )
            return 1

        print(f"Listing Telegram topic messages from {start_message_id}...", flush=True)
        message_ids = await telegram.list_topic_message_ids(
            parsed.chat_id,
            parsed.topic_id,
            start_from_message_id=start_message_id,
            batch_size=args.batch_size,
        )
        ordered_ids = sorted(set(message_ids))
        ordered_messages: list[object] = []
        for start in range(0, len(ordered_ids), args.batch_size):
            chunk = ordered_ids[start : start + args.batch_size]
            messages = await telegram.get_messages_bulk(parsed.chat_id, chunk)
            by_id = {message.id: message for message in messages}
            for message_id in chunk:
                message = by_id.get(message_id)
                if message is None:
                    continue
                ordered_messages.append(message)
            if start + args.batch_size < len(ordered_ids) and args.batch_delay_sec > 0:
                await asyncio.sleep(args.batch_delay_sec)

        assignments = _media_folder_assignments(
            ordered_messages,
            paths_by_heading,
            heading_ids,
            above=args.above,
        )
        print(f"Media assignment mode: {'above' if args.above else 'below'}", flush=True)
        print(f"Found {len(assignments)} media messages to process.", flush=True)

        for message, active_folder in assignments:
            total += 1
            prefix = f"[{total}]"
            planned_name = telegram_media_filename(message)
            known_size = _media_size(message)
            msz_rel, gdrive_rel = _destination_paths(args.target_folder, active_folder, planned_name)
            targets = ("msz", "gdrive") if args.target == "both" else (args.target,)
            target_paths = {"msz": msz_rel, "gdrive": gdrive_rel}
            pending_targets = [
                target
                for target in targets
                if args.no_resume or not state.is_uploaded(message.id, target, target_paths[target], known_size)
            ]
            if args.retry_failed_only:
                pending_targets = [
                    target
                    for target in pending_targets
                    if TelegramDriveState.key(message.id, target, target_paths[target], known_size) in state.failed_keys()
                ]
            if "gdrive" in pending_targets and not args.dry_run:
                pending_targets = await asyncio.to_thread(
                    _prefetch_existing_gdrive_uploads,
                    gdrive=gdrive,
                    root_folder_id=args.gdrive_folder_id,
                    message_id=message.id,
                    target_paths=target_paths,
                    size=known_size,
                    pending_targets=pending_targets,
                    state=state,
                    prefix=prefix,
                )
            if not pending_targets:
                skipped += 1
                print(f"{prefix} Skipping already uploaded: {planned_name}", flush=True)
                continue
            if args.dry_run:
                print(f"{prefix} DRY RUN: {message.id} -> {target_paths}", flush=True)
                skipped += 1
                continue

            local_path: Path | None = None
            uploaded_targets: set[str] = set()
            item_failed = False
            try:
                if known_size is not None:
                    ensure_disk_space(staging_dir, known_size)
                raw_target = staging_dir / f"{message.id}_{Path(planned_name).name}"
                download_progress = TransferLogger(prefix, "Telegram download", planned_name, known_size)
                print(f"{prefix} Downloading Telegram media: {message.id} -> {planned_name}", flush=True)
                downloaded = await telegram.download_media_to_path(
                    message,
                    raw_target,
                    progress=download_progress,
                    download_mode=args.tg_download,
                )
                local_path = Path(downloaded)
                file_name = _ensure_filename(message, local_path)
                size = local_path.stat().st_size
                msz_rel, gdrive_rel = _destination_paths(args.target_folder, active_folder, file_name)
                target_paths = {"msz": msz_rel, "gdrive": gdrive_rel}

                for target in pending_targets:
                    destination = target_paths[target]
                    if not args.no_resume and state.is_uploaded(message.id, target, destination, size):
                        skipped += 1
                        print(f"{prefix} Skipping uploaded {target}: {destination}", flush=True)
                        continue
                    state.mark(message.id, target, destination, size, "uploading", local_path=str(local_path))
                    if target == "msz":
                        if msz is None:
                            raise RuntimeError("MSZ API client is unavailable.")
                        print(f"{prefix} Uploading to MSZ: {destination}", flush=True)
                        remote_id = await _upload_msz(
                            local_path=local_path,
                            destination_path=destination,
                            size=size,
                            args=args,
                            client=msz,
                            browser_holder=browser_holder,
                            browser_state_file=browser_state_file,
                        )
                        state.mark(message.id, target, destination, size, "uploaded", remote_id=remote_id)
                        uploaded_targets.add(target)
                    else:
                        if gdrive is None:
                            raise RuntimeError("Google Drive uploader is unavailable.")
                        parent_rel = _destination_parent(destination)
                        parent_id = gdrive.ensure_folder_path(args.gdrive_folder_id, parent_rel)
                        print(f"{prefix} Uploading to Google Drive: {destination}", flush=True)
                        upload_progress = TransferLogger(prefix, "GDrive upload", file_name, size)
                        file_id = await asyncio.to_thread(
                            gdrive.upload_file,
                            local_path,
                            parent_id=parent_id,
                            file_name=Path(destination).name,
                            size=size,
                            progress_callback=upload_progress,
                            max_retries=10,
                        )
                        upload_progress(size, size)
                        state.mark(message.id, target, destination, size, "uploaded", remote_id=file_id)
                        uploaded_targets.add(target)
                uploaded += 1
            except Exception as exc:
                failed += 1
                item_failed = True
                for target in pending_targets:
                    if target in uploaded_targets:
                        continue
                    destination = target_paths[target]
                    state.mark(message.id, target, destination, known_size, "failed", error=str(exc))
                    _append_failed_log(
                        failed_log,
                        {
                            "message_id": message.id,
                            "target": target,
                            "destination_path": destination,
                            "active_folder": active_folder,
                            "file_name": planned_name,
                            "local_path": str(local_path) if local_path else "",
                            "error": str(exc),
                        },
                    )
                print(f"{prefix} ERROR: {message.id}: {exc}", flush=True)
                if not args.continue_on_error:
                    print("Stopping after first error. Use --continue-on-error to keep going.", flush=True)
                    return 1
            finally:
                should_delete = not item_failed or args.delete_failed_downloads
                if local_path and local_path.exists() and should_delete and not args.keep_downloads:
                    local_path.unlink(missing_ok=True)
    finally:
        await telegram.stop()
        if not args.keep_downloads and (failed == 0 or args.delete_failed_downloads):
            shutil.rmtree(staging_dir, ignore_errors=True)

    print(f"Done. Media={total} Uploaded={uploaded} Skipped={skipped} Failed={failed}")
    print(f"State file: {state_file}")
    print(f"Failed log: {failed_log}")
    return 1 if failed else 0


def main() -> None:
    _load_env_files()
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
