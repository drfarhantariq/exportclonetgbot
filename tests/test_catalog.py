"""Read-only catalog caching, pagination, safe task values, and authorization."""
import asyncio
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from test_miniapp import fixture_server, signed_data
from catalog import AccountCatalog
from clone_topic_by_link import _resolve_endpoints, _build_endpoint_labels
from telegram_client import TelegramService
from pyrogram import raw


class CatalogAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server, self.docs = fixture_server()
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self.headers = {"Authorization": "tma " + signed_data()}

    async def asyncTearDown(self):
        await self.server.close()
        await self.client.close()

    async def post(self, payload):
        response = await self.client.post("/api/catalog", json=payload, headers=self.headers)
        return response, await response.json()

    async def wait(self, job):
        await asyncio.sleep(.03)
        return await (await self.client.get("/api/catalog?job=" + job, headers=self.headers)).json()

    async def test_authentication_and_job_ownership(self):
        self.assertEqual((await self.client.post("/api/catalog", json={"provider": "telegram"})).status, 401)
        with patch.object(self.server.catalog, "telegram_items", AsyncMock(return_value={"items": [], "next": None})):
            _, data = await self.post({"provider": "telegram"})
            self.server.catalog.jobs[data["job"]]["user_id"] = 999
            response = await self.client.get("/api/catalog?job=" + data["job"], headers=self.headers)
            self.assertEqual(response.status, 400)

    async def test_cache_refresh_and_no_task_side_effects(self):
        mock = AsyncMock(return_value={"items": [{"id": "123", "name": "Anatomy"}], "next": None})
        with patch.object(self.server.catalog, "telegram_items", mock):
            response, data = await self.post({"provider": "telegram"})
            self.assertEqual(response.status, 202)
            result = await self.wait(data["job"])
            self.assertEqual(result["status"], "ready")
            _, data = await self.post({"provider": "telegram"})
            self.assertTrue(data["cached"])
            _, data = await self.post({"provider": "telegram", "refresh": True})
            await self.wait(data["job"])
            self.assertEqual(mock.await_count, 2)
        for handler in self.server.handlers.values():
            handler.assert_not_awaited()
        self.server.bot.send_message.assert_not_awaited()

    async def test_errors_do_not_leak_provider_secrets(self):
        with patch.object(self.server.catalog, "telegram_items", AsyncMock(side_effect=RuntimeError("https://token-secret/"))):
            _, data = await self.post({"provider": "telegram"})
            result = await self.wait(data["job"])
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("token-secret", json.dumps(result))

    async def test_validates_provider_and_parent(self):
        for body in ({"provider": "local"}, {"provider": "gdrive", "parent": "../.env"}, {"provider": "msz", "parent": "https://other.example"}):
            response, _ = await self.post(body)
            self.assertEqual(response.status, 400)

    async def test_refresh_coalesces_duplicate_jobs(self):
        gate = asyncio.Event()
        async def read(*args):
            await gate.wait()
            return {"items": [], "next": None}
        with patch.object(self.server.catalog, "telegram_items", read):
            _, first = await self.post({"provider": "telegram"})
            _, second = await self.post({"provider": "telegram"})
            self.assertEqual(first["job"], second["job"])
            gate.set()
            await self.wait(first["job"])


class CatalogProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_forum_topics_paginate_and_emit_correct_links(self):
        server, _ = fixture_server()
        def topic(tid, title):
            return raw.types.ForumTopic(id=tid, date=100, title=title, icon_color=0, top_message=tid + 10,
                read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                unread_reactions_count=0, from_id=raw.types.PeerUser(user_id=1), notify_settings=raw.types.PeerNotifySettings())
        client = SimpleNamespace(resolve_peer=AsyncMock(return_value=SimpleNamespace(channel_id=1234, access_hash=88)),
            invoke=AsyncMock(side_effect=[SimpleNamespace(topics=[topic(10, "Anatomy")], messages=[], count=2),
                                        SimpleNamespace(topics=[topic(20, "Surgery")], messages=[], count=2)]))
        with patch.object(server.catalog, "telegram_client", AsyncMock(return_value=client)):
            result = await server.catalog.telegram_items("-1001234", {}, "fixture")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["source"], "https://t.me/c/1234/10/10")
        query = client.invoke.call_args_list[1].args[0]
        self.assertEqual(query.offset_topic, 10)
        self.assertEqual(query.offset_id, 20)
        await server.close()

    async def test_raw_forum_flag_drives_chat_expansion(self):
        server, _ = fixture_server()
        async def dialogs(**kw):
            yield SimpleNamespace(chat=SimpleNamespace(id=-1000000001234, title="Lectures", username=None,
                                                       type=SimpleNamespace(value="supergroup")))
        client = SimpleNamespace(get_dialogs=dialogs, resolve_peer=AsyncMock(return_value=SimpleNamespace(channel_id=1234, access_hash=1)),
            invoke=AsyncMock(return_value=SimpleNamespace(chats=[SimpleNamespace(id=1234, forum=True)])))
        with patch.object(server.catalog, "telegram_client", AsyncMock(return_value=client)):
            result = await server.catalog.telegram_items("root", {}, "fixture")
        self.assertTrue(result["items"][0]["expandable"])
        self.assertFalse(result["items"][0]["can_select"])
        await server.close()

    def test_google_folder_pagination_and_readonly_destinations(self):
        drive = Mock()
        drive._escape_query_value.side_effect = lambda v: v
        drive.service.files.return_value.list.return_value.execute.return_value = {
            "nextPageToken": "page2", "files": [{"id": "abc", "name": "Anatomy", "capabilities": {"canAddChildren": False}}]}
        with patch("MSZDRIVE_uploader.gdrive_upload.GoogleDriveResumableUploader", return_value=drive), patch.dict("os.environ", {"GDRIVE_TOKEN_JSON": "fixture"}):
            result = AccountCatalog.drive_items("parent", "page1", {})
        self.assertEqual(result["next"], "page2")
        self.assertEqual(result["items"][0]["source"], "https://drive.google.com/drive/folders/abc")
        self.assertFalse(result["items"][0]["writable"])
        self.assertIn("'parent' in parents", drive.service.files.return_value.list.call_args.kwargs["q"])
        self.assertEqual(drive.service.files.return_value.list.call_args.kwargs["pageToken"], "page1")

    def test_msz_tree_uses_ids_for_sources_and_paths_for_destinations(self):
        server, _ = fixture_server()
        entries = [{"id": 1, "name": "Medicine", "type": "folder", "parent_id": None},
                   {"id": 2, "name": "Anatomy", "type": "folder", "parent_id": 1}]
        mock = Mock()
        mock.list_entries.side_effect = [entries, [entries[1]]]
        with patch("MSZDRIVE_uploader.msz_api.MszApiClient", return_value=mock):
            settings = {"msz_base_url": "https://cloud.example", "msz_api_token": "secret"}
            root = server.catalog.msz_items("root", settings, "fingerprint", False)
            children = server.catalog.msz_items("1", settings, "fingerprint", False)
        self.assertEqual([i["name"] for i in root["items"]], ["Medicine"])
        self.assertEqual(children["items"][0]["destination"], "msz:Medicine/Anatomy")
        self.assertEqual(children["items"][0]["source"], "msz:https://cloud.example/drive/folders/2")
        self.assertEqual(mock.list_entries.call_count, 2)

    async def test_clone_to_ordinary_channel_has_no_topic_or_reply(self):
        endpoints = _resolve_endpoints("https://t.me/c/123/10/10", "https://t.me/c/456/1", 0)
        self.assertIsNone(endpoints.destination_topic_id)
        telegram = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(title="Channel")),
                                   get_forum_topic_title=AsyncMock(return_value="Anatomy"))
        labels = await _build_endpoint_labels(telegram, endpoints)
        self.assertEqual(labels["destination_topic_title"], "Channel messages")
        telegram.get_forum_topic_title.assert_awaited_once_with(endpoints.source_chat_id, 10)
        self.assertEqual(TelegramService._reply_kwargs(None), {})

    async def test_authenticated_google_source_download_uses_saved_account(self):
        from MSZDRIVE_uploader.sources import list_gdrive_folder, download_gdrive_file
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            drive = Mock()
            drive.extract_folder_id.return_value = "folder"
            drive.iter_files_under.return_value = [({"id": "file", "size": "5"}, "Anatomy/lecture.mp4")]
            def download(fid, path, **kw):
                path.write_bytes(b"video")
                return path
            drive.download_file.side_effect = download
            with patch("MSZDRIVE_uploader.gdrive_upload.GoogleDriveResumableUploader", return_value=drive), patch.dict("os.environ", {"GDRIVE_TOKEN_JSON": "fixture"}):
                files = await list_gdrive_folder("https://drive.google.com/drive/folders/folder", staging)
                item = await download_gdrive_file(files[0], files[0].path, staging)
            self.assertEqual(item.path.read_bytes(), b"video")
            self.assertTrue(item.cleanup)
            self.assertEqual(drive.download_file.call_args.kwargs["expected_size"], 5)
