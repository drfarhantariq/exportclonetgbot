import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from MSZDRIVE_uploader.gdrive_download import download


def response(data=b'', *, start=0, total=None, status=206, etag='version1', blocks=None):
    total = len(data) if total is None else total
    headers = {'Content-Range': f'bytes {start}-{start + len(data) - 1}/{total}', 'ETag': etag}
    if status == 200:
        headers = {'Content-Length': str(len(data)), 'ETag': etag}
    return MagicMock(status_code=status, headers=headers, iter_content=MagicMock(return_value=iter(blocks or [data])),
                     raise_for_status=MagicMock(), json=MagicMock(return_value={}))


class DriveDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'lecture.mp4'

    def tearDown(self):
        self.directory.cleanup()

    def test_ranges_are_joined_exactly_and_only_then_published(self):
        first, last = response(b'abcd', total=6), response(b'ef', start=4, total=6)
        session = MagicMock(get=MagicMock(side_effect=[first, last]))
        progress = []
        download(session, 'https://example/media', self.path, total_size=6, chunk_size=4,
                 progress_callback=lambda done, total: progress.append((done, total)))
        self.assertEqual(self.path.read_bytes(), b'abcdef')
        self.assertEqual([call.kwargs['headers']['Range'] for call in session.get.call_args_list], ['bytes=0-3', 'bytes=4-5'])
        self.assertEqual(session.get.call_args_list[1].kwargs['headers']['If-Range'], 'version1')
        self.assertEqual(progress[-1], (6, 6))
        first.close.assert_called_once()
        last.close.assert_called_once()

    def test_progress_updates_inside_a_large_range(self):
        session = MagicMock(get=MagicMock(return_value=response(b'abcdef', blocks=[b'ab', b'cd', b'ef'])))
        progress = []
        download(session, 'url', self.path, total_size=6, progress_callback=lambda done, total: progress.append(done))
        self.assertEqual(progress[:3], [2, 4, 6])
        self.assertEqual(session.get.call_args.kwargs['headers']['Range'], 'bytes=0-5')

    def test_transient_http_error_retries_without_corruption(self):
        busy = response(status=503)
        good = response(b'abcd')
        session = MagicMock(get=MagicMock(side_effect=[busy, good]))
        with patch('MSZDRIVE_uploader.gdrive_download.time.sleep') as sleep:
            download(session, 'url', self.path, total_size=4)
        self.assertEqual(self.path.read_bytes(), b'abcd')
        busy.close.assert_called_once()
        sleep.assert_called_once()

    def test_mid_stream_disconnect_resumes_at_written_byte(self):
        def broken_stream():
            yield b'ab'
            raise requests.ConnectionError('disconnected')
        broken = response(b'abcd')
        broken.iter_content.return_value = broken_stream()
        session = MagicMock(get=MagicMock(side_effect=[broken, response(b'cd', start=2, total=4)]))
        with patch('MSZDRIVE_uploader.gdrive_download.time.sleep'):
            download(session, 'url', self.path, total_size=4)
        self.assertEqual(self.path.read_bytes(), b'abcd')
        self.assertEqual(session.get.call_args.kwargs['headers']['Range'], 'bytes=2-3')

    def test_clean_truncated_response_retries_missing_bytes(self):
        short = response(b'abcd', blocks=[b'ab'])
        session = MagicMock(get=MagicMock(side_effect=[short, response(b'cd', start=2, total=4)]))
        with patch('MSZDRIVE_uploader.gdrive_download.time.sleep'):
            download(session, 'url', self.path, total_size=4)
        self.assertEqual(self.path.read_bytes(), b'abcd')

    def test_range_ignored_at_start_can_stream_whole_file(self):
        session = MagicMock(get=MagicMock(return_value=response(b'abcdef', status=200)))
        download(session, 'url', self.path, total_size=6, chunk_size=2)
        self.assertEqual(self.path.read_bytes(), b'abcdef')
        self.assertEqual(session.get.call_count, 1)

    def test_wrong_offset_changed_size_and_changed_version_are_rejected(self):
        cases = [
            [response(b'ab', start=1, total=4)],
            [response(b'abcd', total=5)],
            [response(b'ab', total=4), response(b'cd', start=2, total=4, etag='version2')],
            [response(b'ab', total=4), response(b'abcd', status=200)],
        ]
        for replies in cases:
            with self.subTest(replies=replies):
                self.path.write_bytes(b'original')
                session = MagicMock(get=MagicMock(side_effect=replies))
                with self.assertRaises(RuntimeError):
                    download(session, 'url', self.path, total_size=4, chunk_size=2)
                self.assertEqual(self.path.read_bytes(), b'original')

    def test_permanent_quota_error_is_not_retried(self):
        denied = response(status=403)
        denied.json.return_value = {'error': {'errors': [{'reason': 'downloadQuotaExceeded'}]}}
        denied.raise_for_status.side_effect = requests.HTTPError('quota')
        session = MagicMock(get=MagicMock(return_value=denied))
        with self.assertRaises(requests.HTTPError):
            download(session, 'url', self.path, total_size=4)
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_network_retries_are_bounded_and_do_not_publish_partial_file(self):
        session = MagicMock(get=MagicMock(side_effect=requests.Timeout('timeout')))
        with patch('MSZDRIVE_uploader.gdrive_download.time.sleep'), self.assertRaises(requests.Timeout):
            download(session, 'url', self.path, total_size=4, max_retries=2)
        self.assertEqual(session.get.call_count, 3)
        self.assertFalse(self.path.exists())

    def test_empty_file_completes_without_range_request(self):
        session = MagicMock()
        download(session, 'url', self.path, total_size=0)
        self.assertEqual(self.path.read_bytes(), b'')
        session.get.assert_not_called()
