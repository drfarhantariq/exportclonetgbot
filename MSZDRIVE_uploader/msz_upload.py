from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

try:
    from tqdm.auto import tqdm
except ImportError:
    class tqdm:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            self.total = kwargs.get("total")

        def refresh(self) -> None:
            return None

        def set_postfix_str(self, value: str) -> None:
            return None

        def write(self, value: str) -> None:
            print(value)

        def update(self, value: int) -> None:
            return None

        def close(self) -> None:
            return None

from .browser_upload import MszBrowserUploader
from .msz_api import MszApiClient, ensure_disk_space, infer_extension_from_signature, norm_rel
from .sources import SourceItem, cleanup_item, iter_gdrive, iter_local, iter_telegram_topic
from .state import FailedUploadLog, UploadState


DEFAULT_API_MAX_BYTES = 100_000_000


def _runtime_dir() -> Path:
    return Path(os.getenv("HEROKU_RUNTIME_DIR", "runtime")).expanduser().resolve()


def _load_env_files() -> None:
    for env_path in (Path("MSZDRIVE_uploader/.env"), Path(".env"), Path("heroku_bot/.env")):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hybrid MSZ Drive uploader.")
    parser.add_argument("--source", choices=("local", "gdrive", "telegram-topic"), required=True)
    parser.add_argument("--path", default="", help="Local file/folder for --source local.")
    parser.add_argument("--url", default="", help="Google Drive folder URL for --source gdrive.")
    parser.add_argument("--topic-link", default="", help="Telegram topic/channel link for --source telegram-topic.")
    parser.add_argument("--target-folder", required=True, help="Remote MSZ folder/path.")
    parser.add_argument("--base-url", default=os.getenv("MSZ_BASE_URL", "https://cloud.medicalstudyzone.com"))
    parser.add_argument("--api-token", default=os.getenv("MSZ_API_TOKEN", ""))
    parser.add_argument("--email", default=os.getenv("MSZ_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("MSZ_PASSWORD", ""))
    parser.add_argument("--api-max-bytes", type=int, default=int(os.getenv("MSZ_API_MAX_BYTES", DEFAULT_API_MAX_BYTES)))
    parser.add_argument("--runtime-dir", default=str(_runtime_dir()))
    parser.add_argument("--state-file", default="")
    parser.add_argument("--failed-log", default="")
    parser.add_argument("--browser-state-file", default="")
    parser.add_argument("--browser-folder-url", default=os.getenv("MSZ_BROWSER_FOLDER_URL", ""))
    parser.add_argument("--browser-headed", action="store_true")
    parser.add_argument("--chromium-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""))
    parser.add_argument("--config", default=os.getenv("HEROKU_CONFIG_PATH", "heroku_bot/config.yaml"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-delay-sec", type=float, default=1.0)
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--verify-retries", type=int, default=5)
    parser.add_argument("--verify-delay-sec", type=float, default=3.0)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument(
        "--delete-failed-downloads",
        action="store_true",
        help="Delete staged source files even when their upload fails.",
    )
    parser.add_argument(
        "--strict-browser-verify",
        action="store_true",
        help="Fail browser uploads if the API index does not show the exact expected remote path.",
    )
    parser.add_argument(
        "--retry-failed-only",
        action="store_true",
        help="Only process files that are currently marked failed in state or listed in the failed upload log.",
    )
    parser.add_argument("--no-resume", action="store_true", help="Do not skip files from state or remote index.")
    parser.add_argument("--no-verify-remote", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def _iter_source(args: argparse.Namespace, staging_dir: Path):
    if args.source == "local":
        if not args.path:
            raise ValueError("--path is required for --source local")
        async for item in iter_local(Path(args.path)):
            yield item
        return
    if args.source == "gdrive":
        if not args.url:
            raise ValueError("--url is required for --source gdrive")
        async for item in iter_gdrive(args.url, staging_dir):
            yield item
        return
    if args.source == "telegram-topic":
        if not args.topic_link:
            raise ValueError("--topic-link is required for --source telegram-topic")
        async for item in iter_telegram_topic(
            args.topic_link,
            staging_dir,
            Path(args.config),
            args.batch_size,
            args.batch_delay_sec,
            start_from_message_id=args.start_id,
        ):
            yield item
        return
    raise ValueError(f"Unsupported source: {args.source}")


def _target_rel(target_folder: str, item: SourceItem) -> str:
    rel = norm_rel(item.rel_path)
    if "." not in Path(rel).name:
        inferred = infer_extension_from_signature(item.path)
        if inferred:
            rel += inferred
    return norm_rel(f"{target_folder}/{rel}")


def _remote_parent_folder(target_rel: str) -> str:
    parent = Path(norm_rel(target_rel)).parent
    if str(parent) == ".":
        return ""
    return norm_rel(str(parent))


def _find_remote_match(remote_index: dict[str, object], target_rel: str, size: int):
    target_rel = norm_rel(target_rel)
    exact = remote_index.get(target_rel)
    if exact is not None:
        remote_size = getattr(exact, "size", None)
        if remote_size in (None, size):
            return exact, "exact"
        raise RuntimeError(f"Remote size mismatch for {target_rel}: {remote_size} != {size}")

    suffix = "/" + target_rel
    suffix_matches = [
        remote
        for rel, remote in remote_index.items()
        if norm_rel(rel).endswith(suffix) and getattr(remote, "size", None) in (None, size)
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0], "suffix"

    file_name = Path(target_rel).name
    name_matches = [
        remote
        for remote in remote_index.values()
        if getattr(remote, "name", "") == file_name and getattr(remote, "size", None) in (None, size)
    ]
    if len(name_matches) == 1:
        return name_matches[0], "name+size"

    return None, ""


async def _verify_remote_upload(client, target_rel: str, size: int, args: argparse.Namespace, progress):
    last_index = {}
    attempts = max(1, int(args.verify_retries))
    for attempt in range(1, attempts + 1):
        last_index = client.build_remote_index()
        remote, match_kind = _find_remote_match(last_index, target_rel, size)
        if remote is not None:
            return remote, match_kind, last_index
        if attempt < attempts:
            progress.write(
                f"Verification pending for {target_rel}; retry {attempt}/{attempts} "
                f"in {args.verify_delay_sec:.1f}s"
            )
            await asyncio.sleep(max(0.0, float(args.verify_delay_sec)))
    return None, "", last_index


async def run(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    staging_dir = runtime_dir / "msz_upload_staging"
    state_file = Path(args.state_file) if args.state_file else runtime_dir / "state" / "msz_upload_state.json"
    failed_log_path = Path(args.failed_log) if args.failed_log else runtime_dir / "logs" / "msz_failed_uploads.jsonl"
    browser_state_file = (
        Path(args.browser_state_file)
        if args.browser_state_file
        else runtime_dir / "state" / "msz_browser_state.json"
    )
    staging_dir.mkdir(parents=True, exist_ok=True)

    client = None if args.dry_run else MszApiClient(args.base_url, args.api_token or os.getenv("MSZ_API_TOKEN", ""))
    state = UploadState(state_file)
    failed_log = FailedUploadLog(failed_log_path)
    failed_targets = state.failed_paths() | failed_log.target_paths()
    if args.retry_failed_only:
        print(f"Retrying failed uploads only. Failed targets loaded: {len(failed_targets)}")
    resume_enabled = not args.no_resume
    remote_index = {} if args.dry_run or args.no_verify_remote else client.build_remote_index()

    browser: MszBrowserUploader | None = None

    total = uploaded = skipped = failed = 0
    progress = tqdm(desc="MSZ upload", unit="file")
    try:
        async for item in _iter_source(args, staging_dir):
            total += 1
            progress.total = total
            progress.refresh()
            try:
                uploaded_this_item = False
                failed_this_item = False
                stat = item.path.stat()
                size = int(stat.st_size)
                mtime = int(stat.st_mtime)
                target_rel = _target_rel(args.target_folder, item)
                if args.retry_failed_only and target_rel not in failed_targets:
                    skipped += 1
                    progress.set_postfix_str("not-failed")
                    continue
                remote = None
                remote_match_kind = ""
                if resume_enabled and remote_index:
                    remote, remote_match_kind = _find_remote_match(remote_index, target_rel, size)
                    if remote is not None:
                        state.mark_uploaded(
                            target_rel,
                            size,
                            mtime,
                            "remote-resume",
                            getattr(remote, "id", None),
                        )
                        skipped += 1
                        progress.set_postfix_str(f"skipped:remote:{remote_match_kind}")
                        continue
                if resume_enabled:
                    skip, why = state.should_skip(
                        target_rel,
                        size,
                        mtime,
                        getattr(remote, "size", None) if remote else None,
                        trust_state=True,
                    )
                    if skip:
                        skipped += 1
                        progress.set_postfix_str(f"skipped:{why}")
                        continue

                if args.dry_run:
                    progress.write(f"DRY RUN: {item.path} -> {target_rel}")
                    skipped += 1
                    continue

                ensure_disk_space(staging_dir, size)
                if size < args.api_max_bytes:
                    if client is None:
                        raise RuntimeError("MSZ API client is unavailable.")
                    method = "api"
                    state.mark_started(target_rel, size, mtime, method, str(item.path))
                    remote_id = client.upload_file(item.path, target_rel, max_retries=args.max_retries)
                else:
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
                    browser_target_folder = _remote_parent_folder(target_rel) or norm_rel(args.target_folder)
                    method = "browser"
                    state.mark_started(target_rel, size, mtime, method, str(item.path))
                    info = await browser.upload(item.path, browser_target_folder)
                    remote_id = None
                    progress.write(f"Browser upload observed {len(info['requests'])} upload request(s).")

                state.mark_uploaded(target_rel, size, mtime, method, remote_id)

                if not args.no_verify_remote:
                    if client is None:
                        raise RuntimeError("MSZ API client is unavailable.")
                    verified, match_kind, remote_index = await _verify_remote_upload(
                        client, target_rel, size, args, progress
                    )
                    if verified is None and method == "browser":
                        message = (
                            f"Browser upload finished, but API verification did not find: {target_rel}. "
                            "Treating UI upload completion as success."
                        )
                        if args.strict_browser_verify:
                            raise RuntimeError(message)
                        progress.write(f"WARN: {message}")
                    elif verified is None:
                        raise RuntimeError(f"Upload finished, but remote verification did not find: {target_rel}")
                    elif verified is not None and verified.size not in (None, size):
                        raise RuntimeError(f"Remote size mismatch for {target_rel}: {verified.size} != {size}")
                    elif verified is not None:
                        remote_id = getattr(verified, "id", remote_id)
                        state.mark_uploaded(target_rel, size, mtime, method, remote_id)
                        progress.write(f"Verified remote upload ({match_kind}): {getattr(verified, 'rel_path', target_rel)}")

                uploaded += 1
                uploaded_this_item = True
                progress.set_postfix_str(method)
            except Exception as exc:
                failed += 1
                failed_this_item = True
                try:
                    stat = item.path.stat()
                    target_rel = _target_rel(args.target_folder, item)
                    size = int(stat.st_size)
                    mtime = int(stat.st_mtime)
                    state.mark_failed(target_rel, size, mtime, str(exc))
                    failed_log.append(
                        source=args.source,
                        source_path=str(item.path),
                        rel_path=item.rel_path,
                        target_rel=target_rel,
                        size=size,
                        mtime=mtime,
                        error=str(exc),
                    )
                except Exception:
                    pass
                progress.write(f"ERROR: {item.path}: {exc}")
                if not args.continue_on_error:
                    progress.write("Stopping after first error. Use --continue-on-error to keep going.")
                    break
            finally:
                if not failed_this_item or args.delete_failed_downloads:
                    await cleanup_item(item)
                elif item.cleanup:
                    progress.write(f"Keeping failed download for retry: {item.path}")
                progress.update(1)
    finally:
        progress.close()
        if args.source in {"gdrive", "telegram-topic"} and (failed == 0 or args.delete_failed_downloads):
            shutil.rmtree(staging_dir, ignore_errors=True)

    print(f"Done. Total={total} Uploaded={uploaded} Skipped={skipped} Failed={failed}")
    print(f"State file: {state_file}")
    print(f"Failed upload log: {failed_log_path}")
    return 1 if failed else 0


def main() -> None:
    _load_env_files()
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
