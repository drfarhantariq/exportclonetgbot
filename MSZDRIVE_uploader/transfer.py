from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "MSZDRIVE_uploader"

from .env_utils import load_msz_env_files
from .telegram_index import parse_index


def _runtime_dir() -> Path:
    return Path(os.getenv("HEROKU_RUNTIME_DIR", "runtime")).expanduser().resolve()


def _load_env_files() -> None:
    load_msz_env_files()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Single entrypoint for Telegram, MSZ Drive, Google Drive, and local transfer workflows. "
            "Use prefixes when helpful: msz:<folder-or-url>, gdrive:<folder-id-or-url>, index:<path>."
        )
    )
    parser.add_argument("source", help="Telegram link/index file/MSZ URL or path/GDrive URL/local path.")
    parser.add_argument("dest", nargs="?", default="", help="Destination. Examples: index:out.txt, msz:Folder, gdrive:FOLDER_ID, both")
    parser.add_argument("--to", choices=("auto", "index", "msz", "gdrive", "both", "telegram"), default="auto")
    parser.add_argument(
        "--up",
        choices=("msz", "gd", "gdrive", "telegram", "both"),
        default="",
        help="Upload destination shortcut: --up msz, --up gd, --up telegram, or --up both.",
    )
    parser.add_argument("--from", dest="source_type", choices=("auto", "telegram", "index", "msz", "gdrive", "local"), default="auto")
    parser.add_argument(
        "--index",
        nargs="?",
        const="__auto__",
        default="",
        help="Generate a Telegram folder index from the Telegram source link. Optionally pass an output path.",
    )
    parser.add_argument("--index-done", default="", help="Edited Telegram folder index to use for upload.")
    parser.add_argument("--index-out", default="", help="Output path when generating a Telegram folder index.")
    parser.add_argument("--msz-target-folder", default=os.getenv("MSZ_TARGET_FOLDER", ""))
    parser.add_argument("--gdrive-folder-id", default=os.getenv("GDRIVE_FOLDER_ID", ""))
    parser.add_argument("--gdrive-token-pickle", default=os.getenv("GDRIVE_TOKEN_PICKLE", "token.pickle"))
    parser.add_argument("--config", default=os.getenv("HEROKU_CONFIG_PATH", "heroku_bot/config.yaml"))
    parser.add_argument("--runtime-dir", default=str(_runtime_dir()))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-delay-sec", type=float, default=1.0)
    parser.add_argument(
        "--tg-download",
        choices=("hyper", "auto", "normal"),
        default=os.getenv("TG_DOWNLOAD_MODE", "hyper"),
        help="Telegram media download mode for --index-done uploads. Default: hyper.",
    )
    parser.add_argument("--onwards", action="store_true")
    parser.add_argument(
        "--above",
        action="store_true",
        help="For Telegram uploads, assign media before each enabled text heading to that heading folder.",
    )
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--retry-failed-only", action="store_true")
    parser.set_defaults(no_resume=True)
    parser.add_argument("--no-resume", dest="no_resume", action="store_true", help="Ignore previous transfer state. Default.")
    parser.add_argument("--resume", dest="no_resume", action="store_false", help="Resume from previous successful transfer state.")
    parser.add_argument("--caption-file-names", action="store_true", help="Use Telegram media captions as file names.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-downloads", action="store_true")
    parser.add_argument("--delete-failed-downloads", action="store_true")
    parser.add_argument("--browser-headed", action="store_true")
    parser.add_argument("--browser-folder-url", default=os.getenv("MSZ_BROWSER_FOLDER_URL", ""))
    parser.add_argument("--chromium-executable", default=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""))
    parser.add_argument("--no-browser-folder-title", action="store_true")
    parser.add_argument("--strict-browser-verify", action="store_true")
    parser.add_argument("--verify-remote", action="store_true")
    return parser


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.lower().startswith(prefix) else value


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _host(value: str) -> str:
    return urlparse(value).netloc.lower()


def _is_telegram(value: str) -> bool:
    return _is_url(value) and _host(value) in {"t.me", "telegram.me"}


def _is_gdrive(value: str) -> bool:
    if value.lower().startswith("gdrive:"):
        return True
    host = _host(value)
    return "drive.google.com" in host or "docs.google.com" in host


def _is_msz(value: str) -> bool:
    host = _host(value)
    return "medicalstudyzone.com" in host or value.lower().startswith("msz:")


