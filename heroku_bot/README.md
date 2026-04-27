# Heroku Single Bot Bundle

This folder is a single Telegram control bot that does both jobs:

- `/export` using `run_export`
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
- `/clone --source-link <link> --destination-link <link> [options]`
- `/clone last`

Shortcuts are supported:

- `/export <link>`
- `/clone <source_link> <destination_link>`

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

## Heroku Setup (Recommended)

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
