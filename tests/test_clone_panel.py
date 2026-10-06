import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from heroku_bot.transfer_emojis import fallback


class ClonePanelTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'heroku_bot'))
        cls.app = importlib.import_module('app')

    def state(self, **values):
        state = dict(phase='running', started_at=100, total_messages=100,
                     current_index=4, success=2, skipped=1, failed=0,
                     current_file_name='<lecture>.mp4', current_message_type='Video',
                     transfer_stage='upload', upload_current=50, upload_total=100,
                     upload_speed='10B/s', upload_speed_bps=10,
                     payload={'source_link': 'source', 'destination_link': 'destination',
                              'requested_by_id': 123, 'requested_by_name': 'Admin'})
        state.update(values)
        return state

    async def test_whole_task_and_current_file_have_independent_progress(self):
        app = self.app
        with patch.object(app.time, 'time', return_value=110):
            panel = app._format_clone_status_panel(self.state())
        plain = fallback(panel)
        overall, route = plain.split('ROUTE &amp; QUEUE', 1)
        route, current = route.split('CURRENT FILE', 1)
        self.assertIn('░' * 18 + '  <b>3.0%</b>', overall)
        self.assertIn('3 / 100 messages', overall)
        self.assertIn('🟢 <b>CLONING IN PROGRESS</b>', overall)
        self.assertIn('Task 4 / 100', overall)
        self.assertIn('👤', overall)
        self.assertNotIn('Type', route)
        self.assertNotIn('Video', route)
        for detail in ('🎬', '&lt;lecture&gt;.mp4', '█' * 9 + '░' * 9, '50.0%', '📦', 'Speed', 'File ETA'):
            self.assertIn(detail, current)
        self.assertIn('✅ Forwarded 2 | ⏭️ Skipped 1 | ❌ Failed 0', overall)
        self.assertNotIn('TOTAL SUMMARY', plain)
        self.assertNotIn('Forwarded', current)
        self.assertNotIn('BY ABDULLAH', plain)

    async def test_copied_message_does_not_invent_byte_progress(self):
        panel = self.app._format_clone_status_panel(self.state(transfer_stage='', current_file_name=''))
        self.assertIn('Video', panel)
        self.assertIn('Copying message', panel)
        self.assertNotIn('Transferred', panel)

    async def test_terminal_summary_keeps_final_time_and_all_totals(self):
        app = self.app
        state = self.state(phase='completed', success=90, skipped=7, failed=3, finished_at=120)
        with patch.object(app.time, 'time', return_value=999):
            summary = app._format_clone_status_with_stats(state)
        for detail in ('Clone completed with errors', 'Total Summary', '100 of 100',
                       'Forwarded', '90', 'Skipped', '7', 'Failed', '3', 'Time taken', '20s', 'Task By'):
            self.assertIn(detail, summary)
        for stale in ('Bot Stats', 'ETA', 'Current File', 'Speed'):
            self.assertNotIn(stale, summary)
        with patch.object(app.time, 'time', return_value=9999):
            self.assertEqual(summary, app._format_clone_status_with_stats(state))
        self.assertEqual(app._progress_bar(100), '[●●●●●●●●●●]')
        self.assertEqual(app._clone_progress_bar(100), '█' * 18)

    async def test_live_stats_match_requested_layout(self):
        app = self.app
        with patch.object(app, '_format_bot_stats', return_value='⌬ <b><u>Bot Stats</u></b>\n┟ CPU\n┖ RAM'):
            panel = app._format_clone_status_with_stats(self.state())
        self.assertTrue(panel.endswith('━━━━━━━━━━━━━━━━━━━━\n\n⚙ <b>Bot Stats</b>\n├ CPU\n└ RAM'))
        self.assertEqual(app._clone_panel_time(29401), '8h10m')
        self.assertEqual(app._clone_panel_time(1141), '19m')

    async def test_terminal_checkpoint_records_finish_time_once(self):
        app = self.app
        state = self.state(phase='cancelled')
        store = MagicMock(save=AsyncMock())
        with patch.object(app, '_remember_finished_job', AsyncMock()), patch.object(app.time, 'time', return_value=120):
            await app._save_clone_state(store, 'last', state)
        with patch.object(app, '_remember_finished_job', AsyncMock()), patch.object(app.time, 'time', return_value=999):
            await app._save_clone_state(store, 'last', state)
        self.assertEqual(state['finished_at'], 120)
        summary = app._format_clone_completion_message(state)
        self.assertIn('Clone cancelled', summary)
        self.assertIn('3 of 100', summary)
