# Telegram Topic Cloner

Python 3.11 app that logs in with **your Telegram user account** and clones messages from one or more **private source topics** into mapped **destination group topics** as **fresh messages**, not normal forwards.

It supports:

- historical backfill from existing source topics
- optional continuous watch mode for new messages
- a global boss-key emergency stop
- strict one-by-one processing
- SQLite state and restart-safe recovery
- special video handling through your existing leech bot

## Important Safety Note

This project automates a **user account**, not a bot token. Telegram can rate-limit or restrict accounts that behave too aggressively. Test with a small `CLONE_LIMIT` first, keep delays conservative, and avoid using a high-value personal account until you trust the workflow.

If you shared real API credentials or session strings publicly, rotate them immediately.

## Why This Implementation

The app uses **Pyrogram 2.x** because you asked for a user-session-string workflow and Telegram-side cached media reuse. For topic-aware history scanning it uses **raw MTProto calls through Pyrogram** (`messages.GetReplies`) so it can fetch thread history for forum topics reliably.

That gives you:

- user account login via session string
- fresh recreated messages instead of forwards
- thread-aware history scanning
- Telegram-side media reuse without local large-video downloads in the normal path

## Project Layout

- `main.py`: CLI entrypoint and lifecycle orchestration
- `config.py`: `.env` and YAML config loading
- `db.py`: SQLite queue/state store
- `models.py`: shared enums and dataclasses
- `topic_utils.py`: topic-id and `t.me/c/...` helpers
- `history_scanner.py`: historical source-topic scanning
- `message_classifier.py`: message classification and payload extraction
- `clone_worker.py`: sequential clone worker
- `bot_leech.py`: leech bot DM workflow
- `telegram_client.py`: Pyrogram client wrapper, retries, flood waits, raw topic history calls
- `router.py`: mapping lookup for backfill and watch mode
- `logging_utils.py`: JSON console/file logging

## Requirements

- Python 3.11
- a valid Telegram API app
- a working **Pyrogram user session string**
- access to the source chats/topics and destination chats/topics
- a DM already started with your leech bot

## Setup

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Optional acceleration:

```bash
pip install -r requirements-optional.txt
```

`TgCrypto` is optional. Pyrogram works without it, but media-heavy workloads are usually faster with it. On some Windows/Python combinations it needs Microsoft C++ Build Tools to compile.

Optional session-generation tools:

```bash
pip install -r requirements-session-tools.txt
```

### 3. Create runtime files

Windows:

```powershell
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
```

Linux/macOS:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Then edit `.env` and `config.yaml`.

## Separate Sessions

Do not reuse one Telegram session string across multiple active runtimes.

Recommended split:

- local cloner app: one dedicated **Pyrogram** session string
- leech bot backend: a separate session string matching that bot's own framework

Why:

- one shared session across two active clients can cause unstable auth/access behavior
- debugging becomes much harder when both runtimes impersonate the same exact session
- isolating sessions makes it clear whether a failure belongs to the local app or the bot

### Generate a new Pyrogram session string for the local cloner

From the project root:

```powershell
.\.venv\Scripts\python.exe scripts\generate_pyrogram_session.py
```

It will prompt for Telegram login details if needed and then print a fresh `TG_SESSION_STRING`.

### Generate a separate Telethon session string for the leech bot

First install the optional tool dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-session-tools.txt
```

Then run:

```powershell
.\.venv\Scripts\python.exe scripts\generate_telethon_session.py
```

If your leech bot is Pyrogram-based instead of Telethon-based, generate another Pyrogram session instead and use that only for the bot.

### Where to put them

- put the cloner session into `.env` as `TG_SESSION_STRING`
- put the bot session into your bot deployment's own secret/env config
- do not keep both runtimes on the same string

## Environment Configuration

Example `.env`:

```dotenv
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_SESSION_STRING=your_pyrogram_session_string
LEECH_BOT_USERNAME=@triptiyt_bot
LEECH_BOT_ID=7072430572
DATABASE_PATH=state.db
LOG_LEVEL=INFO
LOG_FILE_PATH=logs/app.log
BOT_RESPONSE_TIMEOUT_SEC=900
BOT_RELEECH_RETRY_LIMIT=2
BOT_RELEECH_RETRY_DELAY_SEC=10
BOT_STALL_TIMEOUT_SEC=180
BOT_STATUS_COMMAND=/status me
BOT_STATUS_RESPONSE_TIMEOUT_SEC=20
ENABLE_BOT_PREFETCH=false
ACTION_DELAY_SEC=2
RETRY_LIMIT=3
CLONE_OLD_MESSAGES=true
CLONE_LIMIT=0
START_FROM_MESSAGE_ID=0
WATCH_NEW_MESSAGES=true
DRY_RUN=false
```

Required variables:

- `TG_API_ID`
- `TG_API_HASH`
- `TG_SESSION_STRING`
- `LEECH_BOT_USERNAME`
- `LEECH_BOT_ID`

Useful runtime knobs:

- `BOT_RESPONSE_TIMEOUT_SEC`: max wait for bot-uploaded media
- `BOT_RELEECH_RETRY_LIMIT`: how many times to resend `/leech` if the bot task dies before upload completes
- `BOT_RELEECH_RETRY_DELAY_SEC`: wait before resending `/leech` for the same source message
- `BOT_STALL_TIMEOUT_SEC`: treat the bot task as stalled if its progress stops changing for this many seconds
- `BOT_STATUS_COMMAND`: optional status probe sent after a stall, usually `/status me`
- `BOT_STATUS_RESPONSE_TIMEOUT_SEC`: how long to wait for the status probe reply before deciding the task is still stalled
- `ENABLE_BOT_PREFETCH`: legacy toggle for overlap mode; keep `false` to preserve strict chronological sequencing
- `ACTION_DELAY_SEC`: delay after write actions
- `RETRY_LIMIT`: retry budget for failed jobs and transient API errors
- `CLONE_OLD_MESSAGES`: default historical backfill behavior
- `CLONE_LIMIT`: `0` means all discovered source messages
- `START_FROM_MESSAGE_ID`: skip source messages below this ID
- `WATCH_NEW_MESSAGES`: default live watch behavior
- `DRY_RUN`: classify and mark jobs without sending
- `ENABLE_BOSS_KEY`: enable or disable the emergency stop hotkey
- `STOP_BOSS_KEY`: global hotkey combination
- `BOSS_KEY_GRACE_SEC`: short grace period before forced process termination
- `STRICT_DESTINATION_SYNC`: stop if the destination already drifted out of source chronology
- `RECONCILE_DESTINATION_HISTORY`: compare against existing destination messages instead of trusting SQLite resume state

## Mapping Configuration

Example `config.yaml`:

```yaml
mappings:
  - source_chat_id: -1003392883305
    source_topic_id: 34638
    destination_chat_id: -1003541699273
    destination_topic_id: 13144
    enabled: true
