"""Transfer settings validation without exposing credential contents."""
from __future__ import annotations

import json
import os
import io
import pickle
import datetime
from urllib.parse import urlparse

ENV_KEYS = {
    "msz_base_url": "MSZ_BASE_URL",
    "msz_email": "MSZ_EMAIL",
    "msz_password": "MSZ_PASSWORD",
    "msz_api_token": "MSZ_API_TOKEN",
    "msz_target_folder": "MSZ_TARGET_FOLDER",
    "gdrive_token_json": "GDRIVE_TOKEN_JSON",
    "gdrive_folder_id": "GDRIVE_FOLDER_ID",
    "telegram_target_topic_link": "TELEGRAM_TARGET_TOPIC_LINK",
}
SECRET_KEYS = {"msz_email", "msz_password", "msz_api_token", "gdrive_token_json"}
MAX_UPLOAD_BYTES = 64 * 1024

# key: (label, enabled flag, disabled flag, default)
TOGGLE_OPTIONS = {
    "transfer_default_dry_run": ("Dry run", "--dry-run", "--no-dry-run", False),
    "transfer_default_resume": ("Resume previous successes", "--resume", "--no-resume", False),
    "transfer_default_continue_on_error": ("Continue on error", "--continue-on-error", "--no-continue-on-error", False),
    "transfer_default_retry_failed_only": ("Retry failed files only", "--retry-failed-only", "--no-retry-failed-only", False),
    "transfer_default_keep_downloads": ("Keep downloaded files", "--keep-downloads", "--no-keep-downloads", False),
    "transfer_default_delete_failed_downloads": ("Delete failed downloads", "--delete-failed-downloads", "--no-delete-failed-downloads", False),
    "transfer_default_caption_file_names": ("Use captions as filenames", "--caption-file-names", "--no-caption-file-names", False),
    "transfer_default_onwards": ("Read messages onwards", "--onwards", "--no-onwards", False),
    "transfer_default_above": ("Files above each heading", "--above", "--no-above", False),
    "transfer_default_verify_remote": ("Verify remote upload", "--verify-remote", "--no-verify-remote", False),
    "transfer_default_strict_browser_verify": ("Strict browser verification", "--strict-browser-verify", "--no-strict-browser-verify", False),
    "transfer_default_browser_folder_title": ("Use browser folder title", "--browser-folder-title", "--no-browser-folder-title", True),
    "transfer_default_browser_headed": ("Visible browser window", "--browser-headed", "--no-browser-headed", False),
    "transfer_default_generate_index": ("Generate Telegram folder index", "--index", "--no-index", False),
}
VALUE_OPTIONS = {
    "transfer_default_batch_size": ("Batch size", "--batch-size", 50),
    "transfer_default_batch_delay_sec": ("Batch delay (seconds)", "--batch-delay-sec", 1.0),
    "transfer_default_tg_download": ("Telegram download mode", "--tg-download", os.getenv("TG_DOWNLOAD_MODE", "hyper")),
    "transfer_default_browser_folder_url": ("Browser destination folder URL", "--browser-folder-url", os.getenv("MSZ_BROWSER_FOLDER_URL", "")),
    "transfer_default_chromium_executable": ("Chromium executable path", "--chromium-executable", os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")),
}
OPTION_DEFAULTS = {key: value[3] for key, value in TOGGLE_OPTIONS.items()}
OPTION_DEFAULTS.update({key: value[2] for key, value in VALUE_OPTIONS.items()})
OPTION_LABELS = {key: value[0] for key, value in {**TOGGLE_OPTIONS, **VALUE_OPTIONS}.items()}


def apply_defaults(tokens: list[str], settings: dict | None) -> list[str]:
    """Capture defaults at submission; explicit positive/negative flags win."""
    if settings is None:
        return list(tokens)
    result = list(tokens)
    explicit = {token.split("=", 1)[0] for token in tokens if token.startswith("--")}
    for key, (_, enabled, disabled, default) in TOGGLE_OPTIONS.items():
        if enabled in explicit or disabled in explicit:
            continue
        value = settings.get(key, default)
        if key == "transfer_default_generate_index":
            # This operation applies only to Telegram links, not edited indexes.
            from urllib.parse import urlparse
            host = urlparse(tokens[0]).netloc.lower() if tokens else ""
            if host not in {"t.me", "telegram.me", "www.t.me"} or "--index-done" in explicit:
                continue
        result.append(enabled if value else disabled)
    for key, (_, flag, default) in VALUE_OPTIONS.items():
        value = settings.get(key, default)
        if flag not in explicit and value != "":
            result.extend([flag, str(value)])
    return result


class _CredentialState:
    """Inert stand-in: uploaded pickle classes never run their own code."""

    def __new__(cls, *args):
        return object.__new__(cls)


class _TokenUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in {
            ("google.oauth2.credentials", "Credentials"),
            ("oauth2client.client", "OAuth2Credentials"),
            ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryManager"),
            ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryData"),
            ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryRefreshManager"),
            ("google.auth._refresh_worker", "RefreshThreadManager"),
        }:
            return _CredentialState
        if module == "datetime" and name in {"datetime", "timedelta", "timezone"}:
            return getattr(datetime, name)
        raise pickle.UnpicklingError("Unsupported token object")


def _pickle_oauth_json(content: bytes) -> str:
    try:
        token = _TokenUnpickler(io.BytesIO(content)).load()
        if type(token) is not _CredentialState:
            raise ValueError()
        state = vars(token)
        info = {"type": "authorized_user"}
        for key in ("client_id", "client_secret", "refresh_token", "token_uri", "scopes"):
            value = state.get("_" + key, state.get(key))
            if value is not None:
                info[key] = value
        return normalize("gdrive_token_json", json.dumps(info))
    except Exception:
        raise ValueError("Upload a valid Google OAuth token.pickle containing client_id, client_secret and refresh_token.") from None


def normalize(key: str, value: object) -> str:
    text = str(value) if value is not None else ""
    if key == "msz_password":
        return text
    text = text.strip()
    if key == "gdrive_token_json" and text:
        try:
            info = json.loads(text)
        except (ValueError, TypeError):
            raise ValueError("Google Drive credentials must be valid OAuth JSON.") from None
        required = ("client_id", "client_secret", "refresh_token")
        if not isinstance(info, dict) or any(not isinstance(info.get(k), str) or not info[k].strip() for k in required):
            raise ValueError("Use authorized-user OAuth JSON containing client_id, client_secret and refresh_token.")
        if info.get("type", "authorized_user") != "authorized_user":
            raise ValueError("Use authorized-user OAuth JSON, not a service-account or client-secret file.")
        return json.dumps(info, separators=(",", ":"))
    if key == "msz_base_url":
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MSZ base URL must be an http or https URL.")
        return text.rstrip("/")
    if key == "telegram_target_topic_link" and text:
        parsed = urlparse(text)
        if parsed.scheme != "https" or parsed.netloc not in {"t.me", "telegram.me"}:
            raise ValueError("Use an https Telegram topic link.")
    return text


def decode_upload(key: str, content: bytes) -> dict[str, str]:
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Settings files must be smaller than 64 KB.")
    if key == "gdrive_token_pickle":
        return {"gdrive_token_json": _pickle_oauth_json(content)}
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Upload a UTF-8 text or JSON file.") from None
    if key == "msz_credentials":
        try:
            info = json.loads(text)
        except ValueError:
            raise ValueError("MSZ credentials must be a JSON object with email, password and/or api_token.") from None
        if not isinstance(info, dict):
            raise ValueError("MSZ credentials must be a JSON object.")
        values = {}
        for setting in ("msz_email", "msz_password", "msz_api_token"):
            aliases = (setting, ENV_KEYS[setting], setting.removeprefix("msz_"))
            present = next((name for name in aliases if name in info), None)
            if present is not None:
                if not isinstance(info[present], str):
                    raise ValueError(f"{setting} must be text.")
                values[setting] = normalize(setting, info[present])
        if not values:
            raise ValueError("No MSZ email, password or api_token found in this file.")
        return values
    if key not in ENV_KEYS:
        raise ValueError("Choose a transfer setting to upload.")
    return {key: normalize(key, text.rstrip("\r\n"))}
