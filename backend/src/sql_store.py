import csv
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

rds_data = boto3.client('rds-data')
_LOCAL_TX = {}
_LOCAL_TX_LOCK = threading.Lock()
_WORDBANK_SEED_LOCK = threading.Lock()
_WORDBANK_SEEDED = False
_WORD_BANK_MIN_COSINE_STORED = 0.125

_ROLE_CANON = {
    'kid': 'Kid',
    'client1': 'Parent 1',
    'client2': 'Parent 2',
    'parent1': 'Parent 1',
    'parent2': 'Parent 2',
}

_CATEGORY_LABELS = {
    'noun': 'Nouns',
    'verb': 'Verbs',
    'adjective': 'Adjectives',
    'function_word': 'Function words',
    'pronoun': 'Pronouns',
    'social_word': 'Social words',
    'interjection': 'Interjections',
    'other': 'Other',
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


def sql_enabled():
    return bool(
        (os.environ.get('SQL_CLUSTER_ARN') and os.environ.get('SQL_SECRET_ARN') and os.environ.get('SQL_DATABASE'))
        or os.environ.get('LOCAL_POSTGRES_URL')
    )


def _using_local_postgres():
    return bool(os.environ.get('LOCAL_POSTGRES_URL'))


def _cfg():
    return {
        'resourceArn': os.environ.get('SQL_CLUSTER_ARN'),
        'secretArn': os.environ.get('SQL_SECRET_ARN'),
        'database': os.environ.get('SQL_DATABASE'),
        'localPostgresUrl': os.environ.get('LOCAL_POSTGRES_URL'),
    }


def _param(name, value):
    if value is None:
        return {'name': name, 'value': {'isNull': True}}
    if isinstance(value, bool):
        return {'name': name, 'value': {'booleanValue': value}}
    if isinstance(value, int):
        return {'name': name, 'value': {'longValue': value}}
    if isinstance(value, float):
        return {'name': name, 'value': {'doubleValue': value}}
    return {'name': name, 'value': {'stringValue': str(value)}}


def _param_value(param):
    value = (param or {}).get('value') or {}
    if value.get('isNull'):
        return None
    for key in ('booleanValue', 'longValue', 'doubleValue', 'stringValue'):
        if key in value:
            return value[key]
    return None


def _local_sql_and_params(sql, params=None):
    param_map = {p['name']: _param_value(p) for p in (params or [])}
    adapted_sql = re.sub(r':([A-Za-z_][A-Za-z0-9_]*)', r'%(\1)s', sql)
    return adapted_sql, param_map


def _local_connect(autocommit=True):
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - depends on local dev env
        raise RuntimeError('Local SQL requires psycopg. Install backend/requirements-local.txt') from exc
    return psycopg.connect(_cfg()['localPostgresUrl'], autocommit=autocommit)


def _local_exec(sql, params=None, transaction_id=None, include_result_metadata=True):
    adapted_sql, param_map = _local_sql_and_params(sql, params)
    managed = False
    if transaction_id:
        conn = _LOCAL_TX.get(transaction_id)
        if conn is None:
            raise RuntimeError(f'Unknown local SQL transaction: {transaction_id}')
    else:
        conn = _local_connect(autocommit=True)
        managed = True

    try:
        with conn.cursor() as cur:
            cur.execute(adapted_sql, param_map)
            resp = {}
            if cur.description and include_result_metadata is not False:
                resp['columnMetadata'] = [{'name': d.name} for d in cur.description]
                resp['records'] = []
                for row in cur.fetchall():
                    record = []
                    for value in row:
                        if value is None:
                            record.append({'isNull': True})
                        elif isinstance(value, bool):
                            record.append({'booleanValue': value})
                        elif isinstance(value, int):
                            record.append({'longValue': value})
                        elif isinstance(value, float):
                            record.append({'doubleValue': value})
                        else:
                            record.append({'stringValue': str(value)})
                    resp['records'].append(record)
            return resp
    finally:
        if managed:
            conn.close()


def _local_batch_exec(sql, parameter_sets, transaction_id=None, chunk_size=100):
    if not parameter_sets:
        return
    adapted_sql, _ = _local_sql_and_params(sql, parameter_sets[0])
    managed = False
    if transaction_id:
        conn = _LOCAL_TX.get(transaction_id)
        if conn is None:
            raise RuntimeError(f'Unknown local SQL transaction: {transaction_id}')
    else:
        conn = _local_connect(autocommit=True)
        managed = True

    try:
        with conn.cursor() as cur:
            for i in range(0, len(parameter_sets), chunk_size):
                batch = [_local_sql_and_params(sql, pset)[1] for pset in parameter_sets[i:i + chunk_size]]
                cur.executemany(adapted_sql, batch)
    finally:
        if managed:
            conn.close()


def _exec(sql, params=None, transaction_id=None, include_result_metadata=True):
    if _using_local_postgres():
        return _local_exec(sql, params=params, transaction_id=transaction_id, include_result_metadata=include_result_metadata)
    cfg = _cfg()
    kwargs = dict(
        resourceArn=cfg['resourceArn'],
        secretArn=cfg['secretArn'],
        database=cfg['database'],
        sql=sql,
        parameters=params or [],
    )
    if transaction_id:
        kwargs['transactionId'] = transaction_id
    if include_result_metadata is not None:
        kwargs['includeResultMetadata'] = include_result_metadata
    return rds_data.execute_statement(**kwargs)


def _begin_tx():
    if _using_local_postgres():
        txid = str(uuid.uuid4())
        conn = _local_connect(autocommit=False)
        with _LOCAL_TX_LOCK:
            _LOCAL_TX[txid] = conn
        return txid
    cfg = _cfg()
    tx = rds_data.begin_transaction(
        resourceArn=cfg['resourceArn'],
        secretArn=cfg['secretArn'],
        database=cfg['database'],
    )
    return tx['transactionId']


def _commit_tx(transaction_id):
    if _using_local_postgres():
        with _LOCAL_TX_LOCK:
            conn = _LOCAL_TX.pop(transaction_id, None)
        if conn is None:
            raise RuntimeError(f'Unknown local SQL transaction: {transaction_id}')
        try:
            conn.commit()
        finally:
            conn.close()
        return {'transactionStatus': 'committed'}
    cfg = _cfg()
    return rds_data.commit_transaction(
        resourceArn=cfg['resourceArn'],
        secretArn=cfg['secretArn'],
        transactionId=transaction_id,
    )


def _rollback_tx(transaction_id):
    if _using_local_postgres():
        with _LOCAL_TX_LOCK:
            conn = _LOCAL_TX.pop(transaction_id, None)
        if conn is None:
            return {'transactionStatus': 'missing'}
        try:
            conn.rollback()
        finally:
            conn.close()
        return {'transactionStatus': 'rolled_back'}
    cfg = _cfg()
    return rds_data.rollback_transaction(
        resourceArn=cfg['resourceArn'],
        secretArn=cfg['secretArn'],
        transactionId=transaction_id,
    )


def _batch_exec(sql, parameter_sets, transaction_id=None, chunk_size=100):
    if not parameter_sets:
        return
    if _using_local_postgres():
        return _local_batch_exec(sql, parameter_sets, transaction_id=transaction_id, chunk_size=chunk_size)
    cfg = _cfg()
    for i in range(0, len(parameter_sets), chunk_size):
        kwargs = dict(
            resourceArn=cfg['resourceArn'],
            secretArn=cfg['secretArn'],
            database=cfg['database'],
            sql=sql,
            parameterSets=parameter_sets[i:i + chunk_size],
        )
        if transaction_id:
            kwargs['transactionId'] = transaction_id
        rds_data.batch_execute_statement(**kwargs)


def _tx(statements):
    cfg = _cfg()
    tx = rds_data.begin_transaction(
        resourceArn=cfg['resourceArn'],
        secretArn=cfg['secretArn'],
        database=cfg['database'],
    )
    txid = tx['transactionId']
    try:
        for st in statements:
            rds_data.execute_statement(
                resourceArn=cfg['resourceArn'],
                secretArn=cfg['secretArn'],
                database=cfg['database'],
                sql=st['sql'],
                parameters=st.get('params', []),
                transactionId=txid,
                includeResultMetadata=True,
            )
        rds_data.commit_transaction(
            resourceArn=cfg['resourceArn'],
            secretArn=cfg['secretArn'],
            transactionId=txid,
        )
    except Exception:
        rds_data.rollback_transaction(
            resourceArn=cfg['resourceArn'],
            secretArn=cfg['secretArn'],
            transactionId=txid,
        )
        raise


def _record_to_dict(meta, record):
    out = {}
    for i, m in enumerate(meta):
        k = m.get('name')
        v = record[i] if i < len(record) else {}
        if 'stringValue' in v:
            out[k] = v['stringValue']
        elif 'longValue' in v:
            out[k] = v['longValue']
        elif 'doubleValue' in v:
            out[k] = v['doubleValue']
        elif 'booleanValue' in v:
            out[k] = v['booleanValue']
        elif v.get('isNull'):
            out[k] = None
        else:
            out[k] = None
    return out


def _query_rows(sql, params=None):
    resp = _exec(sql, params)
    meta = resp.get('columnMetadata', [])
    recs = resp.get('records', [])
    return [_record_to_dict(meta, r) for r in recs]


def _normalize_word(token):
    return token.lower()


def _tokens(text):
    return [_normalize_word(t) for t in re.findall(r"\b[\w']+\b", (text or '').lower())]


def _to_iso8601(v):
    if not v:
        return datetime.now(timezone.utc).isoformat()
    s = str(v)
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _resolve_wordbank_file(filename):
    candidates = [
        _repo_root() / 'wordbank' / filename,
        _repo_root() / 'data' / 'wordbank' / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f'Wordbank file not found: {filename}')


def _normalize_wordbank_word(value):
    return _normalize_word(str(value or '').strip())


def _normalize_wg_category(value):
    raw = str(value or '').strip().lower()
    if not raw or raw == 'na':
        return 'other'
    return raw


def _load_aoa_rows(csv_path, category_column_name):
    rows = []
    with csv_path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            word = _normalize_wordbank_word(row.get('uni_lemma') or row.get('definition'))
            if not word:
                continue
            aoa_raw = row.get('aoa')
            try:
                aoa_months = float(aoa_raw) if aoa_raw not in (None, '') else None
            except Exception:
                aoa_months = None
            rows.append(
                {
                    'word': word,
                    'aoa_months': aoa_months,
                    'category': _normalize_wg_category(row.get(category_column_name)),
                    'lexical_class': _normalize_wg_category(row.get('lexical_class')),
                    'display_label': str(row.get('definition') or '').strip() or word,
                }
            )
    return rows


def _load_w2v_edges(csv_path, min_cosine):
    edges = {}
    scanned = 0
    kept = 0
    with csv_path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        header = next(reader, None) or []
        col_words = [_normalize_wordbank_word(v) for v in header[1:]]
        for row in reader:
            if not row:
                continue
            row_word = _normalize_wordbank_word(row[0])
            if not row_word:
                continue
            for idx, col_word in enumerate(col_words, start=1):
                if idx >= len(row):
                    continue
                scanned += 1
                if not col_word or col_word == row_word:
                    continue
                raw = str(row[idx]).strip()
                if not raw:
                    continue
                try:
                    cosine = float(raw)
                except Exception:
                    continue
                if cosine < min_cosine:
                    continue
                a = row_word if row_word < col_word else col_word
                b = col_word if row_word < col_word else row_word
                key = (a, b)
                prev = edges.get(key)
                if prev is None or cosine > prev:
                    edges[key] = cosine
                kept += 1
    return {'edges': edges, 'scanned': scanned, 'kept': kept}


def _speaker_scope_name(value):
    raw = str(value or 'all').strip().lower()
    if raw == 'all':
        return 'all'
    return _ROLE_CANON.get(raw, 'all')


def normalize_pos_category_key(category_key, normalized_word=''):
    raw = str(category_key or '').strip().lower()
    word = _normalize_word(normalized_word)
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


def pos_category_label(category_key):
    raw = str(category_key or '').strip().lower()
    if not raw:
        raw = 'other'
    if raw in _CATEGORY_LABELS:
        return _CATEGORY_LABELS[raw]
    return raw.replace('_', ' ').title()


def ensure_schema():
    if not sql_enabled():
        return

    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            external_user_id TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            kid_age_months INTEGER
        )
        """,
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS kid_age_months INTEGER",
        """
        CREATE TABLE IF NOT EXISTS kid_unique_words_monthly_benchmark (
            age_months INTEGER NOT NULL,
            unique_word_count INTEGER NOT NULL,
            kid_count INTEGER NOT NULL,
            source_file TEXT NOT NULL DEFAULT 'unique_words_per month.csv',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (age_months, unique_word_count),
            CHECK (age_months >= 0),
            CHECK (unique_word_count >= 0),
            CHECK (kid_count >= 0)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kid_benchmark_age ON kid_unique_words_monthly_benchmark(age_months)",
        "CREATE INDEX IF NOT EXISTS idx_kid_benchmark_age_words ON kid_unique_words_monthly_benchmark(age_months, unique_word_count)",
        """
        CREATE TABLE IF NOT EXISTS wordbank_aoa_ws_prod (
            word TEXT PRIMARY KEY,
            aoa_months DOUBLE PRECISION NOT NULL,
            cdi_category TEXT,
            lexical_class TEXT,
            display_label TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wordbank_aoa_wg_comp (
            word TEXT PRIMARY KEY,
            aoa_months DOUBLE PRECISION,
            category TEXT,
            lexical_class TEXT,
            display_label TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS wordbank_w2v_assocs (
            word_a TEXT NOT NULL,
            word_b TEXT NOT NULL,
            cosine DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(word_a, word_b),
            CHECK (word_a < word_b)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_wordbank_w2v_word_a ON wordbank_w2v_assocs(word_a)",
        "CREATE INDEX IF NOT EXISTS idx_wordbank_w2v_word_b ON wordbank_w2v_assocs(word_b)",
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id UUID PRIMARY KEY,
            external_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            logical_day DATE,
            audio_s3_key TEXT,
            transcript_json_s3_key TEXT,
            num_speakers INTEGER,
            full_text TEXT,
            speaker_stats JSONB,
            raw_item JSONB
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS interactions (
            id BIGSERIAL PRIMARY KEY,
            transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            interaction_index INTEGER NOT NULL,
            external_user_id TEXT NOT NULL,
            speaker_label TEXT NOT NULL,
            speaker_name TEXT NOT NULL,
            start_time NUMERIC NOT NULL,
            end_time NUMERIC NOT NULL,
            duration_s NUMERIC NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            logical_day DATE,
            UNIQUE(transcript_id, interaction_index)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS interaction_metrics (
            interaction_id BIGINT PRIMARY KEY REFERENCES interactions(id) ON DELETE CASCADE,
            response_latency_s NUMERIC,
            previous_speaker_name TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS word_occurrences (
            id BIGSERIAL PRIMARY KEY,
            transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            interaction_id BIGINT NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
            external_user_id TEXT NOT NULL,
            day DATE NOT NULL,
            speaker_name TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS word_annotations (
            id BIGSERIAL PRIMARY KEY,
            transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            interaction_id BIGINT NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
            external_user_id TEXT NOT NULL,
            day DATE NOT NULL,
            speaker_name TEXT NOT NULL,
            token_index INTEGER NOT NULL,
            surface TEXT NOT NULL,
            lemma TEXT,
            normalized_word TEXT NOT NULL,
            pos TEXT NOT NULL,
            tag TEXT,
            category_key TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(interaction_id, token_index)
        )
        """,
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS external_user_id TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS day DATE",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS speaker_name TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS token_index INTEGER",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS surface TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS lemma TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS normalized_word TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS pos TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS tag TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS category_key TEXT",
        "ALTER TABLE word_annotations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
        "ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS logical_day DATE",
        "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS logical_day DATE",
        "CREATE INDEX IF NOT EXISTS idx_transcripts_user_logical_day ON transcripts(external_user_id, logical_day)",
        "CREATE INDEX IF NOT EXISTS idx_interactions_user_day ON interactions(external_user_id, logical_day, speaker_name)",
        "CREATE INDEX IF NOT EXISTS idx_word_occ_user_day ON word_occurrences(external_user_id, day, speaker_name, token)",
        "CREATE INDEX IF NOT EXISTS idx_word_occ_transcript ON word_occurrences(transcript_id)",
        "CREATE INDEX IF NOT EXISTS idx_word_annotations_user_day_category ON word_annotations(external_user_id, day, speaker_name, category_key)",
        "CREATE INDEX IF NOT EXISTS idx_word_annotations_user_day_word ON word_annotations(external_user_id, day, speaker_name, normalized_word)",
        "CREATE INDEX IF NOT EXISTS idx_word_annotations_transcript_interaction ON word_annotations(transcript_id, interaction_id, token_index)",
        """
        CREATE OR REPLACE VIEW v_daily_metrics AS
        WITH words AS (
            SELECT day::date AS date, external_user_id, speaker_name,
                   COUNT(*)::int AS total_word_count,
                   COUNT(DISTINCT token)::int AS unique_word_count
            FROM word_occurrences
            GROUP BY 1,2,3
        ),
        ix AS (
            SELECT COALESCE(logical_day, created_at::date) AS date, external_user_id, speaker_name,
                   COUNT(*)::int AS interaction_count,
                   COALESCE(SUM(duration_s),0)::numeric AS speaking_seconds
            FROM interactions
            GROUP BY 1,2,3
        ),
        lat AS (
            SELECT COALESCE(i.logical_day, i.created_at::date) AS date, i.external_user_id, i.speaker_name,
                   AVG(m.response_latency_s)::numeric AS avg_response_latency_s
            FROM interactions i
            JOIN interaction_metrics m ON m.interaction_id = i.id
            WHERE m.response_latency_s IS NOT NULL
            GROUP BY 1,2,3
        )
        SELECT
            COALESCE(w.date, ix.date, lat.date) AS date,
            COALESCE(w.external_user_id, ix.external_user_id, lat.external_user_id) AS external_user_id,
            COALESCE(w.speaker_name, ix.speaker_name, lat.speaker_name) AS speaker_name,
            COALESCE(w.unique_word_count, 0) AS unique_word_count,
            COALESCE(w.total_word_count, 0) AS total_word_count,
            COALESCE(ix.interaction_count, 0) AS interaction_count,
            COALESCE(ix.speaking_seconds, 0) AS speaking_seconds,
            lat.avg_response_latency_s
        FROM words w
        FULL OUTER JOIN ix
            ON ix.date = w.date
           AND ix.external_user_id = w.external_user_id
           AND ix.speaker_name = w.speaker_name
        FULL OUTER JOIN lat
            ON lat.date = COALESCE(w.date, ix.date)
           AND lat.external_user_id = COALESCE(w.external_user_id, ix.external_user_id)
           AND lat.speaker_name = COALESCE(w.speaker_name, ix.speaker_name)
        """,
        """
        CREATE OR REPLACE VIEW v_weekly_metrics AS
        SELECT
            date_trunc('week', date)::date AS week_start,
            external_user_id,
            speaker_name,
            SUM(unique_word_count)::int AS unique_word_count,
            SUM(total_word_count)::int AS total_word_count,
            SUM(interaction_count)::int AS interaction_count,
            SUM(speaking_seconds)::numeric AS speaking_seconds,
            AVG(avg_response_latency_s)::numeric AS avg_response_latency_s
        FROM v_daily_metrics
        GROUP BY 1,2,3
        """,
    ]
    txid = _begin_tx()
    try:
        _exec("SELECT pg_advisory_lock(842021031)", transaction_id=txid)
        for st in statements:
            _exec(st, transaction_id=txid)
        _exec("SELECT pg_advisory_unlock(842021031)", transaction_id=txid)
        _commit_tx(txid)
    except Exception:
        _rollback_tx(txid)
        raise


