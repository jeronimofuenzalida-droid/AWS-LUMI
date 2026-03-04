import os
import pathlib
import tempfile
import sys
import unittest
from unittest import mock


os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-1')
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

import sql_store


class SqlStorePercentileTests(unittest.TestCase):
    def test_sql_enabled_accepts_local_postgres(self):
        with mock.patch.dict(sql_store.os.environ, {'LOCAL_POSTGRES_URL': 'postgresql://local'}, clear=False):
            self.assertTrue(sql_store.sql_enabled())

    def test_local_sql_and_params_convert_named_placeholders(self):
        sql, params = sql_store._local_sql_and_params(
            'SELECT * FROM users WHERE external_user_id = :u AND kid_age_months >= :m',
            [sql_store._param('u', 'a1'), sql_store._param('m', 24)],
        )
        self.assertEqual(
            sql,
            'SELECT * FROM users WHERE external_user_id = %(u)s AND kid_age_months >= %(m)s',
        )
        self.assertEqual(params, {'u': 'a1', 'm': 24})

    def test_smoothed_percentile_uses_edge_months_only(self):
        captured = {}

        def fake_query_rows(sql, params=None):
            captured['sql'] = sql
            return [{'numerator': 10, 'denominator': 20}]

        with mock.patch.object(sql_store, 'sql_enabled', return_value=True), \
             mock.patch.object(sql_store, 'get_kid_benchmark_age_range', return_value={'minAgeMonths': 8, 'maxAgeMonths': 30}), \
             mock.patch.object(sql_store, '_query_rows', side_effect=fake_query_rows):
            result = sql_store.compute_kid_percentile_smoothed(8, 5)

        self.assertIn('IN (8,9)', captured['sql'])
        self.assertEqual(result['percentile'], 50.0)
        self.assertEqual(result['benchmarkMonthsUsed'], [8, 9])
        self.assertEqual(result['benchmarkAgeRangeMonths'], {'min': 8, 'max': 9})

    def test_smoothed_percentile_uses_three_month_window_in_middle(self):
        captured = {}

        def fake_query_rows(sql, params=None):
            captured['sql'] = sql
            return [{'numerator': 30, 'denominator': 40}]

        with mock.patch.object(sql_store, 'sql_enabled', return_value=True), \
             mock.patch.object(sql_store, 'get_kid_benchmark_age_range', return_value={'minAgeMonths': 8, 'maxAgeMonths': 30}), \
             mock.patch.object(sql_store, '_query_rows', side_effect=fake_query_rows):
            result = sql_store.compute_kid_percentile_smoothed(24, 37)

        self.assertIn('IN (23,24,25)', captured['sql'])
        self.assertEqual(result['percentile'], 75.0)
        self.assertEqual(result['benchmarkMonthsUsed'], [23, 24, 25])
        self.assertEqual(result['benchmarkAgeRangeMonths'], {'min': 23, 'max': 25})

    def test_speaker_scope_clause_accepts_parent_aliases(self):
        self.assertEqual(sql_store._speaker_scope_clause('parent1'), "speaker_name = 'Parent 1'")
        self.assertEqual(sql_store._speaker_scope_clause('client2'), "speaker_name = 'Parent 2'")
        self.assertIn('Parent 1', sql_store._speaker_scope_clause('all'))

    def test_query_pos_categories_groups_by_category_and_word(self):
        captured = {}

        def fake_query_rows(sql, params=None):
            captured['sql'] = sql
            captured['params'] = params
            return [{'category_key': 'animals', 'normalized_word': 'ball'}]

        with mock.patch.object(sql_store, '_query_rows', side_effect=fake_query_rows):
            rows = sql_store.query_pos_categories('a1', '2026-03-01', '2026-03-03', 'parent1')

        self.assertEqual(rows, [{'category_key': 'animals', 'normalized_word': 'ball'}])
        self.assertIn('FROM word_occurrences', captured['sql'])
        self.assertIn("speaker_name = 'Parent 1'", captured['sql'])
        self.assertEqual(sql_store._param_value(captured['params'][0]), 'a1')
        self.assertEqual(sql_store.pos_category_label('article_determiner'), 'Article Determiner')

    def test_normalize_pos_category_key_rolls_legacy_keys_into_new_buckets(self):
        self.assertEqual(sql_store.normalize_pos_category_key('article_determiner', 'the'), 'function_word')
        self.assertEqual(sql_store.normalize_pos_category_key('adverb', 'quickly'), 'adjective')
        self.assertEqual(sql_store.normalize_pos_category_key('noun', 'mommy'), 'social_word')
        self.assertEqual(sql_store.normalize_pos_category_key('other', 'wow'), 'interjection')

    def test_query_semantic_edges_uses_in_list_and_threshold(self):
        captured = {}

        def fake_query_rows(sql, params=None):
            captured['sql'] = sql
            captured['params'] = params
            return []

        with mock.patch.object(sql_store, '_query_rows', side_effect=fake_query_rows):
            sql_store.query_semantic_edges(['dog', 'cat', 'dog'], 0.5)

        self.assertIn('FROM wordbank_w2v_assocs', captured['sql'])
        self.assertIn('cosine >= :c', captured['sql'])
        self.assertIn('word_a IN', captured['sql'])
        self.assertEqual(sql_store._param_value(captured['params'][0]), 0.5)

    def test_query_wg_categories_normalizes_na_to_other(self):
        with mock.patch.object(sql_store, '_query_rows', return_value=[{'category_key': 'NA', 'normalized_word': 'foo'}]):
            rows = sql_store.query_wg_categories('a1', '2026-01-01', '2026-01-02', 'kid')
        self.assertEqual(rows, [{'category_key': 'other', 'normalized_word': 'foo'}])

    def test_load_aoa_rows_maps_fields(self):
        csv_text = "definition,aoa,category,lexical_category,lexical_class,uni_lemma\nDog,20.1,animals,nouns,nouns,dog\n"
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'a.csv'
            p.write_text(csv_text, encoding='utf-8')
            rows = sql_store._load_aoa_rows(p, 'category')
        self.assertEqual(rows[0]['word'], 'dog')
        self.assertEqual(rows[0]['category'], 'animals')
        self.assertEqual(rows[0]['lexical_class'], 'nouns')
        self.assertEqual(rows[0]['display_label'], 'Dog')

    def test_load_w2v_edges_canonicalizes_and_thresholds(self):
        csv_text = "in_node,dog,cat\ncat,0.2,0\nDog,0,0.2\n"
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'w.csv'
            p.write_text(csv_text, encoding='utf-8')
            out = sql_store._load_w2v_edges(p, 0.125)
        self.assertIn(('cat', 'dog'), out['edges'])
        self.assertGreaterEqual(out['edges'][('cat', 'dog')], 0.2)

if __name__ == '__main__':
    unittest.main()
