from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request

from dotenv import load_dotenv

try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

try:
    import psutil
except Exception:
    psutil = None

BUNDLE_DIR = Path(__file__).resolve().parent
load_dotenv(BUNDLE_DIR / ".env")
os.environ.setdefault("HEROKU_RUNTIME_DIR", str(BUNDLE_DIR / "runtime"))
os.environ.setdefault("ALLOW_EMPTY_MAPPINGS", "true")
os.environ.setdefault("HEROKU_CONFIG_PATH", str(BUNDLE_DIR / "config.yaml"))
os.environ.setdefault("LEECH_BOT_USERNAME", "@placeholder_bot")
os.environ.setdefault("LEECH_BOT_ID", "0")

from pyrogram import Client, enums, filters
from pyrogram.errors import (
    FloodWait,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    RPCError,
    SessionPasswordNeeded,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from clone_topic_by_link import run_clone
from export_topic_list import run_export, run_index

DEFAULT_UPLOAD_TOPIC_LINK = "https://t.me/c/3541699273/38603/38604"
DEFAULT_CONFIG_PATH = BUNDLE_DIR / "config.yaml"
MONGODB_DATA_API_URL = os.getenv("MONGODB_DATA_API_URL", "").strip()
MONGODB_DATA_API_KEY = os.getenv("MONGODB_DATA_API_KEY", "").strip()
MONGODB_DATA_SOURCE = os.getenv("MONGODB_DATA_SOURCE", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "topic_ops").strip()
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "bot_state").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
RUNTIME_DIR = Path(os.environ["HEROKU_RUNTIME_DIR"]).expanduser().resolve()
STATE_DIR = RUNTIME_DIR / "state"
EXPORTS_DIR = RUNTIME_DIR / "exports"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_FILE = Path(os.getenv("LOG_FILE_PATH", str(LOG_DIR / "app.log"))).expanduser()
RESTART_MESSAGE_FILE = STATE_DIR / "restart_message.json"
BOT_STARTED_AT = time.time()


def _ensure_runtime_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _snapshot_path(name: str) -> Path:
    _ensure_runtime_dirs()
    safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in name)
    return STATE_DIR / f"{safe_name}.json"


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def _setup_logging() -> None:
    _ensure_runtime_dirs()
    root = logging.getLogger()
    root.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    pyrogram_level = getattr(
        logging,
        os.getenv("PYROGRAM_LOG_LEVEL", "WARNING").upper(),
        logging.WARNING,
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == LOG_FILE for handler in root.handlers):
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler) for handler in root.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    for logger_name in (
        "pyrogram",
        "pyrogram.client",
        "pyrogram.connection",
        "pyrogram.dispatcher",
        "pyrogram.session",
        "pyrogram.session.auth",
        "pyrogram.session.session",
    ):
        logging.getLogger(logger_name).setLevel(pyrogram_level)


def _parse_admin_ids(raw: str) -> set[int]:
    values: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.add(int(token))
        except ValueError as exc:
            raise ValueError(f"Invalid bot admin id: {token}") from exc
    return values


class MongoStateStore:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        data_source: str,
        database: str,
        collection: str,
        mongo_uri: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.data_source = data_source
        self.database = database
        self.collection = collection
        self.mongo_uri = mongo_uri

    @property
    def data_api_enabled(self) -> bool:
        return bool(
            self.base_url and self.api_key and self.data_source and self.database and self.collection
        )

    @property
    def uri_enabled(self) -> bool:
        return bool(self.mongo_uri and self.database and self.collection and MongoClient is not None)

    @property
    def enabled(self) -> bool:
        return self.data_api_enabled or self.uri_enabled

    def _request_json(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(
                "MongoDB Data API settings are incomplete. Set MONGODB_DATA_API_URL, "
                "MONGODB_DATA_API_KEY, MONGODB_DATA_SOURCE, MONGODB_DATABASE, and "
                "MONGODB_COLLECTION."
            )

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/{action}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "apiKey": self.api_key,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MongoDB Data API request failed: {message}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"MongoDB Data API unavailable: {exc}") from exc

        return json.loads(raw) if raw.strip() else {}

    async def save(self, key: str, payload: dict[str, Any]) -> None:
        document = {
            "_id": key,
            "payload": payload,
            "updated_at": time.time(),
        }
        if self.data_api_enabled:
            await asyncio.to_thread(
                self._request_json,
                "updateOne",
                {
                    "dataSource": self.data_source,
                    "database": self.database,
                    "collection": self.collection,
                    "filter": {"_id": key},
                    "update": {"$set": document},
                    "upsert": True,
                },
            )
            return

        if self.uri_enabled:
            await asyncio.to_thread(self._save_with_uri, key, document)
            return

        raise RuntimeError(
            "MongoDB settings are incomplete. Provide Data API variables or MONGODB_URI."
        )

    async def load(self, key: str) -> dict[str, Any] | None:
        if self.data_api_enabled:
            response = await asyncio.to_thread(
                self._request_json,
                "findOne",
                {
                    "dataSource": self.data_source,
                    "database": self.database,
                    "collection": self.collection,
                    "filter": {"_id": key},
                },
            )
            document = response.get("document")
            if not isinstance(document, dict):
                return None
            payload = document.get("payload")
            if not isinstance(payload, dict):
                return None
            return payload

        if self.uri_enabled:
            return await asyncio.to_thread(self._load_with_uri, key)

        raise RuntimeError(
            "MongoDB settings are incomplete. Provide Data API variables or MONGODB_URI."
        )

    def _save_with_uri(self, key: str, document: dict[str, Any]) -> None:
        if MongoClient is None:
            raise RuntimeError("pymongo is not installed")
        with MongoClient(self.mongo_uri, serverSelectionTimeoutMS=10000) as client:
            collection = client[self.database][self.collection]
            collection.update_one({"_id": key}, {"$set": document}, upsert=True)

    def _load_with_uri(self, key: str) -> dict[str, Any] | None:
        if MongoClient is None:
            raise RuntimeError("pymongo is not installed")
        with MongoClient(self.mongo_uri, serverSelectionTimeoutMS=10000) as client:
            collection = client[self.database][self.collection]
            document = collection.find_one({"_id": key})
        if not isinstance(document, dict):
            return None
        payload = document.get("payload")
        return payload if isinstance(payload, dict) else None


def _build_export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--topic-link", default="")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--out", default="")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--batch-delay-sec", type=float, default=2.0)
    parser.add_argument("--upload-topic-link", default=DEFAULT_UPLOAD_TOPIC_LINK)
    parser.add_argument("--caption-file-names", action="store_true")
    parser.add_argument("--onwards", action="store_true")
    return parser


def _build_index_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--topic-link", default="")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--batch-delay-sec", type=float, default=2.0)
    parser.add_argument("--onwards", action="store_true")
    parser.add_argument("--header", default="")
    return parser


def _build_clone_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-link", default="")
    parser.add_argument("--destination-link", default="")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--delay-sec", type=float, default=0.35)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--message-ids", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--hide-sender-name", action="store_true")
    parser.add_argument("--filename-prefix", default="")
    parser.add_argument("--filename-suffix", default="")
    parser.add_argument("--text-prefix", default="")
    parser.add_argument("--text-suffix", default="")
    return parser


def _bundle_help_text() -> str:
    return (
        "Available commands:\n\n"
        "/start - Show the command guide\n"
        "/help - Show all available commands and examples\n"
        "/status - Show the latest clone/export/index status\n"
        "/settings - Open settings (Export / Index / Clone / Other)\n"
        "/settings set <key> <value> - Change a runtime setting\n"
        "/settings reset <key>|all - Reset one or all settings\n"
        "/login - Generate and save a Telegram user session string\n"
        "/log - Upload the current bot log file\n"
        "/cancel [clone|export|index] - Cancel a running job\n"
        "/cancel clone queued <job_id_prefix> - Remove a pending queued clone\n"
        "/restart - Restart the bot process\n\n"
        "Clone:\n"
        "/clone --source-link <link> --destination-link <link> [--config <path>] [--start-id N] "
        "[--limit N] [--delay-sec S] [--batch-size N] [--message-ids 1,2,3] [--dry-run] "
        "[--continue-on-error] [--hide-sender-name] [--filename-prefix TEXT] [--filename-suffix TEXT] "
        "[--text-prefix TEXT] [--text-suffix TEXT]\n"
        "/clone <source_link> <destination_link>\n"
        "/clone status\n"
        "/clone queue - List pending clone jobs (FIFO)\n"
        "/clone last or /clone resume\n\n"
        "Export:\n"
        "/export --topic-link <link> [--config <path>] [--out <file>] [--batch-size N] "
        "[--batch-delay-sec S] [--upload-topic-link <link>] [--caption-file-names] [--onwards]\n"
        "/export <topic_link> [--out <file name with spaces allowed>]\n"
        "/export last or /export resume\n\n"
        "Index:\n"
        "/index --topic-link <link> [--config <path>] [--batch-size N] [--batch-delay-sec S] "
        "[--onwards] [--header \"Custom Header\"]\n"
        "/index <topic_link>\n"
        "/index last or /index resume\n\n"
        "Functionality:\n"
        "- Clone: copies source topic messages to destination topic in order "
        "(one active clone; extras wait in a persisted FIFO queue).\n"
        "- Export: creates a txt list of topic/channel content and uploads it.\n"
        "- Index: posts clickable text-message links back into the same topic.\n"
        "- Index default header is the source topic title (override with --header).\n"
        "- Export --out accepts natural names (spaces) and auto-adds .txt if missing.\n\n"
        "Scans the topic for text messages only, uses each text as a clickable HTML link, "
        "and sends the index back into the same topic. Use --onwards to start from the linked message."
    )


def _botfather_commands_text() -> str:
    return (
        "start - Show command guide\n"
        "help - Show all commands and examples\n"
        "status - Show latest clone/export/index status\n"
        "settings - Open runtime settings menu\n"
        "clone - Clone messages from source topic to destination topic\n"
        "export - Export topic/channel content to txt file\n"
        "index - Generate clickable text index for a topic\n"
        "cancel - Cancel running task or remove a queued clone\n"
        "restart - Restart bot process\n"
        "log - Send latest bot log file\n"
        "login - Generate Telegram session string (admin only)"
    )


def _start_help_markup(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚙️ Add Commands in BotFather", callback_data="botfather:add")],
            [InlineKeyboardButton("📋 Show BotFather Commands", callback_data="botfather:commands")],
        ]
    )


def _botfather_add_steps(bot_username: str) -> str:
    username = (bot_username or "").lstrip("@").strip()
    bot_display = f"@{username}" if username else "<your_bot_username>"
    return (
        "<b>BotFather Setup Steps</b>\n\n"
        "1) Open @BotFather\n"
        "2) Send <code>/setcommands</code>\n"
        f"3) Select your bot: <code>{_html(bot_display)}</code>\n"
        "4) Send the command list from the <b>Show BotFather Commands</b> button\n"
        "5) BotFather will confirm after saving."
    )


def _normalize_export_command(command_text: str) -> str:
    stripped = command_text.strip()
    if not stripped:
        return ""

    tokens = shlex.split(stripped)
    if not tokens:
        return ""
    if len(tokens) == 1 and tokens[0].lower() in {"last", "resume"}:
        return tokens[0]

    normalized: list[str] = []
    index = 0

    first_token = tokens[0]
    if not first_token.startswith("-"):
        normalized.extend(["--topic-link", first_token])
        index = 1

    while index < len(tokens):
        token = tokens[index]
        if token == "--out":
            index += 1
            out_tokens: list[str] = []
            while index < len(tokens) and not tokens[index].startswith("--"):
                out_tokens.append(tokens[index])
                index += 1
            normalized.extend(["--out", " ".join(out_tokens).strip()])
            continue

        normalized.append(token)
        index += 1

    return " ".join(shlex.quote(token) for token in normalized)


def _normalize_index_command(command_text: str) -> str:
    stripped = command_text.strip()
    if not stripped:
        return ""
    if stripped.lower() in {"last", "resume"} or stripped.startswith("-"):
        return stripped
    return f"--topic-link {stripped}"


def _normalize_clone_command(command_text: str) -> str:
    stripped = command_text.strip()
    if not stripped:
        return ""
    if stripped.lower() in {"last", "resume"} or stripped.startswith("-"):
        return stripped

    parts = shlex.split(stripped)
    if len(parts) >= 2:
        remaining = " ".join(parts[2:])
        prefix = f"--source-link {parts[0]} --destination-link {parts[1]}"
        return f"{prefix} {remaining}".strip()
    return stripped


ACTIVE_CLONE_TASK: asyncio.Task | None = None
ACTIVE_CLONE_CANCEL_EVENT: asyncio.Event | None = None
ACTIVE_CLONE_LATEST_STATE: dict[str, Any] | None = None
ACTIVE_EXPORT_TASK: asyncio.Task | None = None
ACTIVE_INDEX_TASK: asyncio.Task | None = None
CLONE_QUEUE_DOC_ID = "clone:queue"
_clone_pending_jobs: list[dict[str, Any]] = []
_clone_queue_cv = asyncio.Condition()
CLONE_QUEUE_WORKER_TASK: asyncio.Task | None = None
ACTIVE_STATUS_WATCH_TASKS: dict[tuple[int, int], asyncio.Task] = {}
ACTIVE_STATUS_VIEWS: dict[tuple[int, int], str] = {}
ACTIVE_STATUS_LAST_TEXTS: dict[tuple[int, int], str] = {}
ACTIVE_LOGIN_FLOWS: dict[int, dict[str, Any]] = {}
SETTINGS_PAGE_SIZE = 10

ENV_SETTING_KEYS = {
    "tg_api_id": "TG_API_ID",
    "tg_api_hash": "TG_API_HASH",
    "mongodb_database": "MONGODB_DATABASE",
    "owner_id": "BOT_ADMIN_USER_IDS",
    "tg_session_string": "TG_SESSION_STRING",
}
SECRET_SETTING_KEYS = {"tg_api_hash", "tg_session_string"}

BOT_SETTINGS_DEFAULTS: dict[str, Any] = {
    "tg_api_id": os.getenv("TG_API_ID", "").strip(),
    "tg_api_hash": os.getenv("TG_API_HASH", "").strip(),
    "mongodb_database": os.getenv("MONGODB_DATABASE", "topic_ops").strip(),
    "owner_id": os.getenv("BOT_ADMIN_USER_IDS", "").strip(),
    "tg_session_string": os.getenv("TG_SESSION_STRING", "").strip(),
    "clone_status_update_interval_sec": 8.0,
    "clone_status_success_update_interval_sec": 8.0,
    "clone_status_keepalive_interval_sec": 30.0,
    "status_command_update_interval_sec": 5.0,
    "clone_default_delay_sec": 0.2,
    "clone_default_batch_size": 50,
    "clone_filename_prefix_default": "",
    "clone_filename_suffix_default": "",
    "clone_text_prefix_default": "",
    "clone_text_suffix_default": "",
    "clone_continue_on_error_default": False,
    "clone_hide_sender_name_default": False,
    "clone_auto_resume_enabled": True,
    "export_default_batch_size": 20,
    "export_default_batch_delay_sec": 2.0,
    "export_default_caption_file_names": True,
    "export_default_onwards": False,
    "index_default_onwards": False,
}

SETTINGS_CATEGORY_ORDER = ("export", "index", "clone", "other")

SETTINGS_CATEGORY_TITLES = {
    "export": "Export",
    "index": "Index",
    "clone": "Clone",
    "other": "Other",
}

SETTINGS_CATEGORIES: dict[str, tuple[str, ...]] = {
    "export": (
        "export_default_batch_size",
        "export_default_batch_delay_sec",
        "export_default_caption_file_names",
        "export_default_onwards",
    ),
    "index": ("index_default_onwards",),
    "clone": (
        "clone_status_update_interval_sec",
        "clone_status_success_update_interval_sec",
        "clone_status_keepalive_interval_sec",
        "clone_default_delay_sec",
        "clone_default_batch_size",
        "clone_filename_prefix_default",
        "clone_filename_suffix_default",
        "clone_text_prefix_default",
        "clone_text_suffix_default",
        "clone_continue_on_error_default",
        "clone_hide_sender_name_default",
        "clone_auto_resume_enabled",
    ),
    "other": (
        "tg_api_id",
        "tg_api_hash",
        "mongodb_database",
        "owner_id",
        "tg_session_string",
        "status_command_update_interval_sec",
    ),
}