def _wordbank_counts():
    rows = _query_rows(
        """
        SELECT
            (SELECT COUNT(*)::int FROM wordbank_aoa_ws_prod) AS ws_rows,
            (SELECT COUNT(*)::int FROM wordbank_aoa_wg_comp) AS wg_rows,
            (SELECT COUNT(*)::int FROM wordbank_w2v_assocs) AS w2v_rows
        """
    )
    if not rows:
        return {'ws_rows': 0, 'wg_rows': 0, 'w2v_rows': 0}
    row = rows[0]
    return {
        'ws_rows': int(row.get('ws_rows') or 0),
        'wg_rows': int(row.get('wg_rows') or 0),
        'w2v_rows': int(row.get('w2v_rows') or 0),
    }


def _is_wordbank_seeded():
    counts = _wordbank_counts()
    # Guard against partial seeds after interrupted/failed ingestion.
    return counts['ws_rows'] >= 300 and counts['wg_rows'] >= 300 and counts['w2v_rows'] >= 10000


def _seed_wordbank_aoa_table(table_name, rows):
    if not rows:
        return 0
    parameter_sets = []
    for row in rows:
        parameter_sets.append(
            [
                _param('w', row['word']),
                _param('a', row['aoa_months']),
                _param('c', row['category']),
                _param('l', row['lexical_class']),
                _param('d', row['display_label']),
            ]
        )
    _batch_exec(
        f"""
        INSERT INTO {table_name}(word, aoa_months, {"cdi_category" if table_name == "wordbank_aoa_ws_prod" else "category"}, lexical_class, display_label)
        VALUES(:w, :a, :c, :l, :d)
        ON CONFLICT(word)
        DO UPDATE SET
            aoa_months = EXCLUDED.aoa_months,
            {"cdi_category" if table_name == "wordbank_aoa_ws_prod" else "category"} = EXCLUDED.{"cdi_category" if table_name == "wordbank_aoa_ws_prod" else "category"},
            lexical_class = EXCLUDED.lexical_class,
            display_label = EXCLUDED.display_label
        """,
        parameter_sets,
        chunk_size=500,
    )
    return len(parameter_sets)


