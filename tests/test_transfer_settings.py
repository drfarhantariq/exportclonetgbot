from __future__ import annotations

import importlib
import io
import json
import os
import pickle
import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from heroku_bot.transfer_settings import decode_upload, normalize, MAX_UPLOAD_BYTES

TOKEN = {"type": "authorized_user", "client_id": "test-client", "client_secret": "test-secret", "refresh_token": "test-refresh"}


class CredentialValidationTests(unittest.TestCase):
    def test_pickle_credentials_convert_to_persistent_oauth_json(self):
        from google.oauth2.credentials import Credentials
        credentials = Credentials(token='old-access', refresh_token='test-refresh',
                                  client_id='test-client', client_secret='test-secret',
                                  token_uri='https://oauth2.googleapis.com/token',
                                  scopes=['https://www.googleapis.com/auth/drive'])
        credentials.expiry = datetime.datetime(2020, 1, 1)
        for protocol in (3, 4, 5):
            values = decode_upload('gdrive_token_pickle', pickle.dumps(credentials, protocol=protocol))
            info = json.loads(values['gdrive_token_json'])
            for key in TOKEN:
                self.assertEqual(info[key], TOKEN[key])
            self.assertNotIn('token', info)

    def test_pickle_rejects_code_execution_and_invalid_credentials(self):
        class UnsafeToken:
            def __reduce__(self):
                return (eval, ('1 + 1',))
        for content in (pickle.dumps(UnsafeToken()), pickle.dumps({}), b'not a pickle',
                        b'x' * (MAX_UPLOAD_BYTES + 1)):
            with self.assertRaises(ValueError):
                decode_upload('gdrive_token_pickle', content)

    def test_oauth_json_keeps_quotes_and_validates_credentials(self):
        encoded = normalize('gdrive_token_json', json.dumps(TOKEN))
        self.assertEqual(json.loads(encoded), TOKEN)
        for invalid in ['broken JSON', '{}', json.dumps({**TOKEN, 'type': 'service_account'})]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize('gdrive_token_json', invalid)

    def test_msz_bundle_import_and_password_preservation(self):
        values = decode_upload('msz_credentials', json.dumps({
            'email': 'test@example.com', 'password': '  password with spaces  ',
            'MSZ_API_TOKEN': 'test-token',
        }).encode())
        self.assertEqual(values['msz_password'], '  password with spaces  ')
        self.assertEqual(values['msz_api_token'], 'test-token')

    def test_file_import_rejects_large_or_non_text_data(self):
        for data in [b'x' * (MAX_UPLOAD_BYTES + 1), b'\xff\xfe']:
            with self.assertRaises(ValueError):
                decode_upload('gdrive_token_json', data)
        with self.assertRaises(ValueError):
            decode_upload('msz_credentials', b'{}')

    def test_individual_token_file_and_bom(self):
        self.assertEqual(decode_upload('msz_api_token', b'\xef\xbb\xbftest-token\n'), {'msz_api_token': 'test-token'})


class SavedSettingsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        bundle = Path(__file__).resolve().parents[1] / 'heroku_bot'
        sys.path.insert(0, str(bundle))
        # Import only; the worker starts exclusively through main().
        with patch.dict(os.environ):
            cls.app = importlib.import_module('app')

    async def test_saved_credentials_reload_from_mongo_and_reach_worker_env(self):
        app = self.app
        settings = dict(app.BOT_SETTINGS_DEFAULTS)
        settings.update(msz_email='test@example.com', msz_password='secret',
                        msz_api_token='test-token', gdrive_token_json=json.dumps(TOKEN),
                        gdrive_folder_id='folder-id')
        store = MagicMock()
        store.save = AsyncMock()
        store.load = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), patch.object(
            app, '_snapshot_path', return_value=Path(directory) / 'settings.json'
        ):
            self.assertTrue(await app._save_bot_settings(store, settings))
            saved = store.save.call_args.args[1]
            self.assertEqual(json.loads(saved['gdrive_token_json']), TOKEN)
            store.load.return_value = saved
            for name in app.TRANSFER_ENV_KEYS.values():
                os.environ.pop(name, None)
            loaded = await app._load_bot_settings(store)
            self.assertEqual(loaded['msz_password'], 'secret')
            self.assertEqual(os.environ['MSZ_API_TOKEN'], 'test-token')
            self.assertEqual(os.environ['GDRIVE_FOLDER_ID'], 'folder-id')
            self.assertEqual(json.loads(os.environ['GDRIVE_TOKEN_JSON']), TOKEN)
            for key in app.TRANSFER_SECRET_KEYS:
                self.assertNotIn(str(loaded[key]), app._masked_setting_value(key, loaded[key]))

    async def test_local_save_is_reported_and_credential_clear_stays_empty(self):
        app = self.app
        store = MagicMock(save=AsyncMock(side_effect=RuntimeError('offline')),
                          load=AsyncMock(side_effect=RuntimeError('offline')))
        settings = dict(app.BOT_SETTINGS_DEFAULTS)
        settings.update(msz_api_token='', gdrive_token_json='')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MSZ_API_TOKEN': 'old-token'}), patch.object(
            app, '_snapshot_path', return_value=Path(directory) / 'settings.json'
        ):
            durable = await app._save_bot_settings(store, settings)
            self.assertFalse(durable)
            self.assertIn('locally', app._transfer_save_notice({'msz_api_token': ''}, durable))
            self.assertEqual(os.environ['MSZ_API_TOKEN'], '')
            await app._load_bot_settings(store)
            self.assertEqual(os.environ['MSZ_API_TOKEN'], '')

    async def test_upload_is_in_memory_and_never_echoes_credentials(self):
        app = self.app
        client = MagicMock(download_media=AsyncMock(return_value=io.BytesIO(json.dumps(TOKEN).encode())))
        message = MagicMock()
        message.document.file_size = 1024
        values = await app._read_settings_upload(client, message, 'gdrive_token_json')
        self.assertEqual(json.loads(values['gdrive_token_json']), TOKEN)
        self.assertTrue(client.download_media.call_args.kwargs['in_memory'])
        notice = app._transfer_save_notice(values, True)
        self.assertNotIn('test-secret', notice)
        self.assertNotIn('test-refresh', notice)

    def test_transfer_settings_category_is_complete(self):
        self.app._assert_settings_categories_complete()
        self.assertIn('transfer', self.app.SETTINGS_CATEGORY_ORDER)

    async def register_handlers(self, store):
        app = self.app
        handlers = {}
        bot = MagicMock()
        def register(*args, **kwargs):
            def decorator(function):
                handlers[function.__name__] = function
                return function
            return decorator
        bot.on_message.side_effect = register
        bot.on_callback_query.side_effect = register
        bot.start = AsyncMock(side_effect=RuntimeError('registration complete'))
        with patch.dict(os.environ, {'HEROKU_BOT_TOKEN': 'test', 'BOT_ADMIN_USER_IDS': '123',
                                    'TG_API_ID': '123', 'TG_API_HASH': 'test'}), patch.object(
            app, '_setup_logging'
        ), patch.object(app, '_apply_bootstrap_settings'), patch.object(
            app, 'MongoStateStore', return_value=store
        ), patch.object(app, 'Client', return_value=bot):
            with self.assertRaisesRegex(RuntimeError, 'registration complete'):
                await app.run_bot()
        return handlers

    async def test_settings_command_preserves_raw_json_and_requires_admin(self):
        app = self.app
        store = MagicMock(load=AsyncMock(return_value=None), save=AsyncMock())
        handlers = await self.register_handlers(store)
        message = SimpleNamespace(text='/settings set gdrive_token_json ' + json.dumps(TOKEN),
                                  from_user=SimpleNamespace(id=999), reply_text=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), patch.object(
            app, '_snapshot_path', return_value=Path(directory) / 'settings.json'
        ):
            await handlers['settings_handler'](MagicMock(), message)
            store.save.assert_not_awaited()
            message.from_user.id = 123
            await handlers['settings_handler'](MagicMock(), message)
            saved = store.save.call_args.args[1]
            self.assertEqual(json.loads(saved['gdrive_token_json']), TOKEN)
            self.assertIn('MongoDB', message.reply_text.call_args.args[0])
            self.assertNotIn('test-secret', message.reply_text.call_args.args[0])

    async def test_settings_upload_prompt_accepts_next_document_and_clears_pending(self):
        app = self.app
        store = MagicMock(load=AsyncMock(return_value=None), save=AsyncMock())
        handlers = await self.register_handlers(store)
        client = MagicMock(download_media=AsyncMock(return_value=io.BytesIO(json.dumps(TOKEN).encode())))
        message = SimpleNamespace(text='/settings upload gdrive_token_json',
                                  from_user=SimpleNamespace(id=123), reply_text=AsyncMock(), reply_to_message=None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), patch.object(
            app, '_snapshot_path', return_value=Path(directory) / 'settings.json'
        ), patch.dict(app.ACTIVE_SETTING_INPUTS, clear=True):
            await handlers['settings_handler'](client, message)
            self.assertEqual(app.ACTIVE_SETTING_INPUTS[123], 'gdrive_token_json')
            message.text = None
            message.document = SimpleNamespace(file_size=1024)
            await handlers['settings_input_handler'](client, message)
            self.assertNotIn(123, app.ACTIVE_SETTING_INPUTS)
            self.assertEqual(json.loads(store.save.call_args.args[1]['gdrive_token_json']), TOKEN)

    async def test_pickle_reply_upload_replaces_token_and_preserves_default_folder(self):
        from google.oauth2.credentials import Credentials
        app = self.app
        settings = dict(app.BOT_SETTINGS_DEFAULTS, gdrive_folder_id='default-folder')
        store = MagicMock(load=AsyncMock(return_value=settings), save=AsyncMock())
        handlers = await self.register_handlers(store)
        data = pickle.dumps(Credentials(token=None, refresh_token='test-refresh',
                                       client_id='test-client', client_secret='test-secret'))
        client = MagicMock(download_media=AsyncMock(return_value=io.BytesIO(data)))
        message = SimpleNamespace(text='/settings upload gdrive_token_pickle',
                                  from_user=SimpleNamespace(id=123), reply_text=AsyncMock(),
                                  reply_to_message=SimpleNamespace(document=SimpleNamespace(file_size=len(data))))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), patch.object(
            app, '_snapshot_path', return_value=Path(directory) / 'settings.json'
        ):
            await handlers['settings_handler'](client, message)
            saved = store.save.call_args.args[1]
            self.assertEqual(json.loads(saved['gdrive_token_json']), TOKEN)
            self.assertEqual(saved['gdrive_folder_id'], 'default-folder')
            self.assertEqual(os.environ['GDRIVE_FOLDER_ID'], 'default-folder')
            self.assertIn('MongoDB', message.reply_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
