# MSZ Drive Uploader

Use one command for almost everything:

```bash
python MSZDRIVE_uploader/transfer.py "<source>" "<destination>" --up <destination_type>
```

For normal links, paste the link directly. The script detects Telegram, Google Drive, and MSZ Drive URLs automatically.

Use prefixes only for raw IDs or plain names:

```text
gdrive:<folder_id>
msz:<folder_id_or_path>
```

Common destination types:

```text
--up msz       upload to MSZ Drive
--up gd        upload to Google Drive
--up gdrive    same as --up gd
--up telegram  upload to Telegram
--up both      upload Telegram source to both MSZ Drive and Google Drive
```

## Setup

Put secrets in `MSZDRIVE_uploader/.env`.

```env
MSZ_BASE_URL=https://cloud.medicalstudyzone.com
MSZ_API_TOKEN=...
MSZ_EMAIL=...
MSZ_PASSWORD=...
MSZ_API_MAX_BYTES=100000000

GDRIVE_TOKEN_PICKLE=/path/to/token.pickle
GDRIVE_FOLDER_ID=...

TG_API_ID=...
TG_API_HASH=...
TG_SESSION_STRING_MSZ=...
TG_USE_MAIN_SESSION_AS_HELPER=true
HYPER_THREADS=0

TELEGRAM_TARGET_TOPIC_LINK=https://t.me/c/<chat>/<topic>/<message>
```

`TG_SESSION_STRING_MSZ` is used by these MSZ uploader scripts. It does not need to match `heroku_bot/.env`.

## Telegram Index Flow

For Telegram source uploads, first generate a text folder index:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index
```

This creates a file under:

```text
runtime/telegram_indexes/
```

Edit the `.txt` file:

```text
[x] 105148 | P | Anatomy
[x] 105158 | P | Physiology
[ ] 105172 | P | random text to ignore
```

Level labels:

```text
P   parent folder
S   subfolder
S1  deeper subfolder
S2  even deeper
```

Default folder rule:

```text
Text message = folder name.
Media below it goes into that folder until the next enabled text message.
```

Opposite folder rule:

```bash
--above
```

With `--above`, media above a text message goes into that text message folder.

## Telegram -> Google Drive

No index needed if you want everything in one Google Drive folder named after the Telegram topic title:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --up gd
```

Use an edited index when you want Telegram text messages to create subfolders:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up gd
```

Use a specific Google Drive folder:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up gd \
  --gdrive-folder-id "<gdrive_folder_id>"
```

Use `--above` if folder headings appear after the media:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up gd \
  --above
```

## Telegram -> MSZ Drive

No index needed if you want everything in one MSZ folder named after the Telegram topic title:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --up msz
```

With an edited index, if no MSZ target folder is passed, the script uses `# topic_title:` from the edited index.

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up msz
```

Use a specific MSZ target folder:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up msz \
  --msz-target-folder "Target Folder"
```

With `--above`:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up msz \
  --above
```

## Telegram -> Both MSZ and Google Drive

No index needed if you want everything in one folder named after the Telegram topic title:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --up both \
  --gdrive-folder-id "<gdrive_folder_id>"
```

Use an edited index when you want text-message folder structure:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up both \
  --gdrive-folder-id "<gdrive_folder_id>"
```

## Google Drive -> MSZ Drive

Short form. The MSZ target folder is taken from the Google Drive source folder name:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://drive.google.com/drive/folders/<folder_id>" \
  --up msz
```

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://drive.google.com/drive/folders/<folder_id>" \
  "msz:Target Folder" \
  --up msz
```

Or:

```bash
python MSZDRIVE_uploader/transfer.py \
  "gdrive:<gdrive_folder_id>" \
  --up msz \
  --msz-target-folder "Target Folder"
```

Files under `100 MB` upload through the MSZ API. Larger files use browser automation. Uploads trust the successful API response or browser UI "Upload complete" message by default. Use `--verify-remote` for API-upload verification or `--strict-browser-verify` for browser-upload verification; those checks scan only the chosen MSZ target folder, not the whole drive.

## Local Files -> MSZ Drive

```bash
python MSZDRIVE_uploader/transfer.py \
  "/path/to/local/folder" \
  "msz:Target Folder" \
  --up msz
```

Single file:

```bash
python MSZDRIVE_uploader/transfer.py \
  "/path/to/file.mp4" \
  "msz:Target Folder" \
  --up msz
