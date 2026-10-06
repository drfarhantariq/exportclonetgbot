from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from heroku_bot.transfer_control import format_status, parse_command, run_process
from MSZDRIVE_uploader.transfer import _build_parser
from scripts.deploy_heroku import CONFIG_KEYS, prepare_deploy_dir
from MSZDRIVE_uploader.gdrive_upload import GoogleDriveResumableUploader


class CommandTests(unittest.TestCase):
    def parse(self, command, **kwargs):
        return parse_command(command, config=Path('config.yaml'), runtime=Path('runtime/transfers'), **kwargs)

    def test_all_transfer_routes(self):
        for source, target, dest in [
            ('msz:Folder', 'gd', 'gdrive:123'), ('gdrive:123', 'msz', 'msz:Folder'),
            ('msz:Folder', 'telegram', 'https://t.me/c/123/4/5'),
            ('gdrive:123', 'telegram', 'https://t.me/c/123/4/5'),
            ('https://t.me/c/123/4/5', 'gd', ''),
            ('https://t.me/c/123/4/5', 'msz', ''),
            ('https://t.me/c/123/4/5', 'both', ''),
        ]:
            with self.subTest(source=source, target=target):
                args = _build_parser().parse_args(self.parse(f'{source} {dest} --up {target}'))
                self.assertEqual(args.up, target)
                self.assertEqual(args.runtime_dir, str(Path('runtime/transfers')))

    def test_edited_index_reply_and_quoted_destination(self):
        args = _build_parser().parse_args(self.parse(
            'https://t.me/c/123/4/5 "msz:Folder With Spaces" --index-done --up msz --above',
            index_path=Path('edited index.txt')))
        self.assertEqual(args.dest, 'msz:Folder With Spaces')
        self.assertEqual(args.index_done, 'edited index.txt')
        self.assertTrue(args.above)

    def test_index_output_is_managed(self):
        for command in ['https://t.me/c/123/4/5 --index',
                        'https://t.me/c/123/4/5 --index=somewhere.txt',
                        'https://t.me/c/123/4/5']:
            args = _build_parser().parse_args(self.parse(command))
            self.assertEqual(args.index, str(Path('runtime/transfers/telegram_indexes/transfer_index.txt')))

    def test_invalid_arguments_and_routes(self):
        for command in ['msz:Folder --up both', 'msz:Folder --up wat',
                        'gdrive:123 --up msz --config=/tmp/config',
                        'msz:Folder --up gd --to telegram', '"unterminated',
                        'gdrive:123 --index --up msz']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.parse(command)

    def test_status_escapes_markup(self):
        panel = format_status({'source': '<bad>', 'detail': 'raw log contents', 'phase': 'failed'})
        self.assertIn('&lt;bad&gt;', panel)
        self.assertNotIn('raw log contents', panel)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_chromium_is_passed_to_transfer_worker(self):
        process = MagicMock(returncode=0)
        process.stdout.read = AsyncMock(return_value=b'')
        process.wait = AsyncMock(return_value=0)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PLAYWRIGHT_CHROMIUM_EXECUTABLE': ''}), patch(
            'heroku_bot.transfer_control.shutil.which', return_value='/app/.apt/usr/bin/chromium'
        ), patch('heroku_bot.transfer_control.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            await run_process([], runtime=Path(directory), on_output=AsyncMock())
            self.assertEqual(spawn.call_args.kwargs['env']['PLAYWRIGHT_CHROMIUM_EXECUTABLE'], '/app/.apt/usr/bin/chromium')

    async def test_output_and_exit_status(self):
        process = MagicMock(returncode=3)
        process.stdout.read = AsyncMock(side_effect=[b'Uploading file\n', b'Failed\n', b''])
        process.wait = AsyncMock(return_value=3)
        output = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch(
            'heroku_bot.transfer_control.asyncio.create_subprocess_exec', AsyncMock(return_value=process)
        ) as spawn:
            code = await run_process(['msz:Folder', '--up', 'gd'], runtime=Path(directory), on_output=output)
            self.assertEqual(code, 3)
            self.assertEqual(output.await_count, 2)
            self.assertIn('Uploading file', (Path(directory) / 'transfer.log').read_text())
            self.assertEqual(spawn.call_args.args[0], sys.executable)
            self.assertNotIn('shell', spawn.call_args.kwargs)

    async def test_cancellation_stops_worker(self):
        process = MagicMock(returncode=None)
        started = asyncio.Event()
        async def read(_):
            started.set()
            await asyncio.Event().wait()
        process.stdout.read = read
        process.wait = AsyncMock(return_value=-15)
        with tempfile.TemporaryDirectory() as directory, patch(
            'heroku_bot.transfer_control.asyncio.create_subprocess_exec', AsyncMock(return_value=process)
        ), patch('heroku_bot.transfer_control.os.killpg', create=True) as killpg:
            task = asyncio.create_task(run_process([], runtime=Path(directory), on_output=AsyncMock()))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            if os.name == 'nt':
                process.terminate.assert_called_once()
            else:
                killpg.assert_called_once()
            process.wait.assert_awaited()


class DeploymentTests(unittest.TestCase):
    def test_bundle_includes_package_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'heroku_bot'
            source.mkdir()
            (source / 'app.py').write_text('# bot')
            package = root / 'MSZDRIVE_uploader'
            package.mkdir()
            (package / 'transfer.py').write_text('# transfer')
            (package / '.env').write_text('SECRET=hidden')
            (package / 'DriveToMSZ.ipynb').write_text('{}')
            deploy = root / 'deploy'
            prepare_deploy_dir(source, deploy)
            self.assertTrue((deploy / 'MSZDRIVE_uploader/transfer.py').exists())
            self.assertFalse((deploy / 'MSZDRIVE_uploader/.env').exists())
            self.assertFalse((deploy / 'MSZDRIVE_uploader/DriveToMSZ.ipynb').exists())
        self.assertIn('GDRIVE_TOKEN_JSON', CONFIG_KEYS)
        self.assertIn('MSZ_API_TOKEN', CONFIG_KEYS)

    def test_drive_json_credentials_work_without_pickle(self):
        with patch.dict(os.environ, {'GDRIVE_TOKEN_JSON': '{"refresh_token":"test"}'}), patch(
            'google.oauth2.credentials.Credentials.from_authorized_user_info'
        ) as credentials, patch('googleapiclient.discovery.build') as build:
            uploader = GoogleDriveResumableUploader(Path('missing-test-token.pickle'))
            credentials.assert_called_once_with({'refresh_token': 'test'})
            self.assertIs(uploader.service, build.return_value)
            self.assertIs(build.call_args.kwargs['credentials'], credentials.return_value)


if __name__ == '__main__':
    unittest.main()
