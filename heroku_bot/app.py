from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from dotenv import load_dotenv

try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

BUNDLE_DIR = Path(__file__).resolve().parent
load_dotenv(BUNDLE_DIR / ".env")
os.environ.setdefault("HEROKU_RUNTIME_DIR", str(BUNDLE_DIR / "runtime"))
os.environ.setdefault("ALLOW_EMPTY_MAPPINGS", "true")
os.environ.setdefault("HEROKU_CONFIG_PATH", str(BUNDLE_DIR / "config.yaml"))
os.environ.setdefault("LEECH_BOT_USERNAME", "@placeholder_bot")
os.environ.setdefault("LEECH_BOT_ID", "0")

from pyrogram import Client, filters

from clone_topic_by_link import run_clone
from export_topic_list import run_export

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


def _ensure_runtime_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


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


def _build_clone_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-link", default="")
    parser.add_argument("--destination-link", default="")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay-sec", type=float, default=0.35)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--message-ids", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--hide-sender-name", action="store_true")
    return parser


def _bundle_help_text() -> str:
    return (
        "Commands:\n"
        "/export --topic-link <link> [--config <path>] [--out <file>] [--batch-size N] "
        "[--batch-delay-sec S] [--upload-topic-link <link>] [--caption-file-names] [--onwards]\n"
        "/export last or /export resume\n"
        "/clone --source-link <link> --destination-link <link> [--config <path>] [--start-id N] "
        "[--limit N] [--delay-sec S] [--batch-size N] [--message-ids 1,2,3] [--dry-run] "
        "[--continue-on-error] [--hide-sender-name]\n"
        "/clone last or /clone resume\n"
        "/status\n"
        "/cancel\n"
        "/restart"
    )


def _normalize_export_command(command_text: str) -> str:
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


def _format_status_payload(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "None"
    return json.dumps(payload, indent=2, sort_keys=True)


def _format_clone_status(state: dict[str, Any]) -> str:
    parts = []
    phase = state.get("phase", "unknown")
    parts.append(f"Phase: {phase.title()}")

    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    source = payload.get("source_link", "n/a")
    dest = payload.get("destination_link", "n/a")
    source_label = _format_clone_endpoint(payload, "source")
    destination_label = _format_clone_endpoint(payload, "destination")
    parts.append(f"Source: {source_label or source}")
    parts.append(f"Leech: {destination_label or dest}")

    if phase in {"running", "queued"}:
        parts.append(
            f"Progress: {state.get('current_index', 0)}/{state.get('total_messages', 0)}"
        )
        parts.append(
            f"Success: {state.get('success', 0)} | Failed: {state.get('failed', 0)}"
        )
        current_message_id = state.get("current_message_id")
        if current_message_id:
            parts.append(f"Current message: {current_message_id}")
        transfer_stage = state.get("transfer_stage")
        if transfer_stage:
            parts.append(f"Transfer stage: {transfer_stage}")
            parts.append(
                "Download: "
                f"{state.get('download_current', 0)}/{state.get('download_total', 0)} "
                f"at {state.get('download_speed', '0B/s')} "
                f"(eta {state.get('download_eta', '-')})"
            )
            parts.append(
                "Upload: "
                f"{state.get('upload_current', 0)}/{state.get('upload_total', 0)} "
                f"at {state.get('upload_speed', '0B/s')} "
                f"(eta {state.get('upload_eta', '-')})"
            )
        if state.get("error"):
            parts.append(f"Last error: {state.get('error')}")
        if phase == "running":
            parts.append("Cancel: /cancel")
    else:
        if state.get("success") is not None or state.get("failed") is not None:
            parts.append(
                f"Success: {state.get('success', 0)} | Failed: {state.get('failed', 0)}"
            )
        if state.get("error"):
            parts.append(f"Error: {state.get('error')}")

    started_at = state.get("started_at")
    updated_at = state.get("updated_at")
    if started_at is not None:
        parts.append(f"Started: {time.ctime(float(started_at))}")
    if updated_at is not None:
        parts.append(f"Updated: {time.ctime(float(updated_at))}")

    return "\n".join(parts)


def _format_clone_status_compact(state: dict[str, Any]) -> str:
    phase = str(state.get("phase", "unknown")).lower()
    current = int(state.get("current_index", 0) or 0)
    total = int(state.get("total_messages", 0) or 0)
    success = int(state.get("success", 0) or 0)
    failed = int(state.get("failed", 0) or 0)
    current_message_id = state.get("current_message_id")

    if phase == "running":
        dl_speed = state.get("download_speed")
        up_speed = state.get("upload_speed")
        speed_text = ""
        if dl_speed or up_speed:
            speed_text = f" | DL: {dl_speed or '0B/s'} | UP: {up_speed or '0B/s'}"
        return (
            f"Live: {current}/{total} | Forwarded: {success} | Failed: {failed} "
            f"| Current: {current_message_id or '-'}{speed_text}"
        )
    if phase == "queued":
        return "Live: queued"
    if phase == "completed":
        return f"Live: completed | Forwarded: {success} | Failed: {failed}"
    if phase == "cancelled":
        return f"Live: cancelled at {current}/{total} | Forwarded: {success} | Failed: {failed}"
    if phase == "failed":
        return f"Live: failed at {current}/{total} | Forwarded: {success} | Failed: {failed}"
    return f"Live: {phase} | Forwarded: {success} | Failed: {failed}"


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


async def _run_export_job(message, payload: dict[str, Any], store: MongoStateStore) -> None:
    await store.save("export:last", {"phase": "queued", "payload": payload})
    status = await message.reply_text("Export started.")
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
        )
    except Exception as exc:
        await store.save(
            "export:last",
            {"phase": "failed", "payload": payload, "error": str(exc)},
        )
        await status.edit_text(f"Export failed: {exc}")
        raise
    else:
        await store.save(
            "export:last",
            {"phase": "completed", "payload": payload, "output": str(output)},
        )
        await status.edit_text(f"Export complete: {output}")


