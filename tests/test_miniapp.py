"""Security and shared-engine behavior of the Telegram Mini App API."""
import asyncio
import hashlib
import hmac
import json
import sys
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "heroku_bot"))
import app as bot_engine
from miniapp import MiniAppServer, task_view, verify_init_data

TOKEN = "test-only-bot-token"
USER = {"id": 123, "first_name": "Demo admin", "username": "demo"}


def signed_data(user=None, date=None):
    fields = {"auth_date": str(date or int(time.time())), "user": json.dumps(user or USER), "query_id": "fixture"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def fixture_server():
    documents = {}

    async def load(store, key):
        return deepcopy(documents.get(key))

    async def save(store, key, document):
        documents[key] = deepcopy(document)

    engine = SimpleNamespace(
        FINISHED_JOB_PHASES=bot_engine.FINISHED_JOB_PHASES,
        ACTIVE_JOB_PHASES=bot_engine.ACTIVE_JOB_PHASES,
        transfer_panel=bot_engine.transfer_panel,
        _format_clone_endpoint=bot_engine._format_clone_endpoint,
        _build_clone_parser=bot_engine._build_clone_parser,
        _build_export_parser=bot_engine._build_export_parser,
        _build_index_parser=bot_engine._build_index_parser,
        _load_state=load, _save_transfer_doc=save,
        _load_bot_settings=AsyncMock(return_value={"secret": "private-credential", "threads": 8, "enabled": True}),
        SETTINGS_CATEGORY_ORDER=("demo",), SETTINGS_CATEGORIES={"demo": ("secret", "threads", "enabled")},
        SETTINGS_CATEGORY_TITLES={"demo": "Account"}, SECRET_SETTING_KEYS={"secret"},
        _settings_key_label=lambda value: value.title(),
        _clone_pending_jobs=[], _clone_queue_cv=asyncio.Condition(),
        TRANSFER_QUEUE=SimpleNamespace(pending=[], condition=asyncio.Condition(), snapshot=lambda: {}),
        _save_clone_queue_snapshot=AsyncMock(), _save_transfer_queue=AsyncMock(),
        ACTIVE_CLONE_TASK=None, ACTIVE_TRANSFER_TASK=None, ACTIVE_EXPORT_TASK=None, ACTIVE_INDEX_TASK=None,
        ACTIVE_LOGIN_FLOWS={}, BOT_STARTED_AT=time.time() - 300,
        RUNTIME_DIR=Path("runtime"), psutil=None,
    )
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    handlers = {kind: AsyncMock() for kind in ("clone", "transfer", "export", "index", "cancel", "settings", "login", "login_step", "help", "log", "status", "restart")}
    server = MiniAppServer(engine, bot, object(), handlers, lambda: {USER["id"]}, TOKEN)
    server.ready = True
    return server, documents


class TelegramAuthenticationTests(unittest.TestCase):
    def test_signed_admin_is_accepted_and_tampering_is_rejected(self):
        raw = signed_data()
        self.assertEqual(verify_init_data(raw, TOKEN, {123}), USER)
        for invalid in ("", raw.replace("fixture", "changed"), raw + "&auth_date=1"):
            with self.assertRaises(ValueError):
                verify_init_data(invalid, TOKEN, {123})

    def test_non_admin_expired_and_future_sessions_are_rejected(self):
        for raw in (signed_data({"id": 456}), signed_data(date=int(time.time()) - 43201), signed_data(date=int(time.time()) + 60)):
            with self.assertRaises(ValueError):
                verify_init_data(raw, TOKEN, {123})


class MiniAppAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server, self.docs = fixture_server()
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self.headers = {"Authorization": "tma " + signed_data()}

    async def asyncTearDown(self):
        await self.server.close()
        await self.client.close()

    async def post(self, path, data):
        return await self.client.post("/api/" + path, json=data, headers=self.headers)

    async def test_public_shell_and_protected_api(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 401)
        response = await self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status, 200)

    async def test_settings_never_return_saved_credentials(self):
        response = await self.client.get("/api/settings", headers=self.headers)
        data = await response.json()
        secret = data["groups"][0]["entries"][0]
        self.assertTrue(secret["configured"])
        self.assertEqual(secret["value"], "")
        self.assertNotIn("private-credential", json.dumps(data))

    async def test_commands_share_handlers_and_repeated_requests_do_not_duplicate(self):
        payload = {"command": "/clone --source-link https://t.me/c/1/2 --destination-link https://t.me/c/3/4", "request_id": "repeat"}
        first = await (await self.post("command", payload)).json()
        second = await (await self.post("command", payload)).json()
        self.assertEqual(first, second)
        await asyncio.sleep(.02)
        handler = self.server.handlers["clone"]
        handler.assert_awaited_once()
        message = handler.call_args.args[1]
        self.assertEqual(message.from_user.id, 123)
        self.assertEqual(message.text, payload["command"])

    async def test_export_and_index_jobs_wait_and_queue_edits_are_persisted(self):
        for kind in ("export", "index", "export"):
            response = await self.post("command", {"command": f"/{kind} --topic-link https://t.me/c/1/2"})
            self.assertEqual(response.status, 202)
        job = self.server.pending[-1]
        response = await self.post("queue", {"kind": "export", "action": "first", "id": job["id"]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.docs["miniapp:queue"]["pending"][0]["id"], job["id"])
        await self.post("queue", {"kind": "export", "action": "clear"})
        self.assertEqual([j["kind"] for j in self.server.pending], ["index"])
        self.server.handlers["export"].assert_not_awaited()

    async def test_chat_submissions_join_the_same_queue_and_keep_request_identity(self):
        message = SimpleNamespace(from_user=SimpleNamespace(**USER), text="/index --topic-link https://t.me/c/1/2", id=18, reply_text=AsyncMock())
        await self.server.enqueue_message("index", message)
        job = self.docs["miniapp:queue"]["pending"][0]
        self.assertEqual(job["native_message_id"], 18)
        await self.server.run_queued(job)
        bridged = self.server.handlers["index"].call_args.args[1]
        self.assertTrue(bridged.miniapp_dispatch)
        self.assertEqual(bridged.id, 18)

    async def test_failed_queue_save_does_not_leave_an_accepted_job(self):
        self.server.engine._save_transfer_doc = AsyncMock(side_effect=OSError("unavailable"))
        response = await self.post("command", {"command": "/index resume", "request_id": "failure"})
        self.assertEqual(response.status, 500)
        self.assertEqual(self.server.pending, [])
        self.assertEqual(self.server.operations, {})
        self.assertEqual(self.server.idempotency, {})

    async def test_index_upload_reuses_document_handler_without_server_path(self):
        form = FormData()
        form.add_field("command", "/transfer https://t.me/c/1/2 --index-done --up gd")
        form.add_field("file", b"Topic\nhttps://t.me/c/1/2/3", filename="edited.txt", content_type="text/plain")
        response = await self.client.post("/api/upload", data=form, headers=self.headers)
        self.assertEqual(response.status, 202)
        await asyncio.sleep(.02)
        message = self.server.handlers["transfer"].call_args.args[1]
        self.assertIs(message.reply_to_message, self.server.bot.send_document.return_value)
        self.assertNotIn("/app/", message.text)

    async def test_session_file_import_uses_existing_settings_validation(self):
        form = FormData()
        form.add_field("command", "/settings upload tg_session_string")
        form.add_field("file", b"fixture-session", filename="session.txt", content_type="text/plain")
        response = await self.client.post("/api/upload", data=form, headers=self.headers)
        self.assertEqual(response.status, 202)
        await asyncio.sleep(.02)
        self.assertEqual(self.server.handlers["settings"].call_args.args[1].text, "/settings set tg_session_string fixture-session")
        self.server.bot.send_document.assert_not_awaited()

    async def test_recovery_resumes_interrupted_jobs_but_does_not_replay_completed_jobs(self):
        interrupted = {"id": "interrupted", "kind": "export", "command": "/export --topic-link source", "user": USER}
        completed = dict(interrupted, id="done", kind="index")
        self.docs["miniapp:queue"] = {"pending": [], "inflight": {"export": interrupted, "index": completed}}
        self.docs["export:last"] = {"phase": "running"}
        self.docs["index:last"] = {"phase": "completed"}
        with patch.dict("os.environ", {"MINIAPP_URL": ""}):
            await self.server.activate()
        self.assertEqual([j["command"] for j in self.server.pending], ["/export resume"])

    async def test_queue_worker_waits_for_existing_native_job(self):
        gate = asyncio.Event()
        native = asyncio.create_task(gate.wait())
        self.server.engine.ACTIVE_EXPORT_TASK = native
        await self.post("command", {"command": "/export resume"})
        worker = self.server.track(self.server.queue_worker())
        await asyncio.sleep(.02)
        self.server.handlers["export"].assert_not_awaited()
        gate.set()
        await native
        await asyncio.sleep(1.05)
        self.server.handlers["export"].assert_awaited_once()
        self.assertEqual(self.docs["miniapp:queue"]["inflight"], {})
        worker.cancel()

    async def test_removed_admin_cannot_dispatch_a_queued_operation(self):
        self.server.admins = lambda: set()
        await self.server.dispatch("removed", "clone", "/clone resume", USER)
        self.server.handlers["clone"].assert_not_awaited()
        self.assertEqual(self.server.operations["removed"]["phase"], "failed")

    async def test_server_paths_and_local_sources_are_blocked(self):
        for command in ("/transfer local:secret --up msz", "/transfer ./config.yaml --up msz", "/transfer msz:Course --from local --up gd", "/transfer msz:Course --gdrive-token-pickle secret", "/transfer https://t.me/c/1/2 --index-done /app/token.pickle --up gd", "/clone --config /app/config.yaml", "/export --conf /app/config.yaml", "/transfer msz:Course --chromium-exec /bin/sh"):
            response = await self.post("command", {"command": command})
            self.assertEqual(response.status, 400, command)
        self.assertEqual(self.server.validate_command("/transfer msz:Course --up gd"), "transfer")
        self.assertEqual(self.server.validate_command("/transfer https://t.me/c/1/2 --index-done --up gd", uploaded=True), "transfer")

    async def test_restart_requires_explicit_app_confirmation_to_skip_chat_prompt(self):
        await self.post("command", {"command": "/restart", "confirmed": True})
        await asyncio.sleep(.02)
        self.assertTrue(self.server.handlers["restart"].call_args.args[1].miniapp_confirmed_restart)

    async def test_multistep_login_only_accepts_input_during_an_existing_flow(self):
        response = await self.post("command", {"login_input": "12345"})
        self.assertEqual(response.status, 400)
        self.server.engine.ACTIVE_LOGIN_FLOWS[123] = {"step": "code"}
        response = await self.post("command", {"login_input": "12345"})
        self.assertEqual(response.status, 202)
        await asyncio.sleep(.02)
        self.server.handlers["login_step"].assert_awaited_once()

    async def test_byte_progress_does_not_replace_overall_message_progress(self):
        self.docs["clone:last"] = dict(phase="running", total_messages=100, success=2, skipped=1, failed=0,
                                      transfer_stage="download", download_current=20, download_total=100,
                                      download_speed_bps=10, started_at=time.time() - 60,
                                      payload={"source_link": "source", "destination_link": "target", "token": "private"})
        native = asyncio.create_task(asyncio.sleep(30))
        self.server.engine.ACTIVE_CLONE_TASK = native
        data = await (await self.client.get("/api/state", headers=self.headers)).json()
        native.cancel()
        await asyncio.gather(native, return_exceptions=True)
        task = data["active"][0]
        self.assertEqual(task["percent"], 3)
        self.assertEqual(task["file_done"], 20)
        self.assertEqual(task["file_eta"], 8)
        self.assertNotIn("private", json.dumps(task))

    async def test_finished_elapsed_is_frozen(self):
        state = {"phase": "completed", "started_at": 100, "finished_at": 120, "total_messages": 3, "success": 3}
        with patch("miniapp.time.time", return_value=10000):
            view = task_view(self.server.engine, "clone", state)
        self.assertEqual(view["elapsed"], 20)
        self.assertEqual(view["percent"], 100)
