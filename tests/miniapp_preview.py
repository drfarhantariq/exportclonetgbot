"""Local-only browser QA fixture. No production credentials or auth bypass."""
import asyncio
import json
import shlex
import time
from pathlib import Path
from urllib.parse import quote
from types import SimpleNamespace
from pyrogram import raw

from aiohttp import web
from test_miniapp import fixture_server, signed_data, USER, bot_engine


async def main():
    server, documents = fixture_server()
    engine = server.engine
    settings = dict(bot_engine.BOT_SETTINGS_DEFAULTS)
    settings.update(tg_api_hash="fixture-secret", msz_api_token="fixture-msz-token")
    engine.SETTINGS_CATEGORY_ORDER = bot_engine.SETTINGS_CATEGORY_ORDER
    engine.SETTINGS_CATEGORY_TITLES = bot_engine.SETTINGS_CATEGORY_TITLES
    engine.SETTINGS_CATEGORIES = bot_engine.SETTINGS_CATEGORIES
    engine.SECRET_SETTING_KEYS = bot_engine.SECRET_SETTING_KEYS
    engine._settings_key_label = bot_engine._settings_key_label
    engine._load_bot_settings.return_value = settings
    documents["clone:last"] = dict(job_id="clone-live", phase="running", total_messages=959, success=25,
                                   skipped=0, failed=0, started_at=time.time() - 1140,
                                   current_file_name="Varicocele atf.mp4", transfer_stage="download",
                                   download_current=7937720, download_total=38660997, download_speed_bps=22628270,
                                   payload={"source_chat_title": "Ss all in 1", "source_topic_title": "Prep Ss 2023",
                                            "destination_chat_title": "PG NEET SS", "destination_topic_title": "Prep Ss 2023"})
    documents["transfer:last"] = dict(job_id="transfer-live", phase="running", stage="uploading", processed=42,
                                      uploaded=38, skipped=4, failed=0, total_files=120, started_at=time.time() - 720,
                                      source="MSZ Drive / Anatomy lectures", destination="Google Drive / PG NEET SS",
                                      file_name="11. SK-22 Paper-4B.mp4", bytes_done=78123125, file_size=126353408,
                                      speed_bps=10242048, last_progress_at=time.time())
    history = []
    for i, kind in enumerate(("clone", "transfer", "index", "export", "clone")):
        state = dict(job_id=f"history-{i}", phase="completed", success=160, skipped=151, processed=311,
                     total_files=311, processed_messages=311, total_messages=311,
                     source="Google Drive / Lecture archives", destination="Telegram / Medicine",
                     started_at=time.time()-4000, finished_at=time.time()-828,
                     payload={"source_link": "Surgery / Endocrinology", "destination_link": "PG NEET SS / Surgery",
                              "topic_link": "Medicine / Topic collection"})
        history.append({"kind": kind, "state": state})
    documents["status:history"] = {"jobs": history}
    engine._clone_pending_jobs.extend([dict(job_id=f"queued-{i}", phase="queued",
                                          payload={"source_link": name, "destination_link": "PG NEET SS / Prep"})
                                       for i, name in enumerate(("Anatomy / Upper limb", "Physiology / Neurophysiology"))])
    engine.ACTIVE_CLONE_TASK = asyncio.create_task(asyncio.Event().wait())
    engine.ACTIVE_TRANSFER_TASK = asyncio.create_task(asyncio.Event().wait())

    async def handler(bot, message):
        kind = message.text.split()[0].lstrip("/")
        if kind == "settings":
            tokens = message.text.split(maxsplit=3)
            if tokens[1] == "set":
                settings[tokens[2]] = bot_engine._normalize_setting_value(tokens[2], tokens[3])
        elif kind in {"clone", "transfer"}:
            source = shlex.split(message.text)[1:]
            engine._clone_pending_jobs.append(dict(job_id=f"submitted-{len(engine._clone_pending_jobs)}", payload={"source_link": " ".join(source)}))
        await message.reply_text("Fixture action accepted.")

    for handler_mock in server.handlers.values():
        handler_mock.side_effect = handler
    async def catalog_telegram(parent, *_):
        if parent == "root":
            return {"items": [{"id": "-100123", "name": "PG NEET SS", "kind": "chat", "expandable": True,
                "forum": True, "description": "Forum · choose a topic", "can_select": False}], "next": None}
        return {"items": [{"id": "-100123:10", "name": "Anatomy", "kind": "topic", "expandable": False,
            "description": "Topic #10", "source": "https://t.me/c/123/10/10", "destination": "https://t.me/c/123/10/10",
            "topic": True, "can_select": True}], "next": None}
    def catalog_cloud(parent, *_):
        name = "Lecture archives" if parent == "root" else "Surgery"
        fid = "folder1" if parent == "root" else "folder2"
        return {"items": [{"id": fid, "name": name, "kind": "folder", "expandable": parent == "root",
            "description": name, "source": "https://drive.google.com/drive/folders/" + fid,
            "destination": "gdrive:" + fid, "can_select": True, "writable": True}], "next": None}
    def catalog_msz(parent, *_):
        data = catalog_cloud(parent)
        for item in data["items"]:
            item["source"] = "msz:https://cloud.example/drive/folders/" + item["id"]
            item["destination"] = "msz:" + ("Lecture archives" if parent == "root" else "Lecture archives/Surgery")
        return data
    server.catalog.telegram_items = catalog_telegram
    server.catalog.drive_items = catalog_cloud
    server.catalog.msz_items = catalog_msz
    async def topic_invoke(query, **_):
        if isinstance(query, raw.functions.channels.GetChannels):
            return SimpleNamespace(chats=[SimpleNamespace(forum=True)])
        return SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=45,
            action=raw.types.MessageActionTopicCreate(title=query.title, icon_color=0x6FB9F0)))])
    async def topic_peer(_):
        return SimpleNamespace(channel_id=123, access_hash=1)
    async def topic_client(*_):
        return SimpleNamespace(resolve_peer=topic_peer, invoke=topic_invoke)
    server.catalog.telegram_client = topic_client
    runner = web.AppRunner(server.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8089).start()
    url = "http://127.0.0.1:8089/#tgWebAppData=" + quote(signed_data(), safe="") + "&tgWebAppVersion=9.0&tgWebAppPlatform=tdesktop"
    output = Path("output/playwright")
    output.mkdir(parents=True, exist_ok=True)
    (output / "fixture-url.txt").write_text(url, encoding="utf-8")
    print("Local QA fixture ready on 127.0.0.1:8089", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
