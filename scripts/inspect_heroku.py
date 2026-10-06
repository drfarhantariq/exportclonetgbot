"""Inspect this deployment without printing credentials or raw config values."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import shlex
import sys
from pathlib import Path

from deploy_heroku import DEFAULT_ENV_FILE, ROOT, get_heroku_prefix, parse_env_file


def main() -> None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True)
    parser.add_argument('--label', default='before')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--msz-probe', action='store_true')
    parser.add_argument('--msz-check', action='store_true')
    args = parser.parse_args()
    local = parse_env_file(DEFAULT_ENV_FILE)
    if local.get('HEROKU_API_KEY'):
        os.environ['HEROKU_API_KEY'] = local['HEROKU_API_KEY']
    prefix = get_heroku_prefix()
    def call(command):
        result = subprocess.run(prefix + command + ['-a', args.app], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120 if command[0] == 'run' else 60)
        if result.returncode:
            detail = result.stderr
            for key, value in local.items():
                if value and any(s in key for s in ('TOKEN', 'PASSWORD', 'API_KEY', 'API_HASH', 'SESSION', 'MONGODB_URI', 'SECRET', 'EMAIL')):
                    detail = detail.replace(value, '<redacted>')
            raise RuntimeError(f'Heroku {command[0]} failed (exit {result.returncode}): {detail[-1500:]}')
        return result.stdout
    config = json.loads(call(['config', '--json']))
    secrets = [v for k, v in {**local, **config}.items() if v and any(s in k for s in ('TOKEN', 'PASSWORD', 'API_KEY', 'API_HASH', 'SESSION', 'MONGODB_URI', 'SECRET', 'EMAIL'))]
    def redact(text):
        for value in sorted(secrets, key=len, reverse=True):
            text = text.replace(value, '<redacted>')
        text = re.sub(r'(mongodb(?:\+srv)?://)[^\s]+', r'\1<redacted>', text)
        return text
    if not args.msz_probe:
        logs = redact(call(['logs', '--num', '1500']))
        output = ROOT / 'runtime' / 'logs' / f'heroku_{args.label}.log'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(logs, encoding='utf-8')
        lines = logs.splitlines()
        start = 0
        if args.label != 'before':
            deploy_lines = [i for i, line in enumerate(lines) if 'app[api]' in line and 'Deploy ' in line]
            start = deploy_lines[-1] if deploy_lines else 0
        relevant = [line for line in lines[start:] if re.search(r'flood|status|watch|Traceback|ERROR|crash|State changed|running\.|starting process|resume', line, re.I)]
        print(f'Saved {len(lines)} redacted log lines to {output}')
        print('\n'.join(relevant[-65:]))
        print('Recent log tail:')
        print('\n'.join(lines[-12:]))
        print('Worker formation:')
        print(redact(call(['ps', '--json'])))
        print('Recent releases:')
        print(redact(call(['releases', '--num', '3'])))
        print('Transfer configuration availability:')
        for key in ('MSZ_API_TOKEN', 'MSZ_EMAIL', 'MSZ_PASSWORD', 'GDRIVE_TOKEN_JSON', 'MONGODB_URI', 'MONGODB_DATA_API_URL', 'TG_SESSION_STRING', 'PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            print(f'{key}: {"configured" if config.get(key) else "not set"}')
    if args.msz_probe:
        code = '''import asyncio, os, requests
import app
async def probe():
    store = app.MongoStateStore(app.MONGODB_DATA_API_URL, app.MONGODB_DATA_API_KEY,
        app.MONGODB_DATA_SOURCE, os.getenv("MONGODB_DATABASE", app.MONGODB_DATABASE),
        app.MONGODB_COLLECTION, app.MONGODB_URI)
    settings = await app._load_bot_settings(store)
    base = settings["msz_base_url"].rstrip("/")
    headers = {"Authorization": "Bearer " + settings["msz_api_token"], "Accept": "application/json"}
    for params in ({"per_page": 1, "section": "all"}, {"per_page": 200, "section": "folder", "folder_id": "ODQ2NTB8cGFkZA"}, {"per_page": 200, "section": "folder", "folder_id": "84650"}):
        response = requests.get(base + "/api/v1/drive/file-entries", headers=headers, params=params, timeout=30)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        print("MSZ_PROBE", params, response.status_code, "json", isinstance(payload, (dict, list)))
        if response.status_code == 422 and isinstance(payload, dict):
            print("VALIDATION_ERRORS", payload.get("errors"))
        if response.status_code == 200:
            from MSZDRIVE_uploader.msz_api import parse_entries
            print("ENTRY_COUNT", len(parse_entries(payload)))
            from collections import Counter
            print("PARENT_COUNTS", dict(Counter(str(e.get("parent_id", e.get("parentId"))) for e in parse_entries(payload))))
            print("PAYLOAD_KEYS", list(payload) if isinstance(payload, dict) else "list")
asyncio.run(probe())
'''
        print('Read-only MSZ API probe:')
        print(redact(call(['run', '--no-tty', '--exit-code', shlex.join(['python', '-c', code])])))
    if args.msz_check:
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
    msz = MszApiClient(settings["msz_base_url"], settings["msz_api_token"])
    print("MSZ_API_READY", msz.api_base.endswith("/api/v1"))
    root, entries = msz.resolve_source_entries(source_url="https://cloud.medicalstudyzone.com/drive/folders/ODQ2NTB8cGFkZA")
    print("MSZ_SOURCE_READY", str(root.id) == "84650", "FILES", len(entries))
    drive = GoogleDriveResumableUploader(Path("token.pickle"))
    folder = drive.get_file_metadata(settings["gdrive_folder_id"] or "root")
    print("GDRIVE_DESTINATION_READY", folder.get("mimeType") == "application/vnd.google-apps.folder")
asyncio.run(check())
'''
        print('Read-only transfer source and destination check:')
        print(redact(call(['run', '--no-tty', '--exit-code', shlex.join(['python', '-c', code])])))
    if args.smoke:
        code = '''import asyncio, os, shutil
from pathlib import Path
import app
from MSZDRIVE_uploader import transfer
from transfer_settings import ENV_KEYS
print("TRANSFER_COMMAND_READY", "transfer -" in app._botfather_commands_text())
print("TRANSFER_SETTINGS_READY", all(k in app.BOT_SETTINGS_DEFAULTS for k in ENV_KEYS))
import pickle, json
from google.oauth2.credentials import Credentials
from transfer_settings import decode_upload
sample = Credentials(token=None, refresh_token="smoke-refresh", client_id="smoke-client", client_secret="smoke-secret")
converted = decode_upload("gdrive_token_pickle", pickle.dumps(sample))
print("GDRIVE_PICKLE_UPLOAD_READY", json.loads(converted["gdrive_token_json"])["refresh_token"] == "smoke-refresh")
markup = app._category_settings_markup("transfer", 0)
print("GDRIVE_PICKLE_BUTTON_READY", any(b.callback_data == "settings:upload:gdrive_token_pickle" for row in markup.inline_keyboard for b in row))
print("STATUS_WATCHER_READY", hasattr(app, "STATUS_EDIT_RETRY_AT"))
async def check_storage():
    store = app.MongoStateStore(app.MONGODB_DATA_API_URL, app.MONGODB_DATA_API_KEY,
        app.MONGODB_DATA_SOURCE, os.getenv("MONGODB_DATABASE", app.MONGODB_DATABASE),
        app.MONGODB_COLLECTION, app.MONGODB_URI)
    await store.load("bot:settings")
    print("MONGODB_SETTINGS_READY", True)
asyncio.run(check_storage())
async def check_browser():
    from playwright.async_api import async_playwright
    bundled = Path(".playwright-browsers").resolve()
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    async with async_playwright() as p:
        kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        binary = Path("/app/.apt/usr/lib/chromium/chromium")
        executable = str(binary) if binary.is_file() else shutil.which("chromium")
        if executable:
            kwargs["executable_path"] = executable
        try:
            browser = await p.chromium.launch(**kwargs)
            page = await browser.new_page()
            await page.set_content("<title>Heroku browser check</title>")
            print("BROWSER_READY", await page.title())
            await browser.close()
        except Exception as exc:
            print("BROWSER_NOT_READY", str(exc)[:1200])
asyncio.run(check_browser())
'''
        print('Remote smoke check:')
        print(redact(call(['run', '--no-tty', '--exit-code', shlex.join(['python', '-c', code])])))


if __name__ == '__main__':
    main()