async def _run_clone_job(message, payload: dict[str, Any], store: MongoStateStore) -> None:
    global ACTIVE_CLONE_TASK, ACTIVE_CLONE_CANCEL_EVENT

    ACTIVE_CLONE_CANCEL_EVENT = asyncio.Event()
    ACTIVE_CLONE_TASK = asyncio.current_task()

    last_status_edit_at = 0.0
    last_reported_success = -1
    last_reported_index = -1
    latest_state_payload = dict(payload)
    status_update_lock = asyncio.Lock()

    def _format_status_text(state: dict[str, Any]) -> str:
        phase = state.get("phase", "running")
        payload_info = state.get("payload") if isinstance(state.get("payload"), dict) else payload
        source_label = _format_clone_endpoint(payload_info, "source")
        destination_label = _format_clone_endpoint(payload_info, "destination")
        route_lines = ""
        if source_label or destination_label:
            route_lines = (
                f"Source: {source_label or payload_info.get('source_link', 'n/a')}\n"
                f"Leech: {destination_label or payload_info.get('destination_link', 'n/a')}\n\n"
            )
        if phase == "queued":
            return f"{route_lines}Clone queued. Waiting to start..."

        total = state.get("total_messages", 0)
        current = state.get("current_index", 0)
        success = state.get("success", 0)
        failed = state.get("failed", 0)
        current_message_id = state.get("current_message_id")

        if phase == "completed":
            return (
                f"{route_lines}Clone complete. Forwarded successfully: {success}, Failed: {failed}."
            )
        if phase == "cancelled":
            return (
                f"{route_lines}Clone cancelled. Progress: {current}/{total} "
                f"(forwarded={success}, failed={failed})."
            )
        if phase == "failed":
            return (
                f"{route_lines}Clone failed after {current}/{total}. "
                f"forwarded={success}, failed={failed}. "
                f"Error: {state.get('error', 'unknown')}"
            )
        return (
            f"{route_lines}"
            f"Cloning messages: {current}/{total}\n"
            f"Forwarded successfully: {success}\n"
            f"Failed: {failed}\n"
            f"Current source message: {current_message_id}\n"
            f"Download speed: {state.get('download_speed', '0B/s')} (eta {state.get('download_eta', '-')})\n"
            f"Upload speed: {state.get('upload_speed', '0B/s')} (eta {state.get('upload_eta', '-')})"
        )

    async def _save_state_inner(state: dict[str, Any]) -> None:
        nonlocal last_status_edit_at, last_reported_success, last_reported_index, latest_state_payload
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
        latest_state_payload = dict(wrapper["payload"])
        await _save_clone_state(store, "last", wrapper)

        now = time.time()
        phase = str(wrapper.get("phase", "running"))
        success = int(wrapper.get("success", 0) or 0)
        current_index = int(wrapper.get("current_index", 0) or 0)

        success_changed = success != last_reported_success
        progress_changed = current_index != last_reported_index

        should_edit = (
            phase != "running"
            or (success_changed and now - last_status_edit_at >= 1.5)
            or (progress_changed and now - last_status_edit_at >= 2.0)
            or now - last_status_edit_at >= 10
        )
        if not should_edit:
            return

        async with status_update_lock:
            try:
                await status.edit_text(_format_status_text(wrapper))
            except Exception:
                pass
            else:
                last_status_edit_at = now
                last_reported_success = success
                last_reported_index = current_index

    await _save_clone_state(store, "last", {"phase": "queued", "payload": payload})
    status = await message.reply_text("Clone started.")
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
            status_callback=_save_state_inner,
            cancel_event=ACTIVE_CLONE_CANCEL_EVENT,
        )
    except asyncio.CancelledError:
        await _save_clone_state(
            store,
            "last",
            {
                "phase": "cancelled",
                "payload": payload,
                "error": "Cancelled by user",
            },
        )
        await status.edit_text("Clone cancelled.")
        raise
    except Exception as exc:
        await _save_clone_state(
            store,
            "last",
            {"phase": "failed", "payload": payload, "error": str(exc)},
        )
        await status.edit_text(f"Clone failed: {exc}")
        raise
    else:
        result = {
            "phase": "completed",
            "payload": latest_state_payload,
            "success": success,
            "failed": failed,
        }
        await _save_clone_state(store, "last", result)
        await status.edit_text(_format_status_text(result))
    finally:
        ACTIVE_CLONE_TASK = None
        ACTIVE_CLONE_CANCEL_EVENT = None


