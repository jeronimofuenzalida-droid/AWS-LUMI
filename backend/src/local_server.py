import base64
import csv
import importlib
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_ROOT = REPO_ROOT / 'artifacts' / 'local' / 'dev_server'
UPLOADS_ROOT = ARTIFACTS_ROOT / 'uploads'
CALIBRATIONS_ROOT = ARTIFACTS_ROOT / 'calibrations'
TRANSCRIPTS_ROOT = ARTIFACTS_ROOT / 'transcripts'
FIXTURE_PATH = REPO_ROOT / 'fixtures' / 'conversationtest.transcribe.raw.json'
LOCAL_API_MODE = (os.environ.get('LOCAL_API_MODE') or 'cloud').strip().lower()
if LOCAL_API_MODE not in {'cloud', 'mock'}:
    LOCAL_API_MODE = 'cloud'

os.environ.setdefault('APP_ENV', 'local')
os.environ.setdefault('LOCAL_DEV_MODE', 'true')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-1')
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')
if LOCAL_API_MODE == 'mock':
    os.environ.setdefault('TRANSCRIPTS_TABLE', 'local-transcripts')
    os.environ.setdefault('UPLOADS_BUCKET', 'local-uploads')
    os.environ.setdefault('ARTIFACTS_BUCKET', 'local-artifacts')
    os.environ.setdefault('CALIBRATION_BUCKET', 'local-calibrations')
    os.environ.setdefault('ASR_DISPATCH_MODE', 'queue_service')
    os.environ.setdefault('ASR_WARM_WINDOW_SECONDS', '300')
    os.environ.setdefault('ASR_GPU_SPOT_ASG_NAME', 'local-gpu')
    os.environ.setdefault('GPU_ONLY_PIPELINE', 'false')
    os.environ.setdefault('ASR_ACTIVITY_TOUCH_THROTTLE_SECONDS', '30')

DEFAULT_PORT = int(os.environ.get('LOCAL_API_PORT') or '3001')
ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
CALIBRATIONS_ROOT.mkdir(parents=True, exist_ok=True)
TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sql_store import ensure_schema, get_user_profile, sql_enabled, upsert_transcript_record, upsert_user_kid_age_months  # noqa: E402

_POS_TAGGING_SPEC = importlib.util.spec_from_file_location(
    'local_pos_tagging',
    REPO_ROOT / 'backend' / 'asr_worker' / 'pos_tagging.py',
)
_POS_TAGGING_MODULE = importlib.util.module_from_spec(_POS_TAGGING_SPEC)
_POS_TAGGING_SPEC.loader.exec_module(_POS_TAGGING_MODULE)
annotate_segments_with_pos = _POS_TAGGING_MODULE.annotate_segments_with_pos
canonical_category_key = _POS_TAGGING_MODULE.canonical_category_key
category_label = _POS_TAGGING_MODULE.category_label

STATE = {
    'uploads': {},
    'calibrations': {},
    'transcripts': {},
    'warmUntilEpoch': 0,
    'gpuActiveAtEpoch': 0,
    'lastWarmTouchEpoch': 0,
}

SPEAKER_ROLE_MAP = {
    'spk_0': 'Parent 1',
    'spk_1': 'Parent 2',
    'spk_2': 'Kid',
}
_APP_MODULE = None
MOCK_ANALYTICS_PATHS = {
    '/analytics/progression/daily',
    '/analytics/progression/weekly',
    '/analytics/progression/monthly',
    '/analytics/progression/pos-categories',
    '/analytics/semantic/network',
    '/analytics/progression/monthly-calendar',
    '/analytics/kid-percentile',
    '/analytics/interactions/latency',
}
_WG_CATEGORY_LOOKUP = None


def _json_body(handler):
    length = int(handler.headers.get('Content-Length') or '0')
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode('utf-8'))


def _send(handler, status, body, extra_headers=None, content_type='application/json'):
    payload = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Access-Control-Allow-Headers', '*')
    handler.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,OPTIONS')
    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(key, value)
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _cloud_required_env():
    return ['TRANSCRIPTS_TABLE', 'UPLOADS_BUCKET', 'ARTIFACTS_BUCKET']