def _is_index(value: str) -> bool:
    raw = _strip_prefix(value, "index:")
    if not raw:
        return False
    path = Path(raw)
    return value.lower().startswith("index:") or (path.exists() and path.suffix.lower() == ".txt")


def _source_type(value: str, explicit: str) -> str:
    if explicit != "auto":
        return explicit
    if _is_telegram(value):
        return "telegram"
    if _is_index(value):
        return "index"
    if _is_gdrive(value):
        return "gdrive"
    if _is_msz(value) or value.lower().startswith("msz:"):
        return "msz"
    return "local" if Path(value).exists() else "msz"


def _dest_target(dest: str, explicit: str) -> str:
    explicit = _target_alias(explicit)
    if explicit != "auto":
        return explicit
    if not dest:
        return "index"
    lowered = dest.lower()
    if lowered == "both" or lowered.startswith("both:"):
        return "both"
    if lowered.startswith("index:") or lowered.endswith(".txt"):
        return "index"
    if lowered.startswith("msz:"):
        return "msz"
    if lowered.startswith("gdrive:") or _is_gdrive(dest):
        return "gdrive"
    return "msz"


def _target_alias(value: str) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized == "gd":
        return "gdrive"
    return normalized


def _telegram_index_topic_folder(index_path: str) -> str:
    if not index_path:
        return ""
    try:
        index = parse_index(Path(index_path).expanduser())
    except Exception:
        return ""
    if index.topic_title:
        return index.topic_title
    for heading in index.headings:
        if heading.enabled and heading.level == 1:
            return heading.name
    return ""


def _safe_default_folder_name(value: str) -> str:
    cleaned = Path(value.replace("\x00", "")).name.strip()
    return cleaned.strip(" .") or ""


def _gdrive_source_folder_name(source: str, token_pickle: str = "") -> str:
    source = _strip_prefix(source.strip(), "gdrive:")
    if not source:
        return ""
    try:
        from .gdrive_upload import GoogleDriveResumableUploader

        folder_id = GoogleDriveResumableUploader.extract_folder_id(source)
        token_path = Path(token_pickle or os.getenv("GDRIVE_TOKEN_PICKLE", "token.pickle"))
        if folder_id and token_path.expanduser().exists():
            gdrive = GoogleDriveResumableUploader(token_path)
            metadata = gdrive.get_file_metadata(folder_id)
            name = _safe_default_folder_name(str(metadata.get("name", "")))
            if name:
                return name
    except Exception:
        pass
    if source.startswith(("http://", "https://")):
        parts = [part for part in urlparse(source).path.split("/") if part]
        if "folders" in parts and parts.index("folders") + 1 < len(parts):
            return f"Google Drive {parts[parts.index('folders') + 1]}"
    return _safe_default_folder_name(source) or "Google Drive Upload"


def _source_default_msz_folder(args: argparse.Namespace) -> str:
    source_type = _source_type(args.source, args.source_type)
    if source_type == "gdrive":
        return _gdrive_source_folder_name(args.source, args.gdrive_token_pickle)
    if source_type == "local":
        return _safe_default_folder_name(args.source)
    return ""


def _msz_dest(args: argparse.Namespace, index_path: str = "") -> str:
    if args.msz_target_folder:
        return args.msz_target_folder
    if args.dest.lower().startswith("msz:"):
        return _strip_prefix(args.dest, "msz:").strip()
    if args.dest and not args.dest.lower().startswith(("gdrive:", "index:", "both")) and not _is_gdrive(args.dest):
        return args.dest
    topic_folder = _telegram_index_topic_folder(index_path)
    if topic_folder:
        return topic_folder
    source_folder = _source_default_msz_folder(args)
    if source_folder:
        return source_folder
    raise ValueError("MSZ destination is missing. Use dest 'msz:<folder>' or --msz-target-folder.")


def _has_explicit_msz_dest(args: argparse.Namespace) -> bool:
    if args.msz_target_folder:
        return True
    if args.dest.lower().startswith("msz:"):
        return bool(_strip_prefix(args.dest, "msz:").strip())
    if args.dest and not args.dest.lower().startswith(("gdrive:", "index:", "both")) and not _is_gdrive(args.dest):
        return True
    return False


def _gdrive_dest(args: argparse.Namespace) -> str:
    if args.gdrive_folder_id:
        return args.gdrive_folder_id
    if args.dest.lower().startswith("gdrive:"):
        return _strip_prefix(args.dest, "gdrive:").strip()
    if args.dest and not args.dest.lower().startswith(("msz:", "index:", "both")):
        return args.dest
    return "root"