BOT_TOGGLE_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "clone_continue_on_error_default",
        "clone_hide_sender_name_default",
        "clone_auto_resume_enabled",
        "export_default_caption_file_names",
        "export_default_onwards",
        "index_default_onwards",
    }
)

SETTINGS_KEY_LABELS: dict[str, str] = {
    "export_default_batch_size": "Batch size",
    "export_default_batch_delay_sec": "Batch delay (sec)",
    "export_default_caption_file_names": "Caption → filenames",
    "export_default_onwards": "Onwards",
    "index_default_onwards": "Onwards",
    "clone_status_update_interval_sec": "Status interval (sec)",
    "clone_status_success_update_interval_sec": "Success status interval",
    "clone_status_keepalive_interval_sec": "Keepalive interval (sec)",
    "clone_default_delay_sec": "Default delay (sec)",
    "clone_default_batch_size": "Default batch size",
    "clone_filename_prefix_default": "Filename prefix",
    "clone_filename_suffix_default": "Filename suffix",
    "clone_text_prefix_default": "Text prefix",
    "clone_text_suffix_default": "Text suffix",
    "clone_continue_on_error_default": "Continue on error",
    "clone_hide_sender_name_default": "Hide sender name",
    "clone_auto_resume_enabled": "Auto-resume clone",
    "tg_api_id": "API ID",
    "tg_api_hash": "API hash",
    "mongodb_database": "MongoDB database",
    "owner_id": "Owner user IDs",
    "tg_session_string": "Session string",
    "status_command_update_interval_sec": "/status refresh (sec)",
}


def _settings_key_label(key: str) -> str:
    return SETTINGS_KEY_LABELS.get(key, key)


def _assert_settings_categories_complete() -> None:
    ordered: list[str] = []
    for category in SETTINGS_CATEGORY_ORDER:
        ordered.extend(SETTINGS_CATEGORIES[category])
    if set(ordered) != set(BOT_SETTINGS_DEFAULTS):
        missing = set(BOT_SETTINGS_DEFAULTS) - set(ordered)
        extra = set(ordered) - set(BOT_SETTINGS_DEFAULTS)
        raise RuntimeError(f"SETTINGS_CATEGORIES out of sync: missing={missing!r} extra={extra!r}")


_assert_settings_categories_complete()

MIN_CLONE_AUTO_EDIT_INTERVAL_SEC = 5.0
MIN_CLONE_KEEPALIVE_EDIT_INTERVAL_SEC = 20.0
MIN_WATCHED_STATUS_INTERVAL_SEC = 5.0
MAX_STATUS_FLOOD_SLEEP_SEC = 30

BOT_SETTINGS_HELP = (
    "Settings commands:\n"
    "/settings\n"
    "/settings show\n"
    "/settings set <key> <value>\n"
    "/settings reset <key>\n"
    "/settings reset all\n\n"
    "Useful keys:\n"
    "- clone_status_update_interval_sec\n"
    "- clone_status_success_update_interval_sec\n"
    "- clone_status_keepalive_interval_sec\n"
    "- status_command_update_interval_sec\n"
    "- clone_default_delay_sec\n"
    "- clone_default_batch_size\n"
    "- clone_filename_prefix_default\n"
    "- clone_filename_suffix_default\n"
    "- clone_text_prefix_default\n"
    "- clone_text_suffix_default\n"
    "- clone_continue_on_error_default\n"
    "- clone_hide_sender_name_default\n"
    "- clone_auto_resume_enabled\n"
    "- export_default_batch_size\n"
    "- export_default_batch_delay_sec\n"
    "- export_default_caption_file_names\n"
    "- export_default_onwards\n"
    "- index_default_onwards\n"
    "- tg_api_id\n"
    "- tg_api_hash\n"
    "- mongodb_database\n"
    "- owner_id\n"
    "- tg_session_string"
)


def _coerce_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected true/false")


def _normalize_setting_value(key: str, value: Any) -> Any:
    if key not in BOT_SETTINGS_DEFAULTS:
        raise ValueError(f"Unknown setting: {key}")

    if key in {
        "clone_status_update_interval_sec",
        "clone_status_success_update_interval_sec",
        "clone_status_keepalive_interval_sec",
        "status_command_update_interval_sec",
        "clone_default_delay_sec",
        "export_default_batch_delay_sec",
    }:
        number = float(value)
        if number < 0:
            raise ValueError(f"{key} must be >= 0")
        return number

    if key in {
        "clone_default_batch_size",
        "export_default_batch_size",
    }:
        number = int(value)
        if number <= 0:
            raise ValueError(f"{key} must be > 0")
        return number

    if key in BOT_TOGGLE_SETTING_KEYS:
        if isinstance(value, bool):
            return value
        return _coerce_bool(str(value))

    if key == "tg_api_id":
        raw = str(value).strip()
        if raw:
            number = int(raw)
            if number <= 0:
                raise ValueError("tg_api_id must be > 0")
        return raw

    if key == "owner_id":
        raw = str(value).strip()
        if raw:
            _parse_admin_ids(raw)
        return raw

    if key in {
        "tg_api_hash",
        "mongodb_database",
        "tg_session_string",
        "clone_filename_prefix_default",
        "clone_filename_suffix_default",
        "clone_text_prefix_default",
        "clone_text_suffix_default",
    }:
        return str(value).strip()

    return value


def _setting_default(key: str) -> Any:
    env_name = ENV_SETTING_KEYS.get(key)
    if env_name:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return BOT_SETTINGS_DEFAULTS[key]


def _masked_setting_value(key: str, value: Any) -> str:
    text = str(value or "")
    if key not in SECRET_SETTING_KEYS:
        return text
    if not text:
        return ""
    if len(text) <= 8:
        return "********"
    return f"{text[:4]}...{text[-4:]}"


def _apply_env_settings(settings: dict[str, Any]) -> None:
    for key, env_name in ENV_SETTING_KEYS.items():
        value = str(settings.get(key, "") or "").strip()
        if value:
            os.environ[env_name] = value


def _apply_bootstrap_settings() -> None:
    stored = _read_json_file(_snapshot_path("bot_settings"))
    if isinstance(stored, dict):
        _apply_env_settings(stored)


async def _close_login_flow(user_id: int) -> None:
    flow = ACTIVE_LOGIN_FLOWS.pop(user_id, None)
    if not flow:
        return
    login_client = flow.get("client")
    if login_client is None:
        return
    try:
        await login_client.disconnect()
    except Exception:
        pass


def _normalize_login_code(raw: str) -> str:
    return "".join(char for char in raw.strip() if char.isdigit())


def _login_help_text() -> str:
    return (
        "<b>Telegram Session Login</b>\n\n"
        "Use <code>/login</code> to generate a Pyrogram user session string.\n"
        "If <code>TG_SESSION_STRING</code> is already set, the bot will not ask you to login again.\n\n"
        "You can also start with:\n"
        "<code>/login &lt;api_id&gt; &lt;api_hash&gt;</code>\n\n"
        "To replace an existing session, use:\n"
        "<code>/login force</code>\n"
        "<code>/login force &lt;api_id&gt; &lt;api_hash&gt;</code>\n\n"
        "Cancel anytime with <code>/login cancel</code>."
    )


def _configured_session_string(settings: dict[str, Any] | None = None) -> str:
    if isinstance(settings, dict):
        value = str(settings.get("tg_session_string") or "").strip()
        if value:
            return value
    return os.getenv("TG_SESSION_STRING", "").strip()


def _resume_source_message_id_from_state(state: dict[str, Any]) -> int:
    resume_after_id = _safe_int(state.get("resume_after_source_message_id"))
    if resume_after_id > 0:
        return resume_after_id

    last_successful_id = _safe_int(state.get("last_successful_source_message_id"))
    if last_successful_id > 0:
        return last_successful_id

    last_processed_id = _safe_int(state.get("last_processed_source_message_id"))
    if last_processed_id > 0:
        return last_processed_id

    current_message_id = _safe_int(state.get("current_message_id"))
    if current_message_id > 0:
        return current_message_id

    current_index = _safe_int(state.get("current_index"))
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else None
    if current_index > 0 and isinstance(payload, dict):
        message_ids = str(payload.get("message_ids") or "").strip()
        if message_ids:
            parsed_message_ids = _parse_message_ids_for_resume(message_ids)
            if parsed_message_ids:
                return parsed_message_ids[min(current_index, len(parsed_message_ids)) - 1]

        start_id = _safe_int(payload.get("start_id"))
        if start_id > 0:
            return start_id + current_index - 1

    return 0


def _resume_clone_payload_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    phase = str(state.get("phase", "") or "").lower()
    if phase not in {"running", "queued"}:
        return None

    payload = state.get("payload") if isinstance(state.get("payload"), dict) else None
    if not payload:
        return None

    resumed = dict(payload)
    resume_after_id = _resume_source_message_id_from_state(state)
    completed_count = (
        _safe_int(state.get("success"))
        + _safe_int(state.get("failed"))
        + _safe_int(state.get("skipped"))
    )
    if completed_count <= 0:
        completed_count = _safe_int(state.get("current_index"))
    total_messages = _safe_int(state.get("total_messages"))

    if resume_after_id > 0:
        message_ids = str(resumed.get("message_ids") or "").strip()
        if message_ids:
            remaining_ids = [
                message_id
                for message_id in _parse_message_ids_for_resume(message_ids)
                if message_id > resume_after_id
            ]
            resumed["message_ids"] = ",".join(str(message_id) for message_id in remaining_ids)
            if not remaining_ids:
                return None
        else:
            resumed["start_id"] = resume_after_id + 1
            if total_messages > 0:
                resumed["limit"] = max(total_messages - completed_count, 0)
            elif _safe_int(resumed.get("limit")) > 0:
                resumed["limit"] = max(_safe_int(resumed.get("limit")) - completed_count, 0)
            if _safe_int(resumed.get("limit")) == 0 and (total_messages > 0 or _safe_int(payload.get("limit")) > 0):
                return None

    resumed["resume_from_restart"] = True
    resumed["resumed_after_source_message_id"] = resume_after_id or None
    return resumed


def _parse_message_ids_for_resume(raw: str) -> list[int]:
    values: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(int(token))
        except ValueError:
            continue
    return sorted(set(value for value in values if value > 0))


async def _load_bot_settings(store: MongoStateStore) -> dict[str, Any]:
    try:
        stored = await store.load("bot:settings")
    except Exception:
        stored = _read_json_file(_snapshot_path("bot_settings"))

    merged = {key: _setting_default(key) for key in BOT_SETTINGS_DEFAULTS}
    if isinstance(stored, dict):
        for key in BOT_SETTINGS_DEFAULTS:
            default_value = _setting_default(key)
            if key not in stored:
                continue
            try:
                value = _normalize_setting_value(key, stored[key])
            except Exception:
                merged[key] = default_value
                continue
            if key in ENV_SETTING_KEYS and not str(value or "").strip() and str(default_value or "").strip():
                continue
            merged[key] = value
    _apply_env_settings(merged)
    return merged


async def _save_bot_settings(store: MongoStateStore, settings: dict[str, Any]) -> None:
    normalized = {
        key: _normalize_setting_value(key, settings.get(key, _setting_default(key)))
        for key in BOT_SETTINGS_DEFAULTS
    }
    _write_json_file(_snapshot_path("bot_settings"), normalized)
    try:
        await store.save("bot:settings", normalized)
    except Exception:
        pass
    _apply_env_settings(normalized)


def _format_bot_settings(settings: dict[str, Any]) -> str:
    lines = ["<b>Bot Settings</b>"]
    for key in sorted(BOT_SETTINGS_DEFAULTS):
        current = settings.get(key, _setting_default(key))
        default = _setting_default(key)
        lines.append(
            f"<code>{key}</code> = <code>{_html(_masked_setting_value(key, current))}</code> "
            f"(default: <code>{_html(_masked_setting_value(key, default))}</code>)"
        )
    lines.append("")
    lines.append("Use <code>/settings set &lt;key&gt; &lt;value&gt;</code>")
    lines.append("Use <code>/settings reset &lt;key&gt;</code> or <code>/settings reset all</code>")
    return "\n".join(lines)


def _display_name(user) -> str:
    first_name = str(getattr(user, "first_name", "") or "").strip()
    last_name = str(getattr(user, "last_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part)
    return full_name or username or "Admin"


def _category_page_count(category: str) -> int:
    keys = SETTINGS_CATEGORIES.get(category, ())
    return max((len(keys) + SETTINGS_PAGE_SIZE - 1) // SETTINGS_PAGE_SIZE, 1)


def _category_page_keys(category: str, page: int) -> list[str]:
    keys = list(SETTINGS_CATEGORIES.get(category, ()))
    if not keys:
        return []
    page_count = _category_page_count(category)
    bounded_page = min(max(page, 0), page_count - 1)
    start = bounded_page * SETTINGS_PAGE_SIZE
    return keys[start : start + SETTINGS_PAGE_SIZE]


def _settings_category_index(category: str) -> int:
    return SETTINGS_CATEGORY_ORDER.index(category)


def _settings_category_by_index(index: int) -> str | None:
    if index < 0 or index >= len(SETTINGS_CATEGORY_ORDER):
        return None
    return SETTINGS_CATEGORY_ORDER[index]


def _settings_key_index(category: str, key: str) -> int:
    return SETTINGS_CATEGORIES[category].index(key)


def _settings_key_by_index(category: str, key_index: int) -> str | None:
    keys = SETTINGS_CATEGORIES[category]
    if key_index < 0 or key_index >= len(keys):
        return None
    return keys[key_index]


def _format_settings_root(user) -> str:
    return (
        f"<b>{_html(_display_name(user))}</b>\n"
        "<b>Settings</b>\n\n"
        "Choose <b>Export</b>, <b>Index</b>, <b>Clone</b>, or <b>Other</b> to open defaults for that feature."
    )


def _settings_root_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Export", callback_data="settings:cat:0:0"),
                InlineKeyboardButton("Index", callback_data="settings:cat:1:0"),
            ],
            [
                InlineKeyboardButton("Clone", callback_data="settings:cat:2:0"),
                InlineKeyboardButton("Other", callback_data="settings:cat:3:0"),
            ],
            [InlineKeyboardButton("Close", callback_data="settings:close")],
        ]
    )


def _format_category_panel(settings: dict[str, Any], category: str, page: int, user) -> str:
    if category not in SETTINGS_CATEGORIES:
        category = "export"
    title = SETTINGS_CATEGORY_TITLES.get(category, category.title())
    page_count = _category_page_count(category)
    page = min(max(page, 0), page_count - 1)
    lines = [
        f"<b>{_html(_display_name(user))}</b>",
        "/settings",
        "",
        f"<b>{_html(title)}</b> defaults",
        f"Page {_html(page + 1)} of {_html(page_count)}",
        "",
        "Tap a button below. On/off options can be toggled on the next screen.",
    ]
    if category == "index":
        lines.append("")
        lines.append("<i>Batch size and delay still follow Export defaults unless you pass flags in /index.</i>")
    return "\n".join(lines)


def _category_settings_markup(category: str, page: int) -> InlineKeyboardMarkup:
    if category not in SETTINGS_CATEGORIES:
        category = "export"
    page_count = _category_page_count(category)
    page = min(max(page, 0), page_count - 1)
    rows: list[list[InlineKeyboardButton]] = []
    keys = _category_page_keys(category, page)
    category_index = _settings_category_index(category)
    start = page * SETTINGS_PAGE_SIZE
    key_buttons = [
        InlineKeyboardButton(
            _settings_key_label(setting_key),
            callback_data=f"settings:item:{category_index}:{page}:view:{start + offset}",
        )
        for offset, setting_key in enumerate(keys)
    ]
    rows.extend(key_buttons[index : index + 2] for index in range(0, len(key_buttons), 2))
    rows.append(
        [
            InlineKeyboardButton("Home", callback_data="settings:home"),
            InlineKeyboardButton("Close", callback_data="settings:close"),
        ]
    )
    if page_count > 1:
        category_index = _settings_category_index(category)
        page_buttons = [
            InlineKeyboardButton(
                str(index),
                callback_data=f"settings:cat:{category_index}:{index}",
            )
            for index in range(page_count)
        ]
        rows.extend(page_buttons[index : index + 8] for index in range(0, len(page_buttons), 8))
    return InlineKeyboardMarkup(rows)


