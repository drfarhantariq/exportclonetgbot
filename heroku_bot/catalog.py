"""Read-only account catalog for Telegram topics and cloud folder pickers."""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from aiohttp import web
from pyrogram import Client, raw
from pyrogram.errors import FloodWait

TTL = 15 * 60
PROVIDERS = {"telegram", "gdrive", "msz"}


class AccountCatalog:
    def __init__(self, server):
        self.server = server
        self.cache, self.jobs, self.running = OrderedDict(), OrderedDict(), {}
        self.locks = {name: asyncio.Lock() for name in PROVIDERS}
        self.telegram, self.telegram_key = None, None
        self.msz_index, self.msz_key, self.msz_time = None, None, 0
        self.msz_client = None
        self.cooldowns = {}

    async def close(self):
        if self.telegram:
            await self.telegram.stop()
            self.telegram = None

    async def request(self, request):
        body = await request.json()
        provider = body.get("provider")
        if provider not in PROVIDERS:
            raise ValueError("Choose Telegram, Google Drive, or MSZ Cloud.")
        parent = str(body.get("parent") or "root")
        cursor = str(body.get("cursor") or "")
        if len(parent) > 160 or len(cursor) > 4096:
            raise ValueError("Invalid folder or page.")
        if parent != "root" and not re.fullmatch(r"[-\w]+", parent):
            raise ValueError("Invalid folder or chat ID.")
        settings = await self.server.engine._load_bot_settings(self.server.store)
        fingerprint = self.fingerprint(provider, settings)
        key = (provider, fingerprint, parent, cursor)
        cached = self.cache.get(key)
        if cached and not body.get("refresh") and time.time() - cached["indexed_at"] < TTL:
            self.cache.move_to_end(key)
            return web.json_response(dict(cached, cached=True, status="ready"))
        if key in self.running:
            self.jobs[self.running[key]].setdefault("subscribers", set()).add(request["user"]["id"])
            return web.json_response({"status": "loading", "job": self.running[key]}, status=202)
        wait = int(self.cooldowns.get(provider, 0) - time.time())
        if wait > 0:
            return web.json_response({"error": f"{provider.title()} requested a wait. Try again in {wait}s.", "retry_after": wait}, status=429)
        if len(self.running) >= 12:
            return web.json_response({"error": "Catalog is busy. Try again shortly."}, status=429)
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"status": "loading", "user_id": request["user"]["id"]}
        self.running[key] = job_id
        while len(self.jobs) > 100:
            oldest = next((jid for jid, value in self.jobs.items() if value["status"] != "loading"), None)
            if oldest is None:
                break
            del self.jobs[oldest]
        self.server.track(self.build(job_id, key, settings, bool(body.get("refresh"))))
        return web.json_response({"status": "loading", "job": job_id}, status=202)

    async def status(self, request):
        job = self.jobs.get(request.query.get("job", ""))
        if not job or (job["user_id"] != request["user"]["id"] and request["user"]["id"] not in job.get("subscribers", set())):
            raise ValueError("Catalog request not found. Open the picker again.")
        return web.json_response({k: v for k, v in job.items() if k not in {"user_id", "subscribers"}})

    @staticmethod
    def fingerprint(provider, settings):
        names = {"telegram": ("tg_api_id", "tg_api_hash", "tg_session_string"),
                 "gdrive": ("gdrive_token_json", "gdrive_token_pickle"),
                 "msz": ("msz_base_url", "msz_api_token")}[provider]
        defaults = {"tg_api_id": "TG_API_ID", "tg_api_hash": "TG_API_HASH", "tg_session_string": "TG_SESSION_STRING",
                    "gdrive_token_json": "GDRIVE_TOKEN_JSON", "gdrive_token_pickle": "GDRIVE_TOKEN_PICKLE",
                    "msz_base_url": "MSZ_BASE_URL", "msz_api_token": "MSZ_API_TOKEN"}
        values = [settings.get(name) or os.getenv(defaults[name], "") for name in names]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    async def build(self, job_id, key, settings, refresh):
        provider, fingerprint, parent, cursor = key
        job = self.jobs[job_id]
        try:
            async with self.locks[provider]:
                if self.cooldowns.get(provider, 0) > time.time():
                    raise ValueError("Telegram requested a wait. Please try again later.")
                if provider == "telegram":
                    data = await self.telegram_items(parent, settings, fingerprint)
                elif provider == "gdrive":
                    data = await asyncio.to_thread(self.drive_items, parent, cursor, settings)
                else:
                    data = await asyncio.to_thread(self.msz_items, parent, settings, fingerprint, refresh)
            data.update(provider=provider, parent=parent, indexed_at=time.time(), cached=False)
            self.cache[key] = data
            while len(self.cache) > 150:
                self.cache.popitem(last=False)
            job.update(status="ready", data=data)
        except FloodWait as exc:
            wait = int(exc.value) + 1
            self.cooldowns[provider] = time.time() + wait
            job.update(status="failed", error=f"Telegram requested a flood wait. Try again in {wait}s.", retry_after=wait)
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            job.update(status="failed", error=str(exc)[:200])
        except Exception:
            # Provider exceptions can contain credential-bearing URLs or headers.
            job.update(status="failed", error=f"Could not read {provider.title()}. Check its saved credentials and account access, then retry.")
        finally:
            self.running.pop(key, None)

    async def telegram_client(self, settings, fingerprint):
        if self.telegram and self.telegram_key != fingerprint:
            await self.telegram.stop()
            self.telegram = None
        if self.telegram is None:
            api_id = int(settings.get("tg_api_id") or os.getenv("TG_API_ID", "0"))
            api_hash = settings.get("tg_api_hash") or os.getenv("TG_API_HASH", "")
            session = settings.get("tg_session_string") or os.getenv("TG_SESSION_STRING", "")
            if not api_id or not api_hash or not session:
                raise ValueError("Connect your Telegram user account in Tools & logs first.")
            client = Client("miniapp-catalog", api_id=api_id, api_hash=api_hash, session_string=session,
                            in_memory=True, no_updates=True, sleep_threshold=0)
            try:
                await client.start()
            except BaseException:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
            self.telegram, self.telegram_key = client, fingerprint
        return self.telegram

    @staticmethod
    def chat_item(chat):
        cid = str(chat.id)
        if not cid.startswith("-100"):
            return None
        kind = getattr(chat.type, "value", str(chat.type))
        forum = bool(getattr(chat, "is_forum", False))
        # Pyrogram 2's high-level Chat predates is_forum; raw flags are filled below.
        link = f"https://t.me/c/{cid[4:]}/1"
        return {"id": cid, "name": chat.title or chat.username or cid, "kind": "chat", "expandable": forum,
                "forum": forum, "description": "Forum · choose a topic" if forum else kind.title(),
                "source": link, "destination": link, "topic": False, "can_select": not forum}

    async def telegram_items(self, parent, settings, fingerprint):
        client = await self.telegram_client(settings, fingerprint)
        if parent == "root":
            items = []
            count = 0
            async for dialog in client.get_dialogs(limit=3001):
                count += 1
                if count > 3000:
                    break
                item = self.chat_item(dialog.chat)
                if item:
                    items.append(item)
            # Dialogs' high-level Chat does not expose the forum flag in Pyrogram 2.
            # Fetch only raw chat metadata in bounded batches, never message histories.
            for offset in range(0, len(items), 100):
                peers = [await client.resolve_peer(int(item["id"])) for item in items[offset:offset + 100]]
                channels = [raw.types.InputChannel(channel_id=p.channel_id, access_hash=p.access_hash) for p in peers]
                result = await client.invoke(raw.functions.channels.GetChannels(id=channels), sleep_threshold=0)
                forums = {str(-1000000000000 - c.id): bool(getattr(c, "forum", False)) for c in result.chats}
                for item in items[offset:offset + 100]:
                    forum = forums.get(item["id"], False)
                    item.update(forum=forum, expandable=forum, can_select=not forum)
                    if forum:
                        item["description"] = "Forum · choose a topic"
            return {"items": sorted(items, key=lambda x: x["name"].casefold()), "next": None,
                    "note": "Showing the first 3,000 dialogs." if count > 3000 else "Chats accessible to the bot's connected Telegram account."}
        chat_id = int(parent)
        peer = await client.resolve_peer(chat_id)
        channel = raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
        topics, seen = [], set()
        offset_date = offset_id = offset_topic = 0
        for _ in range(100):
            result = await client.invoke(raw.functions.channels.GetForumTopics(channel=channel, offset_date=offset_date,
                offset_id=offset_id, offset_topic=offset_topic, limit=100), sleep_threshold=0)
            page = [t for t in result.topics if isinstance(t, raw.types.ForumTopic)]
            added = [t for t in page if t.id not in seen]
            for topic in added:
                seen.add(topic.id)
                link = f"https://t.me/c/{str(chat_id)[4:]}/{topic.id}/{topic.id}"
                topics.append({"id": f"{chat_id}:{topic.id}", "name": topic.title, "kind": "topic", "expandable": False,
                               "description": f"Topic #{topic.id}" + (" · Closed" if getattr(topic, "closed", False) else ""),
                               "source": link, "destination": link, "topic": True, "can_select": True})
            if not added or len(seen) >= result.count or not page:
                break
            last = page[-1]
            messages = {m.id: m for m in result.messages if hasattr(m, "id")}
            message = messages.get(last.top_message)
            offset_date = last.date if getattr(result, "order_by_create_date", False) else getattr(message, "date", last.date)
            offset_id, offset_topic = last.top_message, last.id
        return {"items": sorted(topics, key=lambda x: x["name"].casefold()), "next": None,
                "note": "Select the exact topic to process."}

    @staticmethod
    def drive_items(parent, cursor, settings):
        from MSZDRIVE_uploader.gdrive_upload import GoogleDriveResumableUploader, GDRIVE_FOLDER_MIME
        token_path = Path(settings.get("gdrive_token_pickle") or os.getenv("GDRIVE_TOKEN_PICKLE", "token.pickle"))
        if not os.getenv("GDRIVE_TOKEN_JSON", "").strip() and not token_path.exists():
            raise ValueError("Import Google Drive OAuth credentials in Settings first.")
        drive = GoogleDriveResumableUploader(token_path)
        if parent == "shared_drives":
            response = drive.service.drives().list(pageSize=100, pageToken=cursor or None, fields="nextPageToken,drives(id,name)").execute()
            return {"items": [{"id": d["id"], "name": d["name"], "kind": "folder", "expandable": True,
                "description": "Shared drive", "source": "https://drive.google.com/drive/folders/" + d["id"],
                "destination": "gdrive:" + d["id"], "can_select": True} for d in response.get("drives", [])],
                "next": response.get("nextPageToken"), "note": "Shared drives available to your saved Google account."}
        escape = drive._escape_query_value
        query = f"mimeType = '{GDRIVE_FOLDER_MIME}' and trashed = false"
        shared = parent == "shared"
        query += " and sharedWithMe = true" if shared else f" and '{escape(parent)}' in parents"
        response = drive.service.files().list(q=query, pageSize=200, pageToken=cursor or None, orderBy="name",
            fields="nextPageToken,files(id,name,capabilities(canAddChildren))", supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        items = [{"id": f["id"], "name": f["name"], "kind": "folder", "expandable": True,
                  "description": "Google Drive folder", "source": "https://drive.google.com/drive/folders/" + f["id"],
                  "destination": "gdrive:" + f["id"], "can_select": True,
                  "writable": f.get("capabilities", {}).get("canAddChildren", True)} for f in response.get("files", [])]
        if parent == "root" and not cursor:
            items.insert(0, {"id": "shared_drives", "name": "Shared drives", "kind": "collection", "expandable": True,
                            "description": "Team and organization drives", "can_select": False})
            items.insert(0, {"id": "shared", "name": "Shared with me", "kind": "collection", "expandable": True,
                            "description": "Folders shared with this Google account", "can_select": False})
        return {"items": items, "next": response.get("nextPageToken"),
                "note": "Folders visible to your saved Google Drive account."}

    def msz_items(self, parent, settings, fingerprint, refresh):
        from MSZDRIVE_uploader.msz_api import MszApiClient
        base = settings.get("msz_base_url") or os.getenv("MSZ_BASE_URL", "https://cloud.medicalstudyzone.com")
        token = settings.get("msz_api_token") or os.getenv("MSZ_API_TOKEN", "")
        if not token:
            raise ValueError("Save an MSZ API token in Settings to browse cloud folders.")
        if self.msz_index is None or self.msz_key != fingerprint or time.time() - self.msz_time > TTL or refresh:
            client = MszApiClient(base, token, timeout=20)
            entries = client.list_entries(per_page=200, max_pages=100, extra_params={"type": "folder"})
            if len(entries) >= 20000:
                raise ValueError("MSZ folder index exceeded 20,000 entries. Please use a folder link.")
            self.msz_index = {str(e["id"]): e for e in entries if e.get("type") == "folder" and e.get("id") is not None}
            self.msz_client = client
            self.msz_key, self.msz_time = fingerprint, time.time()
        index = self.msz_index
        if parent != "root" and parent not in index:
            raise ValueError("Folder no longer exists in the MSZ index. Refresh the index.")
        if parent != "root":
            # Some MSZ instances return only top-level folders for section=all.
            # Explicitly index the opened folder so nested entries are not missed.
            client = self.msz_client
            children = client.list_entries(per_page=200, max_pages=100,
                extra_params={"section": "folder", "folder_id": parent, "type": "folder"})
            if len(children) >= 20000:
                raise ValueError("This MSZ folder has too many entries to index. Please use its link.")
            # Remove outdated immediate children before applying the latest listing.
            for fid in list(index):
                if str(index[fid].get("parent_id") or index[fid].get("parentId")) == parent:
                    del index[fid]
            for entry in children:
                if entry.get("type") == "folder" and entry.get("id") is not None:
                    pid = str(entry.get("parent_id") or entry.get("parentId") or parent)
                    if pid == parent:
                        index[str(entry["id"])] = dict(entry, parent_id=parent)
        items = []
        for fid, entry in index.items():
            pid = str(entry.get("parent_id") or entry.get("parentId") or "root")
            if pid not in index:
                pid = "root"
            if pid != parent:
                continue
            parts, visited, current = [], set(), entry
            while current and str(current["id"]) not in visited:
                visited.add(str(current["id"]))
                parts.insert(0, str(current.get("name") or current["id"]))
                current = index.get(str(current.get("parent_id") or current.get("parentId")))
            path = "/".join(parts)
            items.append({"id": fid, "name": str(entry.get("name") or fid), "kind": "folder", "expandable": True,
                          "description": path, "source": "msz:" + base.rstrip("/").removesuffix("/api/v1").removesuffix("/api") + "/drive/folders/" + fid,
                          "destination": "msz:" + path, "can_select": True})
        return {"items": sorted(items, key=lambda x: x["name"].casefold()), "next": None,
                "note": f"Indexed {len(index)} MSZ folders. Expand a folder to see its children."}