def _index_out(args: argparse.Namespace) -> str:
    if args.index and args.index != "__auto__":
        return args.index
    if args.index_out:
        return args.index_out
    if args.dest.lower().startswith("index:"):
        return _strip_prefix(args.dest, "index:").strip()
    if args.dest.lower().endswith(".txt"):
        return args.dest
    return ""


def _common_flags(args: argparse.Namespace) -> list[str]:
    flags = [
        "--config",
        args.config,
        "--runtime-dir",
        args.runtime_dir,
        "--batch-size",
        str(args.batch_size),
        "--batch-delay-sec",
        str(args.batch_delay_sec),
        "--tg-download",
        args.tg_download,
    ]
    if args.continue_on_error:
        flags.append("--continue-on-error")
    if args.retry_failed_only:
        flags.append("--retry-failed-only")
    if args.no_resume:
        flags.append("--no-resume")
    else:
        flags.append("--resume")
    if args.caption_file_names:
        flags.append("--caption-file-names")
    if args.dry_run:
        flags.append("--dry-run")
    if args.keep_downloads:
        flags.append("--keep-downloads")
    if args.delete_failed_downloads:
        flags.append("--delete-failed-downloads")
    if args.above:
        flags.append("--above")
    if args.browser_headed:
        flags.append("--browser-headed")
    if args.browser_folder_url:
        flags.extend(["--browser-folder-url", args.browser_folder_url])
    if args.chromium_executable:
        flags.extend(["--chromium-executable", args.chromium_executable])
    return flags


async def _run_telegram_index(args: argparse.Namespace) -> int:
    from . import telegram_folder_index

    cli = [
        args.source,
        "--config",
        args.config,
        "--batch-size",
        str(args.batch_size),
        "--batch-delay-sec",
        str(args.batch_delay_sec),
    ]
    index_out = _index_out(args)
    if index_out:
        cli.extend(["--out", index_out])
    if args.onwards:
        cli.append("--onwards")
    return await telegram_folder_index.run(telegram_folder_index._build_parser().parse_args(cli))


async def _run_telegram_upload(args: argparse.Namespace, target: str, index_path: str) -> int:
    from . import telegram_to_drive

    cli = ["--target", target]
    if index_path:
        cli.extend(["--index", index_path])
    else:
        cli.extend(["--topic-link", args.source, "--flat-folder", "__topic_title__"])
    if target in {"msz", "both"}:
        if index_path or _has_explicit_msz_dest(args):
            cli.extend(["--target-folder", _msz_dest(args, index_path)])
        else:
            cli.extend(["--target-folder", ""])
    if target in {"gdrive", "both"}:
        cli.extend(["--gdrive-folder-id", _gdrive_dest(args), "--gdrive-token-pickle", args.gdrive_token_pickle])
    cli.extend(_common_flags(args))
    return await telegram_to_drive.run(telegram_to_drive._build_parser().parse_args(cli))


async def _run_gdrive_to_msz(args: argparse.Namespace) -> int:
    from . import msz_upload

    cli = [
        "--source",
        "gdrive",
        "--url",
        args.source,
        "--target-folder",
        _msz_dest(args),
        "--config",
        args.config,
        "--batch-size",
        str(args.batch_size),
        "--batch-delay-sec",
        str(args.batch_delay_sec),
    ]
    if args.continue_on_error:
        cli.append("--continue-on-error")
    if args.retry_failed_only:
        cli.append("--retry-failed-only")
    if args.no_resume:
        cli.append("--no-resume")
    else:
        cli.append("--resume")
    if args.dry_run:
        cli.append("--dry-run")
    if args.delete_failed_downloads:
        cli.append("--delete-failed-downloads")
    if args.browser_headed:
        cli.append("--browser-headed")
    if args.browser_folder_url:
        cli.extend(["--browser-folder-url", args.browser_folder_url])
    if args.chromium_executable:
        cli.extend(["--chromium-executable", args.chromium_executable])
    if args.strict_browser_verify:
        cli.append("--strict-browser-verify")
    if args.verify_remote:
        cli.append("--verify-remote")
    return await msz_upload.run(msz_upload._build_parser().parse_args(cli))