def _format_setting_detail(
    settings: dict[str, Any],
    key: str,
    category: str,
    page: int,
    state: str,
    user,
) -> str:
    current = settings.get(key, _setting_default(key))
    default = _setting_default(key)
    state = state if state in {"view", "edit"} else "view"
    cat_title = SETTINGS_CATEGORY_TITLES.get(category, category)
    label = _settings_key_label(key)
    lines = [
        f"<b>{_html(_display_name(user))}</b>",
        "/settings",
        "",
        f"<b>{_html(cat_title)}</b> · {_html(label)}",
        f"<code>{_html(key)}</code>",
        "",
        f"┠ <b>Current</b> → <code>{_html(_masked_setting_value(key, current))}</code>",
        f"┠ <b>Default</b> → <code>{_html(_masked_setting_value(key, default))}</code>",
    ]
    if key in BOT_TOGGLE_SETTING_KEYS:
        lines.append("┠ Use the toggle button below for on/off.")
    if state == "edit":
        lines.extend(
            [
                "┃",
                "┖ Send:",
                f"<code>/settings set {key} &lt;value&gt;</code>",
            ]
        )
    else:
        lines.append("┖ Tap <b>Edit</b> to show the set command.")
    return "\n".join(lines)


def _setting_detail_markup(
    key: str,
    category: str,
    page: int,
    state: str,
    settings: dict[str, Any],
) -> InlineKeyboardMarkup:
    state = state if state in {"view", "edit"} else "view"
    toggle_label = "View" if state == "edit" else "Edit"
    toggle_state = "view" if state == "edit" else "edit"
    category_index = _settings_category_index(category)
    key_index = _settings_key_index(category, key)
    rows: list[list[InlineKeyboardButton]] = []
    if key in BOT_TOGGLE_SETTING_KEYS:
        current = bool(settings.get(key, _setting_default(key)))
        flip_label = "Turn off" if current else "Turn on"
        rows.append(
            [
                InlineKeyboardButton(
                    flip_label,
                    callback_data=f"settings:toggle:{category_index}:{page}:{state}:{key_index}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                toggle_label,
                callback_data=f"settings:item:{category_index}:{page}:{toggle_state}:{key_index}",
            ),
            InlineKeyboardButton(
                "Reset",
                callback_data=f"settings:reset:{category_index}:{page}:{state}:{key_index}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "Back",
                callback_data=f"settings:cat:{category_index}:{page}",
            ),
            InlineKeyboardButton("Home", callback_data="settings:home"),
        ]
    )
    rows.append([InlineKeyboardButton("Close", callback_data="settings:close")])
    return InlineKeyboardMarkup(rows)


def _html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _readable_file_size(size_in_bytes: Any) -> str:
    try:
        size = float(size_in_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "0B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f}{units[unit_index]}"


def _readable_time(seconds: Any) -> str:
    try:
        remaining = int(max(float(seconds or 0), 0))
    except (TypeError, ValueError):
        remaining = 0
    if remaining <= 0:
        return "0s"

    parts = []
    for suffix, length in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if remaining >= length:
            value, remaining = divmod(remaining, length)
            parts.append(f"{value}{suffix}")
    return "".join(parts) or "0s"


def _restart_success_text() -> str:
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(BOT_STARTED_AT))
    return (
        "⌬ <b><i>Restarted Successfully!</i></b>\n"
        f"┟ <b>Date:</b> {time.strftime('%Y-%m-%d', time.gmtime(BOT_STARTED_AT))}\n"
        f"┠ <b>Time:</b> {time.strftime('%H:%M:%S', time.gmtime(BOT_STARTED_AT))} UTC\n"
        f"┖ <b>Started:</b> {started}"
    )


