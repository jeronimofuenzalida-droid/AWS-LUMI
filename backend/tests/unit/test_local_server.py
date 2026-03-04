import os
import pathlib
import sys
import unittest
from unittest import mock


os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-1')
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')
os.environ['LOCAL_API_MODE'] = 'mock'
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

import local_server


class LocalServerTests(unittest.TestCase):
    def test_build_lambda_event_preserves_last_query_value_and_raw_query(self):
        event = local_server._build_lambda_event(
            'GET',
            '/analytics/progression/daily',
            {'userId': ['a1'], 'speaker': ['kid', 'all']},
            body=None,
        )
        self.assertEqual(event['rawPath'], '/analytics/progression/daily')
        self.assertEqual(event['queryStringParameters']['userId'], 'a1')
        self.assertEqual(event['queryStringParameters']['speaker'], 'all')
        self.assertIn('speaker=kid', event['rawQueryString'])
        self.assertIn('speaker=all', event['rawQueryString'])

    def test_path_parameters_extract_transcript_id(self):
        self.assertEqual(
            local_server._path_parameters('/transcriptions/abc/status'),
            {'transcriptId': 'abc'},
        )
        self.assertEqual(
            local_server._path_parameters('/transcriptions/abc/interactions'),
            {'transcriptId': 'abc'},
        )
        self.assertEqual(
            local_server._path_parameters('/transcriptions/abc'),
            {'transcriptId': 'abc'},
        )
        self.assertEqual(local_server._path_parameters('/v1/app-config'), {})

    def test_relay_lambda_returns_body_bytes_and_filters_headers(self):
        def fake_handler(event, _context):
            self.assertEqual(event['pathParameters'], {'transcriptId': 'tx1'})
            return {
                'statusCode': 202,
                'headers': {
                    'Content-Type': 'application/json',
                    'X-Test': 'ok',
                    'Access-Control-Allow-Origin': '*',
                },
                'body': '{"ok":true}',
            }

        with mock.patch.object(local_server, '_app_attr', return_value=fake_handler):
            status, payload, headers, content_type = local_server._relay_lambda(
                'GET',
                '/transcriptions/tx1',
                {},
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload, b'{"ok":true}')
        self.assertEqual(headers, {'X-Test': 'ok'})
        self.assertEqual(content_type, 'application/json')

    def test_mock_analytics_uses_real_monthly_calendar_path(self):
        with mock.patch.object(local_server, 'sql_enabled', return_value=False):
            status, body, headers, content_type = local_server._handle_mock_request(
                None,
                'GET',
                '/analytics/progression/monthly-calendar',
                {},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {'items': [], 'source': 'local_mock'})
        self.assertIsNone(headers)
        self.assertEqual(content_type, 'application/json')

    def test_mock_pos_categories_returns_local_shape(self):
        with mock.patch.object(local_server, 'sql_enabled', return_value=False), \
             mock.patch.object(local_server, '_mock_pos_categories', return_value=[{'key': 'noun', 'label': 'Nouns', 'uniqueWordCount': 1, 'words': ['ball']}]):
            status, body, headers, content_type = local_server._handle_mock_request(
                None,
                'GET',
                '/analytics/progression/pos-categories',
                {'userId': ['a1'], 'unit': ['day']},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body['userId'], 'a1')
        self.assertEqual(body['categories'][0]['label'], 'Nouns')
        self.assertIsNone(headers)
        self.assertEqual(content_type, 'application/json')

    def test_mock_semantic_network_returns_local_shape(self):
        with mock.patch.object(local_server, 'sql_enabled', return_value=False), \
             mock.patch.object(local_server, '_mock_semantic_network', return_value={'nodes': [{'id': 'dog'}], 'edges': [], 'meta': {'message': 'ok'}}):
            status, body, headers, content_type = local_server._handle_mock_request(
                None,
                'GET',
                '/analytics/semantic/network',
                {'userId': ['a1']},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body['nodes'][0]['id'], 'dog')
        self.assertIsNone(headers)
        self.assertEqual(content_type, 'application/json')

    def test_cloud_health_does_not_require_lambda_relay(self):
        status, body, headers, content_type = local_server._handle_cloud_request(
            'GET',
            '/__local__/health',
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body['mode'], local_server.LOCAL_API_MODE)
        self.assertTrue(body['ok'])
        self.assertIsNone(headers)
        self.assertEqual(content_type, 'application/json')


if __name__ == '__main__':
    unittest.main()
