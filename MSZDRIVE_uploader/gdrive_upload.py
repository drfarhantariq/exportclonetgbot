from __future__ import annotations

import mimetypes
import pickle
import time
from pathlib import Path
from typing import Callable


GDRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveResumableUploader:
    def __init__(
        self,
        token_pickle: Path,
        *,
        set_public_permission: bool = False,
        chunk_size: int = 100 * 1024 * 1024,
    ) -> None:
        self.token_pickle = token_pickle.expanduser().resolve()
        self.set_public_permission = set_public_permission
        self.chunk_size = chunk_size
        self.service = self._authorize()
        self._folder_cache: dict[tuple[str, str], str] = {}

    def _authorize(self):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Google Drive dependencies are missing. Install google-api-python-client, "
                "google-auth-httplib2, and google-auth-oauthlib."
            ) from exc

        if not self.token_pickle.exists():
            raise FileNotFoundError(f"Google Drive token pickle not found: {self.token_pickle}")
        with self.token_pickle.open("rb") as handle:
            credentials = pickle.load(handle)
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    @staticmethod
    def _escape_query_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def find_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> dict | None:
        query = [
            f"'{parent_id}' in parents",
            "trashed = false",
            f"name = '{self._escape_query_value(name)}'",
        ]
        if mime_type:
            query.append(f"mimeType = '{mime_type}'")
        response = (
            self.service.files()
            .list(
                q=" and ".join(query),
                spaces="drive",
                pageSize=10,
                fields="files(id, name, mimeType, size)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = response.get("files", [])
        return files[0] if files else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        cache_key = (parent_id, name)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        existing = self.find_child(parent_id, name, mime_type=GDRIVE_FOLDER_MIME)
        if existing:
            folder_id = existing["id"]
            self._folder_cache[cache_key] = folder_id
            return folder_id

        metadata = {
            "name": name,
            "description": "Uploaded by MSZ reverse sync",
            "mimeType": GDRIVE_FOLDER_MIME,
            "parents": [parent_id],
        }
        folder = (
            self.service.files()
            .create(body=metadata, supportsAllDrives=True, fields="id")
            .execute()
        )
        folder_id = folder["id"]
        self._folder_cache[cache_key] = folder_id
        if self.set_public_permission:
            self.set_permission(folder_id)
        return folder_id

    def ensure_folder_path(self, root_folder_id: str, rel_folder: str) -> str:
        current_id = root_folder_id
        for segment in [part for part in rel_folder.replace("\\", "/").split("/") if part.strip()]:
            current_id = self.ensure_folder(current_id, segment.strip())
        return current_id

    def existing_file_matches(self, parent_id: str, name: str, size: int | None) -> dict | None:
        existing = self.find_child(parent_id, name)
        if not existing:
            return None
        if existing.get("mimeType") == GDRIVE_FOLDER_MIME:
            return None
        if size is None:
            return existing
        try:
            return existing if int(existing.get("size", -1)) == int(size) else None
        except (TypeError, ValueError):
            return None

    def upload_file(
        self,
        file_path: Path,
        *,
        parent_id: str,
        file_name: str,
        size: int | None,
        progress_callback: Callable[[int, int | None], None] | None = None,
        max_retries: int = 10,
    ) -> str:
        try:
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise RuntimeError("google-api-python-client is required for Google Drive upload.") from exc

        existing = self.existing_file_matches(parent_id, file_name, size)
        if existing:
            return existing["id"]

        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        metadata = {
            "name": file_name,
            "description": "Uploaded by MSZ reverse sync",
            "mimeType": mime_type,
            "parents": [parent_id],
        }
        if file_path.stat().st_size == 0:
            media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=False)
            response = self.service.files().create(
                body=metadata,
                media_body=media,
                supportsAllDrives=True,
                fields="id, name, size",
            ).execute()
            file_id = response["id"]
            if self.set_public_permission:
                self.set_permission(file_id)
            return file_id

        media = MediaFileUpload(
            str(file_path),
            mimetype=mime_type,
            resumable=True,
            chunksize=self.chunk_size,
        )
        request = self.service.files().create(
            body=metadata,
            media_body=media,
            supportsAllDrives=True,
            fields="id, name, size",
        )
        response = None
        retries = 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status is not None and progress_callback is not None:
                    uploaded = int(status.total_size * status.progress())
                    progress_callback(uploaded, status.total_size)
            except HttpError as exc:
                if exc.resp.status in {429, 500, 502, 503, 504} and retries < max_retries:
                    retries += 1
                    time.sleep(min(60.0, 2.0**min(retries, 6)))
                    continue
                raise
        file_id = response["id"]
        if self.set_public_permission:
            self.set_permission(file_id)
        verified = self.service.files().get(
            fileId=file_id,
            supportsAllDrives=True,
            fields="id, size",
        ).execute()
        if size is not None and int(verified.get("size", -1)) != int(size):
            raise RuntimeError(f"Google Drive size mismatch for {file_name}: {verified.get('size')} != {size}")
        return file_id

    def set_permission(self, file_id: str) -> None:
        permissions = {
            "role": "reader",
            "type": "anyone",
            "value": None,
            "withLink": True,
        }
        self.service.permissions().create(
            fileId=file_id,
            body=permissions,
            supportsAllDrives=True,
        ).execute()