def _seed_wordbank_w2v_edges(edges_by_pair):
    if not edges_by_pair:
        return 0
    parameter_sets = []
    for (word_a, word_b), cosine in edges_by_pair.items():
        parameter_sets.append([_param('a', word_a), _param('b', word_b), _param('c', cosine)])
    _batch_exec(
        """
        INSERT INTO wordbank_w2v_assocs(word_a, word_b, cosine)
        SELECT LEAST(:a, :b), GREATEST(:a, :b), :c
        WHERE :a <> :b
        ON CONFLICT(word_a, word_b)
        DO UPDATE SET cosine = EXCLUDED.cosine
        """,
        parameter_sets,
        chunk_size=1000,
    )
    return len(parameter_sets)


def ensure_wordbank_seeded():
    global _WORDBANK_SEEDED
    if _WORDBANK_SEEDED:
        return
    if not sql_enabled():
        raise RuntimeError('SQL not enabled')
    ensure_schema()
    with _WORDBANK_SEED_LOCK:
        if _WORDBANK_SEEDED:
            return
        if _is_wordbank_seeded():
            _WORDBANK_SEEDED = True
            print(json.dumps({'event': 'WordbankSeedSkipped', 'reason': 'already_seeded'}))
            return
        counts_before = _wordbank_counts()
        if counts_before['ws_rows'] > 0 or counts_before['wg_rows'] > 0 or counts_before['w2v_rows'] > 0:
            _exec("TRUNCATE TABLE wordbank_w2v_assocs, wordbank_aoa_ws_prod, wordbank_aoa_wg_comp")
        started = time.time()
        ws_file = _resolve_wordbank_file('eng_ws_production_aoas.csv')
        wg_file = _resolve_wordbank_file('eng_wg_comprehension_aoas.csv')
        w2v_file = _resolve_wordbank_file('w2v_assocs.csv')
        ws_rows = _load_aoa_rows(ws_file, 'category')
        wg_rows = _load_aoa_rows(wg_file, 'category')
        w2v = _load_w2v_edges(w2v_file, _WORD_BANK_MIN_COSINE_STORED)
        ws_upserted = _seed_wordbank_aoa_table('wordbank_aoa_ws_prod', ws_rows)
        wg_upserted = _seed_wordbank_aoa_table('wordbank_aoa_wg_comp', wg_rows)
        edge_upserted = _seed_wordbank_w2v_edges(w2v['edges'])
        elapsed_ms = int((time.time() - started) * 1000)
        print(
            json.dumps(
                {
                    'event': 'WordbankSeedDone',
                    'wsRowsUpserted': ws_upserted,
                    'wgRowsUpserted': wg_upserted,
                    'w2vEdgesUpserted': edge_upserted,
                    'w2vCellsScanned': int(w2v['scanned']),
                    'w2vThresholdCandidates': int(w2v['kept']),
                    'elapsedMs': elapsed_ms,
                }
            )
        )
        _WORDBANK_SEEDED = True