```

## MSZ Drive -> Google Drive

Short form. The Google Drive destination uses `GDRIVE_FOLDER_ID`; if that is not set, it uploads under Google Drive `root`.

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://cloud.medicalstudyzone.com/drive/folders/<folder_id>" \
  --up gd
```

Use an MSZ folder URL:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://cloud.medicalstudyzone.com/drive/folders/<folder_id>" \
  "gdrive:<gdrive_folder_id>" \
  --up gd
```

Use an MSZ folder path:

```bash
python MSZDRIVE_uploader/transfer.py \
  "msz:TestUpload" \
  "gdrive:<gdrive_folder_id>" \
  --up gd
```

This keeps the MSZ source folder name as the Google Drive root folder.

If the MSZ API can list a folder but does not return its title, the script opens the folder URL with Playwright and reads the breadcrumb title. The file download still uses the faster MSZ API. To disable browser title lookup:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://cloud.medicalstudyzone.com/drive/folders/<folder_id>" \
  --up gd \
  --no-browser-folder-title
```

## Google Drive -> Telegram

This creates one Telegram text message per folder, then uploads every file in that folder after it. It does not repeat the same folder heading for each file in the same run. Numbered folder and file names use natural ordering, so `1, 2, 3, 11` stays in that order. Videos are uploaded as playable Telegram videos, images as viewable photos, audio as audio, and unknown file types as documents.

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://drive.google.com/drive/folders/<folder_id>" \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --up telegram
```

Or use env `TELEGRAM_TARGET_TOPIC_LINK`:

```bash
python MSZDRIVE_uploader/transfer.py \
  "gdrive:<gdrive_folder_id>" \
  --up telegram
```

## MSZ Drive -> Telegram

Use an MSZ folder URL:

```bash
python MSZDRIVE_uploader/transfer.py \
  "https://cloud.medicalstudyzone.com/drive/folders/<folder_id>" \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --up telegram
```

Use an MSZ folder path:

```bash
python MSZDRIVE_uploader/transfer.py \
  "msz:TestUpload" \
  "https://t.me/c/<chat>/<topic>/<message>" \
  --up telegram
```

## Useful Flags

Fresh mode is the default. The script ignores previous state unless you pass `--resume`.

```bash
--continue-on-error       keep going after a failed file
--retry-failed-only       process only files marked failed in the state file
--no-resume               ignore previous state, default
--resume                  skip files already marked uploaded in the state file
--verify-remote           verify MSZ uploads by scanning only the target folder
--strict-browser-verify   verify browser-routed MSZ uploads by scanning only the target folder
--dry-run                 show what would happen without uploading
--keep-downloads          keep downloaded temp files
--delete-failed-downloads delete failed temp files
```

Telegram file naming:

```bash
--caption-file-names      use each Telegram media caption as the upload file name
```

Telegram download mode:

```bash
--tg-download hyper   fastest, requires user/helper session support
--tg-download auto    try hyper, fallback to normal
--tg-download normal  normal Pyrogram download
```

Browser upload debug:

```bash
--browser-headed
--browser-folder-url "https://cloud.medicalstudyzone.com/drive/folders/<folder_id>"
```

## State and Logs

State files:

```text
runtime/state/
```

Failed logs:

```text
runtime/logs/
```

Telegram indexes:

```text
runtime/telegram_indexes/
```

## Quick Examples

Telegram topic to Google Drive, headings above media:

```bash
python MSZDRIVE_uploader/transfer.py "<telegram_link>" \
  --index-done "runtime/telegram_indexes/My Topic.txt" \
  --up gd \
  --above
```

Telegram topic to Google Drive, no index, one folder named after the topic:

```bash
python MSZDRIVE_uploader/transfer.py "<telegram_link>" --up gd
```

Telegram topic to MSZ, target folder from topic title:

```bash
python MSZDRIVE_uploader/transfer.py "<telegram_link>" \
  --up msz
```

MSZ folder to Telegram:

```bash
python MSZDRIVE_uploader/transfer.py "<msz_folder_url>" "<telegram_topic_link>" --up telegram
```

Google Drive folder to Telegram:

```bash
python MSZDRIVE_uploader/transfer.py "<gdrive_folder_url>" "<telegram_topic_link>" --up telegram
```
