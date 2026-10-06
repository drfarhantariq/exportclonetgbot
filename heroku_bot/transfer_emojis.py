"""Telegram pack lettering with a readable fallback if the server removes entities."""
import html
import json
import logging
import re
from pathlib import Path

PACKS = json.loads(Path(__file__).with_name("transfer_emoji_packs.json").read_text(encoding="utf-8"))
ENABLED = True
ACCEPTED_LOGGED = False
TAG = re.compile(r'<emoji id="(\d+)">(.*?)</emoji>', re.DOTALL)
FALLBACKS = {item["id"]: letter for name, pack in PACKS.items() if name != "icons"
             for letter, item in pack.items()}
FALLBACKS.update({item["id"]: item["alt"] for item in PACKS["icons"].values()})


def lettering(text, pack="ABCEmoji"):
    if not ENABLED:
        return html.escape(text)
    return "".join(_tag(PACKS[pack][c.upper()]) if c.upper() in PACKS[pack]
                   else html.escape(c) for c in text)


def _tag(item):
    # Telegram requires the entity to wrap this document's exact alt emoji.
    return f'<emoji id="{item["id"]}">{html.escape(item["alt"])}</emoji>'


def icon(name):
    item = PACKS["icons"][name]
    return _tag(item) if ENABLED else item["alt"]


def panel_title(kind):
    return f"<b>{lettering('MSZ ' + kind + ' BOT')}</b>"


def limit_entities(text, maximum=100):
    """Keep combined status/history panels within Telegram's emoji budget."""
    count = 0
    def replace(match):
        nonlocal count
        count += 1
        return match[0] if count <= maximum else html.escape(FALLBACKS.get(match[1], html.unescape(match[2])))
    return TAG.sub(replace, text)


def fallback(text):
    return TAG.sub(lambda m: html.escape(FALLBACKS.get(m[1], html.unescape(m[2]))), text)


def visible_length(text):
    return len(html.unescape(re.sub(r"<[^>]*>", "", text)).encode("utf-16-le")) // 2


async def safe_message(call, text, **kwargs):
    """Send/edit, detect rejected or silently removed custom entities, then repair."""
    global ENABLED, ACCEPTED_LOGGED
    if not ENABLED:
        text = fallback(text)
    text = limit_entities(text)
    expected = len(TAG.findall(text))
    try:
        result = await call(text, **kwargs)
    except Exception as exc:
        # Flood waits and unrelated errors retain their existing caller handling.
        if not expected or not any(code in str(exc).upper() for code in
                                   ("CUSTOM_EMOJI", "PREMIUM_ACCOUNT_REQUIRED", "ENTITIES_TOO_LONG")):
            raise
        ENABLED = False
        logging.getLogger("heroku_bot").warning("Custom emoji unavailable; using readable transfer panels")
        return await call(fallback(text), **kwargs)
    entities = getattr(result, "entities", None)
    if expected and hasattr(result, "edit_text") and (entities is None or isinstance(entities, (list, tuple))):
        retained = sum(bool(getattr(e, "custom_emoji_id", None)) for e in (entities or []))
        if retained < expected:
            ENABLED = False
            logging.getLogger("heroku_bot").warning("Telegram removed custom emoji entities; repairing panel")
            edit_kwargs = {k: v for k, v in kwargs.items() if k in
                           {"parse_mode", "disable_web_page_preview", "reply_markup"}}
            result = await result.edit_text(fallback(text), **edit_kwargs)
        elif not ACCEPTED_LOGGED:
            ACCEPTED_LOGGED = True
            logging.getLogger("heroku_bot").info("Transfer custom emoji entities accepted: %s", retained)
    return result