def _speaker_scope_clause(speaker):
    normalized = _speaker_scope_name(speaker)
    if normalized == 'Kid':
        return "speaker_name = 'Kid'"
    if normalized == 'Parent 1':
        return "speaker_name = 'Parent 1'"
    if normalized == 'Parent 2':
        return "speaker_name = 'Parent 2'"
    return "speaker_name IN ('Kid','Parent 1','Parent 2')"


def get_user_profile(external_user_id):
    if not sql_enabled() or not external_user_id:
        return None
    rows = _query_rows(
        "SELECT kid_age_months FROM users WHERE external_user_id = :u LIMIT 1",
        [_param('u', external_user_id)],
    )
    if not rows:
        return None
    raw = rows[0].get('kid_age_months')
    try:
        kid_age_months = None if raw is None else int(raw)
    except Exception:
        kid_age_months = None
    return {'kidAgeMonths': kid_age_months}


def upsert_user_kid_age_months(external_user_id, kid_age_months):
    if not sql_enabled() or not external_user_id:
        return
    _exec(
        """
        INSERT INTO users(external_user_id, kid_age_months)
        VALUES(:u, :m)
        ON CONFLICT(external_user_id)
        DO UPDATE SET kid_age_months = EXCLUDED.kid_age_months
        """,
        [
            _param('u', external_user_id),
            _param('m', int(kid_age_months)),
        ],
    )


