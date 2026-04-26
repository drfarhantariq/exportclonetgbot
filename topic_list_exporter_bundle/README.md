# Telegram Topic List Exporter (Standalone)

This bundle exports one Telegram topic into a `.txt` file in chronological order.

## Output rules

- If message has **video/pdf/image/html**: output its **message link**.
- If message has **text**: output that **text line**.
- Links are one-per-line with no blank lines between consecutive links.
- A blank line is inserted after each 100 consecutive links (for bulk paste chunking).

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create env file:

```bash
cp .env.example .env
```

4. Fill `.env` with your Telegram credentials/session.

## Run

Interactive mode:

```bash
python export_topic_list.py
```

You will be prompted for:
- Example first-message topic link (`https://t.me/c/<chat>/<topic>/<message>`)
- Optional output file path

Optional safer pacing controls:

```bash
python export_topic_list.py --batch-size 20 --batch-delay-sec 2 --reply-batch-size 100
```

For stricter rate safety, use smaller batch and larger delay, for example:

```bash
python export_topic_list.py --batch-size 10 --batch-delay-sec 3
```
