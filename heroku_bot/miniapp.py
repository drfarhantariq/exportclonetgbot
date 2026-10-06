"""Admin-only Telegram Mini App, sharing the bot's queues and event loop."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import shlex
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

from aiohttp import ClientSession, web
from browser_login import BrowserLogin
from catalog import AccountCatalog

STATIC = Path(__file__).with_name("miniapp_static")
COMMANDS = {"clone", "transfer", "export", "index", "cancel", "settings", "login", "help", "log", "status", "restart"}


def verify_init_data(raw, token, admins, *, now=None):
    if not raw or len(raw) > 16384:
        raise ValueError("Open this app from the bot's Open App button.")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    if len(pairs) != len(values):
        raise ValueError("Invalid Telegram authentication.")
    supplied = values.pop("hash", "")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid Telegram authentication.")
    current = time.time() if now is None else now
    age = current - int(values.get("auth_date", "0"))
    if age < -30 or age > 12 * 3600:
        raise ValueError("Session expired. Close and reopen the Mini App.")
    user = json.loads(values.get("user", "{}"))
    if type(user.get("id")) is not int or user["id"] not in admins:
        raise ValueError("This workspace is available to bot admins only.")
    return user


def plain(value):
    return html.unescape(re.sub(r"<[^>]*>", "", str(value)))


def task_view(engine, kind, state):
    if not state:
        return None
    p = state.get("payload") or {}
    phase = state.get("phase", "queued")
    success = int(state.get("uploaded", state.get("success", 0)) or 0)
    skipped, failed = int(state.get("skipped", 0) or 0), int(state.get("failed", 0) or 0)
    if kind == "transfer":
        view = engine.transfer_panel.__globals__["snapshot"](state)
        processed, total = int(state.get("processed", 0) or 0), int(state.get("total_files", 0) or 0)
        source, destination = state.get("source", ""), state.get("destination", "")
        stage = state.get("stage", phase)
        current, file_total = state.get("bytes_done"), state.get("file_size")
        speed, file_eta = view["speed"], view["file_eta"]
    else:
        processed = success + skipped + failed if kind == "clone" else int((state.get("processed_messages", state.get("fetched_messages", state.get("found_message_ids", 0)))) or 0)
        total = int(state.get("total_messages", 0) or 0)
        start, end = float(state.get("started_at", 0) or 0), float(state.get("finished_at", 0) or time.time())
        elapsed = max(0, end - start) if start else 0
        eta = elapsed / processed * max(0, total - processed) if total and processed else None
        view = {"percent": min(100, processed / total * 100) if total else 0, "elapsed": elapsed, "eta": eta}
        source = (engine._format_clone_endpoint(p, "source") or p.get("source_link", "")) if kind == "clone" else p.get("topic_link", "")
        destination = (engine._format_clone_endpoint(p, "destination") or p.get("destination_link", "")) if kind == "clone" else p.get("upload_topic_link", "")
        stage = state.get("transfer_stage") or state.get("stage") or phase
        current, file_total = state.get(f"{stage}_current"), state.get(f"{stage}_total")
        speed = float(state.get(f"{stage}_speed_bps", 0) or 0)
        file_eta = max(0, (file_total - current) / speed) if speed and file_total and current is not None else None
    if phase in engine.FINISHED_JOB_PHASES:
        view["eta"] = 0
        if phase == "completed":
            view["percent"] = 100
    return {"id": state.get("job_id") or f"{kind}:{state.get('started_at', 0)}", "kind": kind,
            "phase": phase, "stage": stage, "source": plain(source)[:800], "destination": plain(destination)[:800],
            "name": plain(source or f"{kind.title()} task")[:120], "processed": processed, "total": total,
            "success": success, "skipped": skipped, "failed": failed, "percent": view["percent"],
            "elapsed": view["elapsed"], "eta": view["eta"], "file": state.get("file_name") or state.get("current_file_name") or "",
            "file_type": state.get("current_message_type", ""), "file_done": current, "file_total": file_total,
            "speed": speed, "file_eta": file_eta, "started_at": state.get("started_at"), "finished_at": state.get("finished_at"),
            "flood_wait": max(0, float(state.get("flood_wait_until", 0) or 0) - time.time())}


class BridgeMessage:
    def __init__(self, server, user, text, operation, reply=None):
        self.server, self.operation = server, operation
        self.from_user = SimpleNamespace(**{k: user.get(k) for k in ("id", "first_name", "last_name", "username")})
        self.chat = SimpleNamespace(id=user["id"])
        self.text, self.id, self.reply_to_message = text, None, reply
        self.miniapp_dispatch = True

    async def reply_text(self, text, **kwargs):
        self.operation["messages"].append(plain(text))
        return await self.server.bot.send_message(self.chat.id, text, **kwargs)

    async def reply_document(self, document, **kwargs):
        result = await self.server.bot.send_document(self.chat.id, document, **kwargs)
        self.operation["messages"].append("File sent to your bot chat.")
        return result


class MiniAppServer:
    def __init__(self, engine, bot, store, handlers, admins, token):
        self.engine, self.bot, self.store = engine, bot, store
        self.handlers, self.admins, self.token = handlers, admins, token
        self.operations, self.idempotency = OrderedDict(), OrderedDict()
        self.rates = defaultdict(deque)
        self.pending, self.runners, self.inflight = [], {}, {}
        self.queue_lock = asyncio.Lock()
        self.tasks = set()
        self.runner = None
        self.ready = False
        self.browser_login = BrowserLogin(self)
        self.catalog = AccountCatalog(self)
        self.app = web.Application(client_max_size=3 * 1024**2, middlewares=[self.middleware])
        self.app.add_routes([web.get("/", self.index), web.get("/health", self.health),
                             web.get("/auth/session", self.browser_login.info),
                             web.get("/auth/login", self.browser_login.start),
                             web.get("/auth/callback", self.browser_login.callback),
                             web.get("/auth/widget", self.browser_login.widget),
                             web.get("/auth/widget-config", self.browser_login.widget_config),
                             web.get("/auth/widget-script", self.browser_login.widget_script),
                             web.get("/auth/legacy-callback", self.browser_login.legacy_callback),
                             web.post("/auth/logout", self.browser_login.logout),
                             web.get("/api/state", self.state), web.get("/api/settings", self.settings),
                             web.post("/api/catalog", self.catalog.request), web.get("/api/catalog", self.catalog.status),
                             web.post("/api/topics", self.catalog.create_topic),
                             web.post("/api/command", self.command), web.post("/api/upload", self.upload),
                             web.post("/api/queue", self.queue_action), web.get("/api/logs", self.logs)])
        self.app.router.add_static("/assets/", STATIC, show_index=False)

    @web.middleware
    async def middleware(self, request, handler):
        try:
            if request.path.startswith("/api/"):
                if not self.ready:
                    return web.json_response({"error": "Bot is connecting. Try again shortly."}, status=503)
                auth = request.headers.get("Authorization", "")
                if auth.startswith("tma "):
                    request["user"] = verify_init_data(auth.removeprefix("tma "), self.token, self.admins())
                else:
                    request["user"], _ = await self.browser_login.authenticate(request, mutation=request.method == "POST")
                if request.method == "POST":
                    uid = request["user"]["id"]
                    hits = self.rates[uid]
                    now = time.monotonic()
                    while hits and now - hits[0] > 60:
                        hits.popleft()
                    if len(hits) >= 30:
                        return web.json_response({"error": "Too many actions. Try again in a minute."}, status=429)
                    hits.append(now)
            response = await handler(request)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            status = 401 if request.path.startswith("/api/") and "user" not in request else 400
            response = web.json_response({"error": str(exc)[:300]}, status=status)
        except web.HTTPException:
            raise
        except Exception:
            logging.getLogger("miniapp").error("Mini App action failed (%s).", request.path)
            response = web.json_response({"error": "Action failed. Check the bot chat or try again."}, status=500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; frame-src https://oauth.telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"})
        return response

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def health(self, request):
        return web.json_response({"status": "ready" if self.ready else "connecting"})

    async def start(self, port=None):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", int(port or os.getenv("PORT", "8080"))).start()

    async def activate(self):
        saved = await self.engine._load_state(self.store, "miniapp:queue") or {}
        self.pending = [j for j in saved.get("pending", []) if j.get("kind") in {"export", "index"} and j.get("user", {}).get("id") in self.admins()][:50]
        for kind, job in saved.get("inflight", {}).items():
            if kind not in {"export", "index"} or job.get("user", {}).get("id") not in self.admins():
                continue
            state = await self.engine._load_state(self.store, kind + ":last") or {}
            # Interrupted jobs use their persisted profile; already finished jobs
            # are never replayed after a restart.
            if state.get("phase") in self.engine.ACTIVE_JOB_PHASES | {"interrupted"}:
                self.pending.insert(0, dict(job, command=f"/{kind} resume"))
        await self.save_queue()
        self.ready = True
        self.track(self.queue_worker())
        url = os.getenv("MINIAPP_URL", "").strip()
        if url:
            async with ClientSession() as session:
                for uid in self.admins():
                    try:
                        async with session.post(f"https://api.telegram.org/bot{self.token}/setChatMenuButton",
                                                json={"chat_id": uid, "menu_button": {"type": "web_app", "text": "Open App", "web_app": {"url": url}}}, timeout=20) as response:
                            data = await response.json()
                            if not data.get("ok"):
                                logging.getLogger("miniapp").warning("Could not configure Mini App menu for admin.")
                    except Exception:
                        logging.getLogger("miniapp").warning("Mini App menu setup will be available via /app.")
        logging.getLogger("miniapp").info("Telegram Mini App is ready.")

    def track(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        self.ready = False
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.catalog.close()
        if self.runner:
            await self.runner.cleanup()

    async def settings(self, request):
        e = self.engine
        values = await e._load_bot_settings(self.store)
        groups = []
        for category in e.SETTINGS_CATEGORY_ORDER:
            entries = []
            for key in e.SETTINGS_CATEGORIES[category]:
                value = values.get(key)
                secret = key in e.SECRET_SETTING_KEYS
                entries.append({"key": key, "label": e._settings_key_label(key), "secret": secret,
                                "configured": bool(value), "value": "" if secret else value,
                                "type": "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "text"})
            groups.append({"id": category, "name": e.SETTINGS_CATEGORY_TITLES[category], "entries": entries})
        return web.json_response({"groups": groups})

    async def state(self, request):
        e = self.engine
        active, latest = [], []
        for kind in ("clone", "transfer", "export", "index"):
            state = await e._load_state(self.store, kind + ":last")
            if state:
                view = task_view(e, kind, state)
                task = getattr(e, f"ACTIVE_{kind.upper()}_TASK", None)
                if state.get("phase") in e.ACTIVE_JOB_PHASES and task is not None and not task.done():
                    active.append(view)
                else:
                    latest.append(view)
        queue = [task_view(e, "clone", dict(j, phase="queued")) for j in list(e._clone_pending_jobs)]
        queue += [task_view(e, "transfer", j) for j in list(e.TRANSFER_QUEUE.pending)]
        for job in self.pending:
            try:
                tokens = shlex.split(job["command"])
            except ValueError:
                tokens = []
            source = tokens[tokens.index("--topic-link") + 1] if "--topic-link" in tokens and tokens.index("--topic-link") + 1 < len(tokens) else f"Saved {job['kind']} profile"
            destination = tokens[tokens.index("--upload-topic-link") + 1] if "--upload-topic-link" in tokens and tokens.index("--upload-topic-link") + 1 < len(tokens) else ""
            queue.append({"id": job["id"], "kind": job["kind"], "phase": "queued", "name": source, "source": source, "destination": destination})
        history_doc = await e._load_state(self.store, "status:history") or {}
        history = [task_view(e, j["kind"], j["state"]) for j in history_doc.get("jobs", []) if j.get("kind") in {"clone", "transfer", "export", "index"} and isinstance(j.get("state"), dict)]
        stats = {"cpu": 0, "ram": 0, "free": 0, "disk": 0, "uptime": max(0, time.time() - e.BOT_STARTED_AT)}
        if e.psutil:
            disk = e.psutil.disk_usage(str(e.RUNTIME_DIR))
            stats.update(cpu=e.psutil.cpu_percent(), ram=e.psutil.virtual_memory().percent, free=disk.free, disk=disk.percent)
        user = request["user"]
        operations = [{k: v for k, v in op.items() if k != "user_id"} for op in self.operations.values() if op["user_id"] == user["id"]][-20:]
        return web.json_response({"user": {"id": user["id"], "name": user.get("first_name", "Admin"), "username": user.get("username")},
                                  "active": active, "queue": queue, "history": history[:50], "latest": latest, "stats": stats,
                                  "operations": operations, "login_step": (e.ACTIVE_LOGIN_FLOWS.get(user["id"]) or {}).get("step"),
                                  "server_time": time.time()})

    def validate_command(self, command, *, uploaded=False):
        if not isinstance(command, str) or len(command) > 65536:
            raise ValueError("Invalid command.")
        head = command.split(maxsplit=1)[0].lstrip("/") if command.strip() else ""
        if head not in COMMANDS:
            raise ValueError("Choose a supported bot command.")
        # Never permit a web client to choose server-side config, token or output paths.
        if head in {"clone", "transfer", "export", "index"}:
            tokens = shlex.split(command)
            if head == "transfer":
                from MSZDRIVE_uploader.transfer import _build_parser
                parser = _build_parser()
            else:
                parser = getattr(self.engine, f"_build_{head}_parser")()
            allowed = {option for action in parser._actions for option in action.option_strings}
            if any(t.startswith("--") and t.split("=", 1)[0] not in allowed for t in tokens):
                raise ValueError("Use the full name of a supported task option.")
            if any(t.split("=", 1)[0] in {"--config", "--out", "--runtime-dir", "--index-out", "--gdrive-token-pickle", "--log-file", "--chromium-executable"} for t in tokens):
                raise ValueError("The bot manages server file paths.")
            for i, token in enumerate(tokens):
                if token.split("=", 1)[0] == "--index-done":
                    if not uploaded or "=" in token or (i + 1 < len(tokens) and not tokens[i + 1].startswith("--")):
                        raise ValueError("Upload the edited index using the file picker.")
                if token.split("=", 1)[0] == "--from" and (token.partition("=")[2] or (tokens[i + 1] if i + 1 < len(tokens) else "")) in {"local", "index"}:
                    raise ValueError("Local server files are not available in the Mini App.")
            if head == "transfer" and len(tokens) > 1 and not tokens[1].startswith("--"):
                source = tokens[1]
                actions = {"help", "status", "queue", "logs", "last", "resume", "cancel", "clear-queue"}
                if source not in actions and not source.lower().startswith(("https://", "http://", "msz:", "gdrive:", "t.me/")):
                    raise ValueError("Use a Telegram, MSZ or Google Drive link. Upload edited indexes using the file picker.")
        return head

    async def command(self, request):
        body = await request.json()
        user = request["user"]
        command = body.get("command", "")
        if body.get("login_input") is not None:
            if user["id"] not in self.engine.ACTIVE_LOGIN_FLOWS:
                raise ValueError("Start Telegram login first.")
            command, kind = str(body["login_input"])[:512], "login_step"
        else:
            kind = self.validate_command(command)
        if kind in {"export", "index"} and len(self.pending) >= 50:
            raise ValueError("Queue is full (50 waiting tasks).")
        key = (user["id"], str(body.get("request_id", "")))
        if key[1] and key in self.idempotency:
            return web.json_response({"operation": self.idempotency[key]})
        oid = uuid.uuid4().hex[:12]
        self.operations[oid] = {"id": oid, "kind": kind, "phase": "pending", "messages": [], "user_id": user["id"], "created_at": time.time()}
        if key[1]:
            self.idempotency[key] = oid
        while len(self.operations) > 200:
            self.operations.popitem(last=False)
        while len(self.idempotency) > 200:
            self.idempotency.popitem(last=False)
        if kind in {"export", "index"} and command.split(maxsplit=1)[-1] not in {"status", "help"}:
            async with self.queue_lock:
                if len(self.pending) >= 50:
                    raise ValueError("Queue is full (50 waiting tasks).")
                job = {"id": oid, "kind": kind, "command": command, "user": user, "created_at": time.time()}
                self.pending.append(job)
                try:
                    await self.save_queue()
                except Exception:
                    self.pending.remove(job)
                    self.operations.pop(oid, None)
                    self.idempotency.pop(key, None)
                    raise
            self.operations[oid]["messages"].append("Task added to the queue.")
        else:
            self.track(self.dispatch(oid, kind, command, user, confirmed=body.get("confirmed") is True))
        return web.json_response({"operation": oid}, status=202)

    async def enqueue_message(self, kind, message):
        """Chat submissions share the export/index queue once the web app is ready."""
        user = {key: getattr(message.from_user, key, None) for key in ("id", "first_name", "last_name", "username")}
        if user["id"] not in self.admins():
            return
        oid = uuid.uuid4().hex[:12]
        async with self.queue_lock:
            if len(self.pending) >= 50:
                await message.reply_text("Queue is full (50 waiting tasks).")
                return
            job = {"id": oid, "kind": kind, "command": message.text, "user": user,
                   "native_message_id": message.id, "created_at": time.time()}
            self.pending.append(job)
            try:
                await self.save_queue()
            except Exception:
                self.pending.remove(job)
                await message.reply_text("Could not save the task queue. Please try again.")
                return
        await message.reply_text(f"{kind.title()} added to the queue. View it in /app.")

    async def dispatch(self, oid, kind, command, user, reply=None, *, confirmed=False, original_id=None):
        op = self.operations.setdefault(oid, {"id": oid, "kind": kind, "phase": "pending", "messages": [], "user_id": user["id"], "created_at": time.time()})
        if user["id"] not in self.admins():
            op.update(phase="failed", messages=["Admin access has been removed."])
            return
        op["phase"] = "running"
        try:
            message = BridgeMessage(self, user, command, op, reply)
            message.id = original_id
            message.miniapp_confirmed_restart = kind == "restart" and confirmed
            await self.handlers[kind](self.bot, message)
            op["phase"] = "completed"
        except asyncio.CancelledError:
            op["phase"] = "cancelled"
            raise
        except Exception:
            op.update(phase="failed", messages=op["messages"] + ["Action failed. Check the bot chat or logs."])
            logging.getLogger("miniapp").error("Mini App %s operation failed.", kind)

    async def save_queue(self):
        await self.engine._save_transfer_doc(self.store, "miniapp:queue", {"pending": list(self.pending), "inflight": dict(self.inflight)})

    async def run_queued(self, job):
        try:
            await self.dispatch(job["id"], job["kind"], job["command"], job["user"], original_id=job.get("native_message_id"))
        except asyncio.CancelledError:
            # Keep the marker so an interrupted job can resume on dyno restart.
            raise
        else:
            async with self.queue_lock:
                self.inflight.pop(job["kind"], None)
                await self.save_queue()

    async def queue_worker(self):
        while True:
            for kind in ("export", "index"):
                running = self.runners.get(kind)
                native = getattr(self.engine, f"ACTIVE_{kind.upper()}_TASK", None)
                if (running and not running.done()) or (native and not native.done()):
                    continue
                async with self.queue_lock:
                    job = next((j for j in self.pending if j["kind"] == kind), None)
                    if job:
                        self.pending.remove(job)
                        self.inflight[kind] = job
                        await self.save_queue()
                if job:
                    self.runners[kind] = self.track(self.run_queued(job))
            await asyncio.sleep(1)

    async def queue_action(self, request):
        data = await request.json()
        kind, action, job_id = data.get("kind"), data.get("action"), str(data.get("id", ""))
        if kind not in {"clone", "transfer", "export", "index"} or action not in {"remove", "first", "clear"}:
            raise ValueError("Invalid queue action.")
        if kind == "clone":
            lock, jobs = self.engine._clone_queue_cv, self.engine._clone_pending_jobs
        elif kind == "transfer":
            lock, jobs = self.engine.TRANSFER_QUEUE.condition, self.engine.TRANSFER_QUEUE.pending
        else:
            lock, jobs = self.queue_lock, self.pending
        async with lock:
            matches = [j for j in jobs if (j.get("kind", kind) == kind) and (action == "clear" or (j.get("job_id") or j.get("id")) == job_id)]
            if not matches:
                raise ValueError("This task has already started or was removed. Refresh the queue.")
            if action == "first":
                jobs.remove(matches[0])
                jobs.insert(0, matches[0])
            else:
                for job in matches:
                    jobs.remove(job)
            if kind == "clone":
                await self.engine._save_clone_queue_snapshot(self.store, list(jobs))
            elif kind == "transfer":
                await self.engine._save_transfer_queue(self.store, self.engine.TRANSFER_QUEUE.snapshot())
            else:
                await self.save_queue()
        return web.json_response({"ok": True})

    async def upload(self, request):
        reader = await request.multipart()
        file_data, filename, command = None, "", ""
        while field := await reader.next():
            if field.name == "command":
                command = (await field.text())[:4096]
            elif field.name == "file":
                filename = Path(field.filename or "upload.txt").name
                buffer = bytearray()
                while chunk := await field.read_chunk():
                    buffer.extend(chunk)
                    if len(buffer) > 2 * 1024**2:
                        raise ValueError("File must be smaller than 2 MB.")
                file_data = bytes(buffer)
        kind = self.validate_command(command, uploaded=True)
        if kind not in {"transfer", "settings"} or file_data is None:
            raise ValueError("Select a settings file or edited index.")
        if kind == "settings" and len(file_data) > 65536:
            raise ValueError("Settings files must be smaller than 64 KB.")
        if kind == "settings" and not re.fullmatch(r"/settings upload (gdrive_token_json|gdrive_token_pickle|msz_credentials|tg_session_string)", command):
            raise ValueError("Choose a supported credential import.")
        if kind == "transfer" and ("--index-done" not in shlex.split(command) or not filename.lower().endswith(".txt")):
            raise ValueError("Choose an edited .txt folder index.")
        if command == "/settings upload tg_session_string":
            try:
                command = "/settings set tg_session_string " + file_data.decode("utf-8-sig").strip()
            except UnicodeError:
                raise ValueError("Choose a UTF-8 text file containing a session string.") from None
            reply = None
        else:
            document = io.BytesIO(file_data)
            document.name = filename
            # A real Telegram document keeps existing import/index handlers identical.
            reply = await self.bot.send_document(request["user"]["id"], document, caption="Mini App file import")
        oid = uuid.uuid4().hex[:12]
        self.track(self.dispatch(oid, kind, command, request["user"], reply))
        return web.json_response({"operation": oid}, status=202)

    async def logs(self, request):
        e = self.engine
        kind = request.query.get("kind", "bot")
        path = e.LOG_FILE
        if kind == "transfer":
            state = await e._load_state(self.store, "transfer:last") or {}
            job_id = str(state.get("job_id", ""))
            if not re.fullmatch(r"[a-f0-9]{12}", job_id):
                return web.json_response({"text": "No transfer log available."})
            path = e.LOG_DIR / "transfers" / (job_id + ".log")
        if not path.is_file():
            return web.json_response({"text": "No log available on this dyno."})
        def tail():
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 48000))
                return handle.read().decode("utf-8", errors="replace")
        text = await asyncio.to_thread(tail)
        settings = await e._load_bot_settings(self.store)
        secrets = [self.token] + [str(settings.get(k) or "") for k in e.SECRET_SETTING_KEYS]
        for value in sorted(secrets, key=len, reverse=True):
            if len(value) > 3:
                text = text.replace(value, "[redacted]")
        text = re.sub(r"mongodb(?:\+srv)?://[^\s]+", "mongodb://[redacted]", text)
        return web.json_response({"text": text})