def get_kid_benchmark_age_range():
    if not sql_enabled():
        return None
    rows = _query_rows(
        """
        SELECT MIN(age_months)::int AS min_age_months, MAX(age_months)::int AS max_age_months
        FROM kid_unique_words_monthly_benchmark
        """
    )
    if not rows:
        return None
    r = rows[0]
    if r.get('min_age_months') is None or r.get('max_age_months') is None:
        return None
    return {'minAgeMonths': int(r['min_age_months']), 'maxAgeMonths': int(r['max_age_months'])}


def upsert_kid_benchmark_rows(rows, source_file='unique_words_per month.csv'):
    if not sql_enabled():
        raise RuntimeError('SQL not enabled')
    for row in rows:
        _exec(
            """
            INSERT INTO kid_unique_words_monthly_benchmark(age_months, unique_word_count, kid_count, source_file)
            VALUES(:a, :u, :c, :s)
            ON CONFLICT(age_months, unique_word_count)
            DO UPDATE SET
                kid_count = EXCLUDED.kid_count,
                source_file = EXCLUDED.source_file
            """,
            [
                _param('a', int(row['age_months'])),
                _param('u', int(row['unique_word_count'])),
                _param('c', int(row['kid_count'])),
                _param('s', source_file),
            ],
        )


def compute_kid_percentile(age_months, current_unique_words):
    if not sql_enabled():
        return None
    rows = _query_rows(
        """
        SELECT
            COALESCE(SUM(CASE WHEN unique_word_count <= :u THEN kid_count ELSE 0 END), 0)::bigint AS numerator,
            COALESCE(SUM(kid_count), 0)::bigint AS denominator
        FROM kid_unique_words_monthly_benchmark
        WHERE age_months = :a
        """,
        [_param('u', int(current_unique_words)), _param('a', int(age_months))],
    )
    if not rows:
        return None
    numerator = int(rows[0].get('numerator') or 0)
    denominator = int(rows[0].get('denominator') or 0)
    if denominator <= 0:
        return None
    percentile = round((100.0 * numerator) / denominator, 1)
    return {
        'percentile': percentile,
        'benchmarkSampleSize': denominator,
        'cumulativeCount': numerator,
    }