def _validate_cloud_env():
    missing = [name for name in _cloud_required_env() if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            'Cloud local mode requires deployed API environment variables. '
            f'Missing: {", ".join(missing)}. Start with scripts/start_local_cloud.ps1.'
        )


def _get_app_module():
    global _APP_MODULE
    if _APP_MODULE is None:
        if LOCAL_API_MODE == 'cloud':
            _validate_cloud_env()
        _APP_MODULE = importlib.import_module('app')
    return _APP_MODULE


def _app_attr(name):
    return getattr(_get_app_module(), name)


def _now_iso():
    return _app_attr('now_iso')()


def _kid_benchmark_month_range():
    return _app_attr('kid_benchmark_month_range')()


def _parse_kid_age_months(value):
    return _app_attr('parse_kid_age_months')(value)


def _build_segments(raw_json):
    return _app_attr('build_segments')(raw_json)


def _auto_assign_speaker_names(segments, calibrations):
    return _app_attr('auto_assign_speaker_names')(segments, calibrations)


def _compute_stats(segments):
    return _app_attr('compute_stats')(segments)


def _build_full_text(segments):
    return _app_attr('build_full_text')(segments)


def _asr_warm_window_seconds():
    return int(os.environ.get('ASR_WARM_WINDOW_SECONDS') or '300')


def _activity_touch_throttle_seconds():
    return int(os.environ.get('ASR_ACTIVITY_TOUCH_THROTTLE_SECONDS') or '30')


def _local_app_config_payload():
    min_months = 8
    max_months = 30
    if sql_enabled():
        try:
            profile_range = _app_attr('kid_benchmark_month_range')()
            min_months, max_months = profile_range
        except Exception:
            pass
    return {
        'kidBenchmarkMinMonths': min_months,
        'kidBenchmarkMaxMonths': max_months,
        'warmWindowSeconds': _asr_warm_window_seconds(),
        'runtimeStatusSemantics': {'cpu': 'worker_capacity', 'gpu': 'instances'},
        'engine': str(os.environ.get('ASR_ENGINE') or 'whisper'),
        'dispatchMode': str(os.environ.get('ASR_DISPATCH_MODE') or 'queue_service'),
        'gpuEnabled': True,
        'gpuOnlyPipeline': False,
    }


def _build_lambda_event(method, path, query, body=None, path_parameters=None):
    return {
        'version': '2.0',
        'rawPath': path,
        'rawQueryString': urlencode(query, doseq=True),
        'queryStringParameters': {k: values[-1] for k, values in query.items()} if query else {},
        'requestContext': {'http': {'method': method, 'path': path}},
        'pathParameters': path_parameters or {},
        'body': json.dumps(body) if body is not None else None,
        'isBase64Encoded': False,
    }


def _path_parameters(path):
    for pattern in (
        r'/transcriptions/(?P<transcriptId>[^/]+)/status',
        r'/transcriptions/(?P<transcriptId>[^/]+)/interactions',
        r'/transcriptions/(?P<transcriptId>[^/]+)',
    ):
        match = re.fullmatch(pattern, path)
        if match:
            return match.groupdict()
    return {}


def _sanitize_lambda_headers(headers, content_type):
    out = {}
    for key, value in (headers or {}).items():
        lowered = key.lower()
        if lowered in {'content-type', 'content-length', 'access-control-allow-origin', 'access-control-allow-headers', 'access-control-allow-methods'}:
            continue
        out[key] = value
    return out, content_type


def _relay_lambda(method, path, query, body=None, path_parameters=None):
    event = _build_lambda_event(method, path, query, body=body, path_parameters=path_parameters or _path_parameters(path))
    resp = _app_attr('handler')(event, None)
    status = int(resp.get('statusCode') or 200)
    headers = resp.get('headers') or {}
    content_type = headers.get('Content-Type') or headers.get('content-type') or 'application/json'
    raw_body = resp.get('body')
    if resp.get('isBase64Encoded'):
        payload = base64.b64decode(raw_body or '')
    elif isinstance(raw_body, str):
        payload = raw_body.encode('utf-8')
    elif isinstance(raw_body, (bytes, bytearray)):
        payload = bytes(raw_body)
    else:
        payload = json.dumps(raw_body or {}).encode('utf-8')
    extra_headers, content_type = _sanitize_lambda_headers(headers, content_type)
    return status, payload, extra_headers, content_type


