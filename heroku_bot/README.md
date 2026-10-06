# Heroku Single Bot Bundle

This folder is a single Telegram control bot that does both jobs:

- `/export` using `run_export`
- `/index` using `run_index`
- `/clone` using `run_clone`
- `/transfer` using the shared `MSZDRIVE_uploader/transfer.py` entry point

It is designed for Heroku worker dynos and persists last command profiles in MongoDB Data API, so restarts do not lose essential bot state.

## Essential Files Copied Here

- `app.py`
- `clone_topic_by_link.py`
- `export_topic_list.py`
- `config.py`
- `telegram_client.py`
- `topic_utils.py`
- `message_classifier.py`
- `models.py`
- `config.yaml`
- `config.example.yaml`
- `Procfile`
- `runtime.txt`

Runtime output folder:

- `runtime/exports` for generated txt exports
- `runtime/state` for local JSON snapshots

## Local Development (Run on Your PC)

You can run this exact Heroku worker locally for real-time testing.

1. Create and activate a virtual environment:

```bash
cd heroku_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

2. Create local env file:

```bash
cp .env.example .env
```

3. Edit `.env` and fill the required values:

- `TG_API_ID`
- `TG_API_HASH`
- `TG_SESSION_STRING`
- `HEROKU_BOT_TOKEN`
- `BOT_ADMIN_USER_IDS`

For state persistence during local testing, choose one option:

- Option A: set `MONGODB_URI` (recommended for local dev)
- Option B: set all Data API vars: `MONGODB_DATA_API_URL`, `MONGODB_DATA_API_KEY`, `MONGODB_DATA_SOURCE`, `MONGODB_DATABASE`, `MONGODB_COLLECTION`

To speed up Telegram downloads like WZML, also set:

- `HELPER_TOKENS` with one or more helper bot tokens separated by spaces or commas
- `HYPER_DUMP_CHAT` with a private/channel dump chat id where the user session can post and every helper bot can read
- `HYPER_THREADS` to tune the number of parallel chunks

4. Run the bot:

```bash
python app.py
```

You should see: `Heroku topic bot is running.`

5. Test in Telegram from an admin account:

- `/start`
- `/status`
- `/export ...`
- `/clone ...`

### Fast Edit-Test Loop

Use one terminal to run the bot and another terminal to edit code.
After each change, stop and restart the process.

If you want auto-restart on file changes:

```bash
pip install watchfiles
watchfiles --filter python "python app.py" .
```

This gives you a local workflow very close to Heroku worker behavior.

## Bot Commands

`/status` and `/clone status` panels keep refreshing while open, including
between queued jobs and when only a transfer/export/index is running. Tap
**Close** to stop watching a panel. Temporary state-loading or network failures
are retried. When Telegram requests a flood wait, status edits pause for its
full requested duration and then resume automatically. A failed manual refresh
does not mark the new text as delivered. Bot restarts discard in-memory panel
watchers; send `/status` again or tap **Refresh** to start one again.

- `/start`
- `/help`
- `/status`
- `/export --topic-link <link> [options]`
- `/export last`
- `/index --topic-link <link> [options]`
- `/index last`
- `/clone --source-link <link> --destination-link <link> [options]`
- `/clone last`
- `/log`
- `/transfer <source> [destination] --up msz|gd|telegram|both [options]`
- `/transfer help`, `/transfer status`, `/transfer last`, `/transfer resume`
- `/cancel transfer`

### Transfers from Telegram

Send these commands privately to the bot from a configured admin account:

```text
/transfer <msz_folder_url> --up gd --gdrive-folder-id <destination_id>
/transfer <drive_folder_url> "msz:Target Folder" --up msz
/transfer <msz_folder_url> <telegram_topic_link> --up telegram
/transfer <drive_folder_url> <telegram_topic_link> --up telegram
/transfer <telegram_topic_link> --up gd --gdrive-folder-id <destination_id>
/transfer <telegram_topic_link> --up msz
/transfer <telegram_topic_link> --up both --gdrive-folder-id <destination_id>
```

The bot runs one transfer at a time in a separate Python process, updates its
progress message, and includes active transfers in `/status`. Additional
transfer requests are rejected while one is active. `/cancel transfer` stops
the process. `/transfer last` repeats the command; `/transfer resume` adds
`--resume` to skip successful files recorded by the existing transfer scripts.
Fresh runs are the default, so repeating without `--resume` can upload again.
Use `--dry-run` to preview a transfer. Output is also written to the bot log.

Transfers have a live panel styled like the clone panel. It shows indexing,
the current filename, download/upload/verification stage, byte progress,
measured speed, elapsed time, estimated remaining time, results, and bot stats.
It refreshes every five seconds even when the worker is quiet and respects
Telegram flood waits. ETA is shown once enough progress is available.

When a transfer finishes, the same message becomes a compact task summary:
task name and ID, size, final time taken, input/output modes, file type (or
folder/batch), requester, source, destination, and uploaded/skipped/failed counts.
The final time is frozen, and live progress and bot stats are removed from the
receipt. Failed and cancelled transfers show their partial results; a completed
batch with failed files is explicitly marked as completed with errors.

New `/transfer` commands join a FIFO queue while a transfer is running.
The panel has **Cancel transfer**, **Transfer Queue**, **Refresh**, **TStats**,
and **Close** buttons. Closing a panel leaves the transfer running.

- `/transfer queue` opens the live queue; each waiting job has a remove button.
- `/transfer cancel <job_id>` cancels that active or waiting job (short IDs work).
- `/transfer clear-queue` removes waiting jobs and keeps the active transfer.
- `/cancel transfer` cancels the active transfer; the next queued job starts.
- `/transfer logs` downloads diagnostics separately from the progress panel.

Queue order, edited index documents, and transfer checkpoints are saved to
MongoDB. A restart resumes an unfinished active job with `--resume`, then
continues the waiting queue. Credentials stay in bot settings; queued jobs
retain their chosen destination.

To preserve Telegram text headings as folders:

1. Send `/transfer <telegram_topic_link> --index`.
2. Download and edit the returned `.txt` file, then send it back to the bot.
3. Reply to that document with `/transfer <telegram_topic_link> --index-done --up gd`
   (or `msz` / `both`). Add `--above` when headings follow their media.

Configure `MSZ_API_TOKEN` for MSZ API operations, plus `MSZ_EMAIL` and
`MSZ_PASSWORD` for large browser uploads. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE`
to your Heroku Chromium binary; the bundle's Aptfile includes Chromium.
Telegram transfers use `TG_SESSION_STRING_MSZ` if set, otherwise
`TG_SESSION_STRING`. `TG_DOWNLOAD_MODE=auto` allows download fallback.

