import re

POS_TAGGING_MODEL = 'spacy/en_core_web_sm'
POS_TAGGING_VERSION = 'v2'

_NLP = None

_CATEGORY_LABELS = {
    'noun': 'Nouns',
    'verb': 'Verbs',
    'adjective': 'Adjectives',
    'function_word': 'Function words',
    'pronoun': 'Pronouns',
    'social_word': 'Social words',
    'interjection': 'Interjections',
}

_SOCIAL_WORD_LEMMAS = {
    'baby',
    'boy',
    'brother',
    'dad',
    'dada',
    'daddy',
    'doctor',
    'family',
    'friend',
    'girl',
    'grandma',
    'grandpa',
    'mama',
    'mom',
    'momma',
    'mommy',
    'mum',
    'nana',
    'papa',
    'person',
    'sister',
    'teacher',
    'teddy',
}

_INTERJECTION_LEMMAS = {
    'ah',
    'aww',
    'bye',
    'goodbye',
    'hello',
    'hey',
    'hi',
    'hmm',
    'oh',
    'oops',
    'ouch',
    'uh',
    'um',
    'wow',
    'yay',
    'yes',
}


def supported_language(language_code):
    raw = str(language_code or '').strip().lower()
    return raw in {'', 'en', 'eng', 'en-us', 'en-gb'}


def canonical_category_key(category_key, normalized_word=''):
    raw = str(category_key or '').strip().lower()
    word = normalize_word(normalized_word, normalized_word)
    if word in _SOCIAL_WORD_LEMMAS:
        return 'social_word'
    if raw == 'interjection' or word in _INTERJECTION_LEMMAS:
        return 'interjection'
    if raw in {'noun'}:
        return 'noun'
    if raw in {'verb'}:
        return 'verb'
    if raw in {'adjective', 'adverb'}:
        return 'adjective'
    if raw in {'pronoun'}:
        return 'pronoun'
    if raw in {'social_word'}:
        return 'social_word'
    return 'function_word'


def map_pos_to_category(pos, lemma='', surface=''):
    raw = str(pos or '').strip().upper()
    normalized = normalize_word(surface, lemma)
    if normalized in _SOCIAL_WORD_LEMMAS:
        return 'social_word'
    if raw == 'INTJ' or normalized in _INTERJECTION_LEMMAS:
        return 'interjection'
    if raw in {'NOUN', 'PROPN'}:
        return 'noun'
    if raw in {'VERB', 'AUX'}:
        return 'verb'
    if raw in {'ADJ', 'ADV'}:
        return 'adjective'
    if raw == 'PRON':
        return 'pronoun'
    return 'function_word'


def category_label(category_key):
    return _CATEGORY_LABELS.get(canonical_category_key(category_key), 'Function words')


def _load_nlp():
    global _NLP
    if _NLP is None:
        import spacy

        _NLP = spacy.load('en_core_web_sm')
    return _NLP


def normalize_word(surface, lemma):
    chosen = str(lemma or '').strip().lower() or str(surface or '').strip().lower()
    return re.sub(r'\s+', ' ', chosen).strip()


def _annotate_text(text):
    nlp = _load_nlp()
    doc = nlp(text or '')
    tokens = []
    for token in doc:
        if not token.is_alpha:
            continue
        normalized = normalize_word(token.text, token.lemma_)
        if not normalized:
            continue
        tokens.append(
            {
                'text': token.text,
                'lemma': normalized,
                'pos': token.pos_,
                'tag': token.tag_,
                'categoryKey': map_pos_to_category(token.pos_, normalized, token.text),
                'charStart': int(token.idx),
                'charEnd': int(token.idx + len(token.text)),
            }
        )
    return tokens


def annotate_segments_with_pos(segments, language_code):
    normalized_lang = str(language_code or '').strip().lower()
    if normalized_lang and not supported_language(normalized_lang):
        return segments, {
            'posTaggingStatus': 'SKIPPED_UNSUPPORTED_LANGUAGE',
            'posTaggingModel': POS_TAGGING_MODEL,
            'posTaggingVersion': POS_TAGGING_VERSION,
        }

    assumed_english = normalized_lang == ''
    annotated = []
    token_index = 0
    for seg in segments or []:
        copy = dict(seg)
        tokens = []
        for tok in _annotate_text(copy.get('text') or ''):
            entry = dict(tok)
            entry['index'] = token_index
            token_index += 1
            tokens.append(entry)
        copy['tokens'] = tokens
        annotated.append(copy)

    return annotated, {
        'posTaggingStatus': 'COMPLETED_ASSUMED_ENGLISH' if assumed_english else 'COMPLETED',
        'posTaggingModel': POS_TAGGING_MODEL,
        'posTaggingVersion': POS_TAGGING_VERSION,
    }
