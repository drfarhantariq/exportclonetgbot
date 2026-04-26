from __future__ import annotations

import asyncio
import getpass
import os

from dotenv import load_dotenv
from pyrogram import Client


def prompt_env(name: str, secret: bool = False) -> str:
    current = os.getenv(name, "").strip()
    if current:
        return current

    prompt = f"{name}: "
    return getpass.getpass(prompt) if secret else input(prompt).strip()


async def main() -> None:
    load_dotenv()

    api_id = int(prompt_env("TG_API_ID"))
    api_hash = prompt_env("TG_API_HASH", secret=True)

    print("Starting interactive Pyrogram login...")
    async with Client(
        "session-generator",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        session_string = await app.export_session_string()

    print()
    print("Pyrogram session string:")
    print(session_string)
    print()
    print("Use this for the local cloner app as TG_SESSION_STRING.")
    print("Do not reuse the same session string in your leech bot.")


if __name__ == "__main__":
    asyncio.run(main())

