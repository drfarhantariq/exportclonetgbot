from __future__ import annotations

import getpass
import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def prompt_env(name: str, secret: bool = False) -> str:
    current = os.getenv(name, "").strip()
    if current:
        return current

    prompt = f"{name}: "
    return getpass.getpass(prompt) if secret else input(prompt).strip()


def main() -> None:
    load_dotenv()

    api_id = int(prompt_env("TG_API_ID"))
    api_hash = prompt_env("TG_API_HASH", secret=True)

    print("Starting interactive Telethon login...")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()

    print()
    print("Telethon session string:")
    print(session_string)
    print()
    print("Use this for your leech bot if that bot is Telethon-based.")
    print("Keep it separate from the local cloner session.")


if __name__ == "__main__":
    main()

