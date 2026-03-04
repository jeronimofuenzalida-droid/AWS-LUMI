import importlib
import os
import pathlib
import sys
import unittest
from unittest import mock
from types import ModuleType


REPO_SRC = pathlib.Path(__file__).resolve().parents[2] / 'src'
sys.path.insert(0, str(REPO_SRC))


class AppPosCategoriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {
            'AWS_DEFAULT_REGION': 'us-west-1',
            'AWS_EC2_METADATA_DISABLED': 'true',
            'TRANSCRIPTS_TABLE': 'unit-transcripts',
            'UPLOADS_BUCKET': 'unit-uploads',
            'ARTIFACTS_BUCKET': 'unit-artifacts',
        }
        cls.env_patcher = mock.patch.dict(os.environ, env, clear=False)
        cls.env_patcher.start()

        fake_table = mock.Mock()
        fake_ddb = mock.Mock()
        fake_ddb.Table.return_value = fake_table
        fake_boto3 = ModuleType('boto3')
        fake_boto3.resource = mock.Mock(return_value=fake_ddb)
        fake_boto3.client = mock.Mock(return_value=mock.Mock())
        fake_boto3_dynamodb = ModuleType('boto3.dynamodb')
        fake_boto3_conditions = ModuleType('boto3.dynamodb.conditions')
        fake_boto3_conditions.Key = mock.Mock()
        cls.boto3_patcher = mock.patch.dict(
            sys.modules,
            {
                'boto3': fake_boto3,
                'boto3.dynamodb': fake_boto3_dynamodb,
                'boto3.dynamodb.conditions': fake_boto3_conditions,
            },
        )
        cls.boto3_patcher.start()

        sys.path[:] = [p for p in sys.path if pathlib.Path(p).resolve() != (pathlib.Path(__file__).resolve().parents[2] / 'asr_worker').resolve()]
        sys.path.insert(0, str(REPO_SRC))

        import app

        cls.app = importlib.reload(app)

    @classmethod
    def tearDownClass(cls):
        cls.boto3_patcher.stop()
        cls.env_patcher.stop()

    def test_handle_progression_pos_categories_day(self):
        event = {
            'queryStringParameters': {
                'userId': 'a1',
                'unit': 'day',
                'from': '2026-03-01',
                'to': '2026-03-03',
                'speaker': 'parent1',
                'tz': 'America/Tijuana',
                'asOfDate': '2026-03-03',
            }
        }
        with mock.patch.object(self.app, '_sql_required', return_value=None), \
             mock.patch.object(self.app, 'ensure_wordbank_seeded', return_value=None), \
             mock.patch.object(self.app, 'query_pos_categories', return_value=[{'category_key': 'noun', 'normalized_word': 'ball'}, {'category_key': 'noun', 'normalized_word': 'dog'}]):
            resp = self.app.handle_progression_pos_categories(event)

        self.assertEqual(resp['statusCode'], 200)
        body = self.app.json.loads(resp['body'])
        self.assertEqual(body['speaker'], 'client1')
        self.assertEqual(body['categories'][0]['label'], 'Nouns')
        self.assertEqual(body['categories'][0]['words'], ['ball', 'dog'])

    def test_handle_progression_pos_categories_uses_wg_category_keys(self):
        event = {
            'queryStringParameters': {
                'userId': 'a1',
                'unit': 'day',
                'from': '2026-03-01',
                'to': '2026-03-03',
                'speaker': 'kid',
            }
        }
        rows = [
            {'category_key': 'animals', 'normalized_word': 'dog'},
            {'category_key': 'animals', 'normalized_word': 'cat'},
            {'category_key': 'other', 'normalized_word': 'zzz'},
        ]
        with mock.patch.object(self.app, '_sql_required', return_value=None), \
             mock.patch.object(self.app, 'ensure_wordbank_seeded', return_value=None), \
             mock.patch.object(self.app, 'query_pos_categories', return_value=rows):
            resp = self.app.handle_progression_pos_categories(event)

        body = self.app.json.loads(resp['body'])
        labels = {item['key']: item['label'] for item in body['categories']}
        words = {item['key']: item['words'] for item in body['categories']}
        self.assertEqual(labels['animals'], 'Animals')
        self.assertEqual(words['animals'], ['cat', 'dog'])
        self.assertEqual(labels['other'], 'Other')
        self.assertEqual(words['other'], ['zzz'])

    def test_handle_progression_pos_categories_month_empty(self):
        event = {
            'queryStringParameters': {
                'userId': 'a1',
                'unit': 'month',
                'fromMonth': '2026-01',
                'toMonth': '2026-03',
                'speaker': 'kid',
            }
        }
        with mock.patch.object(self.app, '_sql_required', return_value=None), \
             mock.patch.object(self.app, 'ensure_wordbank_seeded', return_value=None), \
             mock.patch.object(self.app, 'query_pos_categories', return_value=[]):
            resp = self.app.handle_progression_pos_categories(event)

        self.assertEqual(resp['statusCode'], 200)
        body = self.app.json.loads(resp['body'])
        self.assertEqual(body['categories'], [])
        self.assertEqual(body['fromMonth'], '2026-01')
        self.assertEqual(body['toMonth'], '2026-03')

    def test_handle_semantic_network_missing_age_returns_empty(self):
        event = {
            'queryStringParameters': {
                'userId': 'a1',
                'unit': 'day',
                'from': '2026-03-01',
                'to': '2026-03-03',
            }
        }
        with mock.patch.object(self.app, '_sql_required', return_value=None), \
             mock.patch.object(self.app, 'ensure_wordbank_seeded', return_value=None), \
             mock.patch.object(self.app, 'get_user_profile', return_value={'kidAgeMonths': None}):
            resp = self.app.handle_semantic_network(event)
        self.assertEqual(resp['statusCode'], 200)
        body = self.app.json.loads(resp['body'])
        self.assertEqual(body['nodes'], [])
        self.assertEqual(body['edges'], [])
        self.assertIn('Set kid age', body['meta']['message'])

    def test_handle_semantic_network_returns_nodes_edges(self):
        event = {
            'queryStringParameters': {
                'userId': 'a1',
                'unit': 'day',
                'from': '2026-03-01',
                'to': '2026-03-03',
                'minCosine': '0.55',
            }
        }
        eligible = [
            {'word': 'dog', 'aoa_months': 20, 'cdi_category': 'animals', 'lexical_class': 'nouns', 'display_label': 'dog'},
            {'word': 'cat', 'aoa_months': 18, 'cdi_category': 'animals', 'lexical_class': 'nouns', 'display_label': 'cat'},
        ]
        transcript = [{'normalized_word': 'dog'}, {'normalized_word': 'cat'}]
        edges = [{'word_a': 'cat', 'word_b': 'dog', 'cosine': 0.55}]
        with mock.patch.object(self.app, '_sql_required', return_value=None), \
             mock.patch.object(self.app, 'ensure_wordbank_seeded', return_value=None), \
             mock.patch.object(self.app, 'get_user_profile', return_value={'kidAgeMonths': 24}), \
             mock.patch.object(self.app, 'query_semantic_eligible_words', return_value=eligible), \
             mock.patch.object(self.app, 'query_transcript_words', return_value=transcript), \
             mock.patch.object(self.app, 'query_semantic_edges', return_value=edges):
            resp = self.app.handle_semantic_network(event)
        self.assertEqual(resp['statusCode'], 200)
        body = self.app.json.loads(resp['body'])
        self.assertEqual(len(body['nodes']), 2)
        self.assertEqual(len(body['edges']), 1)
        self.assertEqual(body['meta']['minCosine'], 0.55)


if __name__ == '__main__':
    unittest.main()
