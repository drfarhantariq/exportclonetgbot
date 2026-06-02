from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class UploadState:
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
        self.data["updated_at"] = utc_now()
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def should_skip(
        self,
        rel_path: str,
        size: int,
        mtime: int,
        remote_size: int | None,
        *,
        trust_state: bool = True,
    ) -> tuple[bool, str]:
        record = self.data["files"].get(rel_path)
        if trust_state and (
            isinstance(record, dict)
            and record.get("status") == "uploaded"
            and record.get("size") == size
            and record.get("mtime") == mtime
        ):
            return True, "state"
        if remote_size is None:
            return False, ""
        if int(remote_size) == size:
            return True, "remote"
        return False, ""

    def mark_started(self, rel_path: str, size: int, mtime: int, method: str, source_path: str) -> None:
        previous = self.data["files"].get(rel_path, {})
        attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
        self.data["files"][rel_path] = {
            "status": "uploading",
            "size": size,
            "mtime": mtime,
            "method": method,
            "source_path": source_path,
            "attempts": attempts + 1,
            "started_at": utc_now(),
        }
        self.save()

    def mark_uploaded(self, rel_path: str, size: int, mtime: int, method: str, remote_id: object | None) -> None:
        previous = self.data["files"].get(rel_path, {})
        attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
        self.data["files"][rel_path] = {
            "status": "uploaded",
            "size": size,
            "mtime": mtime,
            "method": method,
            "remote_id": remote_id,
            "attempts": attempts,
            "uploaded_at": utc_now(),
        }
        self.save()

    def mark_failed(self, rel_path: str, size: int, mtime: int, error: str) -> None:
        previous = self.data["files"].get(rel_path, {})
        retries = int(previous.get("retries", 0)) if isinstance(previous, dict) else 0
        self.data["files"][rel_path] = {
            "status": "failed",
            "size": size,
            "mtime": mtime,
            "retries": retries + 1,
            "last_error": error,
            "updated_at": utc_now(),
        }
        self.save()

    def failed_paths(self) -> set[str]:
        failed: set[str] = set()
        for rel_path, record in self.data.get("files", {}).items():
            if isinstance(record, dict) and record.get("status") == "failed":
                failed.add(str(rel_path))
        return failed


class FailedUploadLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(
        self,
        *,
        source: str,
        source_path: str,
        rel_path: str,
        target_rel: str,
        size: int,
        mtime: int,
        error: str,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "failed_at": utc_now(),
            "source": source,
            "source_path": source_path,
            "rel_path": rel_path,
            "target_rel": target_rel,
            "size": size,
            "mtime": mtime,
            "error": error,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def target_paths(self) -> set[str]:
        if not self.path.exists():
            return set()
        paths: set[str] = set()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            target = payload.get("target_rel")
            if target:
                paths.add(str(target))
        return paths
