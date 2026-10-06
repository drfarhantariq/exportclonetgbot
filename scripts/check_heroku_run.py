"""Read deployment and verification results directly from the Heroku API."""
import os
import re
import requests
import sys
import shlex
from pathlib import Path
from deploy_heroku import DEFAULT_ENV_FILE, parse_env_file

config = parse_env_file(DEFAULT_ENV_FILE)
headers = {'Authorization': 'Bearer ' + config['HEROKU_API_KEY'],
           'Accept': 'application/vnd.heroku+json; version=3'}
base = 'https://api.heroku.com/apps/exportclonemszbot'
response = requests.get(base + '/dynos', headers=headers, timeout=30)
response.raise_for_status()
for dyno in response.json():
    print('DYNO', dyno['name'], dyno['state'])
    if '--start-check' in sys.argv and dyno['type'] == 'run' and 'MSZ_SOURCE_READY' in dyno['command']:
        cleanup = requests.delete(base + '/dynos/' + dyno['id'], headers=headers, timeout=30)
        cleanup.raise_for_status()
        print('STOPPED_OLD_CHECK', dyno['name'])
if '--start-check' in sys.argv:
    code = '''import asyncio, os
from pathlib import Path
import app
from MSZDRIVE_uploader.msz_api import MszApiClient
from MSZDRIVE_uploader.gdrive_upload import GoogleDriveResumableUploader
async def check():
    store = app.MongoStateStore(app.MONGODB_DATA_API_URL, app.MONGODB_DATA_API_KEY,
        app.MONGODB_DATA_SOURCE, os.getenv("MONGODB_DATABASE", app.MONGODB_DATABASE),
        app.MONGODB_COLLECTION, app.MONGODB_URI)
    settings = await app._load_bot_settings(store)
    msz = MszApiClient(settings["msz_base_url"], settings["msz_api_token"], timeout=15)
    print("MSZ_API_READY", msz.api_base.endswith("/api/v1"), flush=True)
    root, entries = msz.resolve_source_entries(source_url="https://cloud.medicalstudyzone.com/drive/folders/ODQ2NTB8cGFkZA", log=print)
    print("MSZ_SOURCE_READY", str(root.id) == "84650", "FILES", len(entries), flush=True)
    drive = GoogleDriveResumableUploader(Path("token.pickle"))
    folder = drive.get_file_metadata(settings["gdrive_folder_id"] or "root")
    print("GDRIVE_DESTINATION_READY", folder.get("mimeType") == "application/vnd.google-apps.folder", flush=True)
asyncio.run(check())
'''
    started = requests.post(base + '/dynos', headers=headers, json={
        'command': shlex.join(['python', '-u', '-c', code]), 'attach': False, 'size': 'Basic'}, timeout=30)
    started.raise_for_status()
    print('STARTED_CHECK', started.json()['name'])