def _load_fixture_raw():
    return json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))


def _calibration_roles_for_user(user_id):
    user_cals = STATE['calibrations'].get(user_id, {})
    return {
        'kid': bool(user_cals.get('kid')),
        'parent1': bool(user_cals.get('parent1')),
        'parent2': bool(user_cals.get('parent2')),
    }


def _build_fixture_item(transcript_id, user_id, effective_date, user_time_zone, audio_key=None):
    raw = _load_fixture_raw()
    segments = _build_segments(raw)
    segments = _auto_assign_speaker_names(segments, _calibration_roles_for_user(user_id))
    for seg in segments:
        seg['speakerName'] = SPEAKER_ROLE_MAP.get(seg.get('speakerLabel'), seg.get('speakerName') or 'Speaker')
    try:
        segments, pos_meta = annotate_segments_with_pos(segments, 'en')
    except Exception:
        pos_meta = {
            'posTaggingStatus': 'FAILED',
            'posTaggingModel': 'spacy/en_core_web_sm',
            'posTaggingVersion': 'v1',
        }
    stats = _compute_stats(segments)
    full_text = _build_full_text(segments)
    now = _now_iso()
    item = {
        'transcriptId': transcript_id,
        'userId': user_id,
        'displayUserId': user_id,
        'status': 'COMPLETED',
        'createdAt': now,
        'updatedAt': now,
        'logicalDay': effective_date,
        'effectiveDate': effective_date,
        'userTimeZone': user_time_zone,
        'audioS3Key': audio_key or f'local/uploads/{transcript_id}.bin',
        'transcriptJsonS3Key': f'local/transcripts/{transcript_id}.json',
        'numSpeakers': len({s.get('speakerLabel') for s in segments}),
        'speakerStats': stats,
        'segments': segments,
        'fullText': full_text,
        'message': 'Transcription complete.',
        'processingStage': 'COMPLETED',
        'posTaggingStatus': pos_meta.get('posTaggingStatus'),
        'posTaggingModel': pos_meta.get('posTaggingModel'),
        'posTaggingVersion': pos_meta.get('posTaggingVersion'),
        'workerMode': 'QUEUE_SERVICE',
        'workerCapacityType': 'LOCAL_GPU_MOCK',
    }
    (TRANSCRIPTS_ROOT / f'{transcript_id}.json').write_text(json.dumps(item, indent=2), encoding='utf-8')
    return item


def _touch_warm(trigger='INTERACTION'):
    now = int(time.time())
    if now - int(STATE.get('lastWarmTouchEpoch') or 0) >= _activity_touch_throttle_seconds():
        STATE['lastWarmTouchEpoch'] = now
    STATE['warmUntilEpoch'] = now + _asr_warm_window_seconds()
    if int(STATE.get('gpuActiveAtEpoch') or 0) <= now:
        STATE['gpuActiveAtEpoch'] = now + 6
    return {
        'warm': True,
        'gpuWarm': True,
        'scope': 'global',
        'mode': 'queue_service',
        'warmUntil': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(STATE['warmUntilEpoch'])),
        'gpuWarmUntil': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(STATE['warmUntilEpoch'])),
        'windowSeconds': _asr_warm_window_seconds(),
        'trigger': trigger,
        'message': 'GPU warm window extended.',
    }


