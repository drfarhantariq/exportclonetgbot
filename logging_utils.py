from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_RESERVED_RECORD_KEYS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _RESERVED_RECORD_KEYS:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class PrettyConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        logger_name = record.name.replace("telegram_cloner.", "")
        logger_name = logger_name.replace("telegram_cloner", "app")

        extras: list[str] = []
        for key in (
            "mapping_key",
            "mapping_count",
            "source_message_id",
            "destination_message_id",
            "bot_command_message_id",
            "bot_media_message_id",
            "discovered_count",
            "reconciled_count",
            "enqueued_count",
            "reset_rows",
            "retry_count",
            "wait_seconds",
            "error",
        ):
            value = getattr(record, key, None)
            if value is None:
                continue
            extras.append(f"{key}={value}")

        extras_text = f" [{' | '.join(extras)}]" if extras else ""
        return f"{timestamp} | {record.levelname:<7} | {logger_name} | {record.getMessage()}{extras_text}"


def configure_logging(level_name: str, log_file_path: Path) -> None:
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(PrettyConsoleFormatter())

    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("pyrogram.crypto.aes").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