async def run_bot() -> None:
    _ensure_runtime_dirs()

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
        MONGODB_DATABASE,
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

    @bot.on_message(filters.private & filters.command("start", prefixes="/"))
    async def start_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        await message.reply_text(_bundle_help_text())

    @bot.on_message(filters.private & filters.command("help", prefixes="/"))
    async def help_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        await message.reply_text(_bundle_help_text())

    @bot.on_message(filters.private & filters.command("status", prefixes="/"))
    async def status_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        export_state = await store.load("export:last")
        clone_state = await store.load("clone:last")
        messages = []
        if clone_state:
            messages.append("<b>Clone Status</b>")
            messages.append(_format_clone_status_compact(clone_state))
            messages.append(_format_clone_status(clone_state))
        else:
            messages.append("No saved clone state.")
        if export_state:
            messages.append("\n<b>Export Status</b>")
            messages.append(_format_status_payload(export_state))
        await message.reply_text("\n\n".join(messages), parse_mode="html")

    @bot.on_message(filters.private & filters.command("cancel", prefixes="/"))
    async def cancel_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return
        if ACTIVE_CLONE_CANCEL_EVENT is None or ACTIVE_CLONE_CANCEL_EVENT.is_set():
            await message.reply_text("No active clone task to cancel.")
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
        await message.reply_text("Restarting bot now...")
        try:
            await bot.stop()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable, *sys.argv])

    @bot.on_message(filters.private & filters.command("export", prefixes="/"))
    async def export_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        if command_text.lower() in {"last", "resume"}:
            stored = await store.load("export:last")
            if stored is None:
                await message.reply_text("No saved export profile found.")
                return
            payload = stored.get("payload") if isinstance(stored, dict) else None
            if not isinstance(payload, dict):
                await message.reply_text("Saved export profile is invalid.")
                return
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
                "batch_size": parsed.batch_size,
                "batch_delay_sec": parsed.batch_delay_sec,
                "upload_topic_link": parsed.upload_topic_link,
                "caption_file_names": parsed.caption_file_names,
                "onwards": parsed.onwards,
            }

        await _run_export_job(message, payload, store)

    @bot.on_message(filters.private & filters.command("clone", prefixes="/"))
    async def clone_handler(client, message) -> None:
        if not await _authorized(message):
            await message.reply_text("Not authorized.")
            return

        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""

        if command_text.lower() in {"last", "resume"}:
            stored = await store.load("clone:last")
            if stored is None:
                await message.reply_text("No saved clone profile found.")
                return
            payload = stored.get("payload") if isinstance(stored, dict) else None
            if not isinstance(payload, dict):
                await message.reply_text("Saved clone profile is invalid.")
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
                "delay_sec": parsed.delay_sec,
                "batch_size": parsed.batch_size,
                "message_ids": parsed.message_ids,
                "dry_run": parsed.dry_run,
                "continue_on_error": parsed.continue_on_error,
                "hide_sender_name": parsed.hide_sender_name,
            }

        await _run_clone_job(message, payload, store)

    await bot.start()
    print("Heroku topic bot is running.")
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