```

Each mapping clones one exact source topic into one exact destination topic.

## How `t.me/c/...` Topic Links Map to IDs

Telegram private topic links use:

```text
https://t.me/c/<chat_without_-100>/<topic_id>/<message_id>
```

Example source:

```text
https://t.me/c/3392883305/34638/34639
```

Maps to:

- `source_chat_id = -1003392883305`
- `source_topic_id = 34638`
- `source_message_id = 34639`

Example destination:

```text
https://t.me/c/3541699273/13144/13145
```

Maps to:

- `destination_chat_id = -1003541699273`
- `destination_topic_id = 13144`

In forum topics, the topic ID is the thread starter/root message ID. The app targets the destination thread by replying to that topic root ID when it sends the cloned fresh message.

## What “Clone” Means Here

This app does **not** use normal Telegram forwarding with attribution.

Instead:

- text becomes a newly sent text message in the destination topic
- supported non-video media is re-sent as a new media message using Telegram-side cached media
- video-like media is re-uploaded by your leech bot and then re-sent into the destination topic as a new message

## Historical Cloning Flow

When historical cloning is enabled:

1. The app fetches the reply history for the configured source topic.
2. It keeps only that exact topic/thread.
3. It sorts source message IDs oldest-to-newest.
4. It inserts unseen jobs into SQLite.
5. The worker processes them strictly one at a time.

State is stored in SQLite with:

- source chat/topic/message IDs
- destination chat/topic IDs
- status
- retry count
- last error
- destination message ID
- bot command message ID
- bot media message ID
- timestamps

## Message Handling Rules

### Text messages

Text messages are recreated as fresh destination messages. The app passes message entities through where possible so formatting survives as closely as Telegram allows.

### Supported direct media

These are re-sent as fresh messages using Telegram-side cached media:

- photo
- audio
- voice
- sticker
- non-video document

Captions and caption entities are preserved when the media type supports them.

### Video-like media through the leech bot

These source messages use the special bot workflow:

- video
- animation
- document with a video MIME type

For each such source message the app:

1. Builds a source link like `https://t.me/c/<chat>/<topic>/<message>`
2. Sends `/leech <source_link>` to your configured bot DM
3. Ignores older bot messages
4. Ignores ack/progress/status text
5. Detects the first new bot-uploaded media message
6. Re-sends that uploaded media into the destination topic as a fresh message
7. Uses the **original source caption**, not the bot progress text

The normal path does **not** download large video files locally.

If the bot crashes or loses the task before it uploads media, the app can automatically resend `/leech` for that same source message a limited number of times.

The crash recovery is conservative:

- progress text edits from the bot count as activity
- if progress stops changing for `BOT_STALL_TIMEOUT_SEC`, the app probes the bot with `BOT_STATUS_COMMAND`
- if the bot reports no active task, or stays stalled, the app resends `/leech` for that same source message
- old bot chat history is ignored for each retry because each attempt starts tracking from the new command message ID

Chronology guarantee:

- bot-routed media is processed one source message at a time
- the app waits for the current source message to fully finish before triggering the next leech command
- this avoids multiple queued bot downloads that can disrupt chronological cloning when large files take longer

## CLI Usage

Clone existing only, then exit:

```bash
python main.py --clone-existing --once
```

Clone existing, then keep watching:

```bash
python main.py --clone-existing --watch
```

Watch only:

