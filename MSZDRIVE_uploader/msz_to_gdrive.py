from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any

from .browser_source import MszBrowserSource
from .env_utils import load_msz_env_files
from .gdrive_upload import GoogleDriveResumableUploader
from .msz_api import MszApiClient, MszEntry, ensure_disk_space, norm_rel


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_dir() -> Path:
    return Path(os.getenv("HEROKU_RUNTIME_DIR", "runtime")).expanduser().resolve()


def _load_env_files() -> None:
    load_msz_env_files()


class ReverseSyncState:
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
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(self, key: str) -> dict[str, Any]:
        value = self.data["files"].get(key)
        return value if isinstance(value, dict) else {}

    def mark_downloading(self, key: str, entry: MszEntry, rel_path: str, local_path: Path) -> None:
        previous = self.record(key)
        self.data["files"][key] = {
            **previous,
            "status": "downloading",
            "msz_id": entry.id,
            "msz_rel_path": entry.rel_path,
            "rel_path": rel_path,
            "size": entry.size,
            "local_path": str(local_path),
            "updated_at": _utc_now(),
        }
        self.save()

    def mark_uploading(self, key: str, gdrive_parent_id: str) -> None:
        record = self.record(key)
        attempts = int(record.get("attempts", 0))
        record.update(
            {
                "status": "uploading",
                "gdrive_parent_id": gdrive_parent_id,
                "attempts": attempts + 1,
                "updated_at": _utc_now(),
            }
        )
        self.data["files"][key] = record
        self.save()

    def mark_uploaded(self, key: str, gdrive_file_id: str, local_path: Path | None = None) -> None:
        record = self.record(key)
        record.update(
            {
                "status": "uploaded",
                "gdrive_file_id": gdrive_file_id,
                "local_path": str(local_path) if local_path else record.get("local_path", ""),
                "uploaded_at": _utc_now(),
            }
        )
        self.data["files"][key] = record
        self.save()

    def mark_failed(self, key: str, error: str) -> None:
        record = self.record(key)
        retries = int(record.get("retries", 0))
        record.update(
            {
                "status": "failed",
                "retries": retries + 1,
                "last_error": error,
                "updated_at": _utc_now(),
            }
        )
        self.data["files"][key] = record
        self.save()

    def failed_keys(self) -> set[str]:
        return {
            key
            for key, record in self.data.get("files", {}).items()
            if isinstance(record, dict) and record.get("status") == "failed"
        }

    def uploaded_key(self, key: str, size: int | None, rel_path: str | None = None) -> bool:
        record = self.record(key)
        if record.get("status") != "uploaded" or record.get("size") != size:
            return False
        return rel_path is None or record.get("rel_path") == rel_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync files from MSZ Drive to Google Drive.")
    parser.add_argument(
        "source",
        nargs="?",
        default="",
        help="MSZ source as a folder/file URL, entry id, or path. Examples: URL, ODQwMDl8cGFkZA, TestUpload",
    )
    parser.add_argument(
        "dest",
        nargs="?",
        default="",
        help="Optional Google Drive destination folder id. Defaults to GDRIVE_FOLDER_ID.",
    )
    parser.add_argument("--msz-source-path", default=os.getenv("MSZ_SOURCE_PATH", ""))
    parser.add_argument("--msz-source-id", default=os.getenv("MSZ_SOURCE_ID", ""))
    parser.add_argument("--msz-source-url", default=os.getenv("MSZ_SOURCE_URL", ""))
    parser.add_argument("--gdrive-folder-id", default=os.getenv("GDRIVE_FOLDER_ID", ""))
    parser.add_argument("--gdrive-token-pickle", default=os.getenv("GDRIVE_TOKEN_PICKLE", "token.pickle"))
    parser.add_argument("--base-url", default=os.getenv("MSZ_BASE_URL", "https://cloud.medicalstudyzone.com"))
    parser.add_argument("--api-token", default=os.getenv("MSZ_API_TOKEN", ""))
    parser.add_argument("--email", default=os.getenv("MSZ_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("MSZ_PASSWORD", ""))
    parser.add_argument("--browser-state-file", default="")
    parser.add_argument("--browser-headed", action="store_true")
    parser.add_argument("--chromium-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""))
    parser.add_argument("--no-browser-folder-title", action="store_true")
    parser.add_argument("--runtime-dir", default=str(_runtime_dir()))
    parser.add_argument("--state-file", default="")
    parser.add_argument("--failed-log", default="")
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-failed-only", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--delete-failed-downloads", action="store_true")
    parser.set_defaults(no_resume=True)
    parser.add_argument("--no-resume", dest="no_resume", action="store_true", help="Ignore previous transfer state. Default.")
    parser.add_argument("--resume", dest="no_resume", action="store_false", help="Resume from previous successful transfer state.")
    parser.add_argument("--public", action="store_true", help="Set uploaded Google Drive files/folders public.")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _looks_like_msz_id(value: str) -> bool:
    if "/" in value or "\\" in value or " " in value:
        return False
    if len(value) < 8:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-|=")
    return all(char in allowed for char in value)


def _resolve_source_args(args: argparse.Namespace) -> tuple[str, str, str, str]:
    source_url = args.msz_source_url.strip()
    source_id = args.msz_source_id.strip()
    source_path = args.msz_source_path.strip()
    source = args.source.strip()
    if source and not (source_url or source_id or source_path):
        if _looks_like_url(source):
            source_url = source
        elif _looks_like_msz_id(source):
            source_id = source
        else:
            source_path = source
    gdrive_folder_id = args.gdrive_folder_id.strip() or args.dest.strip()
    return source_path, source_id, source_url, gdrive_folder_id


def _safe_local_path(staging_dir: Path, rel_path: str) -> Path:
    safe_parts = [Path(part).name for part in norm_rel(rel_path).split("/") if part.strip()]
    if not safe_parts:
        safe_parts = ["download.bin"]
    return staging_dir.joinpath(*safe_parts)


def _gdrive_rel_path(root: MszEntry, entry: MszEntry) -> str:
    if root.is_file:
        return entry.name
    root_name = _gdrive_root_folder_name(root)
    child_rel = MszApiClient.rel_path_under(root, entry)
    return norm_rel(f"{root_name}/{child_rel}")


def _gdrive_root_folder_name(root: MszEntry) -> str:
    return Path(root.name).name or "MSZ Source"


def _looks_like_fallback_msz_name(root: MszEntry) -> bool:
    return root.is_folder and str(root.name).startswith("MSZ Folder ")


async def _resolve_browser_folder_title(
    args: argparse.Namespace,
    *,
    source_url: str,
    root: MszEntry,
    entries: list[MszEntry],
    runtime_dir: Path,
    log: Any,
) -> tuple[MszEntry, list[MszEntry]]:
    if args.no_browser_folder_title or not source_url or not _looks_like_fallback_msz_name(root):
        return root, entries
    if not (args.email or os.getenv("MSZ_EMAIL", "")) or not (args.password or os.getenv("MSZ_PASSWORD", "")):
        log("Browser folder-title lookup skipped: MSZ_EMAIL/MSZ_PASSWORD are not configured.")
        return root, entries

    state_file = (
        Path(args.browser_state_file).expanduser()
        if args.browser_state_file
        else runtime_dir / "state" / "msz_browser_state.json"
    )
    browser = MszBrowserSource(
        base_url=args.base_url,
        email=args.email or os.getenv("MSZ_EMAIL", ""),
        password=args.password or os.getenv("MSZ_PASSWORD", ""),
        storage_state_path=state_file,
        headless=not args.browser_headed,
        executable_path=args.chromium_executable,
        verbose=True,
    )
    try:
        title = await browser.resolve_folder_title(source_url)
    except Exception as exc:
        log(f"Browser folder-title lookup failed: {exc}")
        return root, entries
    title = Path(str(title).replace("\x00", "")).name.strip().strip(" .")
    if not title or title == root.name:
        return root, entries

    log(f"Browser resolved folder title: {title}")
    new_root = replace(root, name=title, rel_path=title)
    new_entries = []
    for entry in entries:
        child_rel = MszApiClient.rel_path_under(root, entry)
        new_entries.append(replace(entry, rel_path=norm_rel(f"{new_root.rel_path}/{child_rel}")))
    return new_root, new_entries


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
        self.last_done = -1
        self.last_log_at = 0.0
        self.started_at = time.monotonic()
        self.started_done = 0

    def __call__(self, done: int, total_size: int | None = None) -> None:
        total = total_size or self.total_size
        now = time.monotonic()
        complete = total is not None and done >= total
        enough_time = now - self.last_log_at >= 5.0
        enough_bytes = self.last_done < 0 or done - self.last_done >= 25 * 1024 * 1024
        if not (complete or enough_time or enough_bytes):
            return
        self.last_done = done
        self.last_log_at = now
        elapsed = max(0.001, now - self.started_at)
        transferred = max(0, done - self.started_done)
        bytes_per_second = transferred / elapsed
        speed = f"{_format_bytes(int(bytes_per_second))}/s"
        eta = None
        if total and bytes_per_second > 0 and done < total:
            eta = (total - done) / bytes_per_second
        if total:
            percent = min(100.0, (done / total) * 100)
            print(
                f"{self.prefix} {self.label}: {self.file_name} "
                f"{_format_bytes(done)} / {_format_bytes(total)} ({percent:.1f}%) "
                f"at {speed}, ETA {_format_duration(eta)}",
                flush=True,
            )
        else:
            print(
                f"{self.prefix} {self.label}: {self.file_name} {_format_bytes(done)} "
                f"at {speed}, ETA --",
                flush=True,
            )


async def run(args: argparse.Namespace) -> int:
    source_path, source_id, source_url, gdrive_folder_id = _resolve_source_args(args)
    if not (source_path or source_id or source_url):
        raise ValueError("Provide an MSZ source URL/id/path, or set MSZ_SOURCE_PATH/MSZ_SOURCE_ID/MSZ_SOURCE_URL.")
    if not gdrive_folder_id:
        raise ValueError("Provide a Google Drive destination folder id as the second argument, or set GDRIVE_FOLDER_ID.")

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    staging_dir = runtime_dir / "msz_to_gdrive_staging"
    state_file = Path(args.state_file) if args.state_file else runtime_dir / "state" / "msz_to_gdrive_state.json"
    failed_log = Path(args.failed_log) if args.failed_log else runtime_dir / "logs" / "msz_to_gdrive_failed.jsonl"
    staging_dir.mkdir(parents=True, exist_ok=True)

    msz = MszApiClient(args.base_url, args.api_token or os.getenv("MSZ_API_TOKEN", ""))
    def _log(message: str) -> None:
        print(f"[msz] {message}", flush=True)

    root, entries = msz.resolve_source_entries(
        source_path=source_path,
        source_id=source_id,
        source_url=source_url,
        log=_log,
    )
    root, entries = await _resolve_browser_folder_title(
        args,
        source_url=source_url,
        root=root,
        entries=entries,
        runtime_dir=runtime_dir,
        log=_log,
    )
    print(f"MSZ source resolved: {root.type} id={root.id} path={root.rel_path}")
    if root.is_folder:
        print(f"Google Drive destination folder: {_gdrive_root_folder_name(root)}", flush=True)
    state = ReverseSyncState(state_file)
    failed_keys = state.failed_keys()
    gdrive = None
    if not args.dry_run:
        gdrive = GoogleDriveResumableUploader(
            Path(args.gdrive_token_pickle),
            set_public_permission=args.public,
        )

    total_files = len(entries)
    total = uploaded = skipped = failed = 0
    try:
        async def _iter_entries():
            for api_entry in entries:
                rel = _gdrive_rel_path(root, api_entry)
                yield api_entry, _safe_local_path(staging_dir, rel), rel

        async for entry, local_path, rel_path in _iter_entries():
            key = str(entry.id)
            total += 1
            prefix = f"[{total}/{total_files}]"
            file_name = Path(rel_path).name
            try:
                if args.retry_failed_only and key not in failed_keys:
                    skipped += 1
                    print(f"{prefix} Skipping not-failed file: {entry.rel_path}", flush=True)
                    continue
                if not args.no_resume and state.uploaded_key(key, entry.size, rel_path):
                    skipped += 1
                    print(f"{prefix} Skipping already uploaded from state: {entry.rel_path}", flush=True)
                    continue
                if args.dry_run:
                    print(f"{prefix} DRY RUN: {entry.rel_path} -> gdrive:{gdrive_folder_id}/{rel_path}", flush=True)
                    skipped += 1
                    continue

                if gdrive is None:
                    raise RuntimeError("Google Drive uploader is unavailable.")
                ensure_disk_space(staging_dir, entry.size or 0)
                state.mark_downloading(key, entry, rel_path, local_path)
                if not local_path.exists() or (entry.size is not None and local_path.stat().st_size != entry.size):
                    print(f"{prefix} Downloading from MSZ: {entry.rel_path}", flush=True)
                    download_progress = TransferLogger(prefix, "MSZ download", file_name, entry.size)
                    await asyncio.to_thread(
                        msz.download_file,
                        entry,
                        local_path,
                        max_retries=args.max_retries,
                        progress_callback=download_progress,
                    )
                    download_progress(local_path.stat().st_size, entry.size)
                else:
                    print(f"{prefix} Reusing existing MSZ download: {local_path}", flush=True)

                parent_rel = norm_rel(str(Path(rel_path).parent))
                if parent_rel == ".":
                    parent_rel = ""
                parent_id = gdrive.ensure_folder_path(gdrive_folder_id, parent_rel)
                existing = gdrive.existing_file_matches(parent_id, file_name, entry.size)
                if existing and not args.no_resume:
                    state.mark_uploaded(key, existing["id"], local_path)
                    skipped += 1
                    print(f"{prefix} Skipping existing Google Drive file: {rel_path}", flush=True)
                    await asyncio.to_thread(local_path.unlink, missing_ok=True)
                    continue

                state.mark_uploading(key, parent_id)
                print(f"{prefix} Uploading to Google Drive: {rel_path}", flush=True)
                upload_progress = TransferLogger(prefix, "GDrive upload", file_name, entry.size)

                file_id = await asyncio.to_thread(
                    gdrive.upload_file,
                    local_path,
                    parent_id=parent_id,
                    file_name=file_name,
                    size=entry.size,
                    progress_callback=upload_progress,
                    max_retries=10,
                )
                upload_progress(entry.size or local_path.stat().st_size, entry.size)
                state.mark_uploaded(key, file_id, local_path)
                uploaded += 1
                print(f"{prefix} Uploaded: {rel_path}", flush=True)
                await asyncio.to_thread(local_path.unlink, missing_ok=True)
            except Exception as exc:
                failed += 1
                state.mark_failed(key, str(exc))
                _append_failed_log(
                    failed_log,
                    {
                        "msz_id": entry.id,
                        "msz_rel_path": entry.rel_path,
                        "rel_path": rel_path,
                        "local_path": str(local_path),
                        "size": entry.size,
                        "error": str(exc),
                    },
                )
                print(f"{prefix} ERROR: {entry.rel_path}: {exc}", flush=True)
                if args.delete_failed_downloads and local_path.exists():
                    await asyncio.to_thread(local_path.unlink, missing_ok=True)
                if not args.continue_on_error:
                    print("Stopping after first error. Use --continue-on-error to keep going.", flush=True)
                    break
    finally:
        if failed == 0 or args.delete_failed_downloads:
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