if '--start-panel-check' in sys.argv:
    code = '''import os, io, json, asyncio
from contextlib import redirect_stdout
import app
from transfer_progress import new_state, EventStream, format_status, PREFIX
from transfer_queue import TransferQueue
from MSZDRIVE_uploader.progress_events import ByteProgress
state = new_state(["msz:Smoke Course", "--up", "gd"], job_id="a" * 12)
state["phase"] = "running"
stream = EventStream(state)
for event in ({"event":"totals", "total_files":2}, {"event":"file", "index":1, "file_name":"Smoke Lecture.mp4", "file_size":100}, {"event":"bytes", "stage":"uploading", "bytes_done":50, "file_size":100}):
    stream.feed(PREFIX + json.dumps(event) + "\\n")
panel = format_status(state)
print("TRANSFER_PANEL_READY", "UPLOADING" in panel and "Smoke Lecture.mp4" in panel and "--up" not in panel)
assert all(section in panel for section in ("Overall Progress", "Current File", "Route &amp; Queue", "Results"))
file_line = next(line for line in panel.splitlines() if "50.0%" in line)
assert all(value in file_line for value in ("[●●●●●○○○○○]", "UPLOADING", "Speed"))
print("TRANSFER_FILE_SECTION_READY", True)
app.TRANSFER_QUEUE.active = state
queued = new_state(["msz:Second Course", "--up", "gd"], job_id="b" * 12)
app.TRANSFER_QUEUE.pending.append(queued)
buttons = [button.text for row in app._transfer_markup(state["job_id"]).inline_keyboard for button in row]
print("TRANSFER_CONTROLS_READY", all(any(word in text for text in buttons) for word in ("Cancel", "Transfer Queue", "Refresh", "TStats", "Close")))
queue = TransferQueue()
queue.hydrate(app.TRANSFER_QUEUE.snapshot())
print("TRANSFER_RECOVERY_READY", len(queue.pending) == 2 and "--resume" in queue.pending[0]["argv"])
os.environ["TRANSFER_PROGRESS_EVENTS"] = "1"
with redirect_stdout(io.StringIO()) as out:
    ByteProgress("downloading", "smoke", 100)(50, 100)
print("TRANSFER_TELEMETRY_READY", PREFIX in out.getvalue())
async def mongo_check():
    store = app.MongoStateStore(app.MONGODB_DATA_API_URL, app.MONGODB_DATA_API_KEY, app.MONGODB_DATA_SOURCE, os.getenv("MONGODB_DATABASE", app.MONGODB_DATABASE), app.MONGODB_COLLECTION, app.MONGODB_URI)
    await store.load("transfer:queue")
    print("TRANSFER_QUEUE_STORAGE_READY", True)
asyncio.run(mongo_check())
'''
    started = requests.post(base + '/dynos', headers=headers, json={
        'command': shlex.join(['python', '-u', '-c', code]), 'attach': False, 'size': 'Basic'}, timeout=30)
    started.raise_for_status()
    print('STARTED_PANEL_CHECK', started.json()['name'])
if '--fetch-emoji-packs' in sys.argv:
    code = '''import os, json, requests
for name in ("RetroFontEmoji",):
    try:
        result = requests.post("https://api.telegram.org/bot" + os.environ["HEROKU_BOT_TOKEN"] + "/getStickerSet", data={"name": name}, timeout=30).json()
        if not result.get("ok"):
            print("EMOJI_PACK_ERROR", name, result.get("description"))
            continue
        stickers = result["result"]["stickers"]
        print("EMOJI_PACK_DATA", json.dumps({"name": name, "stickers": [{"i": i, "alt": item.get("emoji"), "id": item.get("custom_emoji_id"), "animated": item.get("is_animated"), "video": item.get("is_video")} for i, item in enumerate(stickers)]}, ensure_ascii=True), flush=True)
    except Exception as exc:
        print("EMOJI_PACK_ERROR", name, type(exc).__name__)
'''
    started = requests.post(base + '/dynos', headers=headers, json={
        'command': shlex.join(['python', '-u', '-c', code]), 'attach': False, 'size': 'Basic'}, timeout=30)
    started.raise_for_status()
    print('STARTED_EMOJI_PACK_LOOKUP', started.json()['name'])
if '--start-download-synthetic-check' in sys.argv:
    code = '''import hashlib, inspect, tempfile
from pathlib import Path
from types import SimpleNamespace
from MSZDRIVE_uploader.gdrive_download import download, DOWNLOAD_CHUNK_SIZE
from MSZDRIVE_uploader.gdrive_upload import GoogleDriveResumableUploader
assert DOWNLOAD_CHUNK_SIZE == 100 * 1024**2
assert inspect.signature(GoogleDriveResumableUploader.download_file).parameters["chunk_size"].default == DOWNLOAD_CHUNK_SIZE
print("GDRIVE_WZML_REQUEST_SIZE_READY", True)
data = bytes(range(256)) * 8192
calls, progress = [], []
class SyntheticSession:
    def get(self, url, headers, **kwargs):
        assert url == "synthetic://memory"
        calls.append(headers["Range"])
        start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        body = data[start:end+1]
        return SimpleNamespace(status_code=206, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": "synthetic"},
            raise_for_status=lambda: None, close=lambda: None,
            iter_content=lambda **kwargs: (body[i:i+65536] for i in range(0, len(body), 65536)))
with tempfile.TemporaryDirectory() as directory:
    result = download(SyntheticSession(), "synthetic://memory", Path(directory) / "test.bin", total_size=len(data),
                      chunk_size=1024**2, progress_callback=lambda done, total: progress.append(done))
    with result.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").digest() == hashlib.sha256(data).digest()
    assert len(set(progress)) > 2 and len(calls) == 2
print("GDRIVE_DOWNLOAD_INTEGRITY_READY", True)
print("GDRIVE_STREAM_PROGRESS_READY", True)
'''
    started = requests.post(base + '/dynos', headers=headers, json={
        'command': shlex.join(['python', '-u', '-c', code]), 'attach': False, 'size': 'Basic'}, timeout=30)
    started.raise_for_status()
    print('STARTED_SYNTHETIC_DOWNLOAD_CHECK', started.json()['name'])
