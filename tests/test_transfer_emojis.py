import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pyrogram.parser.html import HTML
from pyrogram.raw.types import MessageEntityCustomEmoji
from heroku_bot import transfer_emojis as emojis
from heroku_bot.transfer_progress import format_status, new_state


class EmojiTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_panels_preserve_readable_overflow(self):
        with patch.object(emojis, "ENABLED", True):
            text = emojis.lettering("A" * 110)
            limited = emojis.limit_entities(text)
            self.assertEqual(len(emojis.TAG.findall(limited)), 100)
            self.assertTrue(limited.endswith("A" * 10))
            self.assertEqual(emojis.fallback(limited), "A" * 110)

    async def test_send_repair_omits_send_only_reply_argument(self):
        sent = SimpleNamespace(entities=[], edit_text=AsyncMock())
        with patch.object(emojis, "ENABLED", True):
            await emojis.safe_message(AsyncMock(return_value=sent), emojis.lettering("A"),
                                      parse_mode="html", reply_to_message_id=123)
            sent.edit_text.assert_awaited_once_with("A", parse_mode="html")

    async def test_panel_entities_use_exact_alts_and_utf16_offsets(self):
        with patch.object(emojis, "ENABLED", True):
            state = new_state(["msz:Course", "--up", "gd"])
            state.update(phase="running", stage="uploading", file_name="🧪 Lecture <1>.mp4",
                         file_size=100, bytes_done=50)
            panel = format_status(state)
            parsed = await HTML(None).parse(panel)
            entities = [e for e in parsed["entities"] if isinstance(e, MessageEntityCustomEmoji)]
            self.assertGreater(len(entities), 60)
            self.assertLessEqual(len(entities), 100)
            encoded = parsed["message"].encode("utf-16-le")
            by_id = {int(i["id"]): i["alt"] for p in emojis.PACKS.values() for i in p.values()}
            for entity in entities:
                self.assertEqual(encoded[entity.offset*2:(entity.offset+entity.length)*2].decode("utf-16-le"),
                                 by_id[entity.document_id])
            plain = emojis.fallback(panel)
            for label in ("MSZ TRANSFER BOT", "Overall Progress", "Current File", "Route &amp; Queue", "Results"):
                self.assertIn(label.upper() if label not in ("Route &amp; Queue",) else "ROUTE &amp; QUEUE", plain)
            self.assertNotIn("BY ABDULLAH", plain)
            self.assertIn("🧪 Lecture &lt;1&gt;.mp4", plain)
            self.assertLess(emojis.visible_length(panel), 4096)

    async def test_stripped_entities_are_repaired_and_later_messages_readable(self):
        sent = SimpleNamespace(entities=[], edit_text=AsyncMock())
        call = AsyncMock(return_value=sent)
        with patch.object(emojis, "ENABLED", True):
            panel = emojis.lettering("ABC")
            await emojis.safe_message(call, panel, parse_mode="html")
            sent.edit_text.assert_awaited_once_with("ABC", parse_mode="html")
            await emojis.safe_message(call, panel)
            self.assertEqual(call.await_args.args, ("ABC",))

    async def test_permission_rejection_retries_plain_but_flood_wait_propagates(self):
        with patch.object(emojis, "ENABLED", True):
            call = AsyncMock(side_effect=[RuntimeError("CUSTOM_EMOJI_INVALID"), None])
            await emojis.safe_message(call, emojis.lettering("ETA"))
            self.assertEqual(call.await_args.args, ("ETA",))
        with patch.object(emojis, "ENABLED", True):
            call = AsyncMock(side_effect=RuntimeError("FLOOD_WAIT_30"))
            with self.assertRaisesRegex(RuntimeError, "FLOOD_WAIT"):
                await emojis.safe_message(call, emojis.lettering("ETA"))
            self.assertEqual(call.await_count, 1)

    async def test_accepted_entities_do_not_trigger_repair(self):
        sent = SimpleNamespace(entities=[SimpleNamespace(custom_emoji_id="123")], edit_text=AsyncMock())
        with patch.object(emojis, "ENABLED", True):
            await emojis.safe_message(AsyncMock(return_value=sent), emojis.lettering("A"))
            sent.edit_text.assert_not_awaited()