For Google Drive, set `GDRIVE_TOKEN_JSON` to the authorized-user OAuth JSON
produced by `credentials.to_json()` (including its refresh token). The existing
`GDRIVE_TOKEN_PICKLE` file remains supported for local use. Set
`GDRIVE_FOLDER_ID` for a default Drive destination and
`TELEGRAM_TARGET_TOPIC_LINK` for a default Telegram destination. Explicit Drive
destinations should use `--gdrive-folder-id` to override that environment default.
Keep credentials in Heroku config vars, not committed files.

The Heroku `bin/post_compile` hook bundles Playwright's headless Chromium into
the slug for large MSZ uploads; the transfer worker discovers this browser
automatically. This avoids relying on Ubuntu's Chromium Snap launcher.

You can also configure and update credentials directly in the deployed bot:

- Open `/settings` → **Transfer**, choose a setting, tap **Edit**, then send
  the new value or upload a text/JSON file.
- For Google Drive, choose **Google Drive OAuth JSON** → **Edit** and upload
  the authorized-user JSON file, or use `/settings upload gdrive_token_json`
  and send the file. You can also reply to an existing JSON document with
  that command. Raw JSON pasted through `/settings set gdrive_token_json {...}`
  is supported without stripping its quotes.
- To use your existing `token.pickle`, tap **Upload Google Drive token.pickle**
  in Transfer settings, or send `/settings upload gdrive_token_pickle` and
  upload the document (or reply to it with that command). The bot reads only
  supported credential objects and saves their OAuth fields as JSON in MongoDB.
  This replaces any previously saved Google Drive OAuth JSON.
- Set the default destination with `/settings set gdrive_folder_id <folder_id>`.
  Then `/transfer <source_link> --up gd` uses that folder without a destination
  argument. Use `--gdrive-folder-id <other_id>` for a one-time override.