def _progress_bar(percent: float) -> str:
    bounded = min(max(float(percent), 0.0), 100.0)
    filled = int(bounded // 8)
    return f"[{'⬢' * filled}{'⬡' * (12 - filled)}]"


def _format_percent(value: float) -> str:
    return f"{min(max(value, 0.0), 100.0):.1f}%"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_invalid_status_message_error(exc: Exception) -> bool:
    message = str(exc).upper()
    return "MESSAGE_ID_INVALID" in message or "MESSAGE TO EDIT NOT FOUND" in message


async def _edit_status_message(
    status: Any,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    sleep_on_flood: bool = True,
) -> bool:
    if status is None:
        return False

    try:
        await status.edit_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        return True
    except FloodWait as exc:
        wait_seconds = int(getattr(exc, "value", 0) or 0)
        if not sleep_on_flood or wait_seconds > MAX_STATUS_FLOOD_SLEEP_SEC:
            logging.getLogger("heroku_bot").warning(
                "skipping status edit because Telegram requested a flood wait",
                extra={
                    "event": "status_edit_flood_wait_skipped",
                    "wait_seconds": wait_seconds,
                },
            )
            return False
        await asyncio.sleep(wait_seconds + 1)
        try:
            await status.edit_text(
                text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            return True
        except Exception as retry_exc:
            if not _is_invalid_status_message_error(retry_exc):
                logging.getLogger("heroku_bot").debug("status edit retry failed", exc_info=True)
            return False
    except RPCError as exc:
        if not _is_invalid_status_message_error(exc):
            logging.getLogger("heroku_bot").debug("status edit failed", exc_info=True)
        return False
    except Exception:
        logging.getLogger("heroku_bot").debug("status edit failed", exc_info=True)
        return False


async def _reply_status_message(
    message: Any,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any | None:
    try:
        return await message.reply_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except FloodWait as exc:
        wait_seconds = int(getattr(exc, "value", 0) or 0)
        if wait_seconds <= MAX_STATUS_FLOOD_SLEEP_SEC:
            await asyncio.sleep(wait_seconds + 1)
            try:
                return await message.reply_text(
                    text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
            except Exception:
                logging.getLogger("heroku_bot").debug("status reply retry failed", exc_info=True)
                return None

        logging.getLogger("heroku_bot").warning(
            "skipping status reply because Telegram requested a flood wait",
            extra={
                "event": "status_reply_flood_wait_skipped",
                "wait_seconds": wait_seconds,
            },
        )
        return None
    except Exception:
        logging.getLogger("heroku_bot").debug("status reply failed", exc_info=True)
        return None


async def _send_clone_status_message(
    bot: Client,
    chat_id: int,
    reply_to_message_id: int | None,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any | None:
    send_kw: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": enums.ParseMode.HTML,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        send_kw["reply_markup"] = reply_markup
    if reply_to_message_id:
        send_kw["reply_to_message_id"] = reply_to_message_id
    try:
        return await bot.send_message(**send_kw)
    except FloodWait as exc:
        wait_seconds = int(getattr(exc, "value", 0) or 0)
        if wait_seconds <= MAX_STATUS_FLOOD_SLEEP_SEC:
            await asyncio.sleep(wait_seconds + 1)
            try:
                return await bot.send_message(**send_kw)
            except Exception:
                logging.getLogger("heroku_bot").debug("clone status send retry failed", exc_info=True)
                return None
        logging.getLogger("heroku_bot").warning(
            "skipping clone status send because Telegram requested a flood wait",
            extra={"event": "clone_status_send_flood_wait_skipped", "wait_seconds": wait_seconds},
        )
        return None
    except Exception:
        logging.getLogger("heroku_bot").debug("clone status send failed", exc_info=True)
        return None


async def _save_clone_queue_snapshot(store: MongoStateStore, jobs: list[dict[str, Any]]) -> None:
    body = {"jobs": jobs, "updated_at": time.time()}
    try:
        await store.save(CLONE_QUEUE_DOC_ID, body)
    except Exception:
        pass
    _write_json_file(_snapshot_path("clone_queue"), body)


async def _load_clone_queue_jobs_from_store(store: MongoStateStore) -> list[dict[str, Any]]:
    try:
        raw = await store.load(CLONE_QUEUE_DOC_ID)
    except Exception:
        raw = None
    if raw is None:
        snapshot = _read_json_file(_snapshot_path("clone_queue"))
        raw = snapshot if isinstance(snapshot, dict) else None
    jobs = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(jobs, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for entry in jobs:
        if isinstance(entry, dict) and entry.get("job_id") and isinstance(entry.get("payload"), dict):
            cleaned.append(dict(entry))
    return cleaned


async def _hydrate_clone_queue_from_storage(store: MongoStateStore) -> None:
    loaded = await _load_clone_queue_jobs_from_store(store)
    async with _clone_queue_cv:
        _clone_pending_jobs[:] = loaded


async def _enqueue_clone_request(
    *,
    store: MongoStateStore,
    bot: Client,
    chat_id: int,
    command_message_id: int | None,
    payload: dict[str, Any],
) -> tuple[str, int]:
    job_id = str(uuid.uuid4())
    job: dict[str, Any] = {
        "job_id": job_id,
        "payload": dict(payload),
        "requested_chat_id": chat_id,
        "requested_message_id": command_message_id,
        "enqueued_at": time.time(),
    }
    async with _clone_queue_cv:
        _clone_pending_jobs.append(job)
        await _save_clone_queue_snapshot(store, list(_clone_pending_jobs))
        position = len(_clone_pending_jobs)

    short_id = job_id.split("-")[0]
    notice_lines = [
        "<b>Clone queued</b>",
        "",
        f"Position in queue: <b>{position}</b>",
        f"Job id: <code>{_html(short_id)}</code>",
        "",
        "<i>You will receive the live progress panel when this job starts.</i>",
    ]
    notice_text = "\n".join(notice_lines)
    sent = await _send_clone_status_message(
        bot,
        chat_id,
        command_message_id,
        notice_text,
    )
    async with _clone_queue_cv:
        for entry in _clone_pending_jobs:
            if entry.get("job_id") == job_id:
                entry["notice_message_id"] = getattr(sent, "id", None) if sent is not None else None
                break
        await _save_clone_queue_snapshot(store, list(_clone_pending_jobs))
        _clone_queue_cv.notify_all()

    return job_id, position


def _format_clone_queue_listing(jobs_snapshot: list[dict[str, Any]] | None = None) -> str:
    jobs = jobs_snapshot if jobs_snapshot is not None else list(_clone_pending_jobs)
    lines: list[str] = ["<b>Pending clone jobs</b>", ""]
    if not jobs:
        lines.append("No jobs are waiting in the queue.")
        return "\n".join(lines)
    for idx, job in enumerate(jobs, start=1):
        jid = str(job.get("job_id", ""))
        short = jid[:8] if jid else "?"
        pl = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        src = str(pl.get("source_link") or "").strip() or "n/a"
        if len(src) > 48:
            src = src[:45] + "…"
        lines.append(f"{idx}. <code>{_html(short)}</code> · {_html(src)}")
    lines.append("")
    lines.append("Remove with <code>/cancel clone queued &lt;job_id&gt;</code>")
    return "\n".join(lines)


async def _cancel_queued_clone_job_by_token(store: MongoStateStore, token: str) -> dict[str, Any] | None:
    needle = token.strip().lower().replace("-", "")
    if not needle:
        return None
    async with _clone_queue_cv:
        for index, job in enumerate(_clone_pending_jobs):
            jid = str(job.get("job_id", ""))
            compact = jid.lower().replace("-", "")
            if compact == needle or (len(needle) <= len(compact) and compact.startswith(needle)):
                removed = _clone_pending_jobs.pop(index)
                await _save_clone_queue_snapshot(store, list(_clone_pending_jobs))
                _clone_queue_cv.notify_all()
                return removed
    return None


def _status_task_title(state: dict[str, Any]) -> str:
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    current = _safe_int(state.get("current_index"))
    total = _safe_int(state.get("total_messages"))
    current_message_id = state.get("current_message_id")
    topic = _format_clone_endpoint(payload, "source")
    if current_message_id:
        return f"Clone Task {current}/{total or '?'} · Source #{current_message_id}"
    if topic:
        return f"Clone Task · {topic}"
    return "Clone Task"


def _status_requester(payload: dict[str, Any]) -> str:
    user_id = payload.get("requested_by_id")
    name = str(payload.get("requested_by_name") or "Admin").strip() or "Admin"
    if user_id:
        return f"<a href=\"tg://user?id={_html(user_id)}\">{_html(name)}</a> ( #ID{_html(user_id)} )"
    return _html(name)


def _clone_stage_label(state: dict[str, Any]) -> str:
    phase = str(state.get("phase", "unknown")).lower()
    if phase == "running":
        stage = str(state.get("transfer_stage") or "").lower()
        if stage == "upload":
            return "Upload"
        if stage == "download":
            return "Download"
        return "Clone"
    if phase == "completed":
        return "CLONE COMPLETED"
    return phase.title()


def _clone_progress_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    stage = str(state.get("transfer_stage") or "").lower()
    if stage in {"download", "upload"}:
        current = _safe_int(state.get(f"{stage}_current"))
        total = _safe_int(state.get(f"{stage}_total"))
        percent = (current / total * 100.0) if total > 0 else 0.0
        speed = str(state.get(f"{stage}_speed") or "0B/s")
        eta_seconds = None
        speed_bps = _safe_float(state.get(f"{stage}_speed_bps"))
        if speed_bps > 0 and total > 0:
            eta_seconds = max((total - current) / speed_bps, 0.0)
        return {
            "percent": percent,
            "processed": _readable_file_size(current),
            "total": _readable_file_size(total),
            "unit": "",
            "speed": speed,
            "eta_seconds": eta_seconds,
        }

    current = _safe_int(state.get("current_index"))
    total = _safe_int(state.get("total_messages"))
    percent = (current / total * 100.0) if total > 0 else 0.0
    started_at = _safe_float(state.get("started_at"))
    elapsed = max(time.time() - started_at, 0.0) if started_at > 0 else 0.0
    eta_seconds = None
    if current > 0 and total > current and elapsed > 0:
        eta_seconds = (total - current) * (elapsed / current)
    return {
        "percent": percent,
        "processed": str(current),
        "total": str(total),
        "unit": "messages",
        "speed": "-",
        "eta_seconds": eta_seconds,
    }


def _format_bot_stats() -> str:
    if psutil is None:
        return f"⌬ <b><u>Bot Stats</u></b>\n┖ <b>UP</b> → {_readable_time(time.time() - BOT_STARTED_AT)}"

    _ensure_runtime_dirs()
    disk = psutil.disk_usage(str(RUNTIME_DIR))
    return (
        "⌬ <b><u>Bot Stats</u></b>"
        f"\n┟ <b>CPU</b> → {psutil.cpu_percent()}% | <b>F</b> → {_readable_file_size(disk.free)} [{round(100 - disk.percent, 1)}%]"
        f"\n┖ <b>RAM</b> → {psutil.virtual_memory().percent}% | <b>UP</b> → {_readable_time(time.time() - BOT_STARTED_AT)}"
    )


def _format_clone_status_panel(state: dict[str, Any]) -> str:
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    phase = str(state.get("phase", "unknown")).lower()
    snapshot = _clone_progress_snapshot(state)
    percent = snapshot["percent"]
    started_at = _safe_float(state.get("started_at"))
    elapsed = max(time.time() - started_at, 0.0) if started_at > 0 else 0.0
    eta_seconds = snapshot.get("eta_seconds")
    total_time = elapsed + (float(eta_seconds) if eta_seconds is not None else 0.0)
    eta_text = "-" if eta_seconds is None else _readable_time(eta_seconds)

    processed = f"{snapshot['processed']} of {snapshot['total']}"
    if snapshot.get("unit"):
        processed = f"{processed} {snapshot['unit']}"

    source_label = _format_clone_endpoint(payload, "source") or payload.get("source_link", "n/a")
    destination_label = _format_clone_endpoint(payload, "destination") or payload.get("destination_link", "n/a")
    message_type = str(state.get("current_message_type") or "").strip()
    file_name = str(state.get("current_file_name") or "").strip()

    title = "MSZ CLONE BOT BY ABDULLAH"
    title_separator = "━" * len(title)
    lines = [
        title_separator,
        f"<b>{title}</b>",
        title_separator,
        "",
        f"<b>1.</b> <b><i>{_html(_status_task_title(state))}</i></b>",
        "",
        f"<b>Task By {_status_requester(payload)}</b>",
        f"┟ {_progress_bar(percent)} <i>{_format_percent(percent)}</i>",
        f"┠ <b>Processed</b> → <i>{_html(processed)}</i>",
        f"┠ <b>Status</b> → <b>{_html(_clone_stage_label(state))}</b>",
        f"┠ <b>Speed</b> → <i>{_html(snapshot['speed'])}</i>",
        f"┠ <b>Time</b> → <i>{_html(eta_text)} of {_html(_readable_time(total_time))} ( {_html(_readable_time(elapsed))} )</i>",
        "┠ <b>Engine</b> → <i>Pyrogram</i>",
        f"┠ <b>SOURCE</b> → <i>{_html(source_label)}</i>",
        f"┠ <b>DESTINATION</b> → <i>{_html(destination_label)}</i>",
    ]
    flood_wait_until = _safe_float(state.get("flood_wait_until"))
    flood_wait_seconds = _safe_float(state.get("flood_wait_seconds"))
    if flood_wait_until > time.time() or (flood_wait_seconds > 0 and not flood_wait_until):
        remaining = max(flood_wait_until - time.time(), 0.0) if flood_wait_until else flood_wait_seconds
        operation = str(state.get("flood_wait_operation") or "telegram")
        lines.append(
            f"┠ <b>FloodWait</b> → <i>{_html(_readable_time(remaining))} for {_html(operation)}</i>"
        )
    if message_type:
        lines.append(f"┠ <b>TYPE</b> → <i>{_html(message_type)}</i>")
    if file_name:
        lines.append(f"┠ <b>Filename</b> → <i>{_html(file_name)}</i>")

    if phase == "running":
        lines.append("<b>┖ Stop</b> → <i>/cancel</i>")
    elif phase == "completed":
        skipped = _safe_int(state.get("skipped"))
        skipped_text = f" | Skipped {skipped}" if skipped else ""
        lines.append(
            f"┖ <b>Result</b> → <i>Forwarded {state.get('success', 0)} | Failed {state.get('failed', 0)}{skipped_text}</i>"
        )
    elif phase in {"failed", "cancelled"}:
        error = state.get("error")
        if error:
            lines.append(f"┠ <b>Error</b> → <i>{_html(error)}</i>")
        skipped = _safe_int(state.get("skipped"))
        skipped_text = f" | Skipped {skipped}" if skipped else ""
        lines.append(
            f"┖ <b>Result</b> → <i>Forwarded {state.get('success', 0)} | Failed {state.get('failed', 0)}{skipped_text}</i>"
        )
    else:
        lines.append(f"┖ <b>Phase</b> → <i>{_html(phase.title())}</i>")

    last_link = str(state.get("last_successful_message_link") or "").strip()
    if last_link and phase in {"failed", "cancelled"}:
        lines.append(f"\nLast successful transfer: {_html(last_link)}")

    return "\n".join(lines)


def _format_clone_status_with_stats(state: dict[str, Any]) -> str:
    return f"{_format_clone_status_panel(state)}\n\n{_format_bot_stats()}"


def _clone_job_callback_token(job_id: str) -> str:
    if not job_id:
        return ""
    return str(job_id).replace("-", "").lower()[:8]


def _format_clone_queued_section(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return ""
    lines: list[str] = [
        "☷ <b>Queued clone tasks</b>",
        "",
    ]
    for idx, job in enumerate(jobs, start=1):
        jid = str(job.get("job_id", ""))
        short = _clone_job_callback_token(jid) or "?"
        pl = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        src = str(pl.get("source_link") or "").strip() or "n/a"
        if len(src) > 56:
            src = src[:53] + "…"
        req_name = str(pl.get("requested_by_name") or "").strip() or "Admin"
        req_id = pl.get("requested_by_id")
        who = (
            f"<a href=\"tg://user?id={_html(req_id)}\">{_html(req_name)}</a>"
            if req_id
            else _html(req_name)
        )
        dst = str(pl.get("destination_link") or "").strip()
        if len(dst) > 40:
            dst = dst[:37] + "…"
        dst_part = f"\n   ┠ <b>To</b> → <i>{_html(dst)}</i>" if dst else ""
        lines.append(f"<b>{idx}.</b> <code>{_html(short)}</code> · <i>{_html(src)}</i>{dst_part}")
        lines.append(f"   ┖ <b>By</b> {who}")
    lines.extend(["", "<i>Use the Cancel buttons below or /cancel clone queued &lt;id&gt;</i>"])
    return "\n".join(lines)


def _format_clone_status_display(
    clone_state: dict[str, Any] | None,
    queue_jobs: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if clone_state:
        parts.append(_format_clone_status_panel(clone_state))
    elif queue_jobs:
        title = "MSZ CLONE BOT BY ABDULLAH"
        sep = "━" * len(title)
        parts.extend(
            [
                sep,
                f"<b>{title}</b>",
                sep,
                "",
                "<i>No checkpoint on file; clone jobs below are waiting in the FIFO queue.</i>",
            ]
        )
    else:
        parts.append("<i>No saved clone state.</i>")
    if queue_jobs:
        parts.append(_format_clone_queued_section(queue_jobs))
    parts.append(_format_bot_stats())
    return "\n\n".join(parts)


def _format_clone_completion_message(state: dict[str, Any]) -> str:
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    source_label = _format_clone_endpoint(payload, "source") or payload.get("source_link", "n/a")
    destination_label = _format_clone_endpoint(payload, "destination") or payload.get("destination_link", "n/a")

    success = _safe_int(state.get("success"))
    failed = _safe_int(state.get("failed"))
    skipped = _safe_int(state.get("skipped"))
    processed = success + failed + skipped
    total_messages = _safe_int(state.get("total_messages"))
    processed_text = str(processed)
    if total_messages > 0:
        processed_text = f"{processed} of {total_messages}"

    started_at = _safe_float(state.get("started_at"))
    elapsed = max(time.time() - started_at, 0.0) if started_at > 0 else 0.0

    return "\n".join(
        [
            "<b>Clone completed</b>",
            "",
            f"┠ <b>Source</b> → <i>{_html(source_label)}</i>",
            f"┠ <b>Destination</b> → <i>{_html(destination_label)}</i>",
            f"┠ <b>Files processed</b> → <i>{_html(processed_text)}</i>",
            f"┠ <b>Forwarded</b> → <i>{_html(success)}</i>",
            f"┠ <b>Failed</b> → <i>{_html(failed)}</i>",
            f"┠ <b>Skipped</b> → <i>{_html(skipped)}</i>",
            f"┖ <b>Time taken</b> → <i>{_html(_readable_time(elapsed))}</i>",
        ]
    )


def _format_export_status(state: dict[str, Any]) -> str:
    phase = str(state.get("phase", "unknown")).title()
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    stage = str(state.get("stage") or "").replace("_", " ").title()
    started_at = _safe_float(state.get("started_at"))
    elapsed = max(time.time() - started_at, 0.0) if started_at > 0 else 0.0

    lines = ["<b>Export Status</b>", f"Phase: {_html(phase)}"]
    if stage:
        lines.append(f"Stage: {_html(stage)}")

    topic_link = payload.get("topic_link")
    if topic_link:
        lines.append(f"Topic: {_html(topic_link)}")

    output = state.get("output") or payload.get("out_path")
    if output:
        lines.append(f"Output: {_html(output)}")

    batch_size = payload.get("batch_size")
    batch_delay = payload.get("batch_delay_sec")
    if batch_size or batch_delay is not None:
        lines.append(f"Batch: {_html(batch_size or '-')} messages, delay {_html(batch_delay if batch_delay is not None else '-')}s")

    total_messages = _safe_int(state.get("total_messages"))
    fetched_messages = _safe_int(state.get("fetched_messages"))
    processed_messages = _safe_int(state.get("processed_messages"))
    if total_messages > 0:
        current = processed_messages if state.get("processed_messages") is not None else fetched_messages
        percent = current / total_messages * 100.0 if current else 0.0
        lines.append(f"Progress: {_html(current)} of {_html(total_messages)} messages ({_format_percent(percent)})")
    elif fetched_messages or processed_messages:
        lines.append(f"Progress: fetched {_html(fetched_messages)}, processed {_html(processed_messages)}")

    found_message_ids = _safe_int(state.get("found_message_ids"))
    if found_message_ids and not total_messages:
        lines.append(f"Messages found: {_html(found_message_ids)}")

    current_message_id = state.get("current_message_id")
    if current_message_id:
        lines.append(f"Current message: {_html(current_message_id)}")

    media_links = _safe_int(state.get("media_links"))
    text_entries = _safe_int(state.get("text_entries"))
    if media_links or text_entries:
        lines.append(f"Entries: {_html(media_links)} media links, {_html(text_entries)} text blocks")

    upload_topic_link = payload.get("upload_topic_link")
    if upload_topic_link:
        lines.append(f"Upload topic: {_html(upload_topic_link)}")

    flood_wait_until = _safe_float(state.get("flood_wait_until"))
    flood_wait_seconds = _safe_float(state.get("flood_wait_seconds"))
    if flood_wait_until > time.time() or (flood_wait_seconds > 0 and not flood_wait_until):
        remaining = max(flood_wait_until - time.time(), 0.0) if flood_wait_until else flood_wait_seconds
        operation = str(state.get("flood_wait_operation") or "telegram")
        lines.append(f"FloodWait: {_html(_readable_time(remaining))} for {_html(operation)}")

    if elapsed > 0:
        lines.append(f"Elapsed: {_html(_readable_time(elapsed))}")

    flags = []
    if payload.get("caption_file_names"):
        flags.append("caption file names")
    if payload.get("onwards"):
        flags.append("onwards")
    if flags:
        lines.append(f"Options: {_html(', '.join(flags))}")

    if state.get("error"):
        lines.append(f"Error: {_html(state.get('error'))}")

    return "\n".join(lines)


def _format_index_status(state: dict[str, Any]) -> str:
    phase = str(state.get("phase", "unknown")).title()
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    stage = str(state.get("stage") or "").replace("_", " ").title()
    started_at = _safe_float(state.get("started_at"))
    elapsed = max(time.time() - started_at, 0.0) if started_at > 0 else 0.0
    lines = ["<b>Index Status</b>", f"Phase: {_html(phase)}"]
    if stage:
        lines.append(f"Stage: {_html(stage)}")

    topic_link = payload.get("topic_link")
    if topic_link:
        lines.append(f"Topic: {_html(topic_link)}")

    batch_size = payload.get("batch_size")
    batch_delay = payload.get("batch_delay_sec")
    if batch_size or batch_delay is not None:
        lines.append(f"Batch: {_html(batch_size or '-')} messages, delay {_html(batch_delay if batch_delay is not None else '-')}s")

    total_messages = _safe_int(state.get("total_messages"))
    fetched_messages = _safe_int(state.get("fetched_messages"))
    processed_messages = _safe_int(state.get("processed_messages"))
    if total_messages > 0:
        current = processed_messages if state.get("processed_messages") is not None else fetched_messages
        percent = current / total_messages * 100.0 if current else 0.0
        lines.append(f"Progress: {_html(current)} of {_html(total_messages)} messages ({_format_percent(percent)})")
    elif fetched_messages or processed_messages:
        lines.append(f"Progress: fetched {_html(fetched_messages)}, processed {_html(processed_messages)}")

    found_message_ids = _safe_int(state.get("found_message_ids"))
    if found_message_ids and not total_messages:
        lines.append(f"Messages found: {_html(found_message_ids)}")

    current_message_id = state.get("current_message_id")
    if current_message_id:
        lines.append(f"Current message: {_html(current_message_id)}")

    text_entries = _safe_int(state.get("text_entries") if state.get("text_entries") is not None else state.get("count"))
    if text_entries:
        lines.append(f"Text links found: {_html(text_entries)}")

    index_messages = _safe_int(state.get("index_messages"))
    if index_messages:
        lines.append(f"Index messages: {_html(index_messages)}")

    send_client = str(state.get("send_client") or "").strip()
    if send_client:
        lines.append(f"Sending with: {_html(send_client.replace('_', ' '))}")

    flood_wait_until = _safe_float(state.get("flood_wait_until"))
    flood_wait_seconds = _safe_float(state.get("flood_wait_seconds"))
    if flood_wait_until > time.time() or (flood_wait_seconds > 0 and not flood_wait_until):
        remaining = max(flood_wait_until - time.time(), 0.0) if flood_wait_until else flood_wait_seconds
        operation = str(state.get("flood_wait_operation") or "telegram")
        lines.append(f"FloodWait: {_html(_readable_time(remaining))} for {_html(operation)}")

    if elapsed > 0:
        lines.append(f"Elapsed: {_html(_readable_time(elapsed))}")

    header = payload.get("header")
    if header:
        lines.append(f"Header: {_html(header)}")

    if payload.get("onwards"):
        lines.append("Options: onwards")

    if state.get("error"):
        lines.append(f"Error: {_html(state.get('error'))}")

    return "\n".join(lines)


def _format_clone_endpoint(payload: dict[str, Any], prefix: str) -> str:
    chat_title = str(payload.get(f"{prefix}_chat_title") or "").strip()
    topic_title = str(payload.get(f"{prefix}_topic_title") or "").strip()
    chat_id = payload.get(f"{prefix}_chat_id")
    topic_id = payload.get(f"{prefix}_topic_id")

    if not chat_title and chat_id:
        chat_title = str(chat_id)
    if not topic_title and topic_id:
        topic_title = f"Topic {topic_id}"
    if chat_title and topic_title:
        return f"{chat_title} / {topic_title}"
    return chat_title or topic_title


async def _save_clone_state(store: MongoStateStore, label: str, state: dict[str, Any]) -> None:
    try:
        await store.save(f"clone:{label}", state)
    except Exception:
        _write_json_file(_snapshot_path(f"clone_{label}"), state)


async def _load_state(store: MongoStateStore, key: str) -> dict[str, Any] | None:
    if key == "clone:last" and ACTIVE_CLONE_LATEST_STATE is not None:
        return dict(ACTIVE_CLONE_LATEST_STATE)
    try:
        return await store.load(key)
    except Exception:
        return _read_json_file(_snapshot_path(key.replace(":", "_")))


def _status_reply_markup(view: str = "main") -> InlineKeyboardMarkup:
    if view == "overview":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Back", callback_data="status:back")],
                [InlineKeyboardButton("Close", callback_data="status:close")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📜 TStats", callback_data="status:tstats"),
                InlineKeyboardButton("♻️ Refresh", callback_data="status:refresh"),
            ],
            [InlineKeyboardButton("Close", callback_data="status:close")],
        ]
    )


def _clone_status_reply_markup(
    view: str = "main",
    queue_jobs: list[dict[str, Any]] | None = None,
) -> InlineKeyboardMarkup:
    q = queue_jobs or []
    cancel_rows: list[list[InlineKeyboardButton]] = []
    for idx, job in enumerate(q, start=1):
        jid = str(job.get("job_id", "") or "")
        token = _clone_job_callback_token(jid)
        if not token:
            continue
        cancel_rows.append(
            [
                InlineKeyboardButton(
                    f"❌ Cancel queued {idx} · {token}",
                    callback_data=f"clone_qc:{token}",
                )
            ]
        )

    if view == "overview":
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("Back", callback_data="clone_status:back")],
        ]
        rows.extend(cancel_rows)
        rows.append([InlineKeyboardButton("Close", callback_data="clone_status:close")])
        return InlineKeyboardMarkup(rows)

    rows = [
        [
            InlineKeyboardButton("📜 TStats", callback_data="clone_status:tstats"),
            InlineKeyboardButton("♻️ Refresh", callback_data="clone_status:refresh"),
        ],
    ]
    rows.extend(cancel_rows)
    rows.append([InlineKeyboardButton("Close", callback_data="clone_status:close")])
    return InlineKeyboardMarkup(rows)


def _job_status_reply_markup(kind: str, phase: str) -> InlineKeyboardMarkup | None:
    if phase.lower() not in {"queued", "running"}:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel", callback_data=f"job_cancel:{kind}")]]
    )


def _format_tasks_overview(clone_state: dict[str, Any] | None, *, queued_clone_count: int = 0) -> str:
    phase = str((clone_state or {}).get("phase", "")).lower()
    transfer_stage = str((clone_state or {}).get("transfer_stage") or "").lower()
    is_running = phase == "running"
    is_queued = phase == "queued"

    download_count = 1 if is_running and transfer_stage == "download" else 0
    upload_count = 1 if is_running and transfer_stage == "upload" else 0
    clone_count = 1 if is_running and transfer_stage not in {"download", "upload"} else 0
    queued_dl_count = 1 if is_queued else 0
    q_clone = max(int(queued_clone_count), 0)

    download_speed = "0B/s"
    upload_speed = "0B/s"
    if is_running and clone_state:
        if transfer_stage == "download":
            download_speed = str(clone_state.get("download_speed") or "0B/s")
        elif transfer_stage == "upload":
            upload_speed = str(clone_state.get("upload_speed") or "0B/s")

    return "\n".join(
        [
            "☷ <b>Tasks Overview :</b>",
            "",
            f"┏ <b>Download</b>: {download_count} | <b>Upload</b>: {upload_count}",
            "┣ <b>Seed</b>: 0 | <b>Archive</b>: 0",
            "┣ <b>Extract</b>: 0 | <b>Split</b>: 0",
            f"┣ <b>QueueDL</b>: {queued_dl_count} | <b>QueueUP</b>: 0 | <b>CloneQ</b>: {q_clone}",
            "┣ <b>Clone</b>: "
            f"{clone_count} | <b>CheckUp</b>: 0",
            "┣ <b>Paused</b>: 0 | <b>SamVideo</b>: 0",
            "┣ <b>Convert</b>: 0 | <b>FFmpeg</b>: 0",
            "┃",
            f"┣ <b>Total Download Speed</b>: {_html(download_speed)}",
            f"┣ <b>Total Upload Speed</b>: {_html(upload_speed)}",
            "┗ <b>Total Seeding Speed</b>: 0B/s",
        ]
    )


def _format_combined_status_text(
    clone_state: dict[str, Any] | None,
    export_state: dict[str, Any] | None,
    index_state: dict[str, Any] | None,
) -> str:
    messages = []
    if clone_state:
        messages.append(_format_clone_status_panel(clone_state))
    else:
        messages.append("No saved clone state.")

    if export_state:
        messages.append(_format_export_status(export_state))

    if index_state:
        messages.append(_format_index_status(index_state))

    messages.append(_format_bot_stats())
    return "\n\n".join(messages)


async def _load_combined_status_text(store: MongoStateStore) -> tuple[str, dict[str, Any] | None]:
    export_state, clone_state, index_state = await asyncio.gather(
        _load_state(store, "export:last"),
        _load_state(store, "clone:last"),
        _load_state(store, "index:last"),
    )
    return _format_combined_status_text(clone_state, export_state, index_state), clone_state


async def _load_status_view_text(store: MongoStateStore, view: str) -> tuple[str, dict[str, Any] | None]:
    export_state, clone_state, index_state = await asyncio.gather(
        _load_state(store, "export:last"),
        _load_state(store, "clone:last"),
        _load_state(store, "index:last"),
    )
    if view == "overview":
        return _format_tasks_overview(clone_state), clone_state
    return _format_combined_status_text(clone_state, export_state, index_state), clone_state


async def _load_clone_status_view_text(
    store: MongoStateStore, view: str
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    clone_state = await _load_state(store, "clone:last")
    async with _clone_queue_cv:
        queue_snap = list(_clone_pending_jobs)
    if view == "overview":
        return (
            _format_tasks_overview(clone_state, queued_clone_count=len(queue_snap)),
            clone_state,
            queue_snap,
        )
    text = _format_clone_status_display(clone_state, queue_snap)
    return text, clone_state, queue_snap


async def _run_export_job(message, payload: dict[str, Any], store: MongoStateStore) -> None:
    global ACTIVE_EXPORT_TASK

    ACTIVE_EXPORT_TASK = asyncio.current_task()
    bot_settings = await _load_bot_settings(store)
    update_interval_sec = max(
        float(bot_settings["status_command_update_interval_sec"]),
        MIN_WATCHED_STATUS_INTERVAL_SEC,
    )
    job_started_at = time.time()
    latest_state: dict[str, Any] = {
        "phase": "queued",
        "stage": "queued",
        "payload": payload,
        "started_at": job_started_at,
    }
    last_edit_at = 0.0
    last_persist_at = 0.0
    last_stage = ""
    last_progress = -1

    await store.save("export:last", latest_state)
    status = await _reply_status_message(
        message,
        _format_export_status(latest_state),
        reply_markup=_job_status_reply_markup("export", "queued"),
    )

    async def _save_export_status(update: dict[str, Any]) -> None:
        nonlocal latest_state, last_edit_at, last_persist_at, last_stage, last_progress
        now = time.time()
        wrapper = dict(latest_state)
        wrapper.update(update)
        wrapper["payload"] = payload
        wrapper["started_at"] = wrapper.get("started_at") or job_started_at
        latest_state = wrapper

        stage = str(wrapper.get("stage") or "")
        progress = (
            _safe_int(wrapper.get("processed_messages"))
            if wrapper.get("processed_messages") is not None
            else _safe_int(wrapper.get("fetched_messages"))
        )
        if not progress:
            progress = _safe_int(wrapper.get("found_message_ids"))
        phase = str(wrapper.get("phase") or "").lower()
        has_active_flood_wait = _safe_float(wrapper.get("flood_wait_until")) > now
        stage_changed = stage != last_stage
        progress_changed = progress != last_progress
        should_update_now = (
            phase != "running"
            or stage_changed
            or has_active_flood_wait
            or (progress_changed and now - last_edit_at >= update_interval_sec)
        )

        if should_update_now:
            await _edit_status_message(
                status,
                _format_export_status(wrapper),
                reply_markup=_job_status_reply_markup("export", phase),
            )
            last_edit_at = now
            last_stage = stage
            last_progress = progress

        if should_update_now or now - last_persist_at >= update_interval_sec:
            await store.save("export:last", wrapper)
            last_persist_at = now

    try:
        output = await run_export(
            topic_link=payload["topic_link"],
            config_path=payload["config_path"],
            out_path=payload["out_path"],
            batch_size=int(payload["batch_size"]),
            batch_delay_sec=float(payload["batch_delay_sec"]),
            upload_topic_link=payload["upload_topic_link"],
            caption_file_names=bool(payload["caption_file_names"]),
            onwards=bool(payload["onwards"]),
            status_callback=_save_export_status,
        )
    except asyncio.CancelledError:
        cancelled_state = dict(latest_state)
        cancelled_state.update(
            {
                "phase": "cancelled",
                "stage": "cancelled",
                "payload": payload,
                "error": "Cancelled by user",
            }
        )
        await store.save("export:last", cancelled_state)
        await _edit_status_message(
            status,
            _format_export_status(cancelled_state),
            reply_markup=_job_status_reply_markup("export", "cancelled"),
        )
        return
    except Exception as exc:
        failed_state = dict(latest_state)
        failed_state.update({"phase": "failed", "stage": "failed", "payload": payload, "error": str(exc)})
        await store.save("export:last", failed_state)
        await _edit_status_message(
            status,
            _format_export_status(failed_state),
            reply_markup=_job_status_reply_markup("export", "failed"),
        )
        raise
    else:
        completed_state = dict(latest_state)
        completed_state.update(
            {
                "phase": "completed",
                "stage": "completed",
                "payload": payload,
                "output": str(output),
            }
        )
        await store.save("export:last", completed_state)
        await _edit_status_message(
            status,
            _format_export_status(completed_state),
            reply_markup=_job_status_reply_markup("export", "completed"),
        )
    finally:
        if ACTIVE_EXPORT_TASK is asyncio.current_task():
            ACTIVE_EXPORT_TASK = None


async def _run_index_job(message, payload: dict[str, Any], store: MongoStateStore, bot: Client) -> None:
    global ACTIVE_INDEX_TASK

    ACTIVE_INDEX_TASK = asyncio.current_task()
    bot_settings = await _load_bot_settings(store)
    update_interval_sec = max(
        float(bot_settings["status_command_update_interval_sec"]),
        MIN_WATCHED_STATUS_INTERVAL_SEC,
    )
    job_started_at = time.time()
    latest_state: dict[str, Any] = {
        "phase": "queued",
        "stage": "queued",
        "payload": payload,
        "started_at": job_started_at,
    }
    last_edit_at = 0.0
    last_persist_at = 0.0
    last_stage = ""
    last_progress = -1
    last_text_entries = -1

    await store.save("index:last", latest_state)
    status = await _reply_status_message(
        message,
        _format_index_status(latest_state),
        reply_markup=_job_status_reply_markup("index", "queued"),
    )

    async def _save_index_status(update: dict[str, Any]) -> None:
        nonlocal latest_state, last_edit_at, last_persist_at, last_stage, last_progress, last_text_entries
        now = time.time()
        wrapper = dict(latest_state)
        wrapper.update(update)
        wrapper["payload"] = payload
        wrapper["started_at"] = wrapper.get("started_at") or job_started_at
        latest_state = wrapper

        stage = str(wrapper.get("stage") or "")
        progress = (
            _safe_int(wrapper.get("processed_messages"))
            if wrapper.get("processed_messages") is not None
            else _safe_int(wrapper.get("fetched_messages"))
        )
        text_entries = _safe_int(wrapper.get("text_entries"))
        phase = str(wrapper.get("phase") or "").lower()
        has_active_flood_wait = _safe_float(wrapper.get("flood_wait_until")) > now
        stage_changed = stage != last_stage
        progress_changed = progress != last_progress
        text_entries_changed = text_entries != last_text_entries
        should_update_now = (
            phase != "running"
            or stage_changed
            or has_active_flood_wait
            or (progress_changed and now - last_edit_at >= update_interval_sec)
            or (text_entries_changed and now - last_edit_at >= update_interval_sec)
        )

        if should_update_now:
            await _edit_status_message(
                status,
                _format_index_status(wrapper),
                reply_markup=_job_status_reply_markup("index", phase),
            )
            last_edit_at = now
            last_stage = stage
            last_progress = progress
            last_text_entries = text_entries

        if should_update_now or now - last_persist_at >= update_interval_sec:
            await store.save("index:last", wrapper)
            last_persist_at = now

    try:
        count = await run_index(
            topic_link=payload["topic_link"],
            config_path=payload["config_path"],
            batch_size=int(payload["batch_size"]),
            batch_delay_sec=float(payload["batch_delay_sec"]),
            onwards=bool(payload["onwards"]),
            bot=bot,
            header=str(payload.get("header") or "INDEX 👆"),
            status_callback=_save_index_status,
        )
    except asyncio.CancelledError:
        cancelled_state = dict(latest_state)
        cancelled_state.update(
            {
                "phase": "cancelled",
                "stage": "cancelled",
                "payload": payload,
                "error": "Cancelled by user",
            }
        )
        await store.save("index:last", cancelled_state)
        await _edit_status_message(
            status,
            _format_index_status(cancelled_state),
            reply_markup=_job_status_reply_markup("index", "cancelled"),
        )
        return
    except Exception as exc:
        failed_state = dict(latest_state)
        failed_state.update({"phase": "failed", "stage": "failed", "payload": payload, "error": str(exc)})
        await store.save("index:last", failed_state)
        await _edit_status_message(
            status,
            _format_index_status(failed_state),
            reply_markup=_job_status_reply_markup("index", "failed"),
        )
        raise
    else:
        completed_state = dict(latest_state)
        completed_state.update(
            {
                "phase": "completed",
                "stage": "completed",
                "payload": payload,
                "count": count,
                "text_entries": count,
            }
        )
        await store.save("index:last", completed_state)
        await _edit_status_message(
            status,
            _format_index_status(completed_state),
            reply_markup=_job_status_reply_markup("index", "completed"),
        )
    finally:
        if ACTIVE_INDEX_TASK is asyncio.current_task():
            ACTIVE_INDEX_TASK = None


async def _run_clone_job(
    *,
    bot: Client,
    chat_id: int,
    reply_to_message_id: int | None,
    payload: dict[str, Any],
    store: MongoStateStore,
) -> None:
    global ACTIVE_CLONE_TASK, ACTIVE_CLONE_CANCEL_EVENT, ACTIVE_CLONE_LATEST_STATE

    ACTIVE_CLONE_CANCEL_EVENT = asyncio.Event()
    ACTIVE_CLONE_TASK = asyncio.current_task()
    bot_settings = await _load_bot_settings(store)
    job_started_at = time.time()

    last_status_edit_at = 0.0
    last_reported_success = -1
    last_reported_index = -1
    last_reported_flood_wait_until = 0.0
    last_persisted_at = 0.0
    last_persisted_success = -1
    last_persisted_failed = -1
    last_persisted_skipped = -1
    last_persisted_flood_wait_until = 0.0
    latest_state_payload = dict(payload)
    latest_runtime_state: dict[str, Any] = {"phase": "queued", "payload": dict(payload)}
    status_update_lock = asyncio.Lock()
    state_persist_lock = asyncio.Lock()

    async def _status_view_for_state(state: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
        async with _clone_queue_cv:
            q = list(_clone_pending_jobs)
        return _format_clone_status_display(state, q), _clone_status_reply_markup("main", q)

    async def _save_state_inner(state: dict[str, Any]) -> None:
        global ACTIVE_CLONE_LATEST_STATE
        nonlocal last_status_edit_at, last_reported_success, last_reported_index, last_reported_flood_wait_until
        nonlocal last_persisted_at, last_persisted_success, last_persisted_failed, last_persisted_skipped, last_persisted_flood_wait_until
        nonlocal latest_state_payload, latest_runtime_state
        wrapper = dict(state)
        if isinstance(state.get("payload"), dict):
            wrapper["payload"] = state["payload"]
        else:
            enriched_payload = dict(payload)
            for key in (
                "source_chat_id",
                "source_chat_title",
                "source_topic_id",
                "source_topic_title",
                "destination_chat_id",
                "destination_chat_title",
                "destination_topic_id",
                "destination_topic_title",
            ):
                if key in state:
                    enriched_payload[key] = state[key]
            wrapper["payload"] = enriched_payload
        if str(wrapper.get("phase", "")).lower() == "running":
            wrapper["started_at"] = latest_runtime_state.get("started_at") or job_started_at
        latest_state_payload = dict(wrapper["payload"])
        latest_runtime_state = dict(wrapper)
        ACTIVE_CLONE_LATEST_STATE = dict(wrapper)

        now = time.time()
        phase = str(wrapper.get("phase", "running"))
        success = int(wrapper.get("success", 0) or 0)
        failed = int(wrapper.get("failed", 0) or 0)
        skipped = int(wrapper.get("skipped", 0) or 0)
        current_index = int(wrapper.get("current_index", 0) or 0)
        flood_wait_until = _safe_float(wrapper.get("flood_wait_until"))

        terminal_checkpoint = phase == "running" and any(
            wrapper.get(key) is not None
            for key in (
                "last_successful_source_message_id",
                "skipped_reason",
                "error",
            )
        )

        success_changed = success != last_reported_success
        progress_changed = current_index != last_reported_index
        flood_wait_changed = flood_wait_until > 0 and flood_wait_until != last_reported_flood_wait_until
        success_interval = max(
            float(bot_settings["clone_status_success_update_interval_sec"]),
            MIN_CLONE_AUTO_EDIT_INTERVAL_SEC,
        )
        progress_interval = max(
            float(bot_settings["clone_status_update_interval_sec"]),
            MIN_CLONE_AUTO_EDIT_INTERVAL_SEC,
        )
        keepalive_interval = max(
            float(bot_settings["clone_status_keepalive_interval_sec"]),
            MIN_CLONE_KEEPALIVE_EDIT_INTERVAL_SEC,
        )

        should_edit = (
            phase != "running"
            or flood_wait_changed
            or (success_changed and now - last_status_edit_at >= success_interval)
            or (progress_changed and now - last_status_edit_at >= progress_interval)
            or now - last_status_edit_at >= keepalive_interval
        )
        if should_edit:
            async with status_update_lock:
                body_text, markup = await _status_view_for_state(wrapper)
                edited = await _edit_status_message(
                    status,
                    body_text,
                    reply_markup=markup,
                    sleep_on_flood=False,
                )
                if edited:
                    last_status_edit_at = now
                    last_reported_success = success
                    last_reported_index = current_index
                    last_reported_flood_wait_until = flood_wait_until

        should_persist = (
            terminal_checkpoint
            or phase != "running"
            or success != last_persisted_success
            or failed != last_persisted_failed
            or skipped != last_persisted_skipped
            or (flood_wait_until > 0 and flood_wait_until != last_persisted_flood_wait_until)
            or now - last_persisted_at >= keepalive_interval
        )
        if should_persist:
            async with state_persist_lock:
                await _save_clone_state(store, "last", wrapper)
                last_persisted_at = now
                last_persisted_success = success
                last_persisted_failed = failed
                last_persisted_skipped = skipped
                last_persisted_flood_wait_until = flood_wait_until

    queued_state = {"phase": "queued", "payload": payload, "started_at": job_started_at}
    ACTIVE_CLONE_LATEST_STATE = dict(queued_state)
    await _save_clone_state(store, "last", queued_state)
    last_persisted_at = time.time()
    last_persisted_success = 0
    last_persisted_failed = 0
    last_persisted_skipped = 0
    queued_body, queued_markup = await _status_view_for_state(queued_state)
    status = await _send_clone_status_message(
        bot,
        chat_id,
        reply_to_message_id,
        queued_body,
        reply_markup=queued_markup,
    )
    try:
        success, failed = await run_clone(
            source_link=payload["source_link"],
            destination_link=payload["destination_link"],
            config_path=payload["config_path"],
            start_id=int(payload["start_id"]),
            limit=int(payload["limit"]),
            delay_sec=float(payload["delay_sec"]),
            batch_size=int(payload["batch_size"]),
            message_ids=payload["message_ids"],
            dry_run=bool(payload["dry_run"]),
            continue_on_error=bool(payload["continue_on_error"]),
            hide_sender_name=bool(payload["hide_sender_name"]),
            filename_prefix=str(payload.get("filename_prefix", "") or ""),
            filename_suffix=str(payload.get("filename_suffix", "") or ""),
            text_prefix=str(payload.get("text_prefix", "") or ""),
            text_suffix=str(payload.get("text_suffix", "") or ""),
            status_callback=_save_state_inner,
            cancel_event=ACTIVE_CLONE_CANCEL_EVENT,
        )
    except asyncio.CancelledError:
        cancelled_state = {
            "phase": "cancelled",
            "payload": latest_state_payload,
            "error": "Cancelled by user",
            "started_at": latest_runtime_state.get("started_at", job_started_at),
            "current_index": latest_runtime_state.get("current_index", 0),
            "total_messages": latest_runtime_state.get("total_messages", 0),
            "success": latest_runtime_state.get("success", 0),
            "failed": latest_runtime_state.get("failed", 0),
            "skipped": latest_runtime_state.get("skipped", 0),
            "current_message_id": latest_runtime_state.get("current_message_id"),
        }
        if latest_runtime_state.get("last_successful_message_link"):
            cancelled_state["last_successful_message_link"] = latest_runtime_state["last_successful_message_link"]
        if latest_runtime_state.get("resume_after_source_message_id"):
            cancelled_state["resume_after_source_message_id"] = latest_runtime_state["resume_after_source_message_id"]
        for key in (
            "last_processed_source_message_id",
            "current_message_type",
            "current_file_name",
            "skipped_reason",
            "flood_wait_operation",
            "flood_wait_seconds",
            "flood_wait_until",
        ):
            if latest_runtime_state.get(key):
                cancelled_state[key] = latest_runtime_state[key]
        await _save_clone_state(
            store,
            "last",
            cancelled_state,
        )
        ACTIVE_CLONE_LATEST_STATE = dict(cancelled_state)
        cx_body, cx_markup = await _status_view_for_state(cancelled_state)
        await _edit_status_message(
            status,
            cx_body,
            reply_markup=cx_markup,
        )
        raise
    except Exception as exc:
        failed_state = {
            "phase": "failed",
            "payload": latest_state_payload,
            "error": str(exc),
            "current_index": latest_runtime_state.get("current_index", 0),
            "total_messages": latest_runtime_state.get("total_messages", 0),
            "success": latest_runtime_state.get("success", 0),
            "failed": latest_runtime_state.get("failed", 0),
            "skipped": latest_runtime_state.get("skipped", 0),
            "current_message_id": latest_runtime_state.get("current_message_id"),
            "started_at": latest_runtime_state.get("started_at", job_started_at),
        }
        for key in (
            "last_successful_source_message_id",
            "last_processed_source_message_id",
            "last_successful_destination_message_id",
            "last_successful_message_link",
            "resume_after_source_message_id",
            "current_message_type",
            "current_file_name",
            "skipped_reason",
            "flood_wait_operation",
            "flood_wait_seconds",
            "flood_wait_until",
        ):
            if latest_runtime_state.get(key):
                failed_state[key] = latest_runtime_state[key]
        await _save_clone_state(
            store,
            "last",
            failed_state,
        )
        ACTIVE_CLONE_LATEST_STATE = dict(failed_state)
        fx_body, fx_markup = await _status_view_for_state(failed_state)
        await _edit_status_message(
            status,
            fx_body,
            reply_markup=fx_markup,
        )
        raise
    else:
        result = {
            "phase": "completed",
            "payload": latest_state_payload,
            "success": success,
            "failed": failed,
            "skipped": latest_runtime_state.get("skipped", 0),
            "started_at": latest_runtime_state.get("started_at", job_started_at),
            "current_index": latest_runtime_state.get("current_index", 0),
            "total_messages": latest_runtime_state.get("total_messages", 0),
            "current_message_id": latest_runtime_state.get("current_message_id"),
        }
        for key in ("current_message_type", "current_file_name"):
            if latest_runtime_state.get(key):
                result[key] = latest_runtime_state[key]
        if latest_runtime_state.get("last_processed_source_message_id"):
            result["last_processed_source_message_id"] = latest_runtime_state["last_processed_source_message_id"]
        if latest_runtime_state.get("resume_after_source_message_id"):
            result["resume_after_source_message_id"] = latest_runtime_state["resume_after_source_message_id"]
        await _save_clone_state(store, "last", result)
        ACTIVE_CLONE_LATEST_STATE = dict(result)
        latest_runtime_state = dict(result)
        rx_body, rx_markup = await _status_view_for_state(result)
        await _edit_status_message(
            status,
            rx_body,
            reply_markup=rx_markup,
        )
        try:
            completion_kw: dict[str, Any] = {
                "chat_id": chat_id,
                "text": _format_clone_completion_message(result),
                "parse_mode": enums.ParseMode.HTML,
                "disable_web_page_preview": True,
            }
            if reply_to_message_id:
                completion_kw["reply_to_message_id"] = reply_to_message_id
            await bot.send_message(**completion_kw)
        except Exception:
            logging.getLogger("heroku_bot").debug("clone completion notification failed", exc_info=True)
    finally:
        ACTIVE_CLONE_TASK = None
        ACTIVE_CLONE_CANCEL_EVENT = None


async def _run_auto_resume_clone_if_needed(bot: Client, store: MongoStateStore, admin_ids: set[int]) -> None:
    if ACTIVE_CLONE_TASK is not None and not ACTIVE_CLONE_TASK.done():
        return

    bot_settings = await _load_bot_settings(store)
    if not bool(bot_settings.get("clone_auto_resume_enabled", True)):
        return

    state = await _load_state(store, "clone:last")
    if not isinstance(state, dict):
        return

    payload = _resume_clone_payload_from_state(state)
    if payload is None:
        return

    requested_by_id = _safe_int(payload.get("requested_by_id"))
    target_chat_id = requested_by_id if requested_by_id in admin_ids else sorted(admin_ids)[0]
    try:
        await bot.send_message(
            target_chat_id,
            "Heroku restart detected an unfinished clone. Auto-resuming from the last saved checkpoint.",
        )
        await _run_clone_job(
            bot=bot,
            chat_id=target_chat_id,
            reply_to_message_id=None,
            payload=payload,
            store=store,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger("heroku_bot").exception("auto resume clone failed")


async def _clone_queue_worker(bot: Client, store: MongoStateStore, admin_ids: set[int]) -> None:
    global CLONE_QUEUE_WORKER_TASK
    CLONE_QUEUE_WORKER_TASK = asyncio.current_task()
    try:
        await _run_auto_resume_clone_if_needed(bot, store, admin_ids)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger("heroku_bot").exception("clone queue auto-resume failed")

    while True:
        async with _clone_queue_cv:
            while not _clone_pending_jobs:
                await _clone_queue_cv.wait()
            job = _clone_pending_jobs.pop(0)
            await _save_clone_queue_snapshot(store, list(_clone_pending_jobs))

        chat_id = _safe_int(job.get("requested_chat_id"))
        notice_mid = _safe_int(job.get("notice_message_id"))
        payload = job.get("payload")
        if chat_id <= 0 or not isinstance(payload, dict):
            continue

        if notice_mid > 0:
            try:
                await bot.edit_message_text(
                    chat_id,
                    notice_mid,
                    "<i>Starting clone…</i>",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass

        try:
            await _run_clone_job(
                bot=bot,
                chat_id=chat_id,
                reply_to_message_id=_safe_int(job.get("requested_message_id")) or None,
                payload=payload,
                store=store,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("heroku_bot").exception("clone queue job failed")


async def run_bot() -> None:
    _setup_logging()
    _apply_bootstrap_settings()

    bot_token = os.getenv("HEROKU_BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("Missing HEROKU_BOT_TOKEN")

    admin_ids = _parse_admin_ids(os.getenv("BOT_ADMIN_USER_IDS", ""))
    if not admin_ids:
        raise RuntimeError("Missing BOT_ADMIN_USER_IDS")

    api_id = int(os.getenv("TG_API_ID", "0") or 0)
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if api_id == 0 or not api_hash:
        raise RuntimeError("Missing TG_API_ID or TG_API_HASH")

    store = MongoStateStore(
        MONGODB_DATA_API_URL,
        MONGODB_DATA_API_KEY,
        MONGODB_DATA_SOURCE,
        os.getenv("MONGODB_DATABASE", MONGODB_DATABASE).strip(),
        MONGODB_COLLECTION,
        MONGODB_URI,
    )

    bot = Client(
        name="heroku-topic-ops-bot",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    async def _authorized(message) -> bool:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        return isinstance(user_id, int) and user_id in admin_ids

    def _authorized_user_id(user_id: Any) -> bool:
        return isinstance(user_id, int) and user_id in admin_ids

    async def _send_restart_notification() -> None:
        payload = _read_json_file(RESTART_MESSAGE_FILE)
        if not payload:
            return

        chat_id = _safe_int(payload.get("chat_id"))
        message_id = _safe_int(payload.get("message_id"))
        text = _restart_success_text()
        try:
            if chat_id and message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            elif chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception:
            logging.getLogger("heroku_bot").exception("restart notification failed")
        finally:
            RESTART_MESSAGE_FILE.unlink(missing_ok=True)

    async def _watch_status_message(
        chat_id: int,
        message_id: int,
        interval_sec: float,
        last_text: str,
        *,
        status_kind: str = "status",
    ) -> None:
        key = (chat_id, message_id)
        try:
            while True:
                await asyncio.sleep(max(interval_sec, MIN_WATCHED_STATUS_INTERVAL_SEC))
                view = ACTIVE_STATUS_VIEWS.get(key, "main")
                if status_kind == "clone_status":
                    text, clone_state, queue_snap = await _load_clone_status_view_text(store, view)
                    reply_markup = _clone_status_reply_markup(view, queue_snap)
                else:
                    text, clone_state = await _load_status_view_text(store, view)
                    reply_markup = _status_reply_markup(view)
                last_text = ACTIVE_STATUS_LAST_TEXTS.get(key, last_text)
                if text != last_text:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=text,
                            parse_mode=enums.ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=reply_markup,
                        )
                        last_text = text
                        ACTIVE_STATUS_LAST_TEXTS[key] = text
                    except FloodWait:
                        continue
                    except RPCError as exc:
                        if _is_invalid_status_message_error(exc):
                            break
                        logging.getLogger("heroku_bot").debug("watched status edit failed", exc_info=True)
                        continue
                    except Exception:
                        logging.getLogger("heroku_bot").debug("watched status edit failed", exc_info=True)
                        continue

                phase = str((clone_state or {}).get("phase", "")).lower()
                if phase and phase != "running":
                    break
        finally:
            current_task = asyncio.current_task()
            if ACTIVE_STATUS_WATCH_TASKS.get(key) is current_task:
                ACTIVE_STATUS_WATCH_TASKS.pop(key, None)
                ACTIVE_STATUS_VIEWS.pop(key, None)
                ACTIVE_STATUS_LAST_TEXTS.pop(key, None)

    def _cancel_status_watcher(key: tuple[int, int]) -> None:
        task = ACTIVE_STATUS_WATCH_TASKS.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _ensure_status_watcher(status_message, last_text: str, status_kind: str) -> None:
        key = (status_message.chat.id, status_message.id)
        if key in ACTIVE_STATUS_WATCH_TASKS:
            return

        view = ACTIVE_STATUS_VIEWS.get(key, "main")
        if status_kind == "clone_status":
            _, clone_state, _ = await _load_clone_status_view_text(store, view)
        else:
            _, clone_state = await _load_status_view_text(store, view)

        phase = str((clone_state or {}).get("phase", "")).lower()
        if phase != "running":
            return

        bot_settings = await _load_bot_settings(store)
        interval_sec = max(
            float(bot_settings["status_command_update_interval_sec"]),
            MIN_WATCHED_STATUS_INTERVAL_SEC,
        )
        ACTIVE_STATUS_WATCH_TASKS[key] = asyncio.create_task(
            _watch_status_message(
                status_message.chat.id,
                status_message.id,
                interval_sec,
                last_text,
                status_kind=status_kind,
            )
        )

    @bot.on_message(filters.private & filters.command("start", prefixes="/"))
    async def start_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        me = await client.get_me()
        await message.reply_text(
            f"{_bundle_help_text()}\n\n"
            "<b>BotFather Setup</b>\n"
            "Use the buttons below for BotFather steps and command file.",
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_start_help_markup(getattr(me, "username", "") or ""),
        )

    @bot.on_message(filters.private & filters.command("help", prefixes="/"))
    async def help_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        me = await client.get_me()
        await message.reply_text(
            f"{_bundle_help_text()}\n\n"
            "<b>BotFather Setup</b>\n"
            "Use the buttons below for BotFather steps and command file.",
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_start_help_markup(getattr(me, "username", "") or ""),
        )

    @bot.on_callback_query(filters.regex(r"^botfather:add$"))
    async def botfather_add_handler(client, callback_query) -> None:
        message = callback_query.message
        user_id = getattr(getattr(callback_query, "from_user", None), "id", None)
        if message is None:
            await callback_query.answer("Message not available.", show_alert=True)
            return
        if not _authorized_user_id(user_id):
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        me = await client.get_me()
        await callback_query.answer("Sending BotFather steps...")
        await message.reply_text(
            _botfather_add_steps(getattr(me, "username", "") or ""),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @bot.on_callback_query(filters.regex(r"^botfather:commands$"))
    async def botfather_commands_handler(client, callback_query) -> None:
        message = callback_query.message
        user_id = getattr(getattr(callback_query, "from_user", None), "id", None)
        if message is None:
            await callback_query.answer("Message not available.", show_alert=True)
            return
        if not _authorized_user_id(user_id):
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        _ensure_runtime_dirs()
        commands_file = EXPORTS_DIR / "botfather_commands.txt"
        commands_file.write_text(_botfather_commands_text() + "\n", encoding="utf-8")

        await callback_query.answer("Sending BotFather commands file...")
        await message.reply_document(
            document=str(commands_file),
            caption="BotFather commands txt file",
        )

    @bot.on_message(filters.private & filters.command("status", prefixes="/"))
    async def status_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        text, clone_state = await _load_status_view_text(store, "main")
        sent = await message.reply_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_status_reply_markup("main"),
        )

        phase = str((clone_state or {}).get("phase", "")).lower()
        key = (sent.chat.id, sent.id)
        ACTIVE_STATUS_VIEWS[key] = "main"
        ACTIVE_STATUS_LAST_TEXTS[key] = text
        if phase == "running":
            bot_settings = await _load_bot_settings(store)
            interval_sec = max(
                float(bot_settings["status_command_update_interval_sec"]),
                MIN_WATCHED_STATUS_INTERVAL_SEC,
            )
            _cancel_status_watcher(key)
            ACTIVE_STATUS_WATCH_TASKS[key] = asyncio.create_task(
                _watch_status_message(sent.chat.id, sent.id, interval_sec, text, status_kind="status")
            )

    @bot.on_callback_query(filters.regex(r"^status:(close|tstats|back|refresh)$"))
    async def status_callback_handler(client, callback_query) -> None:
        user = getattr(callback_query, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in admin_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        status_message = getattr(callback_query, "message", None)
        if status_message is None:
            await callback_query.answer()
            return

        key = (status_message.chat.id, status_message.id)
        action = str(getattr(callback_query, "data", "") or "").split(":", 1)[-1]

        if action == "close":
            _cancel_status_watcher(key)
            ACTIVE_STATUS_VIEWS.pop(key, None)
            ACTIVE_STATUS_LAST_TEXTS.pop(key, None)
            await callback_query.answer("Closed.")
            try:
                await status_message.delete()
            except Exception:
                try:
                    await status_message.edit_text("Closed.")
                except Exception:
                    pass
            return

        if action == "tstats":
            view = "overview"
        elif action == "back":
            view = "main"
        else:
            view = ACTIVE_STATUS_VIEWS.get(key, "main")

        await callback_query.answer()
        ACTIVE_STATUS_VIEWS[key] = view
        text, _ = await _load_status_view_text(store, view)
        ACTIVE_STATUS_LAST_TEXTS[key] = text
        try:
            await _edit_status_message(
                status_message,
                text,
                reply_markup=_status_reply_markup(view),
                sleep_on_flood=False,
            )
        except Exception:
            pass
        await _ensure_status_watcher(status_message, text, "status")

    @bot.on_callback_query(filters.regex(r"^clone_status:(close|tstats|back|refresh)$"))
    async def clone_status_callback_handler(client, callback_query) -> None:
        user = getattr(callback_query, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in admin_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        status_message = getattr(callback_query, "message", None)
        if status_message is None:
            await callback_query.answer()
            return

        action = str(getattr(callback_query, "data", "") or "").split(":", 1)[-1]
        key = (status_message.chat.id, status_message.id)
        if action == "close":
            _cancel_status_watcher(key)
            ACTIVE_STATUS_VIEWS.pop(key, None)
            ACTIVE_STATUS_LAST_TEXTS.pop(key, None)
            await callback_query.answer("Closed.")
            try:
                await status_message.delete()
            except Exception:
                try:
                    await status_message.edit_text("Closed.")
                except Exception:
                    pass
            return

        if action == "tstats":
            view = "overview"
        elif action == "back":
            view = "main"
        else:
            view = ACTIVE_STATUS_VIEWS.get(key, "main")

        await callback_query.answer()
        ACTIVE_STATUS_VIEWS[key] = view
        text, _, queue_snap = await _load_clone_status_view_text(store, view)
        ACTIVE_STATUS_LAST_TEXTS[key] = text
        try:
            await _edit_status_message(
                status_message,
                text,
                reply_markup=_clone_status_reply_markup(view, queue_snap),
                sleep_on_flood=False,
            )
        except Exception:
            pass
        await _ensure_status_watcher(status_message, text, "clone_status")

    @bot.on_callback_query(filters.regex(r"^clone_qc:[0-9a-fA-F]+$"))
    async def clone_queue_cancel_callback_handler(client, callback_query) -> None:
        user = getattr(callback_query, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in admin_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        data = str(getattr(callback_query, "data", "") or "")
        token = data.split(":", 1)[-1].strip()
        removed = await _cancel_queued_clone_job_by_token(store, token)
        if removed is None:
            await callback_query.answer("Job not found or already started.", show_alert=True)
            return

        notice_mid = _safe_int(removed.get("notice_message_id"))
        ch = _safe_int(removed.get("requested_chat_id"))
        if ch and notice_mid:
            try:
                await client.edit_message_text(
                    ch,
                    notice_mid,
                    "<i>This queued clone job was cancelled.</i>",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass

        status_message = getattr(callback_query, "message", None)
        if status_message is not None:
            key = (status_message.chat.id, status_message.id)
            view = ACTIVE_STATUS_VIEWS.get(key, "main")
            text, _, queue_snap = await _load_clone_status_view_text(store, view)
            ACTIVE_STATUS_LAST_TEXTS[key] = text
            try:
                await _edit_status_message(
                    status_message,
                    text,
                    reply_markup=_clone_status_reply_markup(view, queue_snap),
                    sleep_on_flood=False,
                )
            except Exception:
                pass

        await callback_query.answer("Removed from queue.")

    async def _finish_login_flow(user_id: int, login_client: Client, current_settings: dict[str, Any], message) -> None:
        session_string = await login_client.export_session_string()
        current_settings["tg_session_string"] = _normalize_setting_value("tg_session_string", session_string)
        await _save_bot_settings(store, current_settings)

        saved_to_user = True
        try:
            await login_client.send_message(
                "me",
                f"#WZMLX #PYROGRAM_SESSION_2.0.106\n\n<code>{session_string}</code>",
            )
        except Exception:
            saved_to_user = False

        await _close_login_flow(user_id)
        if saved_to_user:
            await message.reply_text(
                "Login complete. The session string was saved to bot settings and sent to Saved Messages.",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await message.reply_text(
                "Login complete. The session string was saved to bot settings, but I could not send it to Saved Messages.",
                parse_mode=enums.ParseMode.HTML,
            )

    @bot.on_message(filters.private & filters.command("login", prefixes="/"))
    async def login_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int):
            await message.reply_text("Could not identify the requesting user.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""
        tokens = shlex.split(command_text) if command_text else []

        if tokens and tokens[0].lower() in {"cancel", "stop"}:
            await _close_login_flow(user_id)
            await message.reply_text("Login cancelled.")
            return

        if tokens and tokens[0].lower() in {"help", "-h", "--help"}:
            await message.reply_text(_login_help_text(), parse_mode=enums.ParseMode.HTML)
            return

        await _close_login_flow(user_id)
        current_settings = await _load_bot_settings(store)

        force_login = bool(tokens and tokens[0].lower() == "force")
        if force_login:
            tokens = tokens[1:]

        if _configured_session_string(current_settings) and not force_login:
            await message.reply_text(
                "A Telegram user session string is already configured, so login is not needed.\n\n"
                "To replace it, use <code>/login force</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if len(tokens) >= 2:
            try:
                current_settings["tg_api_id"] = _normalize_setting_value("tg_api_id", tokens[0])
                current_settings["tg_api_hash"] = _normalize_setting_value("tg_api_hash", tokens[1])
                await _save_bot_settings(store, current_settings)
            except Exception as exc:
                await message.reply_text(f"Could not use API credentials: {exc}")
                return

        try:
            api_id = int(str(current_settings.get("tg_api_id") or os.getenv("TG_API_ID", "0")).strip() or "0")
        except ValueError:
            api_id = 0
        api_hash = str(current_settings.get("tg_api_hash") or os.getenv("TG_API_HASH", "")).strip()

        if api_id <= 0 or not api_hash:
            await message.reply_text(
                "Set API credentials first:\n"
                "<code>/settings set tg_api_id &lt;api_id&gt;</code>\n"
                "<code>/settings set tg_api_hash &lt;api_hash&gt;</code>\n\n"
                "Or start with:\n"
                "<code>/login &lt;api_id&gt; &lt;api_hash&gt;</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        login_client = Client(
            name=f"session-login-{user_id}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True,
        )
        try:
            await login_client.connect()
        except Exception as exc:
            await message.reply_text(f"Could not start Telegram login: {exc}")
            return

        ACTIVE_LOGIN_FLOWS[user_id] = {
            "step": "phone",
            "client": login_client,
            "settings": current_settings,
        }
        await message.reply_text(
            "Send the phone number for the Telegram user account in international format.\n"
            "Example: <code>+15551234567</code>\n\n"
            "Cancel anytime with <code>/login cancel</code>.",
            parse_mode=enums.ParseMode.HTML,
        )

    @bot.on_message(filters.private & filters.text, group=1)
    async def login_step_handler(client, message) -> None:
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in ACTIVE_LOGIN_FLOWS:
            return
        if not await _authorized(message):
            return

        text = (message.text or "").strip()
        if text.startswith("/"):
            return

        flow = ACTIVE_LOGIN_FLOWS[user_id]
        login_client = flow.get("client")
        if login_client is None:
            await _close_login_flow(user_id)
            await message.reply_text("Login state expired. Start again with /login.")
            return

        step = str(flow.get("step") or "")
        try:
            if step == "phone":
                sent_code = await login_client.send_code(text)
                flow["phone_number"] = text
                flow["phone_code_hash"] = sent_code.phone_code_hash
                flow["step"] = "code"
                await message.reply_text(
                    "Telegram sent a login code. Send that code here.\n"
                    "You can type it with or without spaces.",
                    parse_mode=enums.ParseMode.HTML,
                )
                return

            if step == "code":
                code = _normalize_login_code(text)
                if not code:
                    await message.reply_text("Send the numeric Telegram login code.")
                    return
                try:
                    await login_client.sign_in(
                        phone_number=flow["phone_number"],
                        phone_code_hash=flow["phone_code_hash"],
                        phone_code=code,
                    )
                except SessionPasswordNeeded:
                    flow["step"] = "password"
                    await message.reply_text("Two-step verification is enabled. Send the 2FA password.")
                    return
                await _finish_login_flow(user_id, login_client, flow["settings"], message)
                return

            if step == "password":
                await login_client.check_password(text)
                await _finish_login_flow(user_id, login_client, flow["settings"], message)
                return

            await _close_login_flow(user_id)
            await message.reply_text("Login state was invalid. Start again with /login.")
        except PhoneNumberInvalid:
            await message.reply_text("That phone number was rejected. Send it in international format, like +15551234567.")
        except PhoneCodeInvalid:
            await message.reply_text("That code was invalid. Send the latest Telegram login code again.")
        except PhoneCodeExpired:
            await _close_login_flow(user_id)
            await message.reply_text("The login code expired. Start again with /login.")
        except Exception as exc:
            await _close_login_flow(user_id)
            await message.reply_text(f"Login failed: {exc}")

    @bot.on_message(filters.private & filters.command("settings", prefixes="/"))
    async def settings_handler(client, message) -> None:
        nonlocal admin_ids
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""
        tokens = shlex.split(command_text) if command_text else []

        current_settings = await _load_bot_settings(store)

        if not tokens or tokens[0].lower() in {"show", "list"}:
            await message.reply_text(
                _format_settings_root(getattr(message, "from_user", None)),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_settings_root_markup(),
            )
            return

        action = tokens[0].lower()

        if action == "set":
            if len(tokens) < 3:
                await message.reply_text(BOT_SETTINGS_HELP)
                return
            key = tokens[1].strip()
            raw_value = " ".join(tokens[2:]).strip()
            try:
                current_settings[key] = _normalize_setting_value(key, raw_value)
                await _save_bot_settings(store, current_settings)
                if key == "owner_id":
                    admin_ids = _parse_admin_ids(str(current_settings[key] or ""))
            except Exception as exc:
                await message.reply_text(f"Could not save setting: {exc}")
                return
            await message.reply_text(
                f"Saved <code>{key}</code> = <code>{_html(_masked_setting_value(key, current_settings[key]))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if action == "reset":
            if len(tokens) < 2:
                await message.reply_text(BOT_SETTINGS_HELP)
                return
            key = tokens[1].strip()
            if key.lower() == "all":
                reset_settings = {setting_key: _setting_default(setting_key) for setting_key in BOT_SETTINGS_DEFAULTS}
                await _save_bot_settings(store, reset_settings)
                admin_ids = _parse_admin_ids(str(reset_settings["owner_id"] or ""))
                await message.reply_text("All settings were reset to defaults.")
                return
            if key not in BOT_SETTINGS_DEFAULTS:
                await message.reply_text(f"Unknown setting: {key}")
                return
            current_settings[key] = _setting_default(key)
            await _save_bot_settings(store, current_settings)
            if key == "owner_id":
                admin_ids = _parse_admin_ids(str(current_settings[key] or ""))
            await message.reply_text(
                f"Reset <code>{key}</code> to <code>{_html(_masked_setting_value(key, _setting_default(key)))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        await message.reply_text(BOT_SETTINGS_HELP)

    @bot.on_callback_query(filters.regex(r"^settings:"))
    async def settings_callback_handler(client, callback_query) -> None:
        nonlocal admin_ids
        user = getattr(callback_query, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in admin_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        settings_message = getattr(callback_query, "message", None)
        if settings_message is None:
            await callback_query.answer()
            return

        data = str(getattr(callback_query, "data", "") or "")
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "close":
            await callback_query.answer("Closed.")
            try:
                await settings_message.delete()
            except Exception:
                try:
                    await settings_message.edit_text("Closed.")
                except Exception:
                    pass
            return

        current_settings = await _load_bot_settings(store)

        if action == "home":
            await callback_query.answer()
            try:
                await settings_message.edit_text(
                    _format_settings_root(user),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_settings_root_markup(),
                )
            except Exception:
                pass
            return

        if action == "cat" and len(parts) >= 4:
            raw_category = parts[2]
            page = _safe_int(parts[3])
            if raw_category.isdigit():
                category = _settings_category_by_index(int(raw_category))
            else:
                category = raw_category if raw_category in SETTINGS_CATEGORIES else None
            if category is None:
                await callback_query.answer("Unknown category.", show_alert=True)
                return
            await callback_query.answer()
            try:
                await settings_message.edit_text(
                    _format_category_panel(current_settings, category, page, user),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_category_settings_markup(category, page),
                )
            except Exception:
                pass
            return

        if action == "item" and len(parts) >= 6:
            raw_category = parts[2]
            page = _safe_int(parts[3])
            state = parts[4] if parts[4] in {"view", "edit"} else "view"
            if raw_category.isdigit():
                category = _settings_category_by_index(int(raw_category))
                key_index = _safe_int(parts[5])
                if category is None:
                    await callback_query.answer("Unknown category.", show_alert=True)
                    return
                key = _settings_key_by_index(category, key_index)
            else:
                category = raw_category
                key = ":".join(parts[5:])
            if (
                category is None
                or category not in SETTINGS_CATEGORIES
                or key is None
                or key not in BOT_SETTINGS_DEFAULTS
            ):
                await callback_query.answer("Unknown setting.", show_alert=True)
                return
            if key not in SETTINGS_CATEGORIES[category]:
                await callback_query.answer("Wrong category for this key.", show_alert=True)
                return
            await callback_query.answer()
            try:
                await settings_message.edit_text(
                    _format_setting_detail(current_settings, key, category, page, state, user),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_setting_detail_markup(key, category, page, state, current_settings),
                )
            except Exception:
                pass
            return

        if action == "toggle" and len(parts) >= 6:
            raw_category = parts[2]
            page = _safe_int(parts[3])
            state = parts[4] if parts[4] in {"view", "edit"} else "view"
            if raw_category.isdigit():
                category = _settings_category_by_index(int(raw_category))
                key_index = _safe_int(parts[5])
                if category is None:
                    await callback_query.answer("Unknown category.", show_alert=True)
                    return
                key = _settings_key_by_index(category, key_index)
            else:
                category = raw_category
                key = ":".join(parts[5:])
            if key is None or key not in BOT_TOGGLE_SETTING_KEYS or key not in BOT_SETTINGS_DEFAULTS:
                await callback_query.answer("Not a toggle setting.", show_alert=True)
                return
            if category is None or category not in SETTINGS_CATEGORIES or key not in SETTINGS_CATEGORIES[category]:
                await callback_query.answer("Unknown setting.", show_alert=True)
                return
            previous = bool(current_settings.get(key, _setting_default(key)))
            current_settings[key] = not previous
            try:
                await _save_bot_settings(store, current_settings)
            except Exception as exc:
                current_settings[key] = previous
                await callback_query.answer(f"Could not save: {exc}", show_alert=True)
                return
            await callback_query.answer("Updated.")
            try:
                await settings_message.edit_text(
                    _format_setting_detail(current_settings, key, category, page, state, user),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_setting_detail_markup(key, category, page, state, current_settings),
                )
            except Exception:
                pass
            return

        if action == "reset" and len(parts) >= 6:
            raw_category = parts[2]
            page = _safe_int(parts[3])
            state = parts[4] if parts[4] in {"view", "edit"} else "view"
            if raw_category.isdigit():
                category = _settings_category_by_index(int(raw_category))
                key_index = _safe_int(parts[5])
                if category is None:
                    await callback_query.answer("Unknown category.", show_alert=True)
                    return
                key = _settings_key_by_index(category, key_index)
            else:
                category = raw_category
                key = ":".join(parts[5:])
            if key is None or key not in BOT_SETTINGS_DEFAULTS:
                await callback_query.answer("Unknown setting.", show_alert=True)
                return
            if category is None or category not in SETTINGS_CATEGORIES or key not in SETTINGS_CATEGORIES[category]:
                await callback_query.answer("Unknown setting.", show_alert=True)
                return
            current_settings[key] = _setting_default(key)
            try:
                await _save_bot_settings(store, current_settings)
                if key == "owner_id":
                    admin_ids = _parse_admin_ids(str(current_settings[key] or ""))
            except Exception as exc:
                await callback_query.answer(f"Could not reset: {exc}", show_alert=True)
                return
            await callback_query.answer("Reset.")
            try:
                await settings_message.edit_text(
                    _format_setting_detail(current_settings, key, category, page, state, user),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_setting_detail_markup(key, category, page, state, current_settings),
                )
            except Exception:
                pass
            return

        await callback_query.answer()

    @bot.on_callback_query(filters.regex(r"^job_cancel:(export|index)$"))
    async def job_cancel_callback_handler(client, callback_query) -> None:
        user = getattr(callback_query, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in admin_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        data = str(getattr(callback_query, "data", "") or "")
        kind = data.split(":", 1)[-1]
        task = ACTIVE_EXPORT_TASK if kind == "export" else ACTIVE_INDEX_TASK
        if task is None or task.done():
            await callback_query.answer(f"No active {kind} task.", show_alert=True)
            return

        task.cancel()
        await callback_query.answer(f"{kind.title()} cancellation requested.")

    @bot.on_message(filters.private & filters.command("log", prefixes="/"))
    async def log_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        if not LOG_FILE.exists() or LOG_FILE.stat().st_size <= 0:
            await message.reply_text("No log file is available yet.")
            return

        await message.reply_document(
            document=str(LOG_FILE),
            caption="Current bot log",
        )

    @bot.on_message(filters.private & filters.command("cancel", prefixes="/"))
    async def cancel_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        parts = (message.text or "").split(maxsplit=1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        tokens = rest.split()
        kind = tokens[0].lower() if tokens else ""

        if tokens and kind not in {"clone", "export", "index"}:
            await message.reply_text(
                "Use <code>/cancel clone</code>, <code>/cancel clone queued &lt;job_id&gt;</code>, "
                "<code>/cancel export</code>, or <code>/cancel index</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if kind == "clone" and len(tokens) >= 3 and tokens[1].lower() == "queued":
            job_token = tokens[2].strip()
            removed = await _cancel_queued_clone_job_by_token(store, job_token)
            if removed is None:
                await message.reply_text("No matching queued clone job (check <code>/clone queue</code>).", parse_mode=enums.ParseMode.HTML)
                return
            jid = str(removed.get("job_id", ""))
            short = jid[:8] if jid else "?"
            await message.reply_text(
                f"Removed queued clone job <code>{_html(short)}</code> from the queue.",
                parse_mode=enums.ParseMode.HTML,
            )
            notice_mid = _safe_int(removed.get("notice_message_id"))
            ch = _safe_int(removed.get("requested_chat_id"))
            if ch and notice_mid:
                try:
                    await client.edit_message_text(
                        ch,
                        notice_mid,
                        "<i>This queued clone job was cancelled.</i>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass
            return

        if kind == "export":
            if ACTIVE_EXPORT_TASK is None or ACTIVE_EXPORT_TASK.done():
                await message.reply_text("No active export task to cancel.")
                return
            ACTIVE_EXPORT_TASK.cancel()
            await message.reply_text("Export cancellation requested.")
            return

        if kind == "index":
            if ACTIVE_INDEX_TASK is None or ACTIVE_INDEX_TASK.done():
                await message.reply_text("No active index task to cancel.")
                return
            ACTIVE_INDEX_TASK.cancel()
            await message.reply_text("Index cancellation requested.")
            return

        if kind == "clone":
            if ACTIVE_CLONE_CANCEL_EVENT is None or ACTIVE_CLONE_CANCEL_EVENT.is_set():
                await message.reply_text("No active clone task to cancel.")
                return
            ACTIVE_CLONE_CANCEL_EVENT.set()
            if ACTIVE_CLONE_TASK is not None and not ACTIVE_CLONE_TASK.done():
                ACTIVE_CLONE_TASK.cancel()
            await message.reply_text("Clone cancellation requested.")
            return

        if ACTIVE_EXPORT_TASK is not None and not ACTIVE_EXPORT_TASK.done():
            ACTIVE_EXPORT_TASK.cancel()
            await message.reply_text("Export cancellation requested.")
            return
        if ACTIVE_INDEX_TASK is not None and not ACTIVE_INDEX_TASK.done():
            ACTIVE_INDEX_TASK.cancel()
            await message.reply_text("Index cancellation requested.")
            return
        if ACTIVE_CLONE_CANCEL_EVENT is None or ACTIVE_CLONE_CANCEL_EVENT.is_set():
            await message.reply_text("No active task to cancel.")
            return
        ACTIVE_CLONE_CANCEL_EVENT.set()
        if ACTIVE_CLONE_TASK is not None and not ACTIVE_CLONE_TASK.done():
            ACTIVE_CLONE_TASK.cancel()
        await message.reply_text("Clone cancellation requested.")

    @bot.on_message(filters.private & filters.command("restart", prefixes="/"))
    async def restart_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        await message.reply_text(
            "<i>Are you sure you want to restart the bot?</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes", callback_data="restart:confirm"),
                        InlineKeyboardButton("No", callback_data="restart:cancel"),
                    ]
                ]
            ),
        )

    @bot.on_callback_query(filters.regex(r"^restart:(confirm|cancel)$"))
    async def restart_callback_handler(client, callback_query) -> None:
        user = getattr(callback_query, "from_user", None)
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or user_id not in admin_ids:
            await callback_query.answer("Not authorized.", show_alert=True)
            return

        restart_prompt = getattr(callback_query, "message", None)
        if restart_prompt is None:
            await callback_query.answer()
            return

        action = str(getattr(callback_query, "data", "") or "").split(":", 1)[-1]
        if action == "cancel":
            await callback_query.answer("Cancelled.")
            try:
                await restart_prompt.edit_text("Restart cancelled.")
            except Exception:
                pass
            return

        await callback_query.answer()
        try:
            await restart_prompt.edit_text("<i>Restarting...</i>", parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        _write_json_file(
            RESTART_MESSAGE_FILE,
            {
                "chat_id": restart_prompt.chat.id,
                "message_id": restart_prompt.id,
                "requested_at": time.time(),
            },
        )
        asyncio.create_task(_restart_process())

    async def _restart_process() -> None:
        await asyncio.sleep(1.5)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception:
            logging.getLogger("heroku_bot").exception("process restart failed; exiting for dyno restart")
            os._exit(1)

    @bot.on_message(filters.private & filters.command("export", prefixes="/"))
    async def export_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        bot_settings = await _load_bot_settings(store)
        if not _configured_session_string(bot_settings):
            await message.reply_text(
                "No Telegram user session string is configured yet.\n\n"
                "Set it from your deploy environment as <code>TG_SESSION_STRING</code>, "
                "or generate one with <code>/login</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if command_text.lower() in {"last", "resume"}:
            stored = await store.load("export:last")
            if stored is None:
                await message.reply_text("No saved export profile found.")
                return
            payload = stored.get("payload") if isinstance(stored, dict) else None
            if not isinstance(payload, dict):
                await message.reply_text("Saved export profile is invalid.")
                return
            payload = dict(payload)
            payload.setdefault(
                "caption_file_names",
                bool(bot_settings["export_default_caption_file_names"]),
            )
            payload.setdefault("onwards", bool(bot_settings["export_default_onwards"]))
        else:
            normalized = _normalize_export_command(command_text)
            try:
                parsed = _build_export_parser().parse_args(shlex.split(normalized))
            except SystemExit:
                await message.reply_text(_bundle_help_text())
                return
            payload = {
                "topic_link": parsed.topic_link,
                "config_path": parsed.config,
                "out_path": parsed.out,
                "batch_size": parsed.batch_size
                if "--batch-size" in normalized
                else int(bot_settings["export_default_batch_size"]),
                "batch_delay_sec": parsed.batch_delay_sec
                if "--batch-delay-sec" in normalized
                else float(bot_settings["export_default_batch_delay_sec"]),
                "upload_topic_link": parsed.upload_topic_link,
                "caption_file_names": parsed.caption_file_names
                if "--caption-file-names" in normalized
                else bool(bot_settings["export_default_caption_file_names"]),
                "onwards": parsed.onwards
                if "--onwards" in normalized
                else bool(bot_settings["export_default_onwards"]),
            }

        await _run_export_job(message, payload, store)

    @bot.on_message(filters.private & filters.command("index", prefixes="/"))
    async def index_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        bot_settings = await _load_bot_settings(store)
        if not _configured_session_string(bot_settings):
            await message.reply_text(
                "No Telegram user session string is configured yet.\n\n"
                "Set it from your deploy environment as <code>TG_SESSION_STRING</code>, "
                "or generate one with <code>/login</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if command_text.lower() in {"last", "resume"}:
            stored = await _load_state(store, "index:last")
            if stored is None:
                await message.reply_text("No saved index profile found.")
                return
            payload = stored.get("payload") if isinstance(stored, dict) else None
            if not isinstance(payload, dict):
                await message.reply_text("Saved index profile is invalid.")
                return
            payload = dict(payload)
            payload.setdefault("onwards", bool(bot_settings["index_default_onwards"]))
        else:
            normalized = _normalize_index_command(command_text)
            try:
                parsed = _build_index_parser().parse_args(shlex.split(normalized))
            except SystemExit:
                await message.reply_text(_bundle_help_text())
                return
            if not parsed.topic_link:
                await message.reply_text(_bundle_help_text())
                return
            payload = {
                "topic_link": parsed.topic_link,
                "config_path": parsed.config,
                "batch_size": parsed.batch_size
                if "--batch-size" in normalized
                else int(bot_settings["export_default_batch_size"]),
                "batch_delay_sec": parsed.batch_delay_sec
                if "--batch-delay-sec" in normalized
                else float(bot_settings["export_default_batch_delay_sec"]),
                "onwards": parsed.onwards
                if "--onwards" in normalized
                else bool(bot_settings["index_default_onwards"]),
                "header": parsed.header,
            }

        await _run_index_job(message, payload, store, bot)

    @bot.on_message(filters.private & filters.command("clone", prefixes="/"))
    async def clone_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        if command_text.lower() == "status":
            text, _, queue_snap = await _load_clone_status_view_text(store, "main")
            sent = await message.reply_text(
                text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_clone_status_reply_markup("main", queue_snap),
            )
            key = (sent.chat.id, sent.id)
            ACTIVE_STATUS_VIEWS[key] = "main"
            ACTIVE_STATUS_LAST_TEXTS[key] = text
            await _ensure_status_watcher(sent, text, "clone_status")
            return

        if command_text.lower() == "queue":
            async with _clone_queue_cv:
                snap = list(_clone_pending_jobs)
            await message.reply_text(
                _format_clone_queue_listing(snap),
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        bot_settings = await _load_bot_settings(store)
        if not _configured_session_string(bot_settings):
            await message.reply_text(
                "No Telegram user session string is configured yet.\n\n"
                "Set it from your deploy environment as <code>TG_SESSION_STRING</code>, "
                "or generate one with <code>/login</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if command_text.lower() in {"last", "resume"}:
            stored = await store.load("clone:last")
            if stored is None:
                await message.reply_text("No saved clone profile found.")
                return
            payload = _resume_clone_payload_from_state(stored) if isinstance(stored, dict) else None
            if payload is None and isinstance(stored, dict):
                payload = stored.get("payload") if str(stored.get("phase", "")).lower() not in {"running", "queued"} else None
            if not isinstance(payload, dict):
                await message.reply_text("Saved clone profile is invalid or already fully resumed.")
                return
        else:
            normalized = _normalize_clone_command(command_text)
            try:
                parsed = _build_clone_parser().parse_args(shlex.split(normalized))
            except SystemExit:
                await message.reply_text(_bundle_help_text())
                return
            payload = {
                "source_link": parsed.source_link,
                "destination_link": parsed.destination_link,
                "config_path": parsed.config,
                "start_id": parsed.start_id,
                "limit": parsed.limit,
                "delay_sec": parsed.delay_sec
                if "--delay-sec" in normalized
                else float(bot_settings["clone_default_delay_sec"]),
                "batch_size": parsed.batch_size
                if "--batch-size" in normalized
                else int(bot_settings["clone_default_batch_size"]),
                "message_ids": parsed.message_ids,
                "dry_run": parsed.dry_run,
                "continue_on_error": parsed.continue_on_error
                if "--continue-on-error" in normalized
                else bool(bot_settings["clone_continue_on_error_default"]),
                "hide_sender_name": parsed.hide_sender_name
                if "--hide-sender-name" in normalized
                else bool(bot_settings["clone_hide_sender_name_default"]),
                "filename_prefix": parsed.filename_prefix
                if "--filename-prefix" in normalized
                else str(bot_settings["clone_filename_prefix_default"]),
                "filename_suffix": parsed.filename_suffix
                if "--filename-suffix" in normalized
                else str(bot_settings["clone_filename_suffix_default"]),
                "text_prefix": parsed.text_prefix
                if "--text-prefix" in normalized
                else str(bot_settings["clone_text_prefix_default"]),
                "text_suffix": parsed.text_suffix
                if "--text-suffix" in normalized
                else str(bot_settings["clone_text_suffix_default"]),
            }

        user = getattr(message, "from_user", None)
        payload = dict(payload)
        payload["requested_by_id"] = getattr(user, "id", None)
        payload["requested_by_name"] = (
            getattr(user, "first_name", None)
            or getattr(user, "username", None)
            or "Admin"
        )
        await _enqueue_clone_request(
            store=store,
            bot=client,
            chat_id=message.chat.id,
            command_message_id=getattr(message, "id", None),
            payload=payload,
        )

    await bot.start()
    print("Heroku topic bot is running.")
    await _hydrate_clone_queue_from_storage(store)
    asyncio.create_task(_clone_queue_worker(bot, store, admin_ids))
    asyncio.create_task(_send_restart_notification())
    try:
        await asyncio.Event().wait()
    finally:
        await bot.stop()


def main() -> int:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        return 130
    except Exception as exc:
        print(f"Bot failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
