import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class StatusViewTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'heroku_bot'))
        cls.app = importlib.import_module('app')

    async def asyncSetUp(self):
        self.data = {}
        async def load(key):
            return self.data.get(key)
        async def save(key, state):
            self.data[key] = state
        self.store = SimpleNamespace(load=AsyncMock(side_effect=load), save=AsyncMock(side_effect=save))
        self.directory = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(self.app, '_snapshot_path', side_effect=lambda name: Path(self.directory.name) / (name + '.json')),
            patch.object(self.app, 'JOB_HISTORY_LOCK', asyncio.Lock()),
            patch.object(self.app, 'TRANSFER_QUEUE', self.app.TransferQueue()),
            patch.object(self.app, '_clone_pending_jobs', []),
        ]
        for name in ['ACTIVE_CLONE_TASK', 'ACTIVE_EXPORT_TASK', 'ACTIVE_INDEX_TASK', 'ACTIVE_TRANSFER_TASK',
                     'ACTIVE_CLONE_LATEST_STATE', 'ACTIVE_TRANSFER_STATE']:
            self.patches.append(patch.object(self.app, name, None))
        for name in ['STATUS_CHAT_PANELS', 'STATUS_CHAT_LOCKS', 'STATUS_RETURN_VIEWS',
                     'ACTIVE_STATUS_VIEWS', 'ACTIVE_STATUS_LAST_TEXTS', 'ACTIVE_STATUS_WATCH_TASKS']:
            self.patches.append(patch.dict(getattr(self.app, name), clear=True))
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    async def test_finished_jobs_are_hidden_and_available_in_history(self):
        self.data.update({'clone:last': {'phase': 'completed', 'started_at': 1},
                          'index:last': {'phase': 'failed', 'started_at': 2},
                          'transfer:last': {'phase': 'cancelled', 'job_id': 'a' * 12}})
        text, _ = await self.app._load_status_view_text(self.store, 'active:all')
        self.assertIn('No active jobs', text)
        self.assertNotIn('COMPLETED', text)
        history, _ = await self.app._load_status_view_text(self.store, 'history:all:0')
        for phase in ['COMPLETED', 'FAILED', 'CANCELLED']:
            self.assertIn(phase, history)
        again, _ = await self.app._load_status_view_text(self.store, 'history:transfer:0')
        self.assertIn('CANCELLED', again)
        self.assertNotIn('COMPLETED', again)
        self.assertEqual(len(self.data['status:history']['jobs']), 3)

    async def test_filters_only_include_running_selected_kind(self):
        self.data.update({'clone:last': {'phase': 'running'}, 'index:last': {'phase': 'running'},
                          'export:last': {'phase': 'completed'}, 'transfer:last': {'phase': 'running'}})
        task = SimpleNamespace(done=lambda: False)
        with patch.object(self.app, 'ACTIVE_CLONE_TASK', task), patch.object(self.app, 'ACTIVE_INDEX_TASK', task), patch.object(
                self.app, 'ACTIVE_TRANSFER_TASK', task), patch.object(self.app, '_format_clone_status_panel', return_value='CLONE PANEL'), patch.object(
                self.app, '_format_index_status', return_value='INDEX PANEL'), patch.object(self.app, 'format_transfer_status', return_value='TRANSFER PANEL'):
            for kind in ['clone', 'index', 'transfer']:
                text, _ = await self.app._load_status_view_text(self.store, 'active:' + kind)
                self.assertIn(kind.upper() + ' PANEL', text)
                for other in {'clone', 'index', 'transfer'} - {kind}:
                    self.assertNotIn(other.upper() + ' PANEL', text)
            text, _ = await self.app._load_status_view_text(self.store, 'active:all')
            self.assertIn('CLONE PANEL', text)
            self.assertIn('INDEX PANEL', text)
            self.assertIn('TRANSFER PANEL', text)

    async def test_stale_running_snapshot_is_not_an_active_job(self):
        self.data['index:last'] = {'phase': 'running'}
        text, _ = await self.app._load_status_view_text(self.store, 'active:index')
        self.assertIn('No active index jobs', text)

    async def test_pending_jobs_are_shown_without_old_completed_panel(self):
        self.data['transfer:last'] = {'phase': 'completed'}
        self.app.TRANSFER_QUEUE.pending.append({'job_id': 'b' * 12, 'source': 'Queued Course'})
        text, _ = await self.app._load_status_view_text(self.store, 'active:transfer')
        self.assertIn('Queued Course', text)
        self.assertNotIn('COMPLETED', text)

    async def test_repeated_and_concurrent_requests_replace_same_chat_panel(self):
        client = MagicMock(delete_messages=AsyncMock())
        panels = [SimpleNamespace(chat=SimpleNamespace(id=123), id=501 + i) for i in range(3)]
        messages = [SimpleNamespace(chat=SimpleNamespace(id=123), reply_text=AsyncMock(return_value=p)) for p in panels]
        watcher = AsyncMock()
        old_watch = MagicMock(done=lambda: False)
        self.app.STATUS_CHAT_PANELS[123] = 500
        self.app.ACTIVE_STATUS_WATCH_TASKS[(123, 500)] = old_watch
        await asyncio.gather(*(self.app._replace_chat_status(client, m, self.store, 'active:transfer', watcher) for m in messages))
        self.assertEqual([c.args for c in client.delete_messages.await_args_list], [(123, 500), (123, 501), (123, 502)])
        old_watch.cancel.assert_called_once()
        self.assertEqual(self.app.STATUS_CHAT_PANELS[123], 503)
        self.assertEqual(self.data['status:panel:123']['message_id'], 503)
        self.assertNotIn((123, 501), self.app.ACTIVE_STATUS_VIEWS)

    async def test_replacement_uses_persisted_panel_after_restart(self):
        self.data['status:panel:123'] = {'message_id': 400}
        client = MagicMock(delete_messages=AsyncMock())
        panel = SimpleNamespace(chat=SimpleNamespace(id=123), id=401)
        message = SimpleNamespace(chat=SimpleNamespace(id=123), reply_text=AsyncMock(return_value=panel))
        await self.app._replace_chat_status(client, message, self.store, 'active:all', AsyncMock())
        client.delete_messages.assert_awaited_once_with(123, 400)

    async def test_failed_new_send_preserves_old_panel_and_watcher(self):
        self.app.STATUS_CHAT_PANELS[123] = 500
        client = MagicMock(delete_messages=AsyncMock())
        message = SimpleNamespace(chat=SimpleNamespace(id=123), reply_text=AsyncMock(side_effect=OSError('network')))
        with self.assertRaises(OSError):
            await self.app._replace_chat_status(client, message, self.store, 'active:all', AsyncMock())
        client.delete_messages.assert_not_awaited()
        self.assertEqual(self.app.STATUS_CHAT_PANELS[123], 500)

    async def test_failed_old_delete_rolls_back_new_panel(self):
        self.app.STATUS_CHAT_PANELS[123] = 500
        client = MagicMock(delete_messages=AsyncMock(side_effect=[OSError('network'), OSError('network'), None]))
        panel = SimpleNamespace(chat=SimpleNamespace(id=123), id=501)
        message = SimpleNamespace(chat=SimpleNamespace(id=123), reply_text=AsyncMock(return_value=panel))
        with patch.object(self.app.asyncio, 'sleep', AsyncMock()), self.assertRaises(OSError):
            await self.app._replace_chat_status(client, message, self.store, 'active:all', AsyncMock())
        self.assertEqual(self.app.STATUS_CHAT_PANELS[123], 500)
        self.assertEqual(client.delete_messages.await_args_list[-1].args, (123, 501))

    async def test_long_combined_panels_use_compact_all_jobs_view(self):
        self.data.update({'clone:last': {'phase': 'running'}, 'index:last': {'phase': 'running'}})
        task = SimpleNamespace(done=lambda: False)
        with patch.object(self.app, 'ACTIVE_CLONE_TASK', task), patch.object(self.app, 'ACTIVE_INDEX_TASK', task), patch.object(
                self.app, '_format_clone_status_panel', return_value='A' * 3000), patch.object(self.app, '_format_index_status', return_value='B' * 3000):
            text, _ = await self.app._load_status_view_text(self.store, 'active:all')
        self.assertLess(len(text), 3800)
        self.assertIn('/status clone', text)
        self.assertIn('/status index', text)

    async def test_history_survives_later_running_state_and_is_bounded(self):
        for number in range(52):
            await self.app._save_job_state(self.store, 'index', {'phase': 'completed', 'started_at': number + 1})
        await self.app._save_job_state(self.store, 'index', {'phase': 'running', 'started_at': 100})
        self.assertEqual(len(self.data['status:history']['jobs']), 50)
        self.assertEqual(self.data['status:history']['jobs'][0]['state']['started_at'], 52)

    async def test_callback_preserves_filter_across_history_stats_back_refresh(self):
        from test_transfer_progress import BotProgressTests
        handlers = await BotProgressTests.register_handlers(self, self.store)
        panel = SimpleNamespace(chat=SimpleNamespace(id=123), id=501, edit_text=AsyncMock())
        key = (123, 501)
        self.app.ACTIVE_STATUS_VIEWS[key] = 'active:transfer'
        self.app.STATUS_RETURN_VIEWS[key] = 'active:transfer'
        with patch.object(self.app, '_load_bot_settings', AsyncMock(return_value={'status_command_update_interval_sec': 5})), patch.object(
                self.app, '_watch_status_message', AsyncMock()), patch.object(self.app, '_edit_status_message', AsyncMock(return_value=True)):
            for action, expected in [('history', 'history:transfer:0'), ('tstats', 'overview'), ('back', 'history:transfer:0'),
                                     ('page:1', 'history:transfer:1'), ('refresh', 'history:transfer:1'), ('back', 'active:transfer')]:
                query = SimpleNamespace(data='status:' + action, from_user=SimpleNamespace(id=123), message=panel, answer=AsyncMock())
                await handlers['status_callback_handler'](MagicMock(), query)
                self.assertEqual(self.app.ACTIVE_STATUS_VIEWS[key], expected)
            for task in self.app.ACTIVE_STATUS_WATCH_TASKS.values():
                task.cancel()
            await asyncio.gather(*self.app.ACTIVE_STATUS_WATCH_TASKS.values(), return_exceptions=True)