if '--start-options-check' in sys.argv:
    code = '''import asyncio, os
import app
from transfer_settings import TOGGLE_OPTIONS
from transfer_control import parse_command
from MSZDRIVE_uploader.transfer import _build_parser
async def check():
    app._assert_settings_categories_complete()
    assert all(key in app.BOT_TOGGLE_SETTING_KEYS and key in app.SETTINGS_CATEGORIES["transfer"] for key in TOGGLE_OPTIONS)
    print("TRANSFER_SETTINGS_TOGGLES_READY", len(TOGGLE_OPTIONS) == 14)
    settings = dict(app.BOT_SETTINGS_DEFAULTS, transfer_default_dry_run=True, transfer_default_keep_downloads=True)
    argv = parse_command("msz:Smoke --up gd", config=app.DEFAULT_CONFIG_PATH, runtime=app.RUNTIME_DIR / "transfers", defaults=settings)
    args = _build_parser().parse_args(argv)
    assert args.dry_run and args.keep_downloads
    print("TRANSFER_SAVED_FLAGS_READY", True)
    overridden = parse_command("msz:Smoke --up gd --no-dry-run", config=app.DEFAULT_CONFIG_PATH, runtime=app.RUNTIME_DIR / "transfers", defaults=settings)
    assert not _build_parser().parse_args(overridden).dry_run
    print("TRANSFER_FLAG_OVERRIDES_READY", True)
    buttons = [b.text for page in range(app._category_page_count("transfer")) for row in app._category_settings_markup("transfer", page, settings).inline_keyboard for b in row]
    assert "Dry run · ON" in buttons and "Resume previous successes · OFF" in buttons
    print("TRANSFER_TOGGLE_DISPLAY_READY", True)
    settings["transfer_default_dry_run"] = False
    assert _build_parser().parse_args(argv).dry_run
    print("TRANSFER_QUEUED_OPTIONS_READY", True)
    store = app.MongoStateStore(app.MONGODB_DATA_API_URL, app.MONGODB_DATA_API_KEY, app.MONGODB_DATA_SOURCE, os.getenv("MONGODB_DATABASE", app.MONGODB_DATABASE), app.MONGODB_COLLECTION, app.MONGODB_URI)
    loaded = await app._load_bot_settings(store)
    assert all(isinstance(loaded[key], bool) for key in TOGGLE_OPTIONS)
    print("TRANSFER_OPTION_STORAGE_READY", True)
asyncio.run(check())
'''
    started = requests.post(base + '/dynos', headers=headers, json={
        'command': shlex.join(['python', '-u', '-c', code]), 'attach': False, 'size': 'Basic'}, timeout=30)
    started.raise_for_status()
    print('STARTED_OPTIONS_CHECK', started.json()['name'])