```bash
python main.py --watch
```

Reset saved state for the mappings in the current config, then restart the clone from the beginning:

```bash
python main.py --reset-mapping-state --clone-existing --once
```

If you do not provide flags, defaults come from `.env`.

## Boss Key

The app includes a global emergency stop hotkey intended for "stop everything now" situations.

Default:

```text
ctrl+shift+end
```

When pressed, the app:

1. requests shutdown inside the running event loop
2. stops the worker
3. finds all matching `main.py` processes for this project
4. terminates them together

You can configure it through `.env`:

```dotenv
ENABLE_BOSS_KEY=true
STOP_BOSS_KEY=ctrl+shift+end
BOSS_KEY_GRACE_SEC=2.0
```

## Exact Sync Behavior

Recommended mode for a fresh destination topic:

- create an empty destination topic
- keep `RECONCILE_DESTINATION_HISTORY=false`
- let SQLite track what has already been cloned

In that mode, every run does this:

1. scan the source topic from oldest to newest
2. enqueue unseen source messages in source order
3. process them sequentially
4. if the script stops, resume from the next unfinished source message on the next run

That is the best fit when you want the destination topic to become a clean chronological replica of the source.

If you ever want to restart that same mapping from source message 1 without manually touching SQLite, use:

```bash
python main.py --reset-mapping-state --clone-existing --once
```

That clears saved clone state only for the mappings in the current config file.

Optional advanced mode:

- set `RECONCILE_DESTINATION_HISTORY=true`

That mode tries to compare source history against destination history directly. It is useful only if you already have partially cloned data in the destination and want a best-effort reconciliation pass.

Important Telegram limitation:

- Telegram lets you append new messages
- Telegram does not let you insert older missed messages back into the middle of an existing topic history

Because of that, exact sync is only possible when the destination topic is either:

- empty, or
- already a correct chronological prefix of the source topic

If the destination already contains newer cloned items while older source items are missing, the app now treats that as **destination drift** and stops by default.

Default:

```dotenv
STRICT_DESTINATION_SYNC=true
RECONCILE_DESTINATION_HISTORY=false
```

If you turn it off, the app will continue appending missing items, but the destination may no longer be an exact chronological replica.

## Watch Mode

Watch mode registers a live handler for the configured source chats and filters incoming messages by the exact configured source topic ID. Matching new messages are inserted into SQLite and then processed by the same sequential worker used for historical jobs.

## Flood Wait / Retry Behavior

The app is intentionally conservative:

- exactly one worker
- one message at a time
- action delay after write calls
- explicit `FloodWait` sleep for the Telegram-required duration plus buffer
- exponential backoff for transient network/server errors

## Restart / Resume Behavior

- `pending` jobs remain queued
- `processing` jobs are moved back to `pending` on startup
- `done` jobs are skipped
- `failed` jobs can be retried until `RETRY_LIMIT` is reached

This is restart-safe for normal operation, but there is still a narrow crash window after a successful Telegram send and before the SQLite `done` update. In that exact case a duplicate resend can still happen after a hard crash.

## Logging

Logs go to:

- console
- rotating file at `logs/app.log` by default

The logger emits JSON lines for:

- startup validation
- mapping validation
- history scan counts
- queued clone items
- bot command sent
- bot media detected
- destination send success
- retries
- flood waits
- shutdown summary

## Troubleshooting

### Configuration error

Check that:

- `.env` exists
- `config.yaml` exists
- required env vars are set
- YAML syntax is valid

### Session works but mapping validation fails

Your account likely cannot access one of:

- source chat
- source topic root message
- destination chat
- destination topic root message

Verify the IDs and ensure the user account is still a member of those chats.

### Leech bot timeout

Increase `BOT_RESPONSE_TIMEOUT_SEC`, verify the bot DM is started, and confirm the bot can access the source link you are sending.

If your bot sometimes restarts mid-upload:

- keep `BOT_RELEECH_RETRY_LIMIT` above `0`
- set `BOT_STATUS_COMMAND=/status me` if your bot supports per-user status
- tune `BOT_STALL_TIMEOUT_SEC` high enough that slow uploads are not misclassified as crashes

### Direct media clone fails

Telegram may refuse server-side reuse for some protected media. The error is logged and the job is marked failed for retry.

### Formatting is close but not perfect

Entity round-tripping is usually good, but Telegram formatting is not perfectly lossless for every edge case.

## Limitations

- no edit sync or delete sync
- media groups are processed message-by-message, not rebuilt as albums
- unsupported message classes are skipped and logged
- the bot workflow waits for the first qualifying uploaded media message
- exact-once delivery is not fully guaranteed across a hard crash after a successful send

## Architecture Summary

The runtime is intentionally simple:

- `HistoryScanner` discovers source-topic history
- `StateStore` records every clone attempt in SQLite
- `CloneWorker` processes one queued job at a time
- `TelegramService` wraps Telegram calls, retries, and flood waits
- `BotLeechService` handles the video path through your DM bot
- `MappingRouter` keeps topic matching consistent for both backfill and watch mode

That gives you a resumable, topic-aware, fresh-message cloning pipeline without normal Telegram forwards.