async def _run_local_to_msz(args: argparse.Namespace) -> int:
    from . import msz_upload

    cli = ["--source", "local", "--path", args.source, "--target-folder", _msz_dest(args)]
    if args.continue_on_error:
        cli.append("--continue-on-error")
    if args.retry_failed_only:
        cli.append("--retry-failed-only")
    if args.no_resume:
        cli.append("--no-resume")
    else:
        cli.append("--resume")
    if args.dry_run:
        cli.append("--dry-run")
    if args.browser_headed:
        cli.append("--browser-headed")
    if args.browser_folder_url:
        cli.extend(["--browser-folder-url", args.browser_folder_url])
    if args.chromium_executable:
        cli.extend(["--chromium-executable", args.chromium_executable])
    if args.strict_browser_verify:
        cli.append("--strict-browser-verify")
    if args.verify_remote:
        cli.append("--verify-remote")
    return await msz_upload.run(msz_upload._build_parser().parse_args(cli))


async def _run_msz_to_gdrive(args: argparse.Namespace) -> int:
    from . import msz_to_gdrive

    source = _strip_prefix(args.source, "msz:")
    cli = [source, _gdrive_dest(args), "--gdrive-token-pickle", args.gdrive_token_pickle]
    if args.continue_on_error:
        cli.append("--continue-on-error")
    if args.retry_failed_only:
        cli.append("--retry-failed-only")
    if args.no_resume:
        cli.append("--no-resume")
    else:
        cli.append("--resume")
    if args.dry_run:
        cli.append("--dry-run")
    if args.delete_failed_downloads:
        cli.append("--delete-failed-downloads")
    if args.browser_headed:
        cli.append("--browser-headed")
    if args.chromium_executable:
        cli.extend(["--chromium-executable", args.chromium_executable])
    if args.no_browser_folder_title:
        cli.append("--no-browser-folder-title")
    return await msz_to_gdrive.run(msz_to_gdrive._build_parser().parse_args(cli))


async def _run_drive_to_telegram(args: argparse.Namespace, source_type: str) -> int:
    from . import drive_to_telegram

    source = _strip_prefix(args.source, f"{source_type}:")
    cli = [
        source,
        args.dest,
        "--source-type",
        source_type,
        "--config",
        args.config,
        "--runtime-dir",
        args.runtime_dir,
        "--gdrive-token-pickle",
        args.gdrive_token_pickle,
    ]
    if args.continue_on_error:
        cli.append("--continue-on-error")
    if args.retry_failed_only:
        cli.append("--retry-failed-only")
    if args.no_resume:
        cli.append("--no-resume")
    else:
        cli.append("--resume")
    if args.dry_run:
        cli.append("--dry-run")
    if args.keep_downloads:
        cli.append("--keep-downloads")
    if args.delete_failed_downloads:
        cli.append("--delete-failed-downloads")
    return await drive_to_telegram.run(drive_to_telegram._build_parser().parse_args(cli))


async def run(args: argparse.Namespace) -> int:
    source_type = _source_type(args.source, args.source_type)
    if args.up and args.to != "auto" and _target_alias(args.up) != _target_alias(args.to):
        raise ValueError(f"Conflicting destination flags: --to {args.to} and --up {args.up}")
    explicit_target = args.up or args.to
    target = _dest_target(args.dest, explicit_target)

    if source_type == "telegram" and args.index:
        return await _run_telegram_index(args)

    if source_type == "telegram" and target == "index":
        return await _run_telegram_index(args)

    if source_type == "telegram":
        index_path = args.index_done
        return await _run_telegram_upload(args, target, index_path)

    if source_type == "index":
        return await _run_telegram_upload(args, target, _strip_prefix(args.source, "index:"))

    if source_type == "gdrive" and target == "msz":
        return await _run_gdrive_to_msz(args)

    if source_type == "local" and target == "msz":
        return await _run_local_to_msz(args)

    if source_type == "msz" and target == "gdrive":
        return await _run_msz_to_gdrive(args)

    if source_type in {"gdrive", "msz"} and target == "telegram":
        return await _run_drive_to_telegram(args, source_type)

    if target == "telegram":
        raise ValueError(
            "Telegram upload destination supports Google Drive and MSZ sources. "
            "Examples: gdrive:<folder> --up telegram <telegram_topic_link>, or msz:<folder> --up telegram <telegram_topic_link>."
        )

    raise ValueError(
        f"Unsupported transfer: source={source_type}, target={target}. "
        "Examples: Telegram->index, index->msz/gdrive/both, GDrive->MSZ, MSZ->GDrive."
    )


def main() -> None:
    _load_env_files()
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
