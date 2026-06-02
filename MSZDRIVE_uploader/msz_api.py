from __future__ import annotations

import mimetypes
import os
import re
import time
import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests


def norm_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").strip("/")


def natural_sort_key(value: object) -> tuple[tuple[int, object], ...]:
    text = norm_rel(str(value)).casefold()
    parts = re.split(r"(\d+)", text)
    key: list[tuple[int, object]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def parse_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "entries", "items", "fileEntries"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def infer_extension_from_signature(path: Path) -> str:
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return ""

    if head.startswith(b"\xFF\xD8\xFF"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ".mp4"
    if head.startswith(b"ID3"):
        return ".mp3"
    if head.startswith(b"RIFF") and b"WAVE" in head[:16]:
        return ".wav"
    return ""


@dataclass(frozen=True)
class RemoteFile:
    id: Any
    size: int | None
    name: str
    rel_path: str


@dataclass(frozen=True)
class MszEntry:
    id: Any
    name: str
    type: str
    rel_path: str
    parent_id: Any = None
    size: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_folder(self) -> bool:
        return self.type == "folder"

    @property
    def is_file(self) -> bool:
        return not self.is_folder


class MszApiClient:
    def __init__(self, base_url: str, api_token: str, timeout: int = 60) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.api_token = api_token.strip()
        self.timeout = timeout
        if not self.base_url:
            raise ValueError("MSZ base URL is empty.")
        if not self.api_token:
            raise ValueError("MSZ API token is empty.")
        self.headers = {"Authorization": f"Bearer {self.api_token}"}
        self.api_base = self._resolve_api_base()

    def _resolve_api_base(self) -> str:
        raw = self.base_url
        candidates = []
        if not raw.endswith("/api"):
            candidates.append(raw + "/api")
        if not raw.endswith("/api/v1"):
            candidates.append(raw + "/api/v1")
        candidates.append(raw)

        deduped: list[str] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)

        tested: list[Any] = []
        for candidate in deduped:
            url = candidate + "/drive/file-entries"
            try:
                response = requests.get(
                    url,
                    headers=self.headers,
                    params={"perPage": 1},
                    timeout=self.timeout,
                )
                try:
                    payload = response.json()
                    json_ok = True
                except ValueError:
                    payload = None
                    json_ok = False
                tested.append((url, response.status_code, json_ok))
                if response.status_code in (200, 401, 403) and json_ok:
                    if response.status_code == 200 and not isinstance(payload, (list, dict)):
                        continue
                    return candidate
            except requests.RequestException as exc:
                tested.append((url, repr(exc)))
        raise RuntimeError(f"Could not resolve MSZ API base. Tried: {tested}")

    def list_entries(
        self,
        per_page: int = 200,
        max_pages: int = 200,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen_ids: set[Any] = set()

        for page in range(1, max_pages + 1):
            params = {"perPage": per_page, "page": page}
            if extra_params:
                params.update(extra_params)
            response = requests.get(
                self.api_base + "/drive/file-entries",
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                if page == 1:
                    raise RuntimeError(
                        f"Remote index fetch failed: {response.status_code} {response.text[:400]}"
                    )
                break

            page_entries = parse_entries(response.json())
            if not page_entries:
                break

            added = 0
            for entry in page_entries:
                entry_id = entry.get("id")
                if entry_id in seen_ids:
                    continue
                seen_ids.add(entry_id)
                entries.append(entry)
                added += 1

            if added == 0 or len(page_entries) < per_page:
                break

        return entries

    def _list_child_entries(self, parent_id: Any, per_page: int = 200, max_pages: int = 200) -> list[dict[str, Any]]:
        entries = self.list_entries(
            per_page=per_page,
            max_pages=max_pages,
            extra_params={"parentIds": str(parent_id)},
        )
        return [
            entry
            for entry in entries
            if str(entry.get("parent_id") or entry.get("parentId") or parent_id) == str(parent_id)
        ]

    def _augment_entries_recursively(self, entries: list[dict[str, Any]], per_page: int, max_pages: int) -> list[dict[str, Any]]:
        by_id = {entry.get("id"): entry for entry in entries if entry.get("id") is not None}
        queue = [entry for entry in entries if entry.get("type") == "folder" and entry.get("id") is not None]
        visited_folders: set[Any] = set()

        while queue:
            folder = queue.pop(0)
            folder_id = folder.get("id")
            if folder_id in visited_folders:
                continue
            visited_folders.add(folder_id)
            children = self._list_child_entries(folder_id, per_page=per_page, max_pages=max_pages)
            for child in children:
                child_id = child.get("id")
                if child_id is None or child_id in by_id:
                    continue
                by_id[child_id] = child
                entries.append(child)
                if child.get("type") == "folder":
                    queue.append(child)
        return entries

    def build_entry_index(
        self,
        per_page: int = 200,
        max_pages: int = 200,
        recursive: bool = True,
        log: Any = None,
    ) -> dict[str, MszEntry]:
        if log:
            log("Building MSZ entry index...")
        entries = self.list_entries(per_page=per_page, max_pages=max_pages)
        if log:
            log(f"Initial MSZ index entries: {len(entries)}")
        if recursive:
            entries = self._augment_entries_recursively(entries, per_page=per_page, max_pages=max_pages)
            if log:
                log(f"Recursive MSZ index entries: {len(entries)}")
        id_map = {entry.get("id"): entry for entry in entries if entry.get("id") is not None}
        index: dict[str, MszEntry] = {}

        for entry in entries:
            name = entry.get("name")
            if not name:
                continue

            parts = [str(name)]
            parent_id = entry.get("parent_id")
            visited: set[Any] = set()
            while parent_id and parent_id in id_map and parent_id not in visited:
                visited.add(parent_id)
                parent = id_map[parent_id]
                parent_name = parent.get("name")
                if parent_name:
                    parts.insert(0, str(parent_name))
                parent_id = parent.get("parent_id")

            rel_path = norm_rel("/".join(parts))
            raw_size = entry.get("file_size")
            if raw_size is None:
                raw_size = entry.get("size")
            try:
                size = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                size = None
            index[rel_path] = MszEntry(
                id=entry.get("id"),
                name=str(name),
                type=str(entry.get("type") or "file"),
                rel_path=rel_path,
                parent_id=entry.get("parent_id"),
                size=size,
                raw=entry,
            )

        return index

    def build_remote_index(self, per_page: int = 200, max_pages: int = 200) -> dict[str, RemoteFile]:
        entry_index = self.build_entry_index(per_page=per_page, max_pages=max_pages)
        return {
            rel_path: RemoteFile(id=entry.id, size=entry.size, name=entry.name, rel_path=entry.rel_path)
            for rel_path, entry in entry_index.items()
            if entry.is_file
        }

    def build_remote_index_for_folder(
        self,
        folder_path: str,
        *,
        per_page: int = 200,
        max_pages: int = 200,
        log: Any = None,
    ) -> dict[str, RemoteFile]:
        folder_path = norm_rel(folder_path)
        if not folder_path:
            return self.build_remote_index(per_page=per_page, max_pages=max_pages)

        root = self.resolve_folder_path(folder_path, per_page=per_page, max_pages=max_pages, log=log)
        if root is None:
            if log:
                log(f"Scoped remote index folder was not found: {folder_path}")
            return {}
        files = self.list_descendant_files(root, log=log)
        return {
            entry.rel_path: RemoteFile(id=entry.id, size=entry.size, name=entry.name, rel_path=entry.rel_path)
            for entry in files
            if entry.is_file
        }

    def resolve_folder_path(
        self,
        folder_path: str,
        *,
        per_page: int = 200,
        max_pages: int = 200,
        log: Any = None,
    ) -> MszEntry | None:
        parts = [part.strip() for part in norm_rel(folder_path).split("/") if part.strip()]
        if not parts:
            return None

        parent_id: Any = None
        current: MszEntry | None = None
        current_rel = ""
        for index, part in enumerate(parts):
            children = (
                self.list_entries(per_page=per_page, max_pages=max_pages)
                if parent_id is None
                else self._list_child_entries(parent_id, per_page=per_page, max_pages=max_pages)
            )
            matches = [
                child
                for child in children
                if str(child.get("type") or "") == "folder" and str(child.get("name") or "") == part
            ]
            if not matches:
                if log:
                    log(f"Scoped folder path segment was not found: {'/'.join(parts[: index + 1])}")
                return None
            raw = matches[0]
            current_rel = norm_rel(f"{current_rel}/{part}")
            current = self._entry_from_raw(raw, {raw.get("id"): raw})
            current = MszEntry(
                id=current.id,
                name=current.name,
                type=current.type,
                rel_path=current_rel,
                parent_id=current.parent_id,
                size=current.size,
                raw=current.raw,
            )
            parent_id = current.id
        return current

    def resolve_source_entries(
        self,
        *,
        source_path: str = "",
        source_id: str = "",
        source_url: str = "",
        log: Any = None,
    ) -> tuple[MszEntry, list[MszEntry]]:
        def _log(message: str) -> None:
            if log:
                log(message)

        source_id = source_id.strip()
        source_path = norm_rel(source_path)
        if not source_id and source_url:
            source_id = self._extract_entry_id_from_url(source_url)

        root: MszEntry | None = None
        if source_id:
            source_ids = self._source_id_candidates(source_id)
            _log(f"Resolving MSZ source id directly: {source_id}")
            for candidate_id in source_ids:
                if candidate_id != source_id:
                    _log(f"Trying decoded MSZ source id: {candidate_id}")
                root = self.get_entry(candidate_id)
                if root is not None:
                    break
            if root is None:
                for candidate_id in source_ids:
                    _log(f"Trying shareable-link lookup for: {candidate_id}")
                    share_root, share_children = self._resolve_shareable_source(candidate_id, log=log)
                    if share_root is not None:
                        root = share_root
                        if root.is_file:
                            return root, [root]
                        if share_children:
                            return root, self._descendant_files_from_seed(root, share_children, log=log)
                        descendants = self.list_descendant_files(root, log=log)
                        return root, descendants
            if root is None:
                for candidate_id in source_ids:
                    _log(f"Trying child-list lookup for folder id: {candidate_id}")
                    child_root, child_entries = self._resolve_folder_from_children(candidate_id, log=log)
                    if child_root is not None:
                        root = child_root
                        return root, self._descendant_files_from_seed(root, child_entries, log=log)
            if root is None:
                _log("Direct source-id lookup failed; falling back to shallow index lookup.")
                entry_index = self.build_entry_index(recursive=False, log=log)
                for candidate_id in source_ids:
                    for entry in entry_index.values():
                        raw = entry.raw or {}
                        if str(entry.id) == candidate_id or str(raw.get("hash") or "") == candidate_id:
                            root = entry
                            break
                    if root is not None:
                        break
            if root is None:
                raise RuntimeError(f"MSZ source id was not found: {source_id}")
            if root.is_file:
                return root, [root]
            descendants = self.list_descendant_files(root, log=log)
            return root, descendants

        entry_index = self.build_entry_index(recursive=False, log=log)
        if source_id:
            for entry in entry_index.values():
                if str(entry.id) == source_id:
                    root = entry
                    break
            if root is None:
                raise RuntimeError(f"MSZ source id was not found in remote index: {source_id}")
        elif source_path:
            root = entry_index.get(source_path)
            if root is None:
                candidates = [entry for path, entry in entry_index.items() if path.endswith("/" + source_path)]
                if len(candidates) == 1:
                    root = candidates[0]
            if root is None:
                raise RuntimeError(f"MSZ source path was not found or is ambiguous: {source_path}")
        else:
            raise ValueError("Provide MSZ source path, id, or URL.")

        if root.is_file:
            return root, [root]

        descendants = self.list_descendant_files(root, log=log)
        return root, descendants

    def get_entry(self, entry_id: Any) -> MszEntry | None:
        entry_id_encoded = quote(str(entry_id), safe="")
        candidates = [
            f"{self.api_base}/drive/file-entries/{entry_id_encoded}",
            f"{self.api_base}/file-entries/{entry_id_encoded}",
            f"{self.base_url}/api/v1/drive/file-entries/{entry_id_encoded}",
            f"{self.base_url}/api/drive/file-entries/{entry_id_encoded}",
        ]
        for url in candidates:
            try:
                response = requests.get(url, headers=self.headers, timeout=self.timeout)
            except requests.RequestException:
                continue
            if response.status_code >= 400:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            raw = payload.get("fileEntry") if isinstance(payload, dict) else None
            if raw is None and isinstance(payload, dict):
                raw = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if isinstance(raw, dict) and raw.get("id") is not None:
                return self._entry_from_raw(raw, {raw.get("id"): raw})
        return None

    def _get_shareable_payload(self, entry_id: Any) -> dict[str, Any] | None:
        entry_id_encoded = quote(str(entry_id), safe="")
        url = f"{self.api_base}/file-entries/{entry_id_encoded}/shareable-link"
        try:
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
        except requests.RequestException:
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _resolve_shareable_source(
        self,
        entry_id: Any,
        *,
        log: Any = None,
    ) -> tuple[MszEntry | None, list[dict[str, Any]]]:
        payload = self._get_shareable_payload(entry_id)
        if not payload:
            return None, []
        link = payload.get("link") if isinstance(payload.get("link"), dict) else {}
        raw_root = link.get("entry") if isinstance(link.get("entry"), dict) else None
        if raw_root is None and isinstance(payload.get("entry"), dict):
            raw_root = payload["entry"]
        if raw_root is None:
            return None, []
        root = self._entry_from_raw(raw_root, {raw_root.get("id"): raw_root})
        children = parse_entries(payload.get("folderChildren") or [])
        if log:
            log(f"Shareable-link API resolved {root.name} ({root.id}) with {len(children)} immediate children")
        return root, children

    def _resolve_folder_from_children(
        self,
        folder_id: Any,
        *,
        log: Any = None,
    ) -> tuple[MszEntry | None, list[dict[str, Any]]]:
        children = self._list_child_entries(folder_id, per_page=200, max_pages=50)
        if not children:
            if log:
                log(f"Child-list lookup found no entries for folder id: {folder_id}")
            return None, []

        raw_root = self._parent_from_children(folder_id, children)
        if raw_root is None:
            raw_root = {
                "id": folder_id,
                "name": f"MSZ Folder {folder_id}",
                "type": "folder",
            }
            if log:
                log(
                    "Child-list lookup found children, but no parent metadata; "
                    f"using fallback folder name: {raw_root['name']}"
                )

        root = self._entry_from_raw(raw_root, {raw_root.get("id"): raw_root})
        if log:
            log(f"Child-list API resolved {root.name} ({root.id}) with {len(children)} immediate children")
        return root, children

    @staticmethod
    def _parent_from_children(folder_id: Any, children: list[dict[str, Any]]) -> dict[str, Any] | None:
        for child in children:
            parent = child.get("parent")
            if isinstance(parent, dict) and str(parent.get("id")) == str(folder_id):
                return parent
        return None

    def _descendant_files_from_seed(
        self,
        root: MszEntry,
        seed_children: list[dict[str, Any]],
        *,
        log: Any = None,
    ) -> list[MszEntry]:
        files: list[MszEntry] = []
        queue: list[tuple[MszEntry, str]] = []
        visited_folders: set[Any] = {root.id}

        def _raw_sort_key(raw: dict[str, Any]) -> tuple[tuple[int, object], ...]:
            return natural_sort_key(raw.get("name") or raw.get("file_name") or raw.get("id") or "")

        def _append_child(raw: dict[str, Any], rel_prefix: str) -> None:
            child = self._entry_from_raw(raw, {raw.get("id"): raw})
            child_rel = norm_rel(f"{rel_prefix}/{child.name}")
            child = MszEntry(
                id=child.id,
                name=child.name,
                type=child.type,
                rel_path=norm_rel(f"{root.rel_path}/{child_rel}"),
                parent_id=child.parent_id,
                size=child.size,
                raw=child.raw,
            )
            if child.is_folder:
                queue.append((child, child_rel))
            else:
                files.append(child)

        for raw in sorted(seed_children, key=_raw_sort_key):
            _append_child(raw, "")

        while queue:
            folder, rel_prefix = queue.pop(0)
            if folder.id in visited_folders:
                continue
            visited_folders.add(folder.id)
            if log:
                log(f"Listing MSZ folder via API: {folder.name} ({folder.id})")
            children = self._list_child_entries(folder.id, per_page=200, max_pages=50)
            if log:
                log(f"Found {len(children)} child entries in: {folder.name}")
            for raw in sorted(children, key=_raw_sort_key):
                _append_child(raw, rel_prefix)
        files.sort(key=lambda entry: natural_sort_key(entry.rel_path))
        return files

    def _entry_from_raw(self, raw: dict[str, Any], id_map: dict[Any, dict[str, Any]]) -> MszEntry:
        name = str(raw.get("name") or raw.get("file_name") or raw.get("id"))
        parts = [name]
        original_parent_id = raw.get("parent_id") or raw.get("parentId")
        parent_id = original_parent_id
        visited: set[Any] = set()
        while parent_id and parent_id in id_map and parent_id not in visited:
            visited.add(parent_id)
            parent = id_map[parent_id]
            parent_name = parent.get("name")
            if parent_name:
                parts.insert(0, str(parent_name))
            parent_id = parent.get("parent_id") or parent.get("parentId")
        raw_size = raw.get("file_size")
        if raw_size is None:
            raw_size = raw.get("size")
        try:
            size = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            size = None
        return MszEntry(
            id=raw.get("id"),
            name=name,
            type=str(raw.get("type") or "file"),
            rel_path=norm_rel("/".join(parts)),
            parent_id=original_parent_id,
            size=size,
            raw=raw,
        )

    def list_descendant_files(self, root: MszEntry, *, log: Any = None) -> list[MszEntry]:
        def _log(message: str) -> None:
            if log:
                log(message)

        def _raw_sort_key(raw: dict[str, Any]) -> tuple[tuple[int, object], ...]:
            return natural_sort_key(raw.get("name") or raw.get("file_name") or raw.get("id") or "")

        files: list[MszEntry] = []
        queue: list[tuple[MszEntry, str]] = [(root, "")]
        visited: set[Any] = set()
        while queue:
            folder, rel_prefix = queue.pop(0)
            if folder.id in visited:
                continue
            visited.add(folder.id)
            _log(f"Listing MSZ folder: {folder.name} ({folder.id})")
            children_raw = self._list_child_entries(folder.id, per_page=200, max_pages=50)
            _log(f"Found {len(children_raw)} child entries in: {folder.name}")
            id_map = {child.get("id"): child for child in children_raw if child.get("id") is not None}
            id_map[folder.id] = folder.raw or {"id": folder.id, "name": folder.name, "type": "folder"}
            for child_raw in sorted(children_raw, key=_raw_sort_key):
                child = self._entry_from_raw(child_raw, id_map)
                child_rel = norm_rel(f"{rel_prefix}/{child.name}")
                child = MszEntry(
                    id=child.id,
                    name=child.name,
                    type=child.type,
                    rel_path=norm_rel(f"{root.rel_path}/{child_rel}"),
                    parent_id=child.parent_id,
                    size=child.size,
                    raw=child.raw,
                )
                if child.is_folder:
                    queue.append((child, child_rel))
                else:
                    files.append(child)
        files.sort(key=lambda entry: natural_sort_key(entry.rel_path))
        return files

    @staticmethod
    def _extract_entry_id_from_url(url: str) -> str:
        stripped = url.strip().rstrip("/")
        if not stripped:
            return ""
        parsed = urlparse(stripped)
        path_parts = [part for part in parsed.path.split("/") if part]
        for marker in ("folders", "files", "file-entries", "entries"):
            if marker in path_parts:
                marker_index = path_parts.index(marker)
                if marker_index + 1 < len(path_parts):
                    return path_parts[marker_index + 1]
        return path_parts[-1] if path_parts else stripped.split("/")[-1].split("?")[0]

    @staticmethod
    def _source_id_candidates(source_id: str) -> list[str]:
        candidates = [source_id]
        padded = source_id + "=" * (-len(source_id) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
        except (binascii.Error, ValueError, OSError, UnicodeError):
            decoded = ""
        if "|" in decoded:
            decoded_id = decoded.split("|", 1)[0].strip()
            if decoded_id and decoded_id not in candidates:
                candidates.append(decoded_id)
        return candidates

    @staticmethod
    def rel_path_under(root: MszEntry, entry: MszEntry) -> str:
        if root.is_file:
            return entry.name
        prefix = root.rel_path.rstrip("/") + "/"
        if entry.rel_path.startswith(prefix):
            return norm_rel(entry.rel_path[len(prefix):])
        return entry.name

    def download_file(
        self,
        entry: MszEntry,
        output_path: Path,
        *,
        chunk_size: int = 1024 * 1024,
        max_retries: int = 5,
        progress_callback: Any = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        downloaded = temp_path.stat().st_size if temp_path.exists() else 0

        candidates = self._download_url_candidates(entry)
        last_error = None
        for attempt in range(1, max_retries + 1):
            headers = dict(self.headers)
            mode = "wb"
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
                mode = "ab"
            for url in candidates:
                try:
                    response = requests.get(
                        url,
                        headers=headers,
                        stream=True,
                        timeout=max(self.timeout, 180),
                    )
                    if response.status_code == 416 and temp_path.exists():
                        temp_path.replace(output_path)
                        return output_path
                    if response.status_code not in (200, 206):
                        last_error = f"HTTP {response.status_code} from {url}: {response.text[:400]}"
                        continue
                    if "application/json" in response.headers.get("content-type", "").lower():
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = None
                        redirect_url = self._extract_download_url(payload)
                        response.close()
                        if not redirect_url:
                            last_error = f"Download API returned JSON without a file URL from {url}: {str(payload)[:400]}"
                            continue
                        response = requests.get(
                            redirect_url,
                            headers=headers,
                            stream=True,
                            timeout=max(self.timeout, 180),
                        )
                        if response.status_code not in (200, 206):
                            last_error = (
                                f"HTTP {response.status_code} from resolved download URL "
                                f"{redirect_url}: {response.text[:400]}"
                            )
                            continue
                    if downloaded > 0 and response.status_code == 200:
                        mode = "wb"
                        downloaded = 0
                    with temp_path.open(mode + "") as handle:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if chunk:
                                handle.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback is not None:
                                    progress_callback(downloaded, entry.size)
                    actual_size = temp_path.stat().st_size
                    if entry.size is not None and actual_size != entry.size:
                        downloaded = actual_size
                        last_error = f"Partial MSZ download: got {actual_size}, expected {entry.size}"
                        continue
                    temp_path.replace(output_path)
                    return output_path
                except (OSError, requests.RequestException) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                time.sleep(min(60.0, 2.0**attempt))
        raise RuntimeError(last_error or f"Failed to download MSZ file: {entry.rel_path}")

    @staticmethod
    def _extract_download_url(payload: Any) -> str:
        if isinstance(payload, str):
            return payload if payload.startswith("http") else ""
        if isinstance(payload, list):
            for item in payload:
                value = MszApiClient._extract_download_url(item)
                if value:
                    return value
            return ""
        if not isinstance(payload, dict):
            return ""
        for key in (
            "download_url",
            "downloadUrl",
            "download",
            "url",
            "signedUrl",
            "signed_url",
            "link",
            "href",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for value in payload.values():
            nested = MszApiClient._extract_download_url(value)
            if nested:
                return nested
        return ""

    def _download_url_candidates(self, entry: MszEntry) -> list[str]:
        raw = entry.raw or {}
        urls = []
        for key in (
            "download_url",
            "downloadUrl",
            "url",
            "link",
            "file_url",
            "fileUrl",
        ):
            value = raw.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
            elif isinstance(value, str) and value and not value.startswith(("http", "/api")):
                urls.append(urljoin(self.base_url + "/", value.lstrip("/")))
        entry_id = quote(str(entry.id), safe="")
        entry_hash = raw.get("hash")
        if entry_hash:
            entry_hash = quote(str(entry_hash), safe="")
        urls.extend(
            [
                f"{self.api_base}/file-entries/download?hashes={entry_id}",
                f"{self.api_base}/file-entries/download?entryIds={entry_id}",
                f"{self.api_base}/drive/file-entries/{entry_id}/download",
                f"{self.api_base}/file-entries/{entry_id}/download",
                f"{self.api_base}/download/{entry_id}",
                f"{self.base_url}/api/v1/drive/file-entries/{entry_id}/download",
                f"{self.base_url}/api/drive/file-entries/{entry_id}/download",
                f"{self.base_url}/drive/file-entries/{entry_id}/download",
                f"{self.base_url}/download/{entry_id}",
            ]
        )
        if entry_hash:
            urls.extend(
                [
                    f"{self.api_base}/file-entries/download?hashes={entry_hash}",
                    f"{self.base_url}/download/{entry_hash}",
                    f"{self.base_url}/drive/files/{entry_hash}",
                ]
            )
        deduped = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        return deduped

    def upload_file(self, path: Path, rel_path: str, max_retries: int = 5) -> Any | None:
        rel_path = norm_rel(rel_path)
        file_name = Path(rel_path).name
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        last_error = None
        non_retryable = {400, 401, 403, 404, 405, 422}

        for attempt in range(1, max_retries + 1):
            try:
                with path.open("rb") as handle:
                    response = requests.post(
                        self.api_base + "/uploads",
                        headers=self.headers,
                        data={"relativePath": rel_path},
                        files={"file": (file_name, handle, mime_type)},
                        timeout=max(self.timeout, 180),
                    )
                if response.status_code in (200, 201):
                    try:
                        payload = response.json()
                    except ValueError:
                        return None
                    return (payload.get("fileEntry") or {}).get("id")

                last_error = f"HTTP {response.status_code}: {response.text[:800]}"
                if response.status_code in non_retryable:
                    raise RuntimeError(last_error)
            except (OSError, requests.RequestException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < max_retries:
                time.sleep(min(60.0, 2.0**attempt))

        raise RuntimeError(last_error or "Upload failed")


def ensure_disk_space(path: Path, required_bytes: int, multiplier: float = 1.1) -> None:
    usage = os.statvfs(str(path))
    free = usage.f_bavail * usage.f_frsize
    needed = int(required_bytes * multiplier)
    if free < needed:
        raise RuntimeError(
            f"Insufficient free disk space at {path}: need at least {needed} bytes, have {free} bytes"
        )
