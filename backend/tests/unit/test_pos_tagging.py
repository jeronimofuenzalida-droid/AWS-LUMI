import pathlib
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'asr_worker'))

import pos_tagging


class _FakeToken:
    def __init__(self, text, lemma, pos, tag, idx, is_alpha=True):
        self.text = text
        self.lemma_ = lemma
        self.pos_ = pos
        self.tag_ = tag
        self.idx = idx
        self.is_alpha = is_alpha


class PosTaggingTests(unittest.TestCase):
    def test_supported_language(self):
        self.assertTrue(pos_tagging.supported_language('en'))
        self.assertTrue(pos_tagging.supported_language(''))
        self.assertFalse(pos_tagging.supported_language('es'))

    def test_map_pos_to_category(self):
        self.assertEqual(pos_tagging.map_pos_to_category('NOUN'), 'noun')
        self.assertEqual(pos_tagging.map_pos_to_category('AUX'), 'verb')
        self.assertEqual(pos_tagging.map_pos_to_category('DET'), 'function_word')
        self.assertEqual(pos_tagging.map_pos_to_category('ADV'), 'adjective')

    def test_map_pos_to_category_prioritizes_social_and_interjection_words(self):
        self.assertEqual(pos_tagging.map_pos_to_category('NOUN', 'mommy', 'Mommy'), 'social_word')
        self.assertEqual(pos_tagging.map_pos_to_category('INTJ', 'wow', 'Wow'), 'interjection')

    def test_annotate_segments_with_pos_uses_lemma_and_skips_punctuation(self):
        fake_doc = [
            _FakeToken('Dogs', 'dog', 'NOUN', 'NNS', 0),
            _FakeToken(',', ',', 'PUNCT', ',', 4, is_alpha=False),
            _FakeToken('running', 'run', 'VERB', 'VBG', 6),
        ]

        with mock.patch.object(pos_tagging, '_load_nlp', return_value=lambda text: fake_doc):
            segments, meta = pos_tagging.annotate_segments_with_pos([{'text': 'Dogs, running'}], 'en')

        self.assertEqual(meta['posTaggingStatus'], 'COMPLETED')
        tokens = segments[0]['tokens']
        self.assertEqual([t['lemma'] for t in tokens], ['dog', 'run'])
        self.assertEqual([t['categoryKey'] for t in tokens], ['noun', 'verb'])
        self.assertEqual([t['index'] for t in tokens], [0, 1])


if __name__ == '__main__':
    unittest.main()