def _runtime_status():
    now = int(time.time())
    warm_until = int(STATE.get('warmUntilEpoch') or 0)
    active_at = int(STATE.get('gpuActiveAtEpoch') or 0)
    processing = 0
    for tx in STATE['transcripts'].values():
        if tx.get('finalized'):
            continue
        stage = _transcript_stage(tx)['stage']
        if stage in ('ASR', 'DIARIZATION', 'SPEAKER_IDENTIFICATION', 'FINALIZING'):
            processing += 1
    gpu_active = 1 if warm_until > now and active_at <= now else 0
    gpu_activating = 1 if warm_until > now and active_at > now else 0
    return {
        'cpu': {'active': 0, 'activating': 0, 'busy': 0, 'semantics': 'worker_capacity'},
        'gpu': {'active': gpu_active, 'activating': gpu_activating, 'busy': processing, 'semantics': 'instances'},
        'gpuEnabled': True,
        'gpuOnlyPipeline': False,
        'warmScope': 'global',
        'warmUntil': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(warm_until)) if warm_until else None,
        'gpuWarmUntil': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(warm_until)) if warm_until else None,
        'gpuQueueVisible': sum(1 for tx in STATE['transcripts'].values() if not tx.get('finalized')),
        'gpuQueueNotVisible': 0,
    }


def _transcript_stage(entry):
    now = time.time()
    started = float(entry['createdEpoch'])
    active_at = float(STATE.get('gpuActiveAtEpoch') or started)
    if now < active_at:
        return {'status': 'IN_PROGRESS', 'stage': 'COLD_START', 'message': 'Cold start: waiting for GPU worker...'}
    elapsed = now - active_at
    if elapsed < 1.5:
        return {'status': 'IN_PROGRESS', 'stage': 'ASR', 'message': 'Speech-to-text: transcribing audio on GPU...'}
    if elapsed < 3.0:
        return {'status': 'IN_PROGRESS', 'stage': 'DIARIZATION', 'message': 'Diarization: detecting who spoke when on GPU...'}
    if elapsed < 4.5:
        return {'status': 'IN_PROGRESS', 'stage': 'SPEAKER_IDENTIFICATION', 'message': 'Speaker identification: matching Kid/Parent calibrations on GPU...'}
    if elapsed < 5.0:
        return {'status': 'IN_PROGRESS', 'stage': 'FINALIZING', 'message': 'Finalizing transcript...'}
    return {'status': 'COMPLETED', 'stage': 'COMPLETED', 'message': 'Transcription complete.'}


def _finalize_transcript(entry):
    if entry.get('finalized'):
        return
    item = _build_fixture_item(
        transcript_id=entry['transcriptId'],
        user_id=entry['userId'],
        effective_date=entry['effectiveDate'],
        user_time_zone=entry['userTimeZone'],
        audio_key=entry.get('audioS3Key'),
    )
    if sql_enabled():
        ensure_schema()
        upsert_transcript_record(item)
    entry['result'] = item
    entry['finalized'] = True


def _health_payload():
    return {
        'ok': True,
        'mode': LOCAL_API_MODE,
        'port': DEFAULT_PORT,
        'sqlEnabled': bool(sql_enabled()),
    }


def _mock_pos_categories(user_id):
    global _WG_CATEGORY_LOOKUP
    if _WG_CATEGORY_LOOKUP is None:
        _WG_CATEGORY_LOOKUP = {}
        csv_path = REPO_ROOT / 'wordbank' / 'eng_wg_comprehension_aoas.csv'
        if csv_path.exists():
            with csv_path.open('r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    word = str(row.get('uni_lemma') or row.get('definition') or '').strip().lower()
                    cat = str(row.get('category') or '').strip().lower() or 'other'
                    if cat == 'na':
                        cat = 'other'
                    if word:
                        _WG_CATEGORY_LOOKUP[word] = cat
    grouped = {}
    for tx in STATE['transcripts'].values():
        if tx.get('userId') != user_id or not tx.get('finalized'):
            continue
        for seg in tx['result'].get('segments') or []:
            if seg.get('speakerName') != 'Kid':
                continue
            for tok in seg.get('tokens') or []:
                word = str(tok.get('lemma') or tok.get('text') or '').strip().lower()
                key = _WG_CATEGORY_LOOKUP.get(word) or 'other'
                if not word:
                    continue
                grouped.setdefault(key, set()).add(word)
    categories = []
    for key, words in grouped.items():
        ordered = sorted(words)
        categories.append(
            {
                'key': key,
                'label': key.replace('_', ' ').title(),
                'uniqueWordCount': len(ordered),
                'words': ordered,
            }
        )
    categories.sort(key=lambda item: (-item['uniqueWordCount'], item['label']))
    return categories


def _mock_semantic_network(user_id):
    words = []
    for tx in STATE['transcripts'].values():
        if tx.get('userId') != user_id or not tx.get('finalized'):
            continue
        for seg in tx['result'].get('segments') or []:
            if seg.get('speakerName') != 'Kid':
                continue
            for tok in seg.get('tokens') or []:
                word = str(tok.get('lemma') or tok.get('text') or '').strip().lower()
                if word:
                    words.append(word)
    uniq = sorted(set(words))[:60]
    nodes = [{'id': w, 'label': w, 'aoaMonths': None, 'group': 'other'} for w in uniq]
    edges = []
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:i + 4]:
            cosine = 0.5
            edges.append({'source': a, 'target': b, 'cosine': cosine, 'weight': cosine * 4})
    return {
        'nodes': nodes,
        'edges': edges,
        'meta': {
            'ageMaxMonths': None,
            'minCosine': 0.5,
            'weightedThreshold': 2.0,
            'topKApplied': False,
            'transcriptWordCount': len(set(words)),
            'eligibleWordCount': len(uniq),
            'finalWordCount': len(uniq),
            'message': 'Local mock semantic map',
        },
    }