- Update MSZ settings individually with `/settings set msz_email <email>`,
  `/settings set msz_password <password>`, and
  `/settings set msz_api_token <token>`. Alternatively tap **Upload MSZ
  credentials JSON**, or use `/settings upload msz_credentials`, then send
  a JSON object with `email`, `password`, and/or `api_token` fields.
- `/settings cancel` cancels a pending edit. Credential values are masked in
  settings displays and acknowledgements.

Saved settings apply to future transfers. MongoDB persistence keeps them
across restart/redeployment, using the same `bot:settings` document as other
settings. If MongoDB is unavailable, the bot explicitly reports a local-only
save, which will not survive dyno replacement. Uploads are limited to 64 KB
and processed in memory. An already-running transfer retains the credentials
it started with.

You do **not** need to provide a destination on every command:

```text
/transfer <telegram_topic_link> --up msz
/transfer <drive_folder_url> --up msz
/transfer <msz_folder_url> --up gd
/transfer <drive_folder_url> --up telegram
```

MSZ uploads use a folder named after the Telegram topic or Google Drive source
folder when no default MSZ folder is set. **Default MSZ folder** in Transfer
settings chooses a destination instead. Google Drive uses **Default Drive
folder**, falling back to Drive root. Telegram requires **Default Telegram
topic** to be saved when its destination link is omitted. To choose a different
folder for a particular transfer, pass `--msz-target-folder` or
`--gdrive-folder-id`; to choose a Telegram destination, pass its topic link.

`scripts/deploy_heroku.py` now includes the transfer package and config vars
when building the flattened Heroku bundle. No live deployment is performed
by editing these files. MongoDB saves the last command profile; per-file
resume state, edited indexes and temporary downloads remain on the dyno's
ephemeral filesystem. They do not survive dyno replacement, so cross-dyno
resume cannot reliably skip earlier uploads. Transfers are not automatically
restarted after a bot restart.

Shortcuts are supported:

- `/export <link>`
- `/index <topic_link>`
- `/clone <source_link> <destination_link>`

`/index` scans the linked forum topic for text messages only, turns each text message into a clickable link, and sends the generated index back into the same topic.

Useful `/index` options:

- `--onwards` starts at the linked message instead of scanning from the topic root.
- `--batch-size N` controls how many message IDs are fetched per Telegram request.
- `--batch-delay-sec S` waits between batches to be gentler with flood limits.
- `--header "INDEX"` changes the index header text.

## Required Heroku Config Vars

- `TG_API_ID`
- `TG_API_HASH`
- `TG_SESSION_STRING`
- `HEROKU_BOT_TOKEN`
- `BOT_ADMIN_USER_IDS` (comma-separated Telegram user IDs)
- `MONGODB_DATA_API_URL`
- `MONGODB_DATA_API_KEY`
- `MONGODB_DATA_SOURCE`
- `MONGODB_DATABASE`
- `MONGODB_COLLECTION` (for example `bot_state`)

Optional:

- `HEROKU_RUNTIME_DIR` (defaults to `heroku_bot/runtime`)
- `HELPER_TOKENS` for WZML-style helper bot chunk downloads
- `HYPER_DUMP_CHAT` or `LEECH_DUMP_CHAT` for the helper-bot dump chat
- `HYPER_THREADS` to override automatic chunk parallelism
- `HYPER_MAX_FLOOD_WAIT` to stop helper-client downloads and fall back when Telegram asks for a long wait
- `LOG_FILE_PATH` for the file sent by `/log`

## MSZ Hybrid Uploader

The `MSZDRIVE_uploader` package uploads to `cloud.medicalstudyzone.com` with a hybrid route: files below `MSZ_API_MAX_BYTES` use the MSZ API, while larger files use Playwright/Chromium browser automation.

Set these values in local `.env` or Heroku config vars:

```bash
MSZ_BASE_URL=https://cloud.medicalstudyzone.com
MSZ_API_TOKEN=...
MSZ_EMAIL=...
MSZ_PASSWORD=...
MSZ_API_MAX_BYTES=100000000
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
```

Examples:

```bash
python -m MSZDRIVE_uploader.msz_upload --source local --path /tmp/files --target-folder CoreBTR
python -m MSZDRIVE_uploader.msz_upload --source gdrive --url "https://drive.google.com/drive/folders/..." --target-folder TestUpload
python -m MSZDRIVE_uploader.msz_upload --source telegram-topic --topic-link "https://t.me/c/..." --target-folder TopicUpload
```

For large browser uploads, pass `--browser-folder-url` or set `MSZ_BROWSER_FOLDER_URL` when you want Playwright to open an existing MSZ folder URL before selecting the file.

## MSZ to Google Drive Reverse Sync

The reverse sync downloads from MSZ Drive and uploads to Google Drive with the same core approach used by WZML: Google Drive API v3, OAuth `token.pickle`, folder creation, and resumable `MediaFileUpload` chunks of `100 MB`.

Set these values in local `.env` or Heroku config vars:

```bash
MSZ_BASE_URL=https://cloud.medicalstudyzone.com
MSZ_API_TOKEN=...
GDRIVE_TOKEN_PICKLE=/path/to/token.pickle
GDRIVE_FOLDER_ID=...
```

Choose the MSZ source with one of:

```bash
MSZ_SOURCE_PATH=TestUpload
MSZ_SOURCE_ID=
MSZ_SOURCE_URL=
```

Short examples:

```bash
python -m MSZDRIVE_uploader.msz_to_gdrive "https://cloud.medicalstudyzone.com/drive/folders/ODQwMDl8cGFkZA"
python -m MSZDRIVE_uploader.msz_to_gdrive "https://cloud.medicalstudyzone.com/drive/folders/ODQwMDl8cGFkZA" "<folder_id>"
python -m MSZDRIVE_uploader.msz_to_gdrive TestUpload
```

Explicit examples:

```bash
python -m MSZDRIVE_uploader.msz_to_gdrive --msz-source-path "TestUpload" --gdrive-folder-id "<folder_id>"
python -m MSZDRIVE_uploader.msz_to_gdrive --msz-source-id "<msz_folder_id>" --gdrive-folder-id "<folder_id>"
python -m MSZDRIVE_uploader.msz_to_gdrive --msz-source-path "TestUpload" --gdrive-folder-id "<folder_id>" --retry-failed-only
```

The sync preserves MSZ folder structure under the Google Drive destination folder, processes one file at a time, keeps partial `.part` downloads for resume, and verifies Google Drive uploads by file size.

## WZML-Style Fast Telegram Transfers

For the helper-client downloader to work reliably, create a dump chat and add:

- the `TG_SESSION_STRING` user account, with permission to send messages
- every helper bot from `HELPER_TOKENS`, with permission to read messages

Set the dump chat id as `HYPER_DUMP_CHAT`. Without a dump chat, helper bots can only download from source chats they can already access. If the helper path fails, the bot automatically falls back to the main Pyrogram download so clones continue instead of stopping.

Video uploads preserve duration and dimensions from the source message. If a source message does not expose those values, the bot can probe the downloaded file with `ffprobe`; on Heroku, add the apt buildpack so the root `Aptfile` installs `ffmpeg`:

```bash
heroku buildpacks:add --index 1 heroku-community/apt -a <your-app-name>
```

There is also a `heroku_bot/Aptfile` for deployments where this folder is used as the Heroku app root. The `ffmpeg` apt package includes both `ffmpeg` and `ffprobe`.

The bot also extracts a non-black JPEG thumbnail from the video itself for each video/animation upload, so Telegram does not use a black opening frame. It samples several points through the file and rejects near-black frames. Set `GENERATE_VIDEO_THUMBNAILS=false` to disable this.

## Heroku Setup (Recommended)

### Local deploy script

You can deploy from this workspace without opening the Colab notebook:

```bash
python scripts/deploy_heroku.py --app <your-app-name>
```

The script reads config vars from `heroku_bot/.env`, prepares a clean temporary bundle from `heroku_bot/`, sets Heroku config vars, adds the apt buildpack for `ffmpeg`, pushes to Heroku, and scales `worker=1`.

Useful options:

```bash
# Update changed bot code on the same Heroku app
python scripts/deploy_heroku.py --app <your-app-name> --redeploy

# Delete the old Heroku app and deploy from scratch with the same name
python scripts/deploy_heroku.py --app <your-app-name> --recreate

# Show logs only, without redeploying
python scripts/deploy_heroku.py --app <your-app-name> --logs

# Create the Heroku app if it does not already exist
python scripts/deploy_heroku.py --app <your-app-name> --create-app --region eu

# Set/override a config var without editing .env
python scripts/deploy_heroku.py --app <your-app-name> --config HYPER_THREADS=4
```

`--recreate` destroys the Heroku app before deploying, so its Heroku config and dynos are rebuilt from your local `heroku_bot/.env`. MongoDB data stored outside Heroku is not deleted.

The script installs the Heroku CLI automatically if it is missing. If `HEROKU_EMAIL` and `HEROKU_API_KEY` are present in `heroku_bot/.env`, it also writes `~/.netrc` automatically for API-key auth like the Colab notebook:

```bash
python scripts/deploy_heroku.py --app <your-app-name>
```

For that mode, add `HEROKU_EMAIL` and `HEROKU_API_KEY` to `heroku_bot/.env`, or pass them as `--heroku-email` and `--heroku-api-key`. Use `--no-write-netrc` if you want to rely on an existing `heroku login` session instead.

The Colab notebook remains available as an alternative deploy path.

1. Create app and set stack:

```bash
heroku create <your-app-name>
heroku stack:set heroku-24 -a <your-app-name>
```

2. Set all config vars:

```bash
heroku config:set TG_API_ID=<id> TG_API_HASH=<hash> TG_SESSION_STRING='<session>' HEROKU_BOT_TOKEN='<bot_token>' BOT_ADMIN_USER_IDS='123456789' MONGODB_DATA_API_URL='<url>/action' MONGODB_DATA_API_KEY='<api_key>' MONGODB_DATA_SOURCE='<data_source>' MONGODB_DATABASE='<db>' MONGODB_COLLECTION='bot_state' -a <your-app-name>
```

3. Deploy from repo root:

```bash
git push heroku main
```

4. Scale worker dyno:

```bash
heroku ps:scale worker=1 -a <your-app-name>
```

5. Check logs:

```bash
heroku logs --tail -a <your-app-name>
```

You should see `Heroku topic bot is running.`

## Notes

- The app uses a user session (`TG_SESSION_STRING`) for Telegram account actions and a bot token (`HEROKU_BOT_TOKEN`) for command control.
- Only users in `BOT_ADMIN_USER_IDS` can run commands.
- `/export last` and `/clone last` resume the last saved profile from MongoDB.


`/status` shows only active and queued jobs. Use `/status transfer`, `/status clone`,
`/status index`, or `/status export` to filter the view. The Previous Jobs button
shows saved completed, failed, and cancelled jobs, with eight jobs per page and
up to 50 retained jobs. The filter stays selected across Refresh, TStats, and Back.
Each new `/status` request replaces the previous status panel in the same chat
and cancels its refresh loop. The panel reference and job history are saved in
MongoDB so replacement and history work after a restart.


Transfer Settings contains saved ON/OFF defaults for dry run, resume, continue on
error, retry failed files only, keep downloads, delete failed downloads, captions
as filenames, onwards, files above headings, remote verification, strict browser
verification, browser folder title, visible browser, and Telegram folder index
mode. Each toggle has Turn on/Turn off and Reset buttons. The visible browser
option requires a desktop display and should stay off on Heroku.
Batch size, delay, Telegram download mode, browser folder URL, and Chromium path
are editable values. Credentials and destination folder settings remain in the
same menu. New jobs capture the settings when submitted; changing settings does
not change active/queued jobs. Explicit flags override saved defaults, including
negative flags such as `--no-dry-run`, `--no-keep-downloads`, and `--no-resume`.
`/transfer last` and `/transfer resume` reuse the original job's saved options.


Transfer, clone, and index panels use the ABCEmoji Telegram pack for titles
and section headings, with a blank line after each heading. Animated lightning and timer icons accompany progress.
Filenames, links, byte counts, percentages, and speeds stay readable text.
Combined panels limit custom emoji entities to 100 and use readable text for
additional letters. If Telegram rejects or removes custom emoji entities, the panel automatically
falls back to ordinary lettering. Emoji document IDs and their exact alt emoji
are bundled in transfer_emoji_packs.json; no runtime pack download is needed.