def compute_kid_percentile_smoothed(age_months, current_unique_words, include_adjacent=True):
    if not sql_enabled():
        return None
    age_months = int(age_months)
    current_unique_words = int(current_unique_words)
    age_range = get_kid_benchmark_age_range()
    if not age_range:
        return None
    min_age = int(age_range['minAgeMonths'])
    max_age = int(age_range['maxAgeMonths'])
    candidate = [age_months]
    if include_adjacent:
        candidate = [age_months - 1, age_months, age_months + 1]
    months_used = sorted({m for m in candidate if m >= min_age and m <= max_age})
    if not months_used:
        return None
    month_list_sql = ','.join(str(int(m)) for m in months_used)
    rows = _query_rows(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN unique_word_count <= :u THEN kid_count ELSE 0 END), 0)::bigint AS numerator,
            COALESCE(SUM(kid_count), 0)::bigint AS denominator
        FROM kid_unique_words_monthly_benchmark
        WHERE age_months IN ({month_list_sql})
        """,
        [_param('u', current_unique_words)],
    )
    if not rows:
        return None
    numerator = int(rows[0].get('numerator') or 0)
    denominator = int(rows[0].get('denominator') or 0)
    if denominator <= 0:
        return None
    return {
        'percentile': round((100.0 * numerator) / denominator, 1),
        'benchmarkSampleSize': denominator,
        'cumulativeCount': numerator,
        'benchmarkMonthsUsed': months_used,
        'benchmarkAgeRangeMonths': {'min': min(months_used), 'max': max(months_used)},
    }


def upsert_transcript_record(item):
    if not sql_enabled():
        return

    transcript_id = item.get('transcriptId')
    if not transcript_id:
        return

    external_user_id = item.get('displayUserId') or ''
    created_at = _to_iso8601(item.get('createdAt'))
    updated_at = _to_iso8601(item.get('updatedAt') or item.get('createdAt'))
    logical_day = str(item.get('logicalDay') or created_at[:10])
    status = item.get('status') or 'COMPLETED'
    segments = item.get('segments') or []

    txid = _begin_tx()
    try:
        _exec(
            "INSERT INTO users(external_user_id) VALUES(:u) ON CONFLICT(external_user_id) DO NOTHING",
            [_param('u', external_user_id)],
            transaction_id=txid,
            include_result_metadata=False,
        )
        _exec(
            """
            INSERT INTO transcripts(
                id, external_user_id, status, created_at, updated_at, logical_day,
                audio_s3_key, transcript_json_s3_key, num_speakers, full_text, speaker_stats, raw_item
            ) VALUES(
                CAST(:id AS UUID), :u, :s, CAST(:ca AS timestamptz), CAST(:ua AS timestamptz), CAST(:ld AS date),
                :ak, :tk, :ns, :ft, CAST(:ss AS jsonb), CAST(:raw AS jsonb)
            )
            ON CONFLICT(id)
            DO UPDATE SET
                external_user_id = EXCLUDED.external_user_id,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at,
                logical_day = EXCLUDED.logical_day,
                audio_s3_key = EXCLUDED.audio_s3_key,
                transcript_json_s3_key = EXCLUDED.transcript_json_s3_key,
                num_speakers = EXCLUDED.num_speakers,
                full_text = EXCLUDED.full_text,
                speaker_stats = EXCLUDED.speaker_stats,
                raw_item = EXCLUDED.raw_item
            """,
            [
                _param('id', transcript_id),
                _param('u', external_user_id),
                _param('s', status),
                _param('ca', created_at),
                _param('ua', updated_at),
                _param('ld', logical_day),
                _param('ak', item.get('audioS3Key')),
                _param('tk', item.get('transcriptJsonS3Key')),
                _param('ns', int(item.get('numSpeakers') or 0)),
                _param('ft', item.get('fullText') or ''),
                _param('ss', json.dumps(item.get('speakerStats') or [])),
                _param('raw', json.dumps(item)),
            ],
            transaction_id=txid,
            include_result_metadata=False,
        )
        _exec(
            "DELETE FROM word_occurrences WHERE transcript_id = CAST(:id AS UUID)",
            [_param('id', transcript_id)],
            transaction_id=txid,
            include_result_metadata=False,
        )
        _exec(
            "DELETE FROM word_annotations WHERE transcript_id = CAST(:id AS UUID)",
            [_param('id', transcript_id)],
            transaction_id=txid,
            include_result_metadata=False,
        )
        _exec(
            "DELETE FROM interactions WHERE transcript_id = CAST(:id AS UUID)",
            [_param('id', transcript_id)],
            transaction_id=txid,
            include_result_metadata=False,
        )

        metric_parameter_sets = []
        word_occurrence_parameter_sets = []
        word_annotation_parameter_sets = []
        prev = None
        for idx, seg in enumerate(segments):
            start_time = float(seg.get('startTime') or 0.0)
            end_time = float(seg.get('endTime') or start_time)
            duration_s = max(0.0, end_time - start_time)
            speaker_name = seg.get('speakerName') or f"Speaker {idx + 1}"

            resp = _exec(
            """
            INSERT INTO interactions(
                transcript_id, interaction_index, external_user_id, speaker_label, speaker_name,
                start_time, end_time, duration_s, text, created_at, logical_day
            ) VALUES(
                CAST(:id AS UUID), :ix, :u, :sl, :sn,
                :st, :et, :du, :tx, CAST(:ca AS timestamptz), CAST(:ld AS date)
            )
            RETURNING id
            """,
            [
                _param('id', transcript_id),
                _param('ix', idx),
                _param('u', external_user_id),
                _param('sl', seg.get('speakerLabel') or ''),
                _param('sn', speaker_name),
                _param('st', start_time),
                _param('et', end_time),
                _param('du', duration_s),
                _param('tx', seg.get('text') or ''),
                _param('ca', created_at),
                _param('ld', logical_day),
            ],
            transaction_id=txid,
        )
            inter_id = resp['records'][0][0]['longValue']

            response_latency = None
            prev_speaker = None
            if prev and prev['speaker_name'] != speaker_name:
                response_latency = max(0.0, start_time - prev['end_time'])
                prev_speaker = prev['speaker_name']
            metric_parameter_sets.append(
                [_param('iid', inter_id), _param('lat', response_latency), _param('ps', prev_speaker)]
            )

            for tok in _tokens(seg.get('text') or ''):
                word_occurrence_parameter_sets.append(
                    [
                        _param('id', transcript_id),
                        _param('iid', inter_id),
                        _param('u', external_user_id),
                        _param('d', logical_day),
                        _param('sn', speaker_name),
                        _param('t', tok),
                        _param('ca', created_at),
                    ]
                )

            for tok in seg.get('tokens') or []:
                word_annotation_parameter_sets.append(
                    [
                        _param('id', transcript_id),
                        _param('iid', inter_id),
                        _param('u', external_user_id),
                        _param('d', logical_day),
                        _param('sn', speaker_name),
                        _param('ix', int(tok.get('index') or 0)),
                        _param('sf', tok.get('text') or ''),
                        _param('lm', tok.get('lemma') or ''),
                        _param('nw', _normalize_word((tok.get('lemma') or tok.get('text') or ''))),
                        _param('ps', tok.get('pos') or ''),
                        _param('tg', tok.get('tag') or ''),
                        _param('ck', normalize_pos_category_key(tok.get('categoryKey'), tok.get('lemma') or tok.get('text') or '')),
                        _param('ca', created_at),
                    ]
                )

            prev = {'speaker_name': speaker_name, 'end_time': end_time}

        _batch_exec(
            """
            INSERT INTO interaction_metrics(interaction_id, response_latency_s, previous_speaker_name)
            VALUES(:iid, :lat, :ps)
            ON CONFLICT(interaction_id)
            DO UPDATE SET response_latency_s = EXCLUDED.response_latency_s,
                          previous_speaker_name = EXCLUDED.previous_speaker_name
            """,
            metric_parameter_sets,
            transaction_id=txid,
        )
        _batch_exec(
            """
            INSERT INTO word_occurrences(transcript_id, interaction_id, external_user_id, day, speaker_name, token, created_at)
            VALUES(CAST(:id AS UUID), :iid, :u, CAST(:d AS date), :sn, :t, CAST(:ca AS timestamptz))
            """,
            word_occurrence_parameter_sets,
            transaction_id=txid,
        )
        _batch_exec(
            """
            INSERT INTO word_annotations(
                transcript_id, interaction_id, external_user_id, day, speaker_name, token_index,
                surface, lemma, normalized_word, pos, tag, category_key, created_at
            )
            VALUES(
                CAST(:id AS UUID), :iid, :u, CAST(:d AS date), :sn, :ix,
                :sf, :lm, :nw, :ps, :tg, :ck, CAST(:ca AS timestamptz)
            )
            """,
            word_annotation_parameter_sets,
            transaction_id=txid,
        )
        _commit_tx(txid)
    except Exception:
        _rollback_tx(txid)
        raise


def get_transcript_interactions(transcript_id):
    rows = _query_rows(
        """
        SELECT
            i.interaction_index,
            i.speaker_label,
            i.speaker_name,
            i.start_time,
            i.end_time,
            i.duration_s,
            i.text,
            m.response_latency_s,
            m.previous_speaker_name
        FROM interactions i
        LEFT JOIN interaction_metrics m ON m.interaction_id = i.id
        WHERE i.transcript_id = CAST(:id AS UUID)
        ORDER BY i.interaction_index ASC
        """,
        [_param('id', transcript_id)],
    )
    return [
        {
            'interactionIndex': int(r['interaction_index']),
            'speakerLabel': r['speaker_label'],
            'speakerName': r['speaker_name'],
            'startTime': float(r['start_time'] or 0),
            'endTime': float(r['end_time'] or 0),
            'durationS': float(r['duration_s'] or 0),
            'text': r['text'] or '',
            'responseLatencyS': None if r['response_latency_s'] is None else float(r['response_latency_s']),
            'previousSpeakerName': r['previous_speaker_name'],
        }
        for r in rows
    ]


def query_daily(user_id, date_from, date_to, speaker):
    where = ["external_user_id = :u", "date >= CAST(:f AS date)", "date <= CAST(:t AS date)"]
    where.append(_speaker_scope_clause(speaker))
    rows = _query_rows(
        f"""
        SELECT
            date,
            external_user_id,
            SUM(unique_word_count)::int AS unique_word_count,
            SUM(total_word_count)::int AS total_word_count,
            SUM(interaction_count)::int AS interaction_count,
            SUM(speaking_seconds)::numeric AS speaking_seconds,
            AVG(avg_response_latency_s)::numeric AS avg_response_latency_s
        FROM v_daily_metrics
        WHERE {' AND '.join(where)}
        GROUP BY date, external_user_id
        ORDER BY date ASC
        """,
        [_param('u', user_id), _param('f', date_from), _param('t', date_to)],
    )
    return rows


def query_weekly(user_id, date_from, date_to, speaker):
    speaker_clause = _speaker_scope_clause(speaker)
    return _query_rows(
        f"""
        WITH words AS (
            SELECT
                date_trunc('week', day)::date AS week_start,
                external_user_id,
                COUNT(*)::int AS total_word_count,
                COUNT(DISTINCT token)::int AS unique_word_count
            FROM word_occurrences
            WHERE external_user_id = :u
              AND day >= CAST(:f AS date)
              AND day <= CAST(:t AS date)
              AND {speaker_clause}
            GROUP BY 1,2
        ),
        ix AS (
            SELECT
                date_trunc('week', COALESCE(logical_day, created_at::date))::date AS week_start,
                external_user_id,
                COUNT(*)::int AS interaction_count,
                COALESCE(SUM(duration_s), 0)::numeric AS speaking_seconds
            FROM interactions
            WHERE external_user_id = :u
              AND COALESCE(logical_day, created_at::date) >= CAST(:f AS date)
              AND COALESCE(logical_day, created_at::date) <= CAST(:t AS date)
              AND {speaker_clause}
            GROUP BY 1,2
        ),
        lat AS (
            SELECT
                date_trunc('week', COALESCE(i.logical_day, i.created_at::date))::date AS week_start,
                i.external_user_id,
                AVG(m.response_latency_s)::numeric AS avg_response_latency_s
            FROM interactions i
            JOIN interaction_metrics m ON m.interaction_id = i.id
            WHERE i.external_user_id = :u
              AND COALESCE(i.logical_day, i.created_at::date) >= CAST(:f AS date)
              AND COALESCE(i.logical_day, i.created_at::date) <= CAST(:t AS date)
              AND {speaker_clause.replace('speaker_name', 'i.speaker_name')}
              AND m.response_latency_s IS NOT NULL
            GROUP BY 1,2
        )
        SELECT
            COALESCE(w.week_start, ix.week_start, lat.week_start) AS week_start,
            :u AS external_user_id,
            COALESCE(w.unique_word_count, 0)::int AS unique_word_count,
            COALESCE(w.total_word_count, 0)::int AS total_word_count,
            COALESCE(ix.interaction_count, 0)::int AS interaction_count,
            COALESCE(ix.speaking_seconds, 0)::numeric AS speaking_seconds,
            lat.avg_response_latency_s
        FROM words w
        FULL OUTER JOIN ix
            ON ix.week_start = w.week_start
           AND ix.external_user_id = w.external_user_id
        FULL OUTER JOIN lat
            ON lat.week_start = COALESCE(w.week_start, ix.week_start)
           AND lat.external_user_id = :u
        ORDER BY 1 ASC
        """,
        [_param('u', user_id), _param('f', date_from), _param('t', date_to)],
    )


def query_monthly(user_id, from_month, to_month, speaker='all'):
    speaker_clause = _speaker_scope_clause(speaker)
    return _query_rows(
        f"""
        WITH words AS (
            SELECT
                date_trunc('month', day)::date AS month_start,
                external_user_id,
                COUNT(*)::int AS total_word_count,
                COUNT(DISTINCT token)::int AS unique_word_count
            FROM word_occurrences
            WHERE external_user_id = :u
              AND day >= CAST(:f AS date)
              AND day <= CAST(:t AS date)
              AND {speaker_clause}
            GROUP BY 1,2
        ),
        ix AS (
            SELECT
                date_trunc('month', COALESCE(logical_day, created_at::date))::date AS month_start,
                external_user_id,
                COUNT(*)::int AS interaction_count,
                COALESCE(SUM(duration_s), 0)::numeric AS speaking_seconds
            FROM interactions
            WHERE external_user_id = :u
              AND COALESCE(logical_day, created_at::date) >= CAST(:f AS date)
              AND COALESCE(logical_day, created_at::date) <= CAST(:t AS date)
              AND {speaker_clause}
            GROUP BY 1,2
        ),
        lat AS (
            SELECT
                date_trunc('month', COALESCE(i.logical_day, i.created_at::date))::date AS month_start,
                i.external_user_id,
                AVG(m.response_latency_s)::numeric AS avg_response_latency_s
            FROM interactions i
            JOIN interaction_metrics m ON m.interaction_id = i.id
            WHERE i.external_user_id = :u
              AND COALESCE(i.logical_day, i.created_at::date) >= CAST(:f AS date)
              AND COALESCE(i.logical_day, i.created_at::date) <= CAST(:t AS date)
              AND {speaker_clause.replace('speaker_name', 'i.speaker_name')}
              AND m.response_latency_s IS NOT NULL
            GROUP BY 1,2
        )
        SELECT
            COALESCE(w.month_start, ix.month_start, lat.month_start) AS month_start,
            :u AS external_user_id,
            COALESCE(w.unique_word_count, 0)::int AS unique_word_count,
            COALESCE(w.total_word_count, 0)::int AS total_word_count,
            COALESCE(ix.interaction_count, 0)::int AS interaction_count,
            COALESCE(ix.speaking_seconds, 0)::numeric AS speaking_seconds,
            lat.avg_response_latency_s
        FROM words w
        FULL OUTER JOIN ix
            ON ix.month_start = w.month_start
           AND ix.external_user_id = w.external_user_id
        FULL OUTER JOIN lat
            ON lat.month_start = COALESCE(w.month_start, ix.month_start)
           AND lat.external_user_id = :u
        ORDER BY 1 ASC
        """,
        [_param('u', user_id), _param('f', from_month), _param('t', to_month)],
    )


def query_transcript_words(user_id, date_from, date_to, speaker='kid'):
    speaker_clause = _speaker_scope_clause(speaker)
    return _query_rows(
        f"""
        SELECT DISTINCT token AS normalized_word
        FROM word_occurrences
        WHERE external_user_id = :u
          AND day >= CAST(:f AS date)
          AND day <= CAST(:t AS date)
          AND {speaker_clause}
        ORDER BY normalized_word ASC
        """,
        [_param('u', user_id), _param('f', date_from), _param('t', date_to)],
    )


def query_semantic_eligible_words(age_max_months):
    return _query_rows(
        """
        SELECT word, aoa_months, cdi_category, lexical_class, display_label
        FROM wordbank_aoa_ws_prod
        WHERE aoa_months <= :a
        ORDER BY word ASC
        """,
        [_param('a', float(age_max_months))],
    )


def query_semantic_edges(words, min_cosine):
    normalized = sorted({_normalize_wordbank_word(w) for w in (words or []) if _normalize_wordbank_word(w)})
    if len(normalized) < 2:
        return []
    placeholders = []
    params = [_param('c', float(min_cosine))]
    for idx, word in enumerate(normalized):
        name = f'w{idx}'
        placeholders.append(f':{name}')
        params.append(_param(name, word))
    in_list = ','.join(placeholders)
    return _query_rows(
        f"""
        SELECT word_a, word_b, cosine
        FROM wordbank_w2v_assocs
        WHERE cosine >= :c
          AND word_a IN ({in_list})
          AND word_b IN ({in_list})
        ORDER BY cosine DESC, word_a ASC, word_b ASC
        """,
        params,
    )


def query_wg_categories(user_id, date_from, date_to, speaker='kid'):
    speaker_clause = _speaker_scope_clause(speaker)
    rows = _query_rows(
        f"""
        WITH words AS (
            SELECT DISTINCT token AS normalized_word
            FROM word_occurrences
            WHERE external_user_id = :u
              AND day >= CAST(:f AS date)
              AND day <= CAST(:t AS date)
              AND {speaker_clause}
        )
        SELECT
            COALESCE(NULLIF(LOWER(TRIM(wg.category)), ''), 'other') AS category_key,
            w.normalized_word
        FROM words w
        LEFT JOIN wordbank_aoa_wg_comp wg ON wg.word = w.normalized_word
        ORDER BY category_key ASC, w.normalized_word ASC
        """,
        [_param('u', user_id), _param('f', date_from), _param('t', date_to)],
    )
    out = []
    for row in rows:
        key = _normalize_wg_category(row.get('category_key'))
        out.append({'category_key': key, 'normalized_word': row.get('normalized_word')})
    return out


def query_pos_categories(user_id, date_from, date_to, speaker='all'):
    return query_wg_categories(user_id, date_from, date_to, speaker=speaker)


def query_latency(user_id, date_from, date_to, from_speaker=None, to_speaker=None):
    where = [
        "i.external_user_id = :u",
        "COALESCE(i.logical_day, i.created_at::date) >= CAST(:f AS date)",
        "COALESCE(i.logical_day, i.created_at::date) <= CAST(:t AS date)",
        "m.response_latency_s IS NOT NULL",
    ]
    params = [_param('u', user_id), _param('f', date_from), _param('t', date_to)]
    if from_speaker:
        where.append("m.previous_speaker_name = :fs")
        params.append(_param('fs', from_speaker))
    if to_speaker:
        where.append("i.speaker_name = :ts")
        params.append(_param('ts', to_speaker))

    return _query_rows(
        f"""
        SELECT
            COALESCE(i.logical_day, i.created_at::date) AS date,
            m.previous_speaker_name AS from_speaker,
            i.speaker_name AS to_speaker,
            COUNT(*)::int AS transitions,
            AVG(m.response_latency_s)::numeric AS avg_response_latency_s,
            MIN(m.response_latency_s)::numeric AS min_response_latency_s,
            MAX(m.response_latency_s)::numeric AS max_response_latency_s
        FROM interactions i
        JOIN interaction_metrics m ON m.interaction_id = i.id
        WHERE {' AND '.join(where)}
        GROUP BY 1,2,3
        ORDER BY 1,2,3
        """,
        params,
    )


def query_monthly_calendar(user_id, year, month, speaker='all'):
    month_start = f"{year:04d}-{month:02d}-01"
    rows = _query_rows(
        f"""
        WITH bounds AS (
            SELECT CAST(:m AS date) AS month_start,
                   (CAST(:m AS date) + INTERVAL '1 month - 1 day')::date AS month_end
        ),
        days AS (
            SELECT dd::date AS date
            FROM bounds b,
                 generate_series(b.month_start, b.month_end, INTERVAL '1 day') AS dd
        ),
        daily AS (
            SELECT
                d.date,
                COALESCE(SUM(m.unique_word_count), 0)::int AS unique_word_count,
                COALESCE(SUM(m.total_word_count), 0)::int AS total_word_count,
                COALESCE(SUM(m.interaction_count), 0)::int AS interaction_count,
                COALESCE(SUM(m.speaking_seconds), 0)::numeric AS speaking_seconds,
                AVG(m.avg_response_latency_s)::numeric AS avg_response_latency_s,
                COUNT(DISTINCT t.id)::int AS transcript_count
            FROM days d
            LEFT JOIN v_daily_metrics m
              ON m.date = d.date
             AND m.external_user_id = :u
             AND ({_speaker_scope_clause(speaker)})
            LEFT JOIN transcripts t
              ON t.external_user_id = :u
             AND COALESCE(t.logical_day, t.created_at::date) = d.date
            GROUP BY d.date
            ORDER BY d.date
        )
        SELECT * FROM daily
        """,
        [_param('m', month_start), _param('u', user_id)],
    )

    days = []
    totals = {'uniqueWordCount': 0, 'totalWordCount': 0, 'interactionCount': 0, 'speakingSeconds': 0.0}
    for r in rows:
        unique_count = int(r.get('unique_word_count') or 0)
        total_count = int(r.get('total_word_count') or 0)
        interaction_count = int(r.get('interaction_count') or 0)
        speaking_seconds = float(r.get('speaking_seconds') or 0)
        day = {
            'date': str(r['date']),
            'hasData': (unique_count > 0 or total_count > 0 or interaction_count > 0),
            'uniqueWordCount': unique_count,
            'totalWordCount': total_count,
            'interactionCount': interaction_count,
            'speakingSeconds': speaking_seconds,
            'avgResponseLatencyS': None if r.get('avg_response_latency_s') is None else float(r['avg_response_latency_s']),
            'transcriptCount': int(r.get('transcript_count') or 0),
        }
        days.append(day)
        totals['uniqueWordCount'] += unique_count
        totals['totalWordCount'] += total_count
        totals['interactionCount'] += interaction_count
        totals['speakingSeconds'] += speaking_seconds

    return {
        'userId': user_id,
        'year': year,
        'month': month,
        'speaker': speaker,
        'days': days,
        'monthTotals': totals,
    }



