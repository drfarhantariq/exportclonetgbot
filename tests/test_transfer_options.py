import argparse
import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from MSZDRIVE_uploader.transfer import _build_parser
from heroku_bot.transfer_control import parse_command
from heroku_bot.transfer_settings import TOGGLE_OPTIONS, OPTION_DEFAULTS, apply_defaults


class TransferOptionTests(unittest.TestCase):
    def test_each_boolean_parser_flag_has_a_settings_toggle(self):
        covered = {flag for option in TOGGLE_OPTIONS.values() for flag in option[1:3]}
        for action in _build_parser()._actions:
            if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction)):
                self.assertTrue(set(action.option_strings) <= covered, action.option_strings)

    def test_all_toggle_states_reach_parser(self):
        for key, (_, enabled, disabled, default) in TOGGLE_OPTIONS.items():
            with self.subTest(key=key):
                for value in (True, False):
                    tokens = apply_defaults(['https://t.me/c/123/4', '--up', 'gd'], {key: value})
                    self.assertIn(enabled if value else disabled, tokens)
                    args = _build_parser().parse_args(tokens)
                    dest = enabled[2:].replace('-', '_')
                    if dest == 'resume':
                        self.assertEqual(args.no_resume, not value)
                    elif dest == 'browser_folder_title':
                        self.assertEqual(args.no_browser_folder_title, not value)
                    elif dest == 'index':
                        self.assertEqual(bool(args.index), value)
                    else:
                        self.assertEqual(getattr(args, dest), value)

    def test_explicit_enabled_and_disabled_flags_override_saved_default(self):
        for key, (_, enabled, disabled, default) in TOGGLE_OPTIONS.items():
            for flag, saved in [(enabled, False), (disabled, True)]:
                tokens = apply_defaults(['https://t.me/c/123/4', '--up', 'gd', flag], {key: saved})
                self.assertEqual(tokens.count(flag), 1)
                self.assertNotIn(disabled if flag == enabled else enabled, tokens)

    def test_index_default_only_applies_to_telegram_and_not_edited_index(self):
        settings = {'transfer_default_generate_index': True}
        for source in ['msz:Course', 'gdrive:ID', 'index:course.txt']:
            self.assertNotIn('--index', apply_defaults([source, '--up', 'gd'], settings))
        tokens = apply_defaults(['https://t.me/c/123/4', '--index-done', 'edited.txt', '--up', 'gd'], settings)
        self.assertNotIn('--index', tokens)

    def test_configured_index_uses_managed_output_path(self):
        runtime = Path('runtime/transfers')
        argv = parse_command('https://t.me/c/123/4 --up gd', config=Path('config.yaml'), runtime=runtime,
                             defaults={'transfer_default_generate_index': True})
        self.assertEqual(argv[argv.index('--index') + 1], str(runtime / 'telegram_indexes/transfer_index.txt'))

    def test_value_flags_preserve_explicit_equals_form(self):
        tokens = apply_defaults(['msz:Course', '--up', 'gd', '--batch-size=9', '--tg-download=normal'],
                                {'transfer_default_batch_size': 99, 'transfer_default_tg_download': 'hyper'})
        args = _build_parser().parse_args(tokens)
        self.assertEqual(args.batch_size, 9)
        self.assertEqual(args.tg_download, 'normal')


class TransferOptionsBotTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'heroku_bot'))
        cls.app = importlib.import_module('app')

    def test_every_option_is_a_persistent_settings_item_with_toggle_control(self):
        app = self.app
        app._assert_settings_categories_complete()
        for key, (_, enabled, disabled, default) in TOGGLE_OPTIONS.items():
            self.assertIn(key, app.SETTINGS_CATEGORIES['transfer'])
            self.assertIn(key, app.BOT_TOGGLE_SETTING_KEYS)
            self.assertEqual(app._normalize_setting_value(key, 'on'), True)
            self.assertEqual(app._normalize_setting_value(key, 'off'), False)
            rows = app._setting_detail_markup(key, 'transfer', 0, 'view', app.BOT_SETTINGS_DEFAULTS).inline_keyboard
            self.assertTrue(any(b.text in {'Turn on', 'Turn off'} for row in rows for b in row))

    async def test_toggle_callback_saves_and_reloads_default(self):
        from test_transfer_settings import SavedSettingsTests
        app = self.app
        settings = dict(app.BOT_SETTINGS_DEFAULTS)
        store = MagicMock(load=AsyncMock(return_value=settings), save=AsyncMock())
        handlers = await SavedSettingsTests.register_handlers(self, store)
        key = 'transfer_default_dry_run'
        index = app._settings_key_index('transfer', key)
        category = app._settings_category_index('transfer')
        panel = SimpleNamespace(edit_text=AsyncMock())
        query = SimpleNamespace(data=f'settings:toggle:{category}:0:view:{index}', from_user=SimpleNamespace(id=123),
                                message=panel, answer=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, patch.object(app, '_snapshot_path', return_value=Path(directory) / 'settings.json'), patch.dict(app.os.environ):
            await handlers['settings_callback_handler'](MagicMock(), query)
            saved = store.save.call_args.args[1]
            self.assertTrue(saved[key])
            store.load.return_value = saved
            loaded = await app._load_bot_settings(store)
            self.assertTrue(loaded[key])

    async def test_submission_captures_settings_and_command_can_disable_default(self):
        from test_transfer_progress import BotProgressTests
        app = self.app
        settings = dict(app.BOT_SETTINGS_DEFAULTS, transfer_default_dry_run=True)
        store = MagicMock(load=AsyncMock(return_value=None), save=AsyncMock())
        handlers = await BotProgressTests.register_handlers(self, store)
        queue = app.TransferQueue()
        panels = [SimpleNamespace(chat=SimpleNamespace(id=123), id=501 + n) for n in range(2)]
        messages = [SimpleNamespace(text='msz:Course --up gd' + suffix, id=n+1,
                                    chat=SimpleNamespace(id=123), from_user=SimpleNamespace(id=123, first_name='Admin'),
                                    reply_text=AsyncMock(return_value=panels[n]), reply_to_message=None)
                    for n, suffix in enumerate(['', ' --no-dry-run'])]
        # Handler expects the /transfer command before its arguments.
        for message in messages:
            message.text = '/transfer ' + message.text
        with tempfile.TemporaryDirectory() as directory, patch.object(app, '_snapshot_path', side_effect=lambda name: Path(directory) / (name + '.json')), patch.object(
                app, '_load_bot_settings', AsyncMock(return_value=settings)), patch.object(app, 'TRANSFER_QUEUE', queue), patch.object(app, '_start_transfer_watcher'):
            for message in messages:
                await handlers['transfer_handler'](MagicMock(), message)
            self.assertEqual(len(queue.pending), 2)
            self.assertIn('--dry-run', queue.pending[0]['argv'])
            self.assertIn('--no-dry-run', queue.pending[1]['argv'])
            settings['transfer_default_dry_run'] = False
            self.assertIn('--dry-run', queue.pending[0]['argv'])
