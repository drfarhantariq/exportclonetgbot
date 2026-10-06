from unittest import TestCase
from unittest.mock import Mock, patch

from MSZDRIVE_uploader.msz_api import MszApiClient


class MszApiRequestTests(TestCase):
    def test_detects_versioned_api_with_required_section(self):
        def get(url, *, headers, params, timeout):
            self.assertEqual(headers['Accept'], 'application/json')
            if params.get('section') != 'all':
                return Mock(status_code=422, json=Mock(return_value={'errors': {'section': ['required']}}))
            self.assertEqual(url, 'https://example.com/api/v1/drive/file-entries')
            return Mock(status_code=200, json=Mock(return_value={'data': []}))
        with patch('MSZDRIVE_uploader.msz_api.requests.get', side_effect=get):
            client = MszApiClient('https://example.com', 'test-token')
        self.assertEqual(client.api_base, 'https://example.com/api/v1')

    def test_explicit_api_base_and_scoped_paginated_listing(self):
        responses = [Mock(status_code=200, json=Mock(return_value={'data': []})),
                     Mock(status_code=200, json=Mock(return_value={'data': [
                         {'id': 1, 'name': 'first', 'parent_id': 84650},
                         {'id': 2, 'name': 'second', 'parent_id': 84650}]})),
                     Mock(status_code=200, json=Mock(return_value={'data': [
                         {'id': 3, 'name': 'third', 'parent_id': 84650}]}))]
        with patch('MSZDRIVE_uploader.msz_api.requests.get', side_effect=responses) as get:
            client = MszApiClient('https://example.com/api/v1', 'test-token')
            entries = client._list_child_entries(84650, per_page=2)
        self.assertEqual([entry['id'] for entry in entries], [1, 2, 3])
        for page, call in enumerate(get.call_args_list[1:], 1):
            self.assertEqual(call.kwargs['params'], {
                'section': 'folder', 'per_page': 2, 'page': page, 'folder_id': '84650'})

    def test_encoded_folder_id_accepts_children_with_numeric_parent(self):
        client = object.__new__(MszApiClient)
        with patch.object(client, 'list_entries', return_value=[
            {'id': 1, 'parent_id': 84650}, {'id': 2, 'parent_id': 999}]) as listing:
            children = client._list_child_entries('ODQ2NTB8cGFkZA')
        self.assertEqual([entry['id'] for entry in children], [1])
        self.assertEqual(listing.call_args.kwargs['extra_params'], {
            'section': 'folder', 'folder_id': 'ODQ2NTB8cGFkZA'})

    def test_server_pagination_metadata_prevents_truncation(self):
        client = object.__new__(MszApiClient)
        client.api_base = 'https://example.com/api/v1'
        client.headers = {}
        client.timeout = 60
        responses = [Mock(status_code=200, json=Mock(return_value={
            'data': [{'id': 1}], 'meta': {'last_page': 2}})),
            Mock(status_code=200, json=Mock(return_value={
                'data': [{'id': 2}], 'meta': {'last_page': 2}}))]
        with patch('MSZDRIVE_uploader.msz_api.requests.get', side_effect=responses):
            entries = client.list_entries(per_page=200)
        self.assertEqual([entry['id'] for entry in entries], [1, 2])

    def test_html_fallback_is_not_mistaken_for_api(self):
        html = Mock(status_code=200, json=Mock(side_effect=ValueError))
        api = Mock(status_code=200, json=Mock(return_value={'data': []}))
        with patch('MSZDRIVE_uploader.msz_api.requests.get', side_effect=[html, api]):
            client = MszApiClient('https://example.com', 'test-token')
        self.assertEqual(client.api_base, 'https://example.com/api')
