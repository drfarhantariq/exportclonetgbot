# Heroku Single Bot Bundle

This folder is a single Telegram control bot that does both jobs:

- `/export` using `run_export`
- `/index` using `run_index`
- `/clone` using `run_clone`

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
