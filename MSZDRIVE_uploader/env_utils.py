from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_msz_env_files() -> None:
    for env_path in (Path("MSZDRIVE_uploader/.env"), Path(".env"), Path("heroku_bot/.env")):
        if env_path.exists():
            load_dotenv(env_path, override=False)
    apply_msz_telegram_session_alias()


def apply_msz_telegram_session_alias() -> None:
    session_string = os.getenv("TG_SESSION_STRING_MSZ", "").strip()
    if session_string:
        os.environ["TG_SESSION_STRING"] = session_string
