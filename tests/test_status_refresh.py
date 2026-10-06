from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class StatusRefreshTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'heroku_bot'))
        with patch.dict(os.environ):
            cls.app = importlib.import_module('app')

    async def test_flood_wait_blocks_further_status_edits_until_deadline(self):
        app = self.app
        message = MagicMock(edit_text=AsyncMock(side_effect=[app.FloodWait(80), None]))
        with patch.object(app, 'STATUS_EDIT_RETRY_AT', 0), patch.object(app.time, 'monotonic', return_value=100):
            self.assertFalse(await app._edit_status_message(message, 'first', sleep_on_flood=False))
            self.assertEqual(app.STATUS_EDIT_RETRY_AT, 181)
            self.assertFalse(await app._edit_status_message(message, 'second', sleep_on_flood=False))
            self.assertEqual(message.edit_text.await_count, 1)
            with patch.object(app.time, 'monotonic', return_value=182):
                self.assertTrue(await app._edit_status_message(message, 'second', sleep_on_flood=False))
            self.assertEqual(message.edit_text.await_count, 2)

    async def test_watcher_keeps_running_through_completed_and_queued_clone_states(self):
        app = self.app
        key = (123, 456)
        states = [('transfer active', {'phase': 'completed'}),
                  ('clone starting', {'phase': 'queued'}),
                  ('clone active', {'phase': 'running'})]
        bot = MagicMock(edit_message_text=AsyncMock())
        with patch.object(app.asyncio, 'sleep', AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()])), patch.object(
            app, '_load_status_view_text', AsyncMock(side_effect=states)
        ), patch.object(app, 'STATUS_EDIT_RETRY_AT', 0), patch.dict(
            app.ACTIVE_STATUS_LAST_TEXTS, clear=True
        ), patch.dict(app.ACTIVE_STATUS_VIEWS, clear=True):
            with self.assertRaises(asyncio.CancelledError):
                await app._watch_status_message(bot, MagicMock(), *key, 5, 'old')
            self.assertEqual(bot.edit_message_text.await_count, 3)
            self.assertEqual(app.ACTIVE_STATUS_LAST_TEXTS[key], 'clone active')

    async def test_state_loading_error_does_not_kill_watcher(self):
        app = self.app
        bot = MagicMock(edit_message_text=AsyncMock())
        with patch.object(app.asyncio, 'sleep', AsyncMock(side_effect=[None, None, asyncio.CancelledError()])), patch.object(
            app, '_load_status_view_text', AsyncMock(side_effect=[RuntimeError('temporary'), ('recovered', None)])
        ), patch.object(app, 'STATUS_EDIT_RETRY_AT', 0), patch.dict(
            app.ACTIVE_STATUS_LAST_TEXTS, clear=True
        ), patch.object(app.logging, 'getLogger'):
            with self.assertRaises(asyncio.CancelledError):
                await app._watch_status_message(bot, MagicMock(), 123, 456, 5, 'old')
            bot.edit_message_text.assert_awaited_once()
            self.assertEqual(bot.edit_message_text.call_args.kwargs['text'], 'recovered')

    async def test_failed_edit_preserves_last_successful_text_for_retry(self):
        app = self.app
        bot = MagicMock(edit_message_text=AsyncMock(side_effect=[RuntimeError('network'), None]))
        with patch.object(app.asyncio, 'sleep', AsyncMock(side_effect=[None, None, asyncio.CancelledError()])), patch.object(
            app, '_load_status_view_text', AsyncMock(return_value=('new', None))
        ), patch.object(app, 'STATUS_EDIT_RETRY_AT', 0), patch.dict(
            app.ACTIVE_STATUS_LAST_TEXTS, {(123, 456): 'old'}, clear=True
        ):
            with self.assertRaises(asyncio.CancelledError):
                await app._watch_status_message(bot, MagicMock(), 123, 456, 5, 'old')
            self.assertEqual(bot.edit_message_text.await_count, 2)
            self.assertEqual(app.ACTIVE_STATUS_LAST_TEXTS[(123, 456)], 'new')

    async def test_watcher_waits_full_flood_duration_and_resumes(self):
        app = self.app
        bot = MagicMock(edit_message_text=AsyncMock(side_effect=[app.FloodWait(80), None]))
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        times = iter([100, 100, 100, 100, 182, 182])
        with patch.object(app.asyncio, 'sleep', sleep), patch.object(
            app.time, 'monotonic', side_effect=lambda: next(times)
        ), patch.object(app, '_load_status_view_text', AsyncMock(return_value=('new', None))), patch.object(
            app, 'STATUS_EDIT_RETRY_AT', 0
        ), patch.dict(app.ACTIVE_STATUS_LAST_TEXTS, clear=True), patch.object(app.logging, 'getLogger'):
            with self.assertRaises(asyncio.CancelledError):
                await app._watch_status_message(bot, MagicMock(), 123, 456, 5, 'old')
            self.assertEqual(sleep.await_args_list[1].args[0], 81)
            self.assertEqual(bot.edit_message_text.await_count, 2)

    async def test_deleted_message_stops_watcher_and_cleans_up(self):
        app = self.app
        from pyrogram.errors import MessageIdInvalid
        key = (123, 456)
        bot = MagicMock(edit_message_text=AsyncMock(side_effect=MessageIdInvalid()))
        with patch.object(app.asyncio, 'sleep', AsyncMock()), patch.object(
            app, '_load_status_view_text', AsyncMock(return_value=('new', None))
        ), patch.object(app, 'STATUS_EDIT_RETRY_AT', 0), patch.dict(
            app.ACTIVE_STATUS_WATCH_TASKS, {key: asyncio.current_task()}, clear=True
        ), patch.dict(app.ACTIVE_STATUS_LAST_TEXTS, {key: 'old'}, clear=True), patch.dict(
            app.ACTIVE_STATUS_VIEWS, {key: 'main'}, clear=True
        ):
            await app._watch_status_message(bot, MagicMock(), *key, 5, 'old')
            self.assertNotIn(key, app.ACTIVE_STATUS_WATCH_TASKS)
            self.assertNotIn(key, app.ACTIVE_STATUS_LAST_TEXTS)
            self.assertNotIn(key, app.ACTIVE_STATUS_VIEWS)


if __name__ == '__main__':
    unittest.main()