def _handle_cloud_request(method, path, query, body=None):
    if path == '/__local__/health':
        return 200, _health_payload(), None, 'application/json'
    return _relay_lambda(method, path, query, body=body)


def _handle_mock_request(handler, method, path, query, body=None):
    if path == '/__local__/health':
        return 200, _health_payload(), None, 'application/json'

    if method == 'GET':
        if path == '/v1/app-config':
            return 200, _local_app_config_payload(), None, 'application/json'
        if path == '/v1/asr/runtime-status':
            return 200, _runtime_status(), None, 'application/json'
        if path == '/v1/calibration/status':
            user_id = (query.get('userId') or [''])[-1]
            cal = STATE['calibrations'].get(user_id, {})
            profile = get_user_profile(user_id) if sql_enabled() else None
            body = {
                'userId': user_id,
                'calibrations': {
                    'kid': {'exists': bool(cal.get('kid')), 's3Key': cal.get('kid') or ''},
                    'parent1': {'exists': bool(cal.get('parent1')), 's3Key': cal.get('parent1') or ''},
                    'parent2': {'exists': bool(cal.get('parent2')), 's3Key': cal.get('parent2') or ''},
                },
                'userProfile': profile or {'kidAgeMonths': None},
            }
            return 200, body, None, 'application/json'
        if path in MOCK_ANALYTICS_PATHS:
            if not sql_enabled():
                if path == '/analytics/progression/pos-categories':
                    user_id = (query.get('userId') or [''])[-1]
                    return 200, {'userId': user_id, 'speaker': 'kid', 'unit': (query.get('unit') or ['day'])[-1], 'categories': _mock_pos_categories(user_id), 'source': 'local_mock'}, None, 'application/json'
                if path == '/analytics/semantic/network':
                    user_id = (query.get('userId') or [''])[-1]
                    return 200, _mock_semantic_network(user_id), None, 'application/json'
                if path == '/analytics/kid-percentile':
                    return 200, {'userId': (query.get('userId') or [''])[-1], 'available': False, 'message': 'Local SQL is disabled. Set LOCAL_POSTGRES_URL to enable analytics locally.'}, None, 'application/json'
                return 200, {'items': [], 'source': 'local_mock'}, None, 'application/json'
            return _relay_lambda('GET', path, query)
        if path == '/transcriptions':
            items = []
            for tx in STATE['transcripts'].values():
                stage = _transcript_stage(tx)
                items.append({'transcriptId': tx['transcriptId'], 'status': stage['status'], 'createdAt': tx['createdAt'], 'userId': tx['userId']})
            return 200, {'items': items}, None, 'application/json'

        match = re.fullmatch(r'/transcriptions/([^/]+)/status', path)
        if match:
            transcript_id = match.group(1)
            entry = STATE['transcripts'].get(transcript_id)
            if not entry:
                return 404, {'message': 'Transcript not found'}, None, 'application/json'
            stage = _transcript_stage(entry)
            if stage['status'] == 'COMPLETED':
                _finalize_transcript(entry)
            return 200, {
                'transcriptId': transcript_id,
                'status': stage['status'],
                'stage': stage['stage'],
                'message': stage['message'],
                'progressHint': stage['message'],
            }, None, 'application/json'

        match = re.fullmatch(r'/transcriptions/([^/]+)/interactions', path)
        if match:
            transcript_id = match.group(1)
            entry = STATE['transcripts'].get(transcript_id)
            if not entry:
                return 404, {'message': 'Transcript not found'}, None, 'application/json'
            stage = _transcript_stage(entry)
            if stage['status'] != 'COMPLETED':
                return 409, {'message': 'Transcript is not completed', 'status': stage['status']}, None, 'application/json'
            if not entry.get('finalized'):
                _finalize_transcript(entry)
            segments = entry['result'].get('segments') or []
            out = []
            prev = None
            for idx, seg in enumerate(segments):
                latency = None if not prev or prev.get('speakerName') == seg.get('speakerName') else max(0.0, float(seg['startTime']) - float(prev['endTime']))
                out.append({
                    'interactionIndex': idx,
                    'speakerLabel': seg.get('speakerLabel'),
                    'speakerName': seg.get('speakerName'),
                    'startTime': seg.get('startTime'),
                    'endTime': seg.get('endTime'),
                    'durationS': max(0.0, float(seg.get('endTime') or 0) - float(seg.get('startTime') or 0)),
                    'text': seg.get('text') or '',
                    'responseLatencyS': latency,
                    'previousSpeakerName': None if not prev else prev.get('speakerName'),
                })
                prev = seg
            return 200, {'items': out}, None, 'application/json'

        match = re.fullmatch(r'/transcriptions/([^/]+)', path)
        if match:
            transcript_id = match.group(1)
            entry = STATE['transcripts'].get(transcript_id)
            if not entry:
                return 404, {'message': 'Transcript not found'}, None, 'application/json'
            stage = _transcript_stage(entry)
            if stage['status'] != 'COMPLETED':
                return 409, {'message': 'Transcript is not completed', 'status': stage['status']}, None, 'application/json'
            if not entry.get('finalized'):
                _finalize_transcript(entry)
            return 200, entry['result'], None, 'application/json'

    if method == 'POST':
        if path == '/v1/asr/warmup':
            if body.get('visible', True):
                return 200, _touch_warm(body.get('trigger') or 'INTERACTION'), None, 'application/json'
            return 200, {'warm': False, 'gpuWarm': False, 'scope': 'global', 'mode': 'queue_service'}, None, 'application/json'

        if path == '/upload-url':
            upload_id = str(uuid.uuid4())
            dest = UPLOADS_ROOT / f'{upload_id}.bin'
            STATE['uploads'][upload_id] = {'path': str(dest), 'contentType': body.get('contentType') or 'application/octet-stream', 'stored': False}
            return 200, {'uploadUrl': f'http://127.0.0.1:{DEFAULT_PORT}/__local__/uploads/{upload_id}', 'audioS3Key': f'local/uploads/{upload_id}.bin'}, None, 'application/json'

        if path == '/v1/calibration/presign':
            role = str(body.get('role') or '').strip().lower()
            user_id = str(body.get('userId') or '').strip()
            if role not in ('kid', 'parent1', 'parent2'):
                return 400, {'message': 'role must be kid, parent1, or parent2'}, None, 'application/json'
            if not user_id:
                return 400, {'message': 'userId is required'}, None, 'application/json'
            if role == 'kid':
                months = _parse_kid_age_months(body.get('kidAgeMonths'))
                if months is None:
                    min_age, max_age = _kid_benchmark_month_range()
                    return 400, {'message': f'kidAgeMonths must be between {min_age} and {max_age}'}, None, 'application/json'
                if sql_enabled():
                    ensure_schema()
                    upsert_user_kid_age_months(user_id, months)
            upload_id = str(uuid.uuid4())
            dest = CALIBRATIONS_ROOT / user_id / role / f'{upload_id}.bin'
            dest.parent.mkdir(parents=True, exist_ok=True)
            STATE['uploads'][upload_id] = {'path': str(dest), 'contentType': body.get('contentType') or 'application/octet-stream', 'stored': False, 'calibrationUserId': user_id, 'calibrationRole': role}
            return 200, {'uploadUrl': f'http://127.0.0.1:{DEFAULT_PORT}/__local__/uploads/{upload_id}', 's3Key': f'local/calibrations/{user_id}/{role}'}, None, 'application/json'

        if path == '/transcriptions':
            user_id = str(body.get('userId') or '').strip()
            if not user_id:
                return 400, {'message': 'userId is required'}, None, 'application/json'
            _touch_warm('TRANSCRIPTION')
            transcript_id = str(uuid.uuid4())
            now = _now_iso()
            STATE['transcripts'][transcript_id] = {
                'transcriptId': transcript_id,
                'userId': user_id,
                'createdAt': now,
                'createdEpoch': time.time(),
                'effectiveDate': str(body.get('effectiveDate') or now[:10]),
                'userTimeZone': str(body.get('userTimeZone') or 'UTC'),
                'audioS3Key': body.get('audioS3Key') or f'local/uploads/{transcript_id}.bin',
                'finalized': False,
            }
            return 202, {
                'transcriptId': transcript_id,
                'jobName': f'local-{transcript_id}',
                'engine': 'whisper',
                'workerMode': 'QUEUE_SERVICE',
                'workerCapacityType': 'LOCAL_GPU_MOCK',
                'message': 'API (local): Job queued to GPU worker service.',
            }, None, 'application/json'

    if method == 'PUT':
        match = re.fullmatch(r'/__local__/uploads/([^/]+)', path)
        if not match:
            return 404, {'message': 'Not found'}, None, 'application/json'
        upload_id = match.group(1)
        meta = STATE['uploads'].get(upload_id)
        if not meta:
            return 404, {'message': 'Unknown upload target'}, None, 'application/json'
        dest = Path(meta['path'])
        dest.parent.mkdir(parents=True, exist_ok=True)
        length = int(handler.headers.get('Content-Length') or '0')
        data = handler.rfile.read(length) if length > 0 else b''
        dest.write_bytes(data)
        meta['stored'] = True
        if meta.get('calibrationUserId') and meta.get('calibrationRole'):
            user_cals = STATE['calibrations'].setdefault(meta['calibrationUserId'], {})
            user_cals[meta['calibrationRole']] = f"local/calibrations/{meta['calibrationUserId']}/{meta['calibrationRole']}"
        return 200, b'', {'Content-Length': '0'}, 'text/plain'

    return 404, {'message': 'Not found'}, None, 'application/json'


class LocalApiHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        return

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if method in {'POST', 'PATCH'}:
            body = _json_body(self)
        else:
            body = None
        try:
            if LOCAL_API_MODE == 'cloud':
                status, payload, extra_headers, content_type = _handle_cloud_request(method, path, query, body=body)
            else:
                status, payload, extra_headers, content_type = _handle_mock_request(self, method, path, query, body=body)
        except Exception as exc:
            status = 500
            payload = {'message': 'Local API error', 'detail': str(exc), 'mode': LOCAL_API_MODE, 'path': path}
            extra_headers = None
            content_type = 'application/json'
        return _send(self, status, payload, extra_headers=extra_headers, content_type=content_type)

    def do_OPTIONS(self):
        _send(self, 204, b'', content_type='text/plain')

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')


def main():
    if LOCAL_API_MODE == 'mock' and sql_enabled():
        ensure_schema()
    if LOCAL_API_MODE == 'cloud':
        _validate_cloud_env()
        _get_app_module()
    server = ThreadingHTTPServer(('127.0.0.1', DEFAULT_PORT), LocalApiHandler)
    print(json.dumps({'ok': True, 'message': 'Local API server started', 'port': DEFAULT_PORT, 'sqlEnabled': sql_enabled(), 'mode': LOCAL_API_MODE}))
    server.serve_forever()


if __name__ == '__main__':
    main()