if '--start-status-check' in sys.argv:
    code = '''import asyncio, os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import app
async def check():
    data = {"clone:last": {"phase": "completed", "started_at": 1},
            "index:last": {"phase": "failed", "started_at": 2},
            "transfer:last": {"phase": "cancelled", "job_id": "a" * 12}}
    async def load(key): return data.get(key)
    async def save(key, value): data[key] = value
    store = SimpleNamespace(load=AsyncMock(side_effect=load), save=AsyncMock(side_effect=save))
    async def document(store, key, state): await store.save(key, state)
    with patch.object(app, "_save_transfer_doc", side_effect=document):
        text, _ = await app._load_status_view_text(store, "active:all")
        assert "No active jobs" in text and "COMPLETED" not in text
        print("STATUS_ACTIVE_ONLY_READY", True)
        history, _ = await app._load_status_view_text(store, "history:transfer:0")
        assert "CANCELLED" in history and "COMPLETED" not in history
        print("STATUS_HISTORY_FILTER_READY", True)
        data["transfer:last"] = {"phase": "running"}
        with patch.object(app, "ACTIVE_TRANSFER_TASK", SimpleNamespace(done=lambda: False)), patch.object(app, "format_transfer_status", return_value="TRANSFER PANEL"):
            text, _ = await app._load_status_view_text(store, "active:transfer")
            assert "TRANSFER PANEL" in text and "COMPLETED" not in text
        print("STATUS_TRANSFER_FILTER_READY", True)
        client = MagicMock(delete_messages=AsyncMock())
        app.STATUS_CHAT_PANELS[123] = 500
        panels = [SimpleNamespace(chat=SimpleNamespace(id=123), id=501+i) for i in range(2)]
        messages = [SimpleNamespace(chat=SimpleNamespace(id=123), reply_text=AsyncMock(return_value=p)) for p in panels]
        await asyncio.gather(*(app._replace_chat_status(client, m, store, "active:all", AsyncMock()) for m in messages))
        assert [c.args for c in client.delete_messages.await_args_list] == [(123, 500), (123, 501)]
        print("STATUS_REPLACEMENT_READY", True)
    real_store = app.MongoStateStore(app.MONGODB_DATA_API_URL, app.MONGODB_DATA_API_KEY, app.MONGODB_DATA_SOURCE, os.getenv("MONGODB_DATABASE", app.MONGODB_DATABASE), app.MONGODB_COLLECTION, app.MONGODB_URI)
    await real_store.load("status:history")
    print("STATUS_STORAGE_READY", True)
asyncio.run(check())
'''
    started = requests.post(base + '/dynos', headers=headers, json={
        'command': shlex.join(['python', '-u', '-c', code]), 'attach': False, 'size': 'Basic'}, timeout=30)
    started.raise_for_status()
    print('STARTED_STATUS_CHECK', started.json()['name'])
response = requests.post(base + '/log-sessions', headers=headers,
                         json={'lines': 1500, 'tail': False}, timeout=30)
response.raise_for_status()
try:
    logs = requests.get(response.json()['logplex_url'], timeout=30)
    logs.raise_for_status()
except requests.RequestException:
    print('Log retrieval temporarily unavailable.')
    sys.exit(1)
redacted_logs = logs.text
for key, value in config.items():
    if value and any(word in key for word in ('TOKEN', 'PASSWORD', 'API_KEY', 'API_HASH', 'SESSION', 'MONGODB_URI', 'SECRET', 'EMAIL')):
        redacted_logs = redacted_logs.replace(value, '<redacted>')
Path('runtime/logs/heroku_msz_verification.log').write_text(redacted_logs, encoding='utf-8')
progress = []
for line in redacted_logs.splitlines():
    if re.search(r'app\[run\.[^]]+\]:|heroku\[run\.[^]]+\]:', line) and line[:10] == '2026-10-06' and line[11:16] >= '09:19':
        for key, value in config.items():
            if value and any(word in key for word in ('TOKEN', 'PASSWORD', 'API_KEY', 'API_HASH', 'SESSION', 'MONGODB_URI', 'SECRET', 'EMAIL')):
                line = line.replace(value, '<redacted>')
        if any(word in line for word in ('_READY', '_SPEED_MBPS', 'BENCHMARK_SKIPPED', 'Traceback', 'Error', 'Process exited', 'State changed')):
            print(line)
        elif 'app[run.' in line:
            progress.append(line)
print('\n'.join(progress[-2:]))
