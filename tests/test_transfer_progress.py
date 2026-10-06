import asyncio
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from heroku_bot.transfer_progress import EventStream, apply_event, format_status, new_state, snapshot, PREFIX
from heroku_bot.transfer_queue import TransferQueue
from MSZDRIVE_uploader.progress_events import ByteProgress


class ProgressTests(unittest.TestCase):
    def state(self, **kwargs):
        state = new_state(['msz:Course', '--up', 'gd'], job_id='a' * 12, now=0)
        state.update(phase='running', started_at=100, **kwargs)
        return state

    def event(self, state, event, **kwargs):
        apply_event(state, {'event': event, **kwargs}, now=100)

    def test_download_upload_and_completion_are_distinct(self):
        state = self.state()
        self.event(state, 'totals', total_files=2, total_bytes=200)
        self.event(state, 'file', index=1, file_name='lecture.mp4', file_size=100)
        self.event(state, 'bytes', stage='downloading', bytes_done=50, file_size=100, speed_bps=10, file_eta=5)
        self.assertEqual(snapshot(state, now=105)['percent'], 12.5)
        self.assertIn('DOWNLOADING', format_status(state, now=105))
        self.assertIn('lecture.mp4', format_status(state, now=105))
        self.event(state, 'bytes', stage='uploading', bytes_done=50, file_size=100, speed_bps=20, file_eta=2.5)
        self.assertEqual(snapshot(state, now=105)['percent'], 37.5)
        self.event(state, 'result', result='uploaded')
        self.assertEqual(snapshot(state, now=110)['percent'], 50)
        self.assertEqual(snapshot(state, now=110)['eta'], 10)
        self.assertEqual(state['uploaded'], 1)

    def test_completed_result_is_idempotent_and_skips_do_not_inflate_speed(self):
        state = self.state()
        self.event(state, 'totals', total_files=3)
        self.event(state, 'file', index=1, file_name='skip')
        self.event(state, 'result', result='skipped')
        self.event(state, 'result', result='skipped')
        self.assertEqual(state['processed'], 1)
        self.assertIsNone(snapshot(state, now=110)['eta'])

    def test_quiet_worker_speed_expires_but_elapsed_keeps_advancing(self):
        state = self.state(speed_bps=123, last_progress_at=100)
        self.assertEqual(snapshot(state, now=110)['speed'], 123)
        self.assertEqual(snapshot(state, now=120)['speed'], 0)
        self.assertEqual(snapshot(state, now=120)['elapsed'], 20)

    def test_stale_file_eta_is_not_presented_as_live(self):
        state = self.state(file_eta=10, last_progress_at=100)
        self.assertEqual(snapshot(state, now=110)['file_eta'], 10)
        self.assertIsNone(snapshot(state, now=120)['file_eta'])

    def test_terminal_time_is_frozen_and_job_complete_bar_is_full(self):
        state = self.state(finished_at=115)
        state['phase'] = 'completed'
        self.assertEqual(snapshot(state, now=999)['elapsed'], 15)
        self.assertEqual(snapshot(state, now=999)['percent'], 100)

    def test_finished_batch_becomes_summary_with_frozen_totals(self):
        state = self.state(finished_at=3272, total_files=311, processed=311,
                           total_bytes=1024**3, uploaded=160, skipped=151,
                           file_name='last-file.mp4', file_size=1024,
                           requester='<Abdullah>', requester_username='abdullah')
        state['phase'] = 'completed'
        panel = format_status(state, now=9999)
        for detail in ('TRANSFER COMPLETED', 'Time Taken', '52m 52s', '1.0 GB',
                       '#MSZDrive', '#GDrive', '@abdullah', 'Folder / batch',
                       '311 processed / 311 total', 'Uploaded 160 | Skipped 151 | Failed 0'):
            self.assertIn(detail, panel)
        for stale in ('last-file.mp4', 'Current File', 'ETA', 'Speed', 'Overall Progress'):
            self.assertNotIn(stale, panel)
        self.assertEqual(panel, format_status(state, now=99999))

    def test_single_file_summary_escapes_names_and_reports_type(self):
        state = self.state(total_files=1, processed=1, uploaded=1,
                           file_name='<lecture&>.mp4', file_size=1024,
                           finished_at=115, requester='<Admin>', requester_id=123)
        state['phase'] = 'completed'
        panel = format_status(state)
        for detail in ('&lt;lecture&amp;&gt;.mp4', 'video/mp4', '1.0 KB',
                       'tg://user?id=123', '&lt;Admin&gt;'):
            self.assertIn(detail, panel)

    def test_summary_distinguishes_partial_failure_and_cancellation(self):
        state = self.state(finished_at=115, total_files=3, processed=2, uploaded=1, failed=1)
        state['phase'] = 'completed'
        self.assertIn('COMPLETED WITH ERRORS', format_status(state))
        state['phase'] = 'failed'
        self.assertIn('TRANSFER FAILED', format_status(state))
        state['phase'] = 'cancelled'
        panel = format_status(state)
        self.assertIn('TRANSFER CANCELLED', panel)
        self.assertIn('2 processed / 3 total', panel)
        self.assertIn('Unknown size', panel)

    def test_indexing_has_scan_progress_and_no_invented_eta(self):
        state = self.state(source_type='telegram')
        self.event(state, 'scan', done=20, total=100)
        panel = format_status(state, now=110)
        self.assertIn('INDEXING FILES', panel)
        self.assertIn('20 of 100 messages', panel)
        self.assertIn('Available after indexing', panel)

    def test_panel_hides_shell_arguments_and_raw_logs_and_escapes_names(self):
        state = self.state(file_name='<lecture&>', detail='Traceback SECRET_TOKEN', source='<source>')
        panel = format_status(state, now=110)
        self.assertIn('&lt;lecture&amp;&gt;', panel)
        self.assertNotIn('Traceback', panel)
        self.assertNotIn('SECRET_TOKEN', panel)
        self.assertNotIn('--up', panel)
        self.assertNotIn('--config', panel)

    def test_partial_event_chunks_and_regular_logs(self):
        state = self.state()
        stream = EventStream(state)
        line = PREFIX + json.dumps({'event': 'file', 'index': 1, 'file_name': 'My File.mp4', 'file_size': 10}) + '\n'
        stream.feed('regular log\n' + line[:15])
        self.assertNotIn('file_name', state)
        stream.feed(line[15:])
        self.assertEqual(state['file_name'], 'My File.mp4')
        stream.feed(PREFIX + '{broken}\n' + PREFIX + json.dumps({'event': 'bytes', 'stage': 'uploading', 'bytes_done': float('nan')}) + '\n')
        self.assertEqual(state['bytes_done'], 0)

    def test_both_destinations_count_three_operations(self):
        state = self.state(target='both')
        self.event(state, 'totals', total_files=1)
        self.event(state, 'file', index=1, file_name='lecture', file_size=100)
        self.event(state, 'bytes', stage='uploading', operation='MSZ upload', bytes_done=100, file_size=100)
        self.assertAlmostEqual(snapshot(state, now=101)['percent'], 200 / 3)
        self.event(state, 'bytes', stage='uploading', operation='GDrive upload', bytes_done=50, file_size=100)
        self.assertAlmostEqual(snapshot(state, now=101)['percent'], 250 / 3)

    def test_local_upload_has_one_operation(self):
        state = self.state(source_type='local')
        self.event(state, 'totals', total_files=1)
        self.event(state, 'file', index=1, file_name='local', file_size=100)
        self.event(state, 'bytes', stage='uploading', bytes_done=50, file_size=100)
        self.assertEqual(snapshot(state, now=101)['percent'], 50)

    def test_reporter_emits_measured_bytes_only_when_enabled(self):
        with patch.dict(os.environ, {'TRANSFER_PROGRESS_EVENTS': '1'}), redirect_stdout(io.StringIO()) as buffer:
            with patch('MSZDRIVE_uploader.progress_events.time.monotonic', side_effect=[100, 105]):
                report = ByteProgress('downloading', 'lecture', 100)
                report(50, 100)
        events = [json.loads(line.split(PREFIX)[1]) for line in buffer.getvalue().splitlines()]
        self.assertEqual(events[-1]['speed_bps'], 10)
        self.assertEqual(events[-1]['file_eta'], 5)
        with patch.dict(os.environ, {'TRANSFER_PROGRESS_EVENTS': ''}), redirect_stdout(io.StringIO()) as buffer:
            ByteProgress('uploading', 'lecture')(100)
        self.assertEqual(buffer.getvalue(), '')


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def job(self, name):
        return {'job_id': name, 'argv': ['msz:Course', '--up', 'gd'], 'phase': 'queued'}

    async def test_fifo_one_active_and_removing_waiting_job(self):
        queue, save = TransferQueue(), AsyncMock()
        await queue.enqueue(self.job('first'), save)
        await queue.enqueue(self.job('second'), save)
        await queue.enqueue(self.job('third'), save)
        self.assertEqual((await queue.take(save))['job_id'], 'first')
        removed = await queue.remove('second', save)
        self.assertEqual(removed['phase'], 'cancelled')
        self.assertEqual(queue.active['job_id'], 'first')
        await queue.finish(save)
        self.assertEqual((await queue.take(save))['job_id'], 'third')

    async def test_concurrent_submissions_do_not_lose_jobs(self):
        queue, save = TransferQueue(), AsyncMock()
        await asyncio.gather(*(queue.enqueue(self.job(str(i)), save) for i in range(20)))
        self.assertEqual(len(queue.pending), 20)
        self.assertEqual(len({job['job_id'] for job in queue.pending}), 20)

    async def test_clear_waiting_queue_keeps_active_job(self):
        queue, save = TransferQueue(), AsyncMock()
        await queue.enqueue(self.job('first'), save)
        await queue.enqueue(self.job('second'), save)
        await queue.take(save)
        self.assertEqual(len(await queue.clear(save)), 1)
        self.assertEqual(queue.active['job_id'], 'first')

    def test_restart_requeues_active_with_resume_and_retains_order(self):
        queue = TransferQueue()
        queue.hydrate({'active': self.job('active'), 'pending': [self.job('waiting')]})
        self.assertEqual([j['job_id'] for j in queue.pending], ['active', 'waiting'])
        self.assertIn('--resume', queue.pending[0]['argv'])
        self.assertTrue(queue.pending[0]['recovered'])

    def test_completed_active_snapshot_is_not_replayed(self):
        queue = TransferQueue()
        queue.hydrate({'active': self.job('active'), 'pending': [self.job('waiting')]}, {'job_id': 'active', 'phase': 'completed'})
        self.assertEqual([j['job_id'] for j in queue.pending], ['waiting'])

    async def test_failed_queue_save_rolls_back_submission(self):
        queue = TransferQueue()
        with self.assertRaises(RuntimeError):
            await queue.enqueue(self.job('first'), AsyncMock(side_effect=RuntimeError('disk unavailable')))
        self.assertEqual(queue.pending, [])


class BotProgressTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'heroku_bot'))
        cls.app = importlib.import_module('app')

    async def register_handlers(self, store):
        app, handlers, bot = self.app, {}, MagicMock()
        def register(*args, **kwargs):
            def decorator(function):
                handlers[function.__name__] = function
                return function
            return decorator
        bot.on_message.side_effect = register
        bot.on_callback_query.side_effect = register
        bot.start = AsyncMock(side_effect=RuntimeError('registered'))
        with patch.dict(os.environ, {'HEROKU_BOT_TOKEN': 'test', 'BOT_ADMIN_USER_IDS': '123',
                                    'TG_API_ID': '123', 'TG_API_HASH': 'test'}), patch.object(
            app, '_setup_logging'), patch.object(app, '_apply_bootstrap_settings'), patch.object(
            app, 'MongoStateStore', return_value=store), patch.object(app, 'Client', return_value=bot):
            with self.assertRaisesRegex(RuntimeError, 'registered'):
                await app.run_bot()
        return handlers

    async def test_handler_queues_concurrent_commands_and_has_cancel_buttons(self):
        app, queue = self.app, TransferQueue()
        store = MagicMock(load=AsyncMock(return_value=None), save=AsyncMock())
        handlers = await self.register_handlers(store)
        panels = [SimpleNamespace(chat=SimpleNamespace(id=123), id=501),
                  SimpleNamespace(chat=SimpleNamespace(id=123), id=502)]
        messages = [SimpleNamespace(text='/transfer msz:Course --up gd', id=i + 1,
                                   chat=SimpleNamespace(id=123), from_user=SimpleNamespace(id=123, first_name='Admin'),
                                   reply_text=AsyncMock(return_value=panels[i]), reply_to_message=None) for i in range(2)]
        with tempfile.TemporaryDirectory() as directory, patch.object(app, '_snapshot_path', side_effect=lambda name: Path(directory) / (name + '.json')), patch.object(
            app, 'TRANSFER_QUEUE', queue), patch.object(app, '_start_transfer_watcher'), patch.dict(os.environ):
            await asyncio.gather(*(handlers['transfer_handler'](MagicMock(), message) for message in messages))
            self.assertEqual(len(queue.pending), 2)
            self.assertEqual({j['notice_message_id'] for j in queue.pending}, {501, 502})
            markup = app._transfer_markup(queue.pending[0]['job_id'])
            self.assertTrue(any('Remove from queue' in b.text for row in markup.inline_keyboard for b in row))

    async def test_callbacks_require_admin_and_closing_does_not_cancel_transfer(self):
        app, queue = self.app, TransferQueue()
        queue.active = new_state(['msz:Course', '--up', 'gd'], job_id='d' * 12)
        queue.active['phase'] = 'running'
        handlers = await self.register_handlers(MagicMock(load=AsyncMock(return_value=None), save=AsyncMock()))
        panel = SimpleNamespace(chat=SimpleNamespace(id=123), id=501, delete=AsyncMock())
        query = SimpleNamespace(data='transfer_panel:cancel:' + 'd' * 12,
                                from_user=SimpleNamespace(id=999), message=panel, answer=AsyncMock())
        with patch.object(app, 'TRANSFER_QUEUE', queue), patch.object(app, 'ACTIVE_TRANSFER_TASK', MagicMock()) as task:
            await handlers['transfer_panel_callback'](MagicMock(), query)
            task.cancel.assert_not_called()
            query.data = 'transfer_panel:close:' + 'd' * 12
            query.from_user.id = 123
            await handlers['transfer_panel_callback'](MagicMock(), query)
            task.cancel.assert_not_called()
            panel.delete.assert_awaited_once()

    async def test_checkpoint_restore_rejects_paths_outside_state_directory(self):
        app = self.app
        saved = {'files': {'../outside.json': {'bad': True}, 'good.json': {'uploaded': [1]}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, '_load_state', AsyncMock(return_value=saved)):
            runtime = Path(directory) / 'runtime'
            await app._transfer_checkpoint(MagicMock(), {'source': 'a', 'destination': 'b'}, runtime, restore=True)
            self.assertTrue((runtime / 'state' / 'good.json').is_file())
            self.assertFalse((runtime / 'outside.json').exists())

    def test_profile_key_survives_display_name_change_and_isolates_destinations(self):
        app = self.app
        state = new_state(['msz:Course', '--up', 'gd', '--gdrive-folder-id', 'first'])
        key = app._transfer_profile_key(state)
        state['source'] = 'Pretty folder name from API'
        self.assertEqual(app._transfer_profile_key(state), key)
        other = new_state(['msz:Course', '--up', 'gd', '--gdrive-folder-id', 'second'])
        self.assertNotEqual(app._transfer_profile_key(other), key)

    async def test_job_uses_progress_events_and_persists_final_result(self):
        app = self.app
        job = new_state(['msz:Course', '--up', 'gd'], job_id='a' * 12)
        job.update(requested_chat_id=123, notice_message_id=456)
        bot = MagicMock(edit_message_text=AsyncMock())
        store = MagicMock(save=AsyncMock(), load=AsyncMock(return_value=None))
        async def process(argv, **kwargs):
            for event in ({'event': 'totals', 'total_files': 1},
                          {'event': 'file', 'index': 1, 'file_name': 'lecture.mp4'},
                          {'event': 'result', 'result': 'uploaded'}):
                await kwargs['on_output'](PREFIX + json.dumps(event) + '\n')
            return 0
        with tempfile.TemporaryDirectory() as directory, patch.object(app, '_snapshot_path', side_effect=lambda name: Path(directory) / (name + '.json')), patch.object(
            app, '_load_bot_settings', AsyncMock()), patch.object(app, '_transfer_checkpoint', AsyncMock()), patch.object(
            app, '_start_transfer_watcher'), patch.object(app, 'run_transfer_process', side_effect=process), patch.object(
            app, 'TRANSFER_QUEUE', TransferQueue()), patch.object(app, 'TRANSFER_SHUTTING_DOWN', False), patch.dict(app.TRANSFER_HISTORY, clear=True):
            await app._run_transfer_job(bot, job, store, Path(directory))
            self.assertEqual(job['phase'], 'completed')
            self.assertEqual(job['uploaded'], 1)
            self.assertIn('TRANSFER COMPLETED', bot.edit_message_text.call_args.kwargs['text'])
            self.assertIn('Time Taken', bot.edit_message_text.call_args.kwargs['text'])
            self.assertIn('video/mp4', bot.edit_message_text.call_args.kwargs['text'])
            self.assertNotIn('Bot Stats', bot.edit_message_text.call_args.kwargs['text'])
            self.assertTrue(any(call.args[0] == 'transfer:last' for call in store.save.call_args_list))

    async def test_stale_cancel_button_does_not_stop_new_job(self):
        app, queue = self.app, TransferQueue()
        queue.active = {'job_id': 'new-job', 'phase': 'running'}
        task = MagicMock()
        with patch.object(app, 'TRANSFER_QUEUE', queue), patch.object(app, 'ACTIVE_TRANSFER_TASK', task):
            self.assertFalse(await app._cancel_transfer(MagicMock(), 'old-job'))
            task.cancel.assert_not_called()

    async def test_shutdown_marks_active_job_for_recovery(self):
        app = self.app
        job = new_state(['msz:Course', '--up', 'gd'], job_id='b' * 12)
        job.update(requested_chat_id=123, notice_message_id=456)
        store = MagicMock(save=AsyncMock(), load=AsyncMock(return_value=None))
        with tempfile.TemporaryDirectory() as directory, patch.object(app, '_snapshot_path', side_effect=lambda name: Path(directory) / (name + '.json')), patch.object(
            app, '_load_bot_settings', AsyncMock()), patch.object(app, '_transfer_checkpoint', AsyncMock()), patch.object(
            app, '_start_transfer_watcher'), patch.object(app, 'run_transfer_process', AsyncMock(side_effect=asyncio.CancelledError)), patch.object(
            app, 'TRANSFER_QUEUE', TransferQueue()), patch.object(app, 'TRANSFER_SHUTTING_DOWN', True):
            await app._run_transfer_job(MagicMock(edit_message_text=AsyncMock()), job, store, Path(directory))
        self.assertEqual(job['phase'], 'interrupted')

    async def test_panel_refresh_recovers_after_edit_failure_without_worker_output(self):
        app = self.app
        bot = MagicMock()
        key = (123, 456)
        calls = 0
        async def sleep(_):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise asyncio.CancelledError
        with patch.object(app.asyncio, 'sleep', side_effect=sleep), patch.object(app, '_transfer_view', AsyncMock(return_value='live panel')), patch.object(
            app, '_edit_status_message', AsyncMock(side_effect=[OSError('network'), True])) as edit, patch.dict(
            app.ACTIVE_STATUS_LAST_TEXTS, clear=True), patch.dict(app.ACTIVE_STATUS_VIEWS, clear=True), patch.dict(app.TRANSFER_HISTORY, clear=True):
            with self.assertRaises(asyncio.CancelledError):
                await app._watch_transfer_panel(bot, MagicMock(), *key, 'c' * 12)
            self.assertEqual(edit.await_count, 2)
            self.assertEqual(app.ACTIVE_STATUS_LAST_TEXTS[key], 'live panel')


class WorkerTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_msz_dry_run_reports_total_files_and_individual_results(self):
        from MSZDRIVE_uploader import msz_to_gdrive
        from MSZDRIVE_uploader.msz_api import MszEntry
        root = MszEntry(id=1, name='Course', type='folder', rel_path='Course')
        entries = [MszEntry(id=2, name='first.mp4', type='video', rel_path='Course/first.mp4', size=100),
                   MszEntry(id=3, name='second.mp4', type='video', rel_path='Course/second.mp4', size=200)]
        client = MagicMock(resolve_source_entries=MagicMock(return_value=(root, entries)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TRANSFER_PROGRESS_EVENTS': '1'}), patch.object(
            msz_to_gdrive, 'MszApiClient', return_value=client), patch.object(
            msz_to_gdrive, '_resolve_browser_folder_title', AsyncMock(return_value=(root, entries))), redirect_stdout(io.StringIO()) as output:
            args = msz_to_gdrive._build_parser().parse_args(['msz:Course', 'folder', '--runtime-dir', directory, '--dry-run'])
            self.assertEqual(await msz_to_gdrive.run(args), 0)
        state = new_state(['msz:Course', '--up', 'gd'])
        EventStream(state).feed(output.getvalue())
        self.assertEqual(state['total_files'], 2)
        self.assertEqual(state['total_bytes'], 300)
        self.assertEqual(state['processed'], 2)
        self.assertEqual(state['skipped'], 2)


if __name__ == '__main__':
    unittest.main()
