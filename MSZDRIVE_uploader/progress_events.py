"""Machine-readable progress for the bot; command-line logging stays available."""
from __future__ import annotations

import json
import os
import threading
import time

PREFIX = "@@TRANSFER_PROGRESS@@"
_lock = threading.Lock()


def emit(event: str, **values) -> None:
    if os.getenv("TRANSFER_PROGRESS_EVENTS") != "1":
        return
    with _lock:
        print(PREFIX + json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


def stage(name: str, file_name: str = "", **values) -> None:
    emit("stage", stage=name, file_name=file_name, **values)


def totals(files: int, size: int | None = None) -> None:
    emit("totals", total_files=files, total_bytes=size)


def start_file(index: int, file_name: str, size: int | None = None) -> None:
    emit("file", index=index, file_name=file_name, file_size=size)


def outcome(result: str) -> None:
    emit("result", result=result)


class ByteProgress:
    def __init__(self, stage_name: str, file_name: str, size: int | None = None, *, operation: str = ""):
        self.stage = stage_name
        self.file_name = file_name
        self.size = size
        self.operation = operation or stage_name
        self.started = time.monotonic()
        self.last_at = 0.0
        self.last_done = 0
        stage(stage_name, file_name, file_size=size, operation=self.operation)

    def __call__(self, done: int, total: int | None = None):
        now = time.monotonic()
        size = total or self.size
        if now - self.last_at < 1 and not (size and done >= size):
            return
        # Retries can restart the byte counter; do not carry the old speed over.
        if done < self.last_done:
            self.started = now
        self.last_at = now
        self.last_done = done
        rate = done / max(now - self.started, 0.001)
        emit("bytes", stage=self.stage, file_name=self.file_name, operation=self.operation,
             bytes_done=done, file_size=size, speed_bps=rate,
             file_eta=(max(0, size - done) / rate) if size and rate > 0 else None)
