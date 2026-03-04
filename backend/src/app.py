import json
import os
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

try:
    from aws_xray_sdk.core import xray_recorder, patch_all
except Exception:  # pragma: no cover - optional dependency in fast API-only deploys
    xray_recorder = None

    def patch_all():
        return None
patch_all()

import boto3
from boto3.dynamodb.conditions import Key
from sql_store import (
    compute_kid_percentile,
    compute_kid_percentile_smoothed,
    ensure_schema,
    ensure_wordbank_seeded,
    get_kid_benchmark_age_range,
    get_user_profile,
    get_transcript_interactions,
    pos_category_label,
    query_daily,
    query_latency,
    query_monthly,
    query_monthly_calendar,
    query_pos_categories,
    query_semantic_edges,
    query_semantic_eligible_words,
    query_transcript_words,
    query_weekly,
    sql_enabled,
    upsert_user_kid_age_months,
    upsert_transcript_record,
)
from runtime_config import build_app_config_payload

ddb = boto3.resource('dynamodb')
ecs = boto3.client('ecs')
autoscaling = boto3.client('autoscaling')
lambda_client = boto3.client('lambda')
sqs = boto3.client('sqs')

table = ddb.Table(os.environ['TRANSCRIPTS_TABLE'])
UPLOADS_BUCKET = os.environ['UPLOADS_BUCKET']
ARTIFACTS_BUCKET = os.environ['ARTIFACTS_BUCKET']
TRANSCRIPT_ID_INDEX = os.environ.get('TRANSCRIPT_ID_INDEX', 'TranscriptIdIndex')

# Generate presigned URLs against the regional S3 endpoint.
# The global endpoint (bucket.s3.amazonaws.com) has been observed to return 500
# on CORS preflight in browsers for this workload.
AWS_REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-west-1'
s3 = boto3.client(
    's3',
    region_name=AWS_REGION,
    endpoint_url=f'https://s3.{AWS_REGION}.amazonaws.com',
)

ALLOWED_TYPES = {
    'audio/mpeg',
    'audio/wav',
    'audio/x-wav',
    'audio/mp4',
    'audio/aac',
    'audio/flac',
    'audio/ogg',
    'audio/webm',
    'video/mp4',
}
EMPTY_USER_KEY = '__EMPTY__'
_SQL_SCHEMA_READY = False

CALIBRATION_BUCKET = os.environ.get('CALIBRATION_BUCKET') or UPLOADS_BUCKET
SPEAKER_ID_FUNCTION_NAME = os.environ.get('SPEAKER_ID_FUNCTION_NAME') or ''
ASR_ENGINE = (os.environ.get('ASR_ENGINE') or 'whisper').strip().lower()
ASR_DISPATCH_MODE = (os.environ.get('ASR_DISPATCH_MODE') or 'queue_service').strip().lower()
ASR_JOBS_QUEUE_URL = os.environ.get('ASR_JOBS_QUEUE_URL') or ''
ASR_WARM_STATE_TABLE = os.environ.get('ASR_WARM_STATE_TABLE') or ''
ASR_WORKER_SERVICE_NAME = os.environ.get('ASR_WORKER_SERVICE_NAME') or ''
ASR_WARM_WINDOW_SECONDS = max(60, int(os.environ.get('ASR_WARM_WINDOW_SECONDS') or '300'))
ASR_WARM_SCOPE = 'global'
ECS_CLUSTER_ARN = os.environ.get('ECS_CLUSTER_ARN') or ''
ECS_TASK_DEF_ARN = os.environ.get('ECS_TASK_DEF_ARN') or ''
ECS_CONTAINER_NAME = os.environ.get('ECS_CONTAINER_NAME') or 'asr-worker'
ECS_SUBNET_IDS = [s.strip() for s in (os.environ.get('ECS_SUBNET_IDS') or '').split(',') if s.strip()]
ECS_SECURITY_GROUP_IDS = [s.strip() for s in (os.environ.get('ECS_SECURITY_GROUP_IDS') or '').split(',') if s.strip()]
ECS_TASK_ASSIGN_PUBLIC_IP = (os.environ.get('ECS_TASK_ASSIGN_PUBLIC_IP') or 'ENABLED').upper()
ECS_GPU_TASK_NETWORK_MODE = (os.environ.get('ECS_GPU_TASK_NETWORK_MODE') or 'awsvpc').strip().lower()
ECS_GPU_SPOT_CAPACITY_PROVIDER = os.environ.get('ECS_GPU_SPOT_CAPACITY_PROVIDER') or ''
ECS_GPU_ONDEMAND_CAPACITY_PROVIDER = os.environ.get('ECS_GPU_ONDEMAND_CAPACITY_PROVIDER') or ''
ASR_GPU_SPOT_ASG_NAME = os.environ.get('ASR_GPU_SPOT_ASG_NAME') or ''
ASR_GPU_ONDEMAND_ASG_NAME = os.environ.get('ASR_GPU_ONDEMAND_ASG_NAME') or ''
ECS_FARGATE_TASK_DEF_ARN = os.environ.get('ECS_FARGATE_TASK_DEF_ARN') or ''
ECS_PREFER_GPU = (os.environ.get('ECS_PREFER_GPU') or 'false').strip().lower() in ('1', 'true', 'yes', 'y')
warm_state_table = ddb.Table(ASR_WARM_STATE_TABLE) if ASR_WARM_STATE_TABLE else None
GPU_ONLY_PIPELINE = (os.environ.get('GPU_ONLY_PIPELINE') or 'false').strip().lower() in ('1', 'true', 'yes', 'y')
ASR_ACTIVITY_TOUCH_THROTTLE_SECONDS = max(5, int(os.environ.get('ASR_ACTIVITY_TOUCH_THROTTLE_SECONDS') or '30'))

ALLOWED_CAL_ROLES = {'kid', 'parent1', 'parent2'}
DEFAULT_KID_BENCHMARK_MIN_MONTHS = 8
DEFAULT_KID_BENCHMARK_MAX_MONTHS = 30

STAGE_COLD_START = 'COLD_START'
STAGE_ASR = 'ASR'
STAGE_DIARIZATION = 'DIARIZATION'
STAGE_SPEAKER_IDENTIFICATION = 'SPEAKER_IDENTIFICATION'
STAGE_FINALIZING = 'FINALIZING'
STAGE_COMPLETED = 'COMPLETED'
STAGE_FAILED = 'FAILED'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_epoch():
    return int(time.time())


def epoch_to_iso(epoch_seconds):
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat()


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        },
        'body': json.dumps(body),
    }


def parse_body(event):
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        import base64
        raw = base64.b64decode(raw).decode('utf-8')
    return json.loads(raw)


def _asr_runtime_label(item, stage):
    worker_mode = (item.get('workerMode') or '').upper()
    worker_capacity = (item.get('workerCapacityType') or '').upper()

    if stage == STAGE_SPEAKER_IDENTIFICATION:
        if GPU_ONLY_PIPELINE:
            return 'GPU worker'
        return 'Lambda (Speaker ID)'
    if stage in (STAGE_COMPLETED, STAGE_FAILED):
        # Preserve last meaningful runtime hint if available.
        if worker_capacity in ('SPOT', 'ON_DEMAND'):
            return f'GPU worker ({worker_capacity})'
        if worker_capacity in ('FARGATE_FALLBACK', 'FARGATE_DEFAULT'):
            return 'CPU worker (Fargate)'
        if worker_mode == 'QUEUE_SERVICE':
            return 'GPU worker (queue service)' if GPU_ONLY_PIPELINE else 'CPU worker (queue service)'
        return 'Lambda/API'

    if stage == STAGE_COLD_START:
        if worker_mode == 'QUEUE_SERVICE':
            return 'Lambda/API -> GPU queue service' if GPU_ONLY_PIPELINE else 'Lambda/API -> CPU queue service'
        if worker_capacity in ('SPOT', 'ON_DEMAND'):
            return f'Lambda/API -> GPU worker ({worker_capacity})'
        if worker_capacity in ('FARGATE_FALLBACK', 'FARGATE_DEFAULT'):
            return 'Lambda/API -> CPU worker (Fargate)'
        if worker_mode in ('RUN_TASK', 'RUN_TASK_FALLBACK'):
            return 'Lambda/API -> ECS task placement'
        return 'Lambda/API'

    if stage in (STAGE_ASR, STAGE_DIARIZATION, STAGE_FINALIZING):
        if worker_capacity in ('SPOT', 'ON_DEMAND'):
            return f'GPU worker ({worker_capacity})'
        if worker_capacity in ('FARGATE_FALLBACK', 'FARGATE_DEFAULT'):
            return 'CPU worker (Fargate)'
        if worker_mode == 'QUEUE_SERVICE':
            return 'GPU worker (queue service)' if GPU_ONLY_PIPELINE else 'CPU worker (queue service)'
        return 'ASR worker'

    return 'Lambda/API'


def _format_runtime_message(item, stage, base_message):
    msg = (base_message or '').strip()
    if not msg:
        return msg
    runtime = _asr_runtime_label(item, stage or '')
    return f'{runtime}: {msg}' if runtime else msg


def normalize_user_key(user_id):
    return EMPTY_USER_KEY if (user_id is None or user_id == '') else user_id


def denormalize_user_key(user_key):
    return '' if user_key == EMPTY_USER_KEY else user_key


def safe_filename(name):
    return re.sub(r'[^A-Za-z0-9._-]', '_', name)


def normalize_content_type(content_type):
    return (content_type or '').split(';')[0].strip().lower()


def kid_benchmark_month_range():
    if sql_enabled():
        try:
            r = get_kid_benchmark_age_range()
            if r and r.get('minAgeMonths') is not None and r.get('maxAgeMonths') is not None:
                return int(r['minAgeMonths']), int(r['maxAgeMonths'])
        except Exception as e:
            print('kid_benchmark_month_range lookup failed:', repr(e))
    return DEFAULT_KID_BENCHMARK_MIN_MONTHS, DEFAULT_KID_BENCHMARK_MAX_MONTHS


def handle_app_config(_event):
    min_months, max_months = kid_benchmark_month_range()
    return response(
        200,
        build_app_config_payload(
            kid_benchmark_min_months=min_months,
            kid_benchmark_max_months=max_months,
            warm_window_seconds=ASR_WARM_WINDOW_SECONDS,
            engine=ASR_ENGINE,
            dispatch_mode=ASR_DISPATCH_MODE,
            gpu_enabled=bool(ASR_GPU_SPOT_ASG_NAME or ASR_GPU_ONDEMAND_ASG_NAME),
            gpu_only_pipeline=GPU_ONLY_PIPELINE,
        ),
    )


def parse_kid_age_months(value):
    if value is None or value == '':
        return None
    try:
        months = int(value)
    except Exception:
        return None
    min_age, max_age = kid_benchmark_month_range()
    if months < min_age or months > max_age:
        return None
    return months


def safe_id(value):
    # Keep it simple: alnum, dash, underscore. (No slashes.)
    return re.sub(r'[^A-Za-z0-9_-]', '_', value or '')

def calibration_s3_key(role, user_id, file_name):
    # One folder per user, and only 3 objects maximum: kid, parent1, parent2 (overwrite allowed).
    safe_user = safe_id(user_id or '')
    return f"calibrations/{safe_user}/{role}"


def get_item_by_transcript_id(transcript_id):
    res = table.query(
        IndexName=TRANSCRIPT_ID_INDEX,
        KeyConditionExpression=Key('transcriptId').eq(transcript_id),
        Limit=1,
    )
    items = res.get('Items', [])
    return items[0] if items else None


def decimalize(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [decimalize(v) for v in value]
    if isinstance(value, dict):
        return {k: decimalize(v) for k, v in value.items()}
    return value


def build_segments(raw_json):
    items = raw_json.get('results', {}).get('items', [])
    speaker_map = {}
    for seg in raw_json.get('results', {}).get('speaker_labels', {}).get('segments', []):
        speaker_label = seg.get('speaker_label', 'spk_unknown')
        for si in seg.get('items', []):
            st = si.get('start_time')
            if st is not None:
                speaker_map[st] = speaker_label

    segments = []
    current = None
    for it in items:
        kind = it.get('type')
        alternatives = it.get('alternatives', [])
        if not alternatives:
            continue
        content = alternatives[0].get('content', '')

        if kind == 'pronunciation':
            st = float(it.get('start_time', 0.0))
            et = float(it.get('end_time', st))
            st_key = it.get('start_time')
            speaker = speaker_map.get(st_key, 'spk_unknown')

            if current and current['speakerLabel'] == speaker:
                current['text'] += (' ' if current['text'] else '') + content
                current['endTime'] = et
            else:
                if current:
                    segments.append(current)
                current = {
                    'speakerLabel': speaker,
                    'startTime': st,
                    'endTime': et,
                    'text': content,
                }
        elif kind == 'punctuation' and current:
            current['text'] += content

    if current:
        segments.append(current)

    # Default speaker names Speaker 1..N based on discovered label order.
    discovered = []
    for s in segments:
        if s['speakerLabel'] not in discovered:
            discovered.append(s['speakerLabel'])
    speaker_names = {label: f'Speaker {idx + 1}' for idx, label in enumerate(discovered)}
    for s in segments:
        s['speakerName'] = speaker_names.get(s['speakerLabel'], 'Speaker')

    return segments


def compute_stats(segments):
    per_speaker_words = defaultdict(list)
    per_speaker_name = {}

    for seg in segments:
        speaker = seg['speakerLabel']
        if seg.get('speakerName'):
            per_speaker_name[speaker] = seg.get('speakerName')
        tokens = re.findall(r"\b[\w']+\b", seg.get('text', '').lower())
        per_speaker_words[speaker].extend(tokens)

    stats = []
    for speaker, words in per_speaker_words.items():
        counts = Counter(words)
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        stats.append({
            'speakerLabel': speaker,
            'speakerName': per_speaker_name.get(speaker, ''),
            'uniqueWordCount': len(set(words)),
            'topWords': [{'word': w, 'count': c} for w, c in top],
        })
    return stats


def build_full_text(segments):
    lines = []
    for seg in segments:
        who = seg.get('speakerName') or seg.get('speakerLabel')
        lines.append(
            f"[{seg['startTime']:.2f}-{seg['endTime']:.2f}] {who}: {seg['text']}"
        )
    return '\n'.join(lines)


def _parse_day(created_at_iso):
    # createdAt is stored as ISO8601 with offset (or Z). Use UTC date.
    if not created_at_iso:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        s = str(created_at_iso).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _parse_effective_date(value):
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def _normalize_tz(value):
    raw = (value or '').strip()
    if not raw:
        return 'UTC'
    try:
        ZoneInfo(raw)
        return raw
    except Exception:
        return 'UTC'


def _today_for_tz(tz_name):
    tz = _normalize_tz(tz_name)
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _resolve_logical_day(effective_date_raw, user_timezone_raw):
    tz_name = _normalize_tz(user_timezone_raw)
    parsed_effective = _parse_effective_date(effective_date_raw)
    if parsed_effective:
        return {
            'effectiveDate': parsed_effective.isoformat(),
            'userTimeZone': tz_name,
            'logicalDay': parsed_effective.isoformat(),
        }
    return {
        'effectiveDate': None,
        'userTimeZone': tz_name,
        'logicalDay': _today_for_tz(tz_name).isoformat(),
    }


def calibration_exists_for_user(display_user_id):
    # Login is mandatory; treat blank as no calibration.
    if not display_user_id:
        return {'kid': False, 'parent1': False, 'parent2': False}

    out = {}
    for role in ('kid', 'parent1', 'parent2'):
        key = calibration_s3_key(role, display_user_id, '')
        try:
            s3.head_object(Bucket=CALIBRATION_BUCKET, Key=key)
            out[role] = True
        except Exception as e:
            # boto3 raises ClientError for missing keys with 404/NoSuchKey
            code = None
            try:
                code = e.response.get('Error', {}).get('Code')
            except Exception:
                code = None
            out[role] = False
    return out


def auto_assign_speaker_names(segments, available_roles):
    # available_roles: dict role->bool
    # Output speakerName set per segment. This is a heuristic (not voice recognition).
    labels = []
    for s in segments:
        lbl = s.get('speakerLabel')
        if lbl and lbl not in labels:
            labels.append(lbl)

    # Defaults Speaker 1..N.
    speaker_names = {label: f'Speaker {idx + 1}' for idx, label in enumerate(labels)}

    # Choose candidates based on calibration availability.
    cal_names = []
    if available_roles.get('kid'):
        cal_names.append('Kid')
    if available_roles.get('parent1'):
        cal_names.append('Parent 1')
    if available_roles.get('parent2'):
        cal_names.append('Parent 2')
    if not cal_names:
        for s in segments:
            s['speakerName'] = speaker_names.get(s.get('speakerLabel'), 'Speaker')
        return segments

    # Build per-speaker heuristics from transcript text/duration.
    per = {}
    for seg in segments:
        lbl = seg.get('speakerLabel')
        if not lbl:
            continue
        st = float(seg.get('startTime') or 0.0)
        et = float(seg.get('endTime') or st)
        dur = max(0.0, et - st)
        words = re.findall(r"\b[\w']+\b", (seg.get('text') or '').lower())
        if lbl not in per:
            per[lbl] = {'dur': 0.0, 'words': 0, 'utterances': 0}
        per[lbl]['dur'] += dur
        per[lbl]['words'] += len(words)
        per[lbl]['utterances'] += 1

    # Assign Kid to the speaker with the smallest average utterance length (kid tends to speak in shorter bursts).
    remaining = set(labels)
    mapping = {}
    if 'Kid' in cal_names and remaining:
        def kid_score(lbl):
            d = per.get(lbl) or {'words': 0, 'utterances': 1}
            return (d['words'] / max(1, d['utterances']))
        kid_lbl = sorted(list(remaining), key=kid_score)[0]
        mapping[kid_lbl] = 'Kid'
        remaining.remove(kid_lbl)

    # Assign Parent(s) to the most talkative remaining speakers (by duration).
    def talk_score(lbl):
        d = per.get(lbl) or {'dur': 0.0, 'words': 0}
        return (-d['dur'], -d['words'], lbl)

    parents = [n for n in cal_names if n.startswith('Parent')]
    for name in parents:
        if not remaining:
            break
        best = sorted(list(remaining), key=lambda l: talk_score(l))[0]
        mapping[best] = name
        remaining.remove(best)

    # Apply mapping; others keep Speaker N.
    for seg in segments:
        lbl = seg.get('speakerLabel')
        seg['speakerName'] = mapping.get(lbl, speaker_names.get(lbl, 'Speaker'))
    return segments


def process_completed_transcription(item):
    transcribe_client = boto3.client('transcribe')
    tx = transcribe_client.get_transcription_job(TranscriptionJobName=item['jobName'])
    job = tx.get('TranscriptionJob', {})
    status = job.get('TranscriptionJobStatus')

    if status == 'FAILED':
        reason = job.get('FailureReason', 'Unknown error')
        table.update_item(
            Key={'userId': item['userId'], 'transcriptId': item['transcriptId']},
            UpdateExpression='SET #s = :s, message = :m, updatedAt = :u, processingStage = :ps, processingStageUpdatedAt = :u',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'FAILED', ':m': f'Failed: {reason}', ':u': now_iso(), ':ps': STAGE_FAILED},
        )
        return {'status': 'FAILED', 'message': reason}

    if status != 'COMPLETED':
        return {'status': 'IN_PROGRESS', 'progressHint': f'Transcribe status: {status}'}

    transcript_uri = job.get('Transcript', {}).get('TranscriptFileUri')
    if not transcript_uri:
        return {'status': 'FAILED', 'message': 'Missing TranscriptFileUri'}

    # Transcribe writes output directly to our artifacts bucket (OutputBucketName/OutputKey).
    # TranscriptFileUri is typically an unauthenticated HTTPS URL that can 403 for private buckets,
    # so we read the object from S3 with IAM instead.
    transcript_id = item['transcriptId']
    raw_key = item.get('transcriptJsonS3Key') or f'transcripts/{transcript_id}/raw.json'
    obj = s3.get_object(Bucket=ARTIFACTS_BUCKET, Key=raw_key)
    raw_json = json.loads(obj['Body'].read().decode('utf-8'))

    segments = build_segments(raw_json)
    display_user_id = item.get('displayUserId', denormalize_user_key(item.get('userId', '')))
    cal = calibration_exists_for_user(display_user_id)
    num_speakers = len({s['speakerLabel'] for s in segments})

    # If we have a real speaker-id worker configured, run an async labeling phase.
    if SPEAKER_ID_FUNCTION_NAME and any(cal.values()):
        stats = compute_stats(segments)
        full_text = build_full_text(segments)
        table.update_item(
            Key={'userId': item['userId'], 'transcriptId': transcript_id},
            UpdateExpression=(
                'SET #s = :s, updatedAt = :u, transcriptJsonS3Key = :tk, '
                'numSpeakers = :n, speakerStats = :sp, #seg = :sg, fullText = :ft, '
                'labelingStartedAt = if_not_exists(labelingStartedAt, :u), processingStage = :ps, processingStageUpdatedAt = :u, message = :m'
            ),
            ExpressionAttributeNames={'#s': 'status', '#seg': 'segments'},
            ExpressionAttributeValues=decimalize({
                ':s': 'LABELING',
                ':u': now_iso(),
                ':tk': raw_key,
                ':n': num_speakers,
                ':sp': stats,
                ':sg': segments,
                ':ft': full_text,
                ':ps': STAGE_SPEAKER_IDENTIFICATION,
                ':m': 'Speaker identification: matching Kid/Parent calibrations...',
            }),
        )
        try:
            lambda_client.invoke(
                FunctionName=SPEAKER_ID_FUNCTION_NAME,
                InvocationType='Event',
                Payload=json.dumps({'transcriptId': transcript_id}).encode('utf-8'),
            )
        except Exception:
            pass
        return {'status': 'IN_PROGRESS', 'progressHint': 'Identifying speakers...'}

    # Fallback: finish immediately with best-effort heuristic names.
    segments = auto_assign_speaker_names(segments, cal)
    stats = compute_stats(segments)
    full_text = build_full_text(segments)
    table.update_item(
        Key={'userId': item['userId'], 'transcriptId': transcript_id},
        UpdateExpression=(
            'SET #s = :s, updatedAt = :u, transcriptJsonS3Key = :tk, '
            'numSpeakers = :n, speakerStats = :sp, #seg = :sg, fullText = :ft, message = :m, processingStage = :ps, processingStageUpdatedAt = :u'
        ),
        ExpressionAttributeNames={'#s': 'status', '#seg': 'segments'},
        ExpressionAttributeValues=decimalize({
            ':s': 'COMPLETED',
            ':u': now_iso(),
            ':tk': raw_key,
            ':n': num_speakers,
            ':sp': stats,
            ':sg': segments,
            ':ft': full_text,
            ':m': 'Transcription complete.',
            ':ps': STAGE_COMPLETED,
        }),
    )
    try:
        completed_item = get_item_by_transcript_id(transcript_id)
        if completed_item:
            _request_sql_sync_if_needed(completed_item)
    except Exception as e:
        print('Post-completion SQL sync failed:', repr(e))
    return {'status': 'COMPLETED'}


def handle_upload_url(event):
    body = parse_body(event)
    file_name = body.get('fileName')
    content_type = body.get('contentType')

    if not file_name or not content_type:
        return response(400, {'message': 'fileName and contentType are required'})
    if normalize_content_type(content_type) not in ALLOWED_TYPES:
        return response(400, {'message': 'Unsupported contentType'})

    key = f"uploads/{uuid.uuid4()}-{safe_filename(file_name)}"
    upload_url = s3.generate_presigned_url(
        ClientMethod='put_object',
        Params={
            'Bucket': UPLOADS_BUCKET,
            'Key': key,
            'ContentType': content_type,
        },
        ExpiresIn=900,
    )
    return response(200, {'uploadUrl': upload_url, 's3Key': key})

def handle_calibration_presign(event):
    body = parse_body(event)
    role = (body.get('role') or '').strip().lower()
    user_id = (body.get('userId') or '').strip()
    file_name = body.get('fileName')
    content_type = body.get('contentType')
    effective_date = body.get('effectiveDate')
    user_timezone = body.get('userTimeZone')
    kid_age_months = body.get('kidAgeMonths')

    if role not in ALLOWED_CAL_ROLES:
        return response(400, {'message': 'Invalid role'})
    if not user_id:
        return response(400, {'message': 'Login required'})
    if not file_name or not content_type:
        return response(400, {'message': 'fileName and contentType are required'})
    if not content_type.startswith('audio/'):
        return response(400, {'message': 'contentType must be audio/*'})
    if role == 'kid':
        months = parse_kid_age_months(kid_age_months)
        if months is None:
            min_age, max_age = kid_benchmark_month_range()
            return response(400, {'message': f'kidAgeMonths is required for kid calibration and must be between {min_age} and {max_age}'})
        if not sql_enabled():
            return response(503, {'message': 'Kid calibration requires user profile storage, but SQL user store is unavailable'})
        try:
            upsert_user_kid_age_months(user_id, months)
        except Exception as e:
            return response(503, {'message': 'Kid calibration requires user profile storage, but SQL user store is unavailable', 'detail': str(e)})

    key = calibration_s3_key(role, user_id, file_name)
    metadata = {
        'role': role,
        'userid': safe_id(user_id),
        'uploadedat': now_iso(),
        'originalfilename': safe_filename(file_name),
    }
    logical = _resolve_logical_day(effective_date, user_timezone)
    metadata['effectivedate'] = logical['logicalDay']
    metadata['usertimezone'] = logical['userTimeZone']
    upload_url = s3.generate_presigned_url(
        ClientMethod='put_object',
        Params={
            'Bucket': CALIBRATION_BUCKET,
            'Key': key,
            'ContentType': content_type,
            'Metadata': metadata,
        },
        ExpiresIn=900,
    )
    # Frontend must send these x-amz-meta-* headers with the PUT, because they're part of the signature.
    return response(200, {'uploadUrl': upload_url, 'bucket': CALIBRATION_BUCKET, 's3Key': key, 'metadata': metadata})

def handle_calibration_status(event):
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})

    safe_user = safe_id(user_id)
    if not safe_user:
        return response(400, {'message': 'Invalid userId'})

    out = {'userId': user_id, 'calibrations': {}, 'userProfile': {'kidAgeMonths': None}}
    if sql_enabled():
        try:
            profile = get_user_profile(user_id) or {}
            out['userProfile'] = {'kidAgeMonths': profile.get('kidAgeMonths')}
        except Exception:
            out['userProfile'] = {'kidAgeMonths': None}
    for role in sorted(ALLOWED_CAL_ROLES):
        key = f"calibrations/{safe_user}/{role}"
        exists = False
        try:
            s3.head_object(Bucket=CALIBRATION_BUCKET, Key=key)
            exists = True
        except Exception:
            exists = False
        out['calibrations'][role] = {'exists': exists, 's3Key': key}

    return response(200, out)


def _int_or_default(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _service_name_or_arn():
    if not ASR_WORKER_SERVICE_NAME:
        raise RuntimeError('ASR worker service name is not configured')
    return ASR_WORKER_SERVICE_NAME


def _ensure_warm_state_config():
    if not warm_state_table:
        raise RuntimeError('ASR warm-state table is not configured')
    if not ECS_CLUSTER_ARN:
        raise RuntimeError('ECS cluster is not configured')
    if not ASR_WORKER_SERVICE_NAME:
        raise RuntimeError('ASR worker service is not configured')


def _get_gpu_instance_state_counts():
    asg_names = [n for n in [ASR_GPU_SPOT_ASG_NAME, ASR_GPU_ONDEMAND_ASG_NAME] if n]
    if not asg_names:
        return {'active': 0, 'activating': 0}
    try:
        resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=asg_names)
        groups = resp.get('AutoScalingGroups') or []
        active = 0
        activating = 0
        for g in groups:
            for inst in g.get('Instances') or []:
                state = (inst.get('LifecycleState') or '').lower()
                if state == 'inservice':
                    active += 1
                    continue
                if state.startswith('terminating') or state in ('standby', 'detached'):
                    continue
                if state:
                    activating += 1
        return {'active': active, 'activating': activating}
    except Exception:
        return {'active': 0, 'activating': 0}


def _get_gpu_running_instances():
    return _get_gpu_instance_state_counts().get('active', 0)


def _is_gpu_task(task):
    launch_type = (task.get('launchType') or '').upper()
    cp_name = (task.get('capacityProviderName') or '').upper()
    if launch_type == 'EC2':
        return True
    if cp_name and 'GPU' in cp_name:
        return True
    return False


def _get_asr_capacity_counts():
    if not ECS_CLUSTER_ARN:
        return {
            'cpu': {'active': 0, 'activating': 0, 'busy': 0},
            'gpu': {'active': 0, 'activating': 0, 'busy': 0},
        }
    gpu_instances = _get_gpu_instance_state_counts()
    cpu_active = 0
    cpu_activating = 0
    cpu_busy = 0
    gpu_busy_container_instances = set()
    gpu_busy_fallback_tasks = 0

    try:
        running_task_arns = []
        pending_task_arns = []
        paginator = ecs.get_paginator('list_tasks')
        for page in paginator.paginate(cluster=ECS_CLUSTER_ARN, desiredStatus='RUNNING'):
            running_task_arns.extend(page.get('taskArns') or [])
        for page in paginator.paginate(cluster=ECS_CLUSTER_ARN, desiredStatus='PENDING'):
            pending_task_arns.extend(page.get('taskArns') or [])

        for task_arns, bucket in ((running_task_arns, 'RUNNING'), (pending_task_arns, 'PENDING')):
            for i in range(0, len(task_arns), 100):
                batch = task_arns[i:i + 100]
                if not batch:
                    continue
                desc = ecs.describe_tasks(cluster=ECS_CLUSTER_ARN, tasks=batch)
                for task in desc.get('tasks') or []:
                    is_gpu = _is_gpu_task(task)
                    if bucket == 'RUNNING':
                        if is_gpu:
                            ci_arn = task.get('containerInstanceArn') or ''
                            if ci_arn:
                                gpu_busy_container_instances.add(ci_arn)
                            else:
                                gpu_busy_fallback_tasks += 1
                        else:
                            cpu_active += 1
                            cpu_busy += 1
                    elif not is_gpu:
                        cpu_activating += 1
    except Exception as e:
        print(json.dumps({'event': 'AsrRuntimeStatusTaskCountFailed', 'error': str(e)}))

    gpu_busy = len(gpu_busy_container_instances) + gpu_busy_fallback_tasks
    if ASR_DISPATCH_MODE == 'queue_service':
        active_jobs = 0
        try:
            if warm_state_table:
                state_item = warm_state_table.get_item(Key={'scope': ASR_WARM_SCOPE}).get('Item') or {}
                active_jobs = max(0, _int_or_default(state_item.get('activeJobs')))
        except Exception as e:
            print(json.dumps({'event': 'AsrRuntimeStatusActiveJobsLookupFailed', 'error': str(e)}))
            active_jobs = 0
        gpu_active = _int_or_default(gpu_instances.get('active'))
        cpu_busy = min(cpu_active, active_jobs)
        gpu_busy = min(gpu_active, active_jobs)

    return {
        'cpu': {'active': cpu_active, 'activating': cpu_activating, 'busy': cpu_busy},
        'gpu': {
            'active': _int_or_default(gpu_instances.get('active')),
            'activating': _int_or_default(gpu_instances.get('activating')),
            'busy': gpu_busy,
        },
    }


def handle_asr_runtime_status(_event):
    warm_until = None
    gpu_warm_until = None
    queue_visible = 0
    queue_not_visible = 0
    try:
        if warm_state_table:
            item = warm_state_table.get_item(Key={'scope': ASR_WARM_SCOPE}).get('Item') or {}
            warm_epoch = _int_or_default(item.get('warmUntilEpoch'))
            if warm_epoch > 0:
                warm_until = epoch_to_iso(warm_epoch)
            gpu_warm_epoch = _int_or_default(item.get('gpuWarmUntilEpoch'))
            if gpu_warm_epoch > 0:
                gpu_warm_until = epoch_to_iso(gpu_warm_epoch)
            queue_visible = _int_or_default(item.get('queueVisible'))
            queue_not_visible = _int_or_default(item.get('queueNotVisible'))
    except Exception:
        warm_until = None
        gpu_warm_until = None
        queue_visible = 0
        queue_not_visible = 0

    counts = _get_asr_capacity_counts()
    cpu = counts.get('cpu') or {}
    gpu = counts.get('gpu') or {}
    return response(
        200,
        {
            'cpu': {
                'active': _int_or_default(cpu.get('active')),
                'activating': _int_or_default(cpu.get('activating')),
                'busy': _int_or_default(cpu.get('busy')),
                'semantics': 'worker_capacity',
                # legacy fields (temporary)
                'running': _int_or_default(cpu.get('active')),
                'warming': _int_or_default(cpu.get('activating')),
                'used': _int_or_default(cpu.get('busy')),
            },
            'gpu': {
                'active': _int_or_default(gpu.get('active')),
                'activating': _int_or_default(gpu.get('activating')),
                'busy': _int_or_default(gpu.get('busy')),
                'semantics': 'instances',
                # legacy fields (temporary)
                'running': _int_or_default(gpu.get('active')),
                'warming': _int_or_default(gpu.get('activating')),
                'used': _int_or_default(gpu.get('busy')),
            },
            'gpuEnabled': bool(ASR_GPU_SPOT_ASG_NAME or ASR_GPU_ONDEMAND_ASG_NAME),
            'gpuOnlyPipeline': GPU_ONLY_PIPELINE,
            'warmScope': ASR_WARM_SCOPE,
            'warmUntil': warm_until,
            'gpuWarmUntil': gpu_warm_until,
            'gpuQueueVisible': queue_visible,
            'gpuQueueNotVisible': queue_not_visible,
        },
    )


def _touch_warm_window(trigger):
    _ensure_warm_state_config()
    now = now_epoch()
    warm_until_epoch = now + ASR_WARM_WINDOW_SECONDS
    key = {'scope': ASR_WARM_SCOPE}
    existing = warm_state_table.get_item(Key=key).get('Item') or {}
    prev_warm = _int_or_default(existing.get('warmUntilEpoch'))
    active_jobs = max(0, _int_or_default(existing.get('activeJobs')))
    updated_at = now_iso()

    warm_state_table.put_item(
        Item={
            'scope': ASR_WARM_SCOPE,
            'warmUntilEpoch': warm_until_epoch,
            'activeJobs': active_jobs,
            'updatedAt': updated_at,
            'lastTrigger': trigger,
            'lastTriggeredAt': updated_at,
        }
    )

    ecs.update_service(
        cluster=ECS_CLUSTER_ARN,
        service=_service_name_or_arn(),
        desiredCount=1,
    )

    print(
        json.dumps(
            {
                'event': 'WarmWindowTouched',
                'trigger': trigger,
                'previousWarmUntilEpoch': prev_warm,
                'newWarmUntilEpoch': warm_until_epoch,
                'desiredCount': 1,
            }
        )
    )
    return {
        'scope': ASR_WARM_SCOPE,
        'warmUntilEpoch': warm_until_epoch,
        'warmUntil': epoch_to_iso(warm_until_epoch),
        'desiredCount': 1,
        'windowSeconds': ASR_WARM_WINDOW_SECONDS,
    }


def _touch_gpu_warm_window(trigger):
    if not warm_state_table:
        return {
            'scope': ASR_WARM_SCOPE,
            'gpuWarm': False,
            'windowSeconds': ASR_WARM_WINDOW_SECONDS,
        }
    if not (ASR_GPU_SPOT_ASG_NAME or ASR_GPU_ONDEMAND_ASG_NAME):
        return {
            'scope': ASR_WARM_SCOPE,
            'gpuWarm': False,
            'windowSeconds': ASR_WARM_WINDOW_SECONDS,
        }

    now = now_epoch()
    gpu_warm_until_epoch = now + ASR_WARM_WINDOW_SECONDS
    key = {'scope': ASR_WARM_SCOPE}
    updated_at = now_iso()
    warm_state_table.update_item(
        Key=key,
        UpdateExpression=(
            'SET updatedAt = :u, gpuWarmUntilEpoch = :g, gpuWarmUpdatedAt = :u, '
            'gpuWarmLastTrigger = :t, gpuWarmLastTriggeredAt = :u'
        ),
        ExpressionAttributeValues={
            ':u': updated_at,
            ':g': gpu_warm_until_epoch,
            ':t': trigger,
        },
    )
    print(
        json.dumps(
            {
                'event': 'GpuWarmWindowTouched',
                'trigger': trigger,
                'gpuWarmUntilEpoch': gpu_warm_until_epoch,
            }
        )
    )
    return {
        'scope': ASR_WARM_SCOPE,
        'gpuWarm': True,
        'gpuWarmUntilEpoch': gpu_warm_until_epoch,
        'gpuWarmUntil': epoch_to_iso(gpu_warm_until_epoch),
        'windowSeconds': ASR_WARM_WINDOW_SECONDS,
    }


def _touch_gpu_warm_window_if_needed(trigger, visible=True):
    if not warm_state_table:
        return {
            'scope': ASR_WARM_SCOPE,
            'gpuWarm': False,
            'windowSeconds': ASR_WARM_WINDOW_SECONDS,
            'throttled': False,
        }
    if trigger == 'INTERACTION' and not visible:
        item = warm_state_table.get_item(Key={'scope': ASR_WARM_SCOPE}).get('Item') or {}
        gpu_warm_until_epoch = _int_or_default(item.get('gpuWarmUntilEpoch'))
        return {
            'scope': ASR_WARM_SCOPE,
            'gpuWarm': gpu_warm_until_epoch > 0,
            'gpuWarmUntilEpoch': gpu_warm_until_epoch,
            'gpuWarmUntil': epoch_to_iso(gpu_warm_until_epoch) if gpu_warm_until_epoch > 0 else None,
            'windowSeconds': ASR_WARM_WINDOW_SECONDS,
            'throttled': True,
        }

    if trigger == 'INTERACTION':
        item = warm_state_table.get_item(Key={'scope': ASR_WARM_SCOPE}).get('Item') or {}
        last_touch_epoch = _int_or_default(item.get('gpuInteractionLastTouchedEpoch'))
        now = now_epoch()
        if last_touch_epoch > 0 and now - last_touch_epoch < ASR_ACTIVITY_TOUCH_THROTTLE_SECONDS:
            gpu_warm_until_epoch = _int_or_default(item.get('gpuWarmUntilEpoch'))
            print(
                json.dumps(
                    {
                        'event': 'GpuWarmTouchSkipped',
                        'trigger': trigger,
                        'reason': 'throttled',
                        'throttleSeconds': ASR_ACTIVITY_TOUCH_THROTTLE_SECONDS,
                    }
                )
            )
            return {
                'scope': ASR_WARM_SCOPE,
                'gpuWarm': gpu_warm_until_epoch > 0,
                'gpuWarmUntilEpoch': gpu_warm_until_epoch,
                'gpuWarmUntil': epoch_to_iso(gpu_warm_until_epoch) if gpu_warm_until_epoch > 0 else None,
                'windowSeconds': ASR_WARM_WINDOW_SECONDS,
                'throttled': True,
            }
        updated = _touch_gpu_warm_window(trigger)
        warm_state_table.update_item(
            Key={'scope': ASR_WARM_SCOPE},
            UpdateExpression='SET gpuInteractionLastTouchedEpoch = :e',
            ExpressionAttributeValues={':e': now},
        )
        return {**updated, 'throttled': False}

    return {**_touch_gpu_warm_window(trigger), 'throttled': False}


def _gpu_warm_status_snapshot():
    counts = _get_asr_capacity_counts()
    gpu = counts.get('gpu') or {}
    gpu_active = _int_or_default(gpu.get('active'))
    gpu_busy = _int_or_default(gpu.get('busy'))
    gpu_activating = _int_or_default(gpu.get('activating'))
    gpu_available = max(0, gpu_active - gpu_busy)
    return {
        'running': gpu_active,
        'used': gpu_busy,
        'warming': gpu_activating,
        'active': gpu_active,
        'busy': gpu_busy,
        'activating': gpu_activating,
        'available': gpu_available,
    }


def _enqueue_asr_job(transcript_id):
    if not ASR_JOBS_QUEUE_URL:
        raise RuntimeError('ASR jobs queue is not configured')
    payload = {'transcriptId': transcript_id, 'enqueuedAt': now_iso()}
    send_kwargs = dict(
        QueueUrl=ASR_JOBS_QUEUE_URL,
        MessageBody=json.dumps(payload),
    )
    try:
        segment = xray_recorder.current_segment()
        if segment:
            from aws_xray_sdk.core.models.trace_header import TraceHeader
            trace_header = TraceHeader(
                root=segment.trace_id,
                parent=segment.id,
                sampled=segment.sampled,
            )
            send_kwargs['MessageSystemAttributes'] = {
                'AWSTraceHeader': {
                    'StringValue': str(trace_header),
                    'DataType': 'String',
                }
            }
    except Exception:
        pass
    sent = sqs.send_message(**send_kwargs)
    message_id = sent.get('MessageId') or ''
    print(
        json.dumps(
            {
                'event': 'AsrJobQueued',
                'transcriptId': transcript_id,
                'messageId': message_id,
            }
        )
    )
    return message_id


def _trigger_asr_dispatch_async(transcript_id):
    if not transcript_id:
        raise RuntimeError('transcriptId is required')
    function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME') or ''
    if not function_name:
        raise RuntimeError('AWS_LAMBDA_FUNCTION_NAME is not available')
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='Event',
        Payload=json.dumps({'action': 'dispatch_asr_run_task', 'transcriptId': transcript_id}).encode('utf-8'),
    )
    print(json.dumps({'event': 'AsrDispatchRequested', 'transcriptId': transcript_id}))


def _start_run_task_for_transcript_item(item, worker_mode_label='RUN_TASK', dispatch_profile='default'):
    transcript_id = item.get('transcriptId')
    user_key = item.get('userId')
    if not transcript_id or not user_key:
        raise RuntimeError('Transcript item missing keys')
    task_arn, worker_capacity_type = _run_asr_task(transcript_id, dispatch_profile=dispatch_profile)
    now2 = now_iso()
    table.update_item(
        Key={'userId': user_key, 'transcriptId': transcript_id},
        UpdateExpression=(
            'SET #s = :s, workerTaskArn = :t, workerCapacityType = :ct, workerMode = :wm, workerStartedAt = if_not_exists(workerStartedAt, :u), '
            'workerUpdatedAt = :u, updatedAt = :u, message = :m, processingStage = :ps, processingStageUpdatedAt = :u'
        ),
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': 'PROCESSING',
            ':t': task_arn,
            ':ct': worker_capacity_type,
            ':wm': worker_mode_label,
            ':u': now2,
            ':m': 'Cold start: starting worker...',
            ':ps': STAGE_COLD_START,
        },
    )
    return task_arn, worker_capacity_type


def _mark_transcript_failed(user_key, transcript_id, message, error_code='ASR_WORKER_FAILED'):
    now = now_iso()
    table.update_item(
        Key={'userId': user_key, 'transcriptId': transcript_id},
        UpdateExpression='SET #s = :s, message = :m, errorCode = :c, updatedAt = :u, workerCompletedAt = :u, processingStage = :ps, processingStageUpdatedAt = :u',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': 'FAILED',
            ':m': message,
            ':c': error_code,
            ':u': now,
            ':ps': STAGE_FAILED,
        },
    )


def handle_asr_warmup(event):
    body = parse_body(event)
    user_id = (body.get('userId') or '').strip()
    trigger = (body.get('trigger') or 'LOGIN').strip().upper()
    visible = bool(body.get('visible', True))
    if not user_id:
        return response(400, {'message': 'userId is required'})

    if ASR_DISPATCH_MODE == 'run_task':
        try:
            gpu_state = _gpu_warm_status_snapshot()
            if gpu_state['available'] > 0 or gpu_state['warming'] > 0:
                return response(
                    200,
                    {
                        'warm': True,
                        'scope': ASR_WARM_SCOPE,
                        'mode': 'run_task',
                        'warmUntil': None,
                        'windowSeconds': ASR_WARM_WINDOW_SECONDS,
                        'gpu': gpu_state,
                        'message': 'GPU worker already available or warming; warmup not triggered.',
                    },
                )
            warm = _touch_gpu_warm_window_if_needed(trigger, visible=visible)
            return response(
                200,
                {
                    'warm': bool(warm.get('gpuWarm')),
                    'scope': warm.get('scope', ASR_WARM_SCOPE),
                    'mode': 'run_task',
                    'warmUntil': warm.get('gpuWarmUntil'),
                    'gpuWarmUntil': warm.get('gpuWarmUntil'),
                    'windowSeconds': warm.get('windowSeconds', ASR_WARM_WINDOW_SECONDS),
                    'trigger': trigger,
                    'gpu': _gpu_warm_status_snapshot(),
                    'message': 'GPU warm window activated for run_task mode.' if warm.get('gpuWarm') else 'GPU warmup unavailable in run_task mode.',
                },
            )
        except Exception as e:
            return response(500, {'message': f'Failed to warm GPU worker pool: {str(e)}'})

    try:
        warm = _touch_warm_window(trigger)
        gpu_warm = _touch_gpu_warm_window_if_needed(trigger, visible=visible) if GPU_ONLY_PIPELINE else None
        return response(
            200,
            {
                'warm': True,
                'scope': warm['scope'],
                'warmUntil': warm['warmUntil'],
                'gpuWarm': bool(gpu_warm.get('gpuWarm')) if gpu_warm else False,
                'gpuWarmUntil': gpu_warm.get('gpuWarmUntil') if gpu_warm else None,
                'desiredCount': warm['desiredCount'],
                'windowSeconds': warm['windowSeconds'],
                'mode': ASR_DISPATCH_MODE,
                'trigger': trigger,
            },
        )
    except Exception as e:
        return response(500, {'message': f'Failed to warm ASR worker: {str(e)}'})


def _run_asr_task(transcript_id, dispatch_profile='default'):
    if not ECS_CLUSTER_ARN:
        raise RuntimeError('ECS cluster is not configured')
    if not ECS_TASK_DEF_ARN and not ECS_FARGATE_TASK_DEF_ARN:
        raise RuntimeError('ECS task definition is not configured')
    if not ECS_SUBNET_IDS or not ECS_SECURITY_GROUP_IDS:
        raise RuntimeError('ECS subnets/security groups are not configured')

    container_overrides = {
        'containerOverrides': [
            {
                'name': ECS_CONTAINER_NAME,
                'environment': [
                    {'name': 'ASR_TRANSCRIPT_ID', 'value': transcript_id},
                    {'name': 'AWS_REGION', 'value': AWS_REGION},
                ],
            }
        ]
    }
    awsvpc_conf = {
        'subnets': ECS_SUBNET_IDS,
        'securityGroups': ECS_SECURITY_GROUP_IDS,
    }

    def run_with_provider(provider_label, provider_name):
        print(json.dumps({'event': 'AsrRunTaskAttempt', 'providerLabel': provider_label, 'capacityProvider': provider_name, 'transcriptId': transcript_id}))
        kwargs = {
            'cluster': ECS_CLUSTER_ARN,
            'taskDefinition': ECS_TASK_DEF_ARN,
            'count': 1,
            'overrides': container_overrides,
            'capacityProviderStrategy': [{'capacityProvider': provider_name, 'weight': 1}],
        }
        if ECS_GPU_TASK_NETWORK_MODE == 'awsvpc':
            kwargs['networkConfiguration'] = {'awsvpcConfiguration': awsvpc_conf}
        run = ecs.run_task(**kwargs)
        failures = run.get('failures') or []
        if failures:
            reason = '; '.join(f"{f.get('arn', '-')}: {f.get('reason', 'unknown')}" for f in failures)
            print(json.dumps({'event': 'AsrRunTaskFailure', 'providerLabel': provider_label, 'capacityProvider': provider_name, 'transcriptId': transcript_id, 'reason': reason}))
            raise RuntimeError(f"{provider_label}({provider_name}) failures: {reason}")
        tasks = run.get('tasks') or []
        if not tasks:
            print(json.dumps({'event': 'AsrRunTaskFailure', 'providerLabel': provider_label, 'capacityProvider': provider_name, 'transcriptId': transcript_id, 'reason': 'no_tasks'}))
            raise RuntimeError(f'{provider_label}({provider_name}) returned no task')
        task_arn = tasks[0].get('taskArn', '')
        print(json.dumps({'event': 'AsrRunTaskSubmitted', 'providerLabel': provider_label, 'capacityProvider': provider_name, 'transcriptId': transcript_id, 'taskArn': task_arn}))
        return task_arn

    def run_fargate_fallback():
        if GPU_ONLY_PIPELINE:
            raise RuntimeError('GPU_ONLY_PIPELINE forbids CPU fallback')
        if not ECS_FARGATE_TASK_DEF_ARN:
            raise RuntimeError('Fargate fallback task definition is not configured')
        print(json.dumps({'event': 'AsrRunTaskAttempt', 'providerLabel': 'FARGATE', 'launchType': 'FARGATE', 'transcriptId': transcript_id}))
        fargate_conf = dict(awsvpc_conf)
        fargate_conf['assignPublicIp'] = ECS_TASK_ASSIGN_PUBLIC_IP
        run = ecs.run_task(
            cluster=ECS_CLUSTER_ARN,
            taskDefinition=ECS_FARGATE_TASK_DEF_ARN,
            count=1,
            launchType='FARGATE',
            platformVersion='LATEST',
            networkConfiguration={'awsvpcConfiguration': fargate_conf},
            overrides=container_overrides,
        )
        failures = run.get('failures') or []
        if failures:
            reason = '; '.join(f"{f.get('arn', '-')}: {f.get('reason', 'unknown')}" for f in failures)
            print(json.dumps({'event': 'AsrRunTaskFailure', 'providerLabel': 'FARGATE', 'launchType': 'FARGATE', 'transcriptId': transcript_id, 'reason': reason}))
            raise RuntimeError(f'FARGATE fallback failures: {reason}')
        tasks = run.get('tasks') or []
        if not tasks:
            print(json.dumps({'event': 'AsrRunTaskFailure', 'providerLabel': 'FARGATE', 'launchType': 'FARGATE', 'transcriptId': transcript_id, 'reason': 'no_tasks'}))
            raise RuntimeError('FARGATE fallback returned no task')
        task_arn = tasks[0].get('taskArn', '')
        print(json.dumps({'event': 'AsrRunTaskSubmitted', 'providerLabel': 'FARGATE', 'launchType': 'FARGATE', 'transcriptId': transcript_id, 'taskArn': task_arn}))
        return task_arn

    dispatch_profile_norm = str(dispatch_profile or '').lower()
    async_profile = dispatch_profile_norm in ('async', 'background')
    cpu_only_profile = dispatch_profile_norm in ('cpu_only', 'fargate_only')
    gpu_timeout_seconds = 22 if async_profile else 12
    gpu_ok_statuses = ('RUNNING', 'PENDING') if async_profile else ('RUNNING',)

    def starts_quickly(task_arn, timeout_seconds=12, acceptable_statuses=None, poll_seconds=3):
        deadline = time.time() + max(5, int(timeout_seconds))
        last_status = ''
        ok_statuses = set(acceptable_statuses or ('RUNNING',))
        while time.time() < deadline:
            desc = ecs.describe_tasks(cluster=ECS_CLUSTER_ARN, tasks=[task_arn])
            tasks = desc.get('tasks') or []
            if not tasks:
                last_status = ''
            else:
                task = tasks[0]
                last_status = (task.get('lastStatus') or '')
                if last_status in ok_statuses:
                    return True, last_status
                if last_status in ('STOPPED', 'DEPROVISIONING'):
                    reason = task.get('stoppedReason') or task.get('stopCode') or 'stopped'
                    return False, f'{last_status}:{reason}'
            time.sleep(max(1, int(poll_seconds)))
        return False, last_status or 'UNKNOWN'

    def stop_if_present(task_arn, reason):
        if not task_arn:
            return
        try:
            ecs.stop_task(cluster=ECS_CLUSTER_ARN, task=task_arn, reason=reason)
        except Exception:
            pass

    errors = []
    spot_task_arn = ''
    ondemand_task_arn = ''
    gpu_providers_configured = bool(ECS_GPU_SPOT_CAPACITY_PROVIDER and ECS_GPU_ONDEMAND_CAPACITY_PROVIDER)

    if cpu_only_profile:
        fallback_task_arn = run_fargate_fallback()
        print(json.dumps({'event': 'AsrRunTaskSelected', 'workerCapacityType': 'FARGATE_RECOVERY', 'transcriptId': transcript_id, 'taskArn': fallback_task_arn, 'dispatchProfile': dispatch_profile}))
        return fallback_task_arn, 'FARGATE_FALLBACK'

    # Default mode: CPU Fargate first. Set ECS_PREFER_GPU=true to invert this behavior.
    if not GPU_ONLY_PIPELINE and not ECS_PREFER_GPU and ECS_FARGATE_TASK_DEF_ARN:
        try:
            fallback_task_arn = run_fargate_fallback()
            return fallback_task_arn, 'FARGATE_DEFAULT'
        except Exception as e:
            errors.append(str(e))

    if gpu_providers_configured:
        try:
            spot_task_arn = run_with_provider('SPOT', ECS_GPU_SPOT_CAPACITY_PROVIDER)
            ok, status = starts_quickly(
                spot_task_arn,
                timeout_seconds=gpu_timeout_seconds,
                acceptable_statuses=gpu_ok_statuses,
                poll_seconds=3,
            )
            if ok:
                print(
                    json.dumps(
                        {
                            'event': 'AsrRunTaskSelected',
                            'workerCapacityType': 'SPOT',
                            'transcriptId': transcript_id,
                            'taskArn': spot_task_arn,
                            'dispatchProfile': dispatch_profile,
                            'selectionStatus': status,
                        }
                    )
                )
                return spot_task_arn, 'SPOT'
            errors.append(f'SPOT task stuck in {status or "UNKNOWN"}')
        except Exception as e:
            errors.append(str(e))

        try:
            ondemand_task_arn = run_with_provider('ON_DEMAND', ECS_GPU_ONDEMAND_CAPACITY_PROVIDER)
            ok, status = starts_quickly(
                ondemand_task_arn,
                timeout_seconds=gpu_timeout_seconds,
                acceptable_statuses=gpu_ok_statuses,
                poll_seconds=3,
            )
            if ok:
                stop_if_present(spot_task_arn, 'Using On-Demand capacity')
                print(
                    json.dumps(
                        {
                            'event': 'AsrRunTaskSelected',
                            'workerCapacityType': 'ON_DEMAND',
                            'transcriptId': transcript_id,
                            'taskArn': ondemand_task_arn,
                            'dispatchProfile': dispatch_profile,
                            'selectionStatus': status,
                        }
                    )
                )
                return ondemand_task_arn, 'ON_DEMAND'
            errors.append(f'ON_DEMAND task stuck in {status or "UNKNOWN"}')
        except Exception as e:
            errors.append(str(e))
    else:
        errors.append('GPU capacity providers are not configured')

    if not GPU_ONLY_PIPELINE:
        try:
            fallback_task_arn = run_fargate_fallback()
            stop_if_present(spot_task_arn, 'Falling back to Fargate')
            stop_if_present(ondemand_task_arn, 'Falling back to Fargate')
            print(
                json.dumps(
                    {
                        'event': 'AsrRunTaskSelected',
                        'workerCapacityType': 'FARGATE_FALLBACK',
                        'transcriptId': transcript_id,
                        'taskArn': fallback_task_arn,
                        'errors': errors,
                        'dispatchProfile': dispatch_profile,
                    }
                )
            )
            return fallback_task_arn, 'FARGATE_FALLBACK'
        except Exception as e:
            errors.append(str(e))

    raise RuntimeError(' | '.join(errors) if errors else 'ECS run_task returned no task')


def _maybe_recover_stopped_run_task(item):
    if not ECS_CLUSTER_ARN:
        return item
    if not item:
        return item
    if (item.get('status') or '').upper() not in ('PROCESSING', 'QUEUED'):
        return item
    if (item.get('processingStage') or '') != STAGE_COLD_START:
        return item
    worker_mode = (item.get('workerMode') or '')
    if not worker_mode.startswith('RUN_TASK'):
        return item
    task_arn = item.get('workerTaskArn') or ''
    if not task_arn:
        return item

    try:
        desc = ecs.describe_tasks(cluster=ECS_CLUSTER_ARN, tasks=[task_arn])
    except Exception as e:
        print(json.dumps({'event': 'RunTaskRecoverDescribeFailed', 'transcriptId': item.get('transcriptId'), 'taskArn': task_arn, 'error': str(e)}))
        return item

    tasks = desc.get('tasks') or []
    if not tasks:
        return item
    task = tasks[0]
    last_status = (task.get('lastStatus') or '').upper()
    if last_status != 'STOPPED':
        return item

    transcript_id = item.get('transcriptId')
    user_key = item.get('userId')
    stop_reason = task.get('stoppedReason') or task.get('stopCode') or 'worker stopped'
    current_cap = (item.get('workerCapacityType') or '').upper()
    retry_done = bool(item.get('gpuFallbackRetriedAt'))

    print(json.dumps({'event': 'RunTaskStoppedBeforeStageProgress', 'transcriptId': transcript_id, 'taskArn': task_arn, 'workerCapacityType': current_cap, 'stopReason': stop_reason, 'retryDone': retry_done}))

    if current_cap in ('SPOT', 'ON_DEMAND') and not retry_done:
        try:
            now = now_iso()
            table.update_item(
                Key={'userId': user_key, 'transcriptId': transcript_id},
                UpdateExpression='SET gpuFallbackRetriedAt = :u, workerUpdatedAt = :u, updatedAt = :u, message = :m',
                ExpressionAttributeValues={
                    ':u': now,
                    ':m': 'Cold start: waiting for GPU worker...',
                },
            )
            fresh = get_item_by_transcript_id(transcript_id) or item
            _start_run_task_for_transcript_item(fresh, 'RUN_TASK_RECOVERY')
            return get_item_by_transcript_id(transcript_id) or fresh
        except Exception as e:
            print(json.dumps({'event': 'RunTaskRecoveryFailed', 'transcriptId': transcript_id, 'error': str(e)}))
            _mark_transcript_failed(user_key, transcript_id, f'Failed: GPU worker stopped and CPU fallback failed: {stop_reason}', 'ASR_WORKER_RECOVERY_FAILED')
            return get_item_by_transcript_id(transcript_id) or item

    _mark_transcript_failed(user_key, transcript_id, f'Failed: ASR worker stopped: {stop_reason}', 'ASR_WORKER_FAILED')
    return get_item_by_transcript_id(transcript_id) or item


def handle_create_transcription(event):
    body = parse_body(event)
    user_id = body.get('userId', '')
    user_key = normalize_user_key(user_id)
    s3_key = body.get('s3Key')
    effective_date = body.get('effectiveDate')
    user_timezone = body.get('userTimeZone')
    logical = _resolve_logical_day(effective_date, user_timezone)

    # Login is mandatory now.
    if not user_id:
        return response(400, {'message': 'userId is required (login is mandatory)'})

    if not s3_key:
        return response(400, {'message': 's3Key is required'})

    # Require at least one calibration for this user before allowing transcription.
    exists = calibration_exists_for_user(user_id)
    if not any(exists.values()):
        return response(400, {'message': 'At least one calibration (kid/parent1/parent2) is required before transcribing'})

    transcript_id = str(uuid.uuid4())
    now = now_iso()

    if ASR_ENGINE == 'transcribe':
        job_name = f"transcript-{transcript_id}"
        media_uri = f"s3://{UPLOADS_BUCKET}/{s3_key}"
        output_key = f"transcripts/{transcript_id}/raw.json"

        transcribe_client = boto3.client('transcribe')
        transcribe_client.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={'MediaFileUri': media_uri},
            IdentifyLanguage=True,
            Settings={
                'ShowSpeakerLabels': True,
                'MaxSpeakerLabels': 10,
            },
            OutputBucketName=ARTIFACTS_BUCKET,
            OutputKey=output_key,
        )

        item = {
            'userId': user_key,
            'transcriptId': transcript_id,
            'displayUserId': user_id,
            'engine': 'transcribe',
            'status': 'IN_PROGRESS',
            'createdAt': now,
            'updatedAt': now,
            'audioS3Key': s3_key,
            'jobName': job_name,
            'transcriptJsonS3Key': output_key,
            'effectiveDate': logical.get('effectiveDate'),
            'userTimeZone': logical.get('userTimeZone'),
            'logicalDay': logical.get('logicalDay'),
            'processingStage': STAGE_ASR,
            'processingStageUpdatedAt': now,
            'message': 'Speech-to-text: transcribing audio...',
        }
        table.put_item(Item=item)
        return response(200, {'transcriptId': transcript_id, 'jobName': job_name, 'engine': 'transcribe'})

    if ASR_ENGINE != 'whisper':
        return response(500, {'message': f'Unsupported ASR_ENGINE: {ASR_ENGINE}'})

    output_key = f"transcripts/{transcript_id}/raw_whisper.json"
    item = {
        'userId': user_key,
        'transcriptId': transcript_id,
        'displayUserId': user_id,
        'engine': 'whisper',
        'status': 'QUEUED',
        'createdAt': now,
        'updatedAt': now,
        'audioS3Key': s3_key,
        'jobName': f"whisper-{transcript_id}",
        'transcriptJsonS3Key': output_key,
        'message': 'Cold start: waiting for GPU worker...',
        'processingStage': STAGE_COLD_START,
        'processingStageUpdatedAt': now,
        'effectiveDate': logical.get('effectiveDate'),
        'userTimeZone': logical.get('userTimeZone'),
        'logicalDay': logical.get('logicalDay'),
    }
    table.put_item(Item=item)

    def fail_start(error_message, error_code):
        table.update_item(
            Key={'userId': user_key, 'transcriptId': transcript_id},
            UpdateExpression='SET #s = :s, message = :m, errorCode = :c, updatedAt = :u, workerCompletedAt = :u, processingStage = :ps, processingStageUpdatedAt = :u',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':s': 'FAILED',
                ':m': f'Failed: {error_message}',
                ':c': error_code,
                ':ps': STAGE_FAILED,
                ':u': now_iso(),
            },
        )
        return response(500, {'message': error_message})

    def start_with_run_task(worker_mode_label):
        task_arn, worker_capacity_type = _start_run_task_for_transcript_item(item, worker_mode_label)
        return response(
            200,
            {
                'transcriptId': transcript_id,
                'jobName': item['jobName'],
                'engine': 'whisper',
                'taskRef': task_arn,
                'workerCapacityType': worker_capacity_type,
                'workerMode': worker_mode_label,
            },
        )

    if ASR_DISPATCH_MODE == 'run_task':
        try:
            gpu_warm = _touch_gpu_warm_window_if_needed('TRANSCRIPTION', visible=True)
            _trigger_asr_dispatch_async(transcript_id)
            return response(
                200,
                {
                    'transcriptId': transcript_id,
                    'jobName': item['jobName'],
                    'engine': 'whisper',
                    'workerMode': 'RUN_TASK',
                    'dispatchMode': 'ASYNC',
                    'message': 'Cold start: waiting for GPU worker...',
                    'warmUntil': gpu_warm.get('gpuWarmUntil'),
                    'gpuWarmUntil': gpu_warm.get('gpuWarmUntil'),
                    'gpuOnlyPipeline': GPU_ONLY_PIPELINE,
                },
            )
        except Exception as e:
            return fail_start(f'Failed to start ASR worker: {str(e)}', 'ECS_RUN_TASK_FAILED')

    if ASR_DISPATCH_MODE == 'queue_service':
        try:
            warm = _touch_warm_window('TRANSCRIPTION')
            gpu_warm = _touch_gpu_warm_window_if_needed('TRANSCRIPTION', visible=True) if GPU_ONLY_PIPELINE else None
            message_id = _enqueue_asr_job(transcript_id)
            now2 = now_iso()
            table.update_item(
                Key={'userId': user_key, 'transcriptId': transcript_id},
                UpdateExpression=(
                    'SET #s = :s, workerMode = :wm, workerQueueMessageId = :mid, workerStartedAt = :u, workerUpdatedAt = :u, updatedAt = :u, '
                    'message = :m, processingStage = :ps, processingStageUpdatedAt = :u, warmUntil = :wu'
                ),
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={
                    ':s': 'PROCESSING',
                    ':wm': 'QUEUE_SERVICE',
                    ':mid': message_id,
                    ':u': now2,
                    ':m': 'Cold start: waiting for GPU worker...',
                    ':ps': STAGE_COLD_START,
                    ':wu': warm['warmUntil'],
                },
            )
            return response(
                200,
                {
                    'transcriptId': transcript_id,
                    'jobName': item['jobName'],
                    'engine': 'whisper',
                    'workerMode': 'QUEUE_SERVICE',
                    'warmUntil': warm['warmUntil'],
                    'gpuWarmUntil': gpu_warm.get('gpuWarmUntil') if gpu_warm else None,
                    'gpuOnlyPipeline': GPU_ONLY_PIPELINE,
                },
            )
        except Exception as queue_error:
            if GPU_ONLY_PIPELINE:
                return fail_start(f'Failed queue dispatch ({str(queue_error)})', 'ASR_DISPATCH_FAILED')
            # Safety fallback: keep flow available by using the existing run_task path.
            try:
                print(json.dumps({'event': 'QueueDispatchFallback', 'reason': str(queue_error), 'transcriptId': transcript_id}))
                return start_with_run_task('RUN_TASK_FALLBACK')
            except Exception as run_task_error:
                return fail_start(
                    f'Failed queue dispatch ({str(queue_error)}) and run_task fallback ({str(run_task_error)})',
                    'ASR_DISPATCH_FAILED',
                )

    return fail_start(f'Unsupported ASR_DISPATCH_MODE: {ASR_DISPATCH_MODE}', 'INVALID_ASR_DISPATCH_MODE')


def handle_status(event):
    transcript_id = event.get('pathParameters', {}).get('transcriptId')
    if not transcript_id:
        return response(400, {'message': 'transcriptId is required'})

    item = get_item_by_transcript_id(transcript_id)
    if not item:
        return response(404, {'message': 'Transcript not found'})
    item = _maybe_recover_stopped_run_task(item)

    status = item.get('status', 'IN_PROGRESS')
    stage = item.get('processingStage')
    engine = (item.get('engine') or ASR_ENGINE or 'whisper').lower()
    worker_mode = item.get('workerMode')
    worker_capacity_type = item.get('workerCapacityType')
    if status == 'COMPLETED':
        _request_sql_sync_if_needed(item)
        stage_out = stage or STAGE_COMPLETED
        msg = _format_runtime_message(item, stage_out, item.get('message') or 'Transcription complete.')
        return response(200, {'status': 'COMPLETED', 'stage': stage_out, 'message': msg, 'progressHint': msg, 'workerMode': worker_mode, 'workerCapacityType': worker_capacity_type})
    if status == 'FAILED':
        stage_out = stage or STAGE_FAILED
        msg = _format_runtime_message(item, stage_out, item.get('message', 'Failed'))
        return response(200, {'status': 'FAILED', 'stage': stage_out, 'message': msg, 'progressHint': msg, 'workerMode': worker_mode, 'workerCapacityType': worker_capacity_type})
    if status == 'LABELING':
        stage_out = stage or STAGE_SPEAKER_IDENTIFICATION
        base = item.get('message') or ('Speaker identification: matching Kid/Parent calibrations on GPU...' if GPU_ONLY_PIPELINE else 'Speaker identification: matching Kid/Parent calibrations...')
        msg = _format_runtime_message(item, stage_out, base)
        return response(200, {'status': 'IN_PROGRESS', 'stage': stage_out, 'message': msg, 'progressHint': msg, 'workerMode': worker_mode, 'workerCapacityType': worker_capacity_type})
    if status in ('QUEUED', 'PROCESSING'):
        hint = item.get('message') or ('Transcribe job is running' if engine == 'transcribe' else 'Whisper worker is processing audio')
        msg = _format_runtime_message(item, stage, hint)
        return response(200, {'status': 'IN_PROGRESS', 'stage': stage, 'message': msg, 'progressHint': msg, 'workerMode': worker_mode, 'workerCapacityType': worker_capacity_type})

    # Backward compatibility for old Transcribe items.
    if engine == 'transcribe' and status == 'IN_PROGRESS':
        check = process_completed_transcription(item)
        if check.get('status') == 'COMPLETED':
            fresh = get_item_by_transcript_id(transcript_id)
            if fresh:
                _request_sql_sync_if_needed(fresh)
        return response(200, check)

    base = item.get('message', 'Pending worker completion')
    msg = _format_runtime_message(item, stage, base)
    return response(200, {'status': 'IN_PROGRESS', 'stage': stage, 'message': msg, 'progressHint': msg, 'workerMode': worker_mode, 'workerCapacityType': worker_capacity_type})


def serialize_numbers(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [serialize_numbers(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize_numbers(v) for k, v in value.items()}
    return value


def handle_get_transcription(event):
    transcript_id = event.get('pathParameters', {}).get('transcriptId')
    item = get_item_by_transcript_id(transcript_id)
    if not item:
        return response(404, {'message': 'Transcript not found'})

    if item.get('status') != 'COMPLETED':
        return response(409, {'message': 'Transcript is not completed', 'status': item.get('status')})

    out = {
        'transcriptId': item['transcriptId'],
        'userId': item.get('displayUserId', denormalize_user_key(item.get('userId', ''))),
        'numSpeakers': item.get('numSpeakers', 0),
        'speakerStats': item.get('speakerStats', []),
        'segments': item.get('segments', []),
        'fullText': item.get('fullText', ''),
        'audioS3Key': item.get('audioS3Key'),
        'transcriptJsonS3Key': item.get('transcriptJsonS3Key'),
        'createdAt': item.get('createdAt'),
        'effectiveDate': item.get('effectiveDate'),
        'userTimeZone': item.get('userTimeZone'),
        'logicalDay': item.get('logicalDay'),
        'status': item.get('status'),
        'posTaggingStatus': item.get('posTaggingStatus'),
        'posTaggingModel': item.get('posTaggingModel'),
        'posTaggingVersion': item.get('posTaggingVersion'),
    }
    _request_sql_sync_if_needed(item)
    return response(200, serialize_numbers(out))


def _trigger_sql_sync_async(transcript_id):
    if not transcript_id or not sql_enabled():
        return False
    function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME') or ''
    if not function_name:
        return False
    try:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=json.dumps({'action': 'sync_sql', 'transcriptId': transcript_id}).encode('utf-8'),
        )
        return True
    except Exception as e:
        print('Async SQL sync trigger failed:', repr(e))
        return False


def _request_sql_sync_if_needed(item):
    if not sql_enabled():
        return
    if not item or item.get('status') != 'COMPLETED' or item.get('sqlSyncedAt'):
        return
    if str(os.environ.get('LOCAL_DEV_MODE') or '').strip().lower() == 'true':
        _try_sync_sql(item)
        return
    transcript_id = item.get('transcriptId')
    if not transcript_id:
        return
    now = now_iso()
    now_epoch_value = now_epoch()
    requested_epoch = _int_or_default(item.get('sqlSyncRequestedAtEpoch'))
    if requested_epoch and now_epoch_value - requested_epoch < 300:
        return
    try:
        table.update_item(
            Key={'userId': item['userId'], 'transcriptId': transcript_id},
            UpdateExpression='SET sqlSyncRequestedAt = :t, sqlSyncRequestedAtEpoch = :e',
            ExpressionAttributeValues={':t': now, ':e': now_epoch_value},
        )
    except Exception as e:
        print('Failed to mark sqlSyncRequestedAt:', repr(e))
    _trigger_sql_sync_async(transcript_id)


def _try_sync_sql(item):
    global _SQL_SCHEMA_READY
    if not sql_enabled():
        return
    if (item or {}).get('status') != 'COMPLETED':
        return
    # Cheap idempotence marker in Dynamo item.
    if item.get('sqlSyncedAt'):
        return
    try:
        if not _SQL_SCHEMA_READY:
            ensure_schema()
            _SQL_SCHEMA_READY = True
        upsert_transcript_record(serialize_numbers(item))
        table.update_item(
            Key={'userId': item['userId'], 'transcriptId': item['transcriptId']},
            UpdateExpression='SET sqlSyncedAt = :t REMOVE sqlSyncRequestedAt, sqlSyncRequestedAtEpoch',
            ExpressionAttributeValues={':t': now_iso()},
        )
    except Exception as e:
        print('SQL sync failed:', repr(e))


def handle_async_sql_sync(event):
    transcript_id = (event or {}).get('transcriptId')
    if not transcript_id:
        return {'ok': False, 'message': 'transcriptId is required'}
    item = get_item_by_transcript_id(transcript_id)
    if not item:
        return {'ok': False, 'message': 'Transcript not found'}
    _try_sync_sql(item)
    return {'ok': True, 'transcriptId': transcript_id}


def handle_async_asr_dispatch(event):
    transcript_id = (event or {}).get('transcriptId')
    if not transcript_id:
        return {'ok': False, 'message': 'transcriptId is required'}
    item = get_item_by_transcript_id(transcript_id)
    if not item:
        return {'ok': False, 'message': 'Transcript not found'}
    if (item.get('status') or '').upper() in ('FAILED', 'COMPLETED'):
        return {'ok': True, 'transcriptId': transcript_id, 'skipped': True, 'reason': f"terminal:{item.get('status')}"}
    if item.get('workerTaskArn'):
        return {'ok': True, 'transcriptId': transcript_id, 'skipped': True, 'reason': 'already_started'}
    try:
        task_arn, worker_capacity_type = _start_run_task_for_transcript_item(item, 'RUN_TASK', dispatch_profile='async')
        return {
            'ok': True,
            'transcriptId': transcript_id,
            'taskRef': task_arn,
            'workerCapacityType': worker_capacity_type,
            'workerMode': 'RUN_TASK',
        }
    except Exception as e:
        now = now_iso()
        try:
            table.update_item(
                Key={'userId': item['userId'], 'transcriptId': transcript_id},
                UpdateExpression='SET #s = :s, message = :m, errorCode = :c, updatedAt = :u, workerCompletedAt = :u, processingStage = :ps, processingStageUpdatedAt = :u',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={
                    ':s': 'FAILED',
                    ':m': f'Failed: Failed to start ASR worker: {str(e)}',
                    ':c': 'ECS_RUN_TASK_FAILED',
                    ':u': now,
                    ':ps': STAGE_FAILED,
                },
            )
        except Exception as update_err:
            print('Failed to mark transcript start failure:', repr(update_err))
        print('Async ASR dispatch failed:', repr(e))
        return {'ok': False, 'transcriptId': transcript_id, 'message': str(e)}


def handle_list_transcriptions(event):
    q = event.get('queryStringParameters') or {}
    user_id = q.get('userId')

    if user_id is None:
        res = table.scan(ProjectionExpression='userId, displayUserId, transcriptId, createdAt, #s', ExpressionAttributeNames={'#s': 'status'})
        items = res.get('Items', [])
    else:
        user_key = normalize_user_key(user_id)
        res = table.query(
            KeyConditionExpression=Key('userId').eq(user_key),
            ProjectionExpression='userId, displayUserId, transcriptId, createdAt, #s',
            ExpressionAttributeNames={'#s': 'status'},
        )
        items = res.get('Items', [])

    mapped = []
    for item in items:
        mapped.append({
            'userId': item.get('displayUserId', denormalize_user_key(item.get('userId', ''))),
            'transcriptId': item.get('transcriptId'),
            'createdAt': item.get('createdAt'),
            'status': item.get('status'),
        })

    mapped.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
    return response(200, {'items': mapped[:50]})


def _date_or_default(value, default):
    if value:
        return value
    return default


def _parse_ymd(value):
    return date.fromisoformat(value)


def _week_start_utc(d):
    # ISO weeks start on Monday.
    return d - timedelta(days=d.weekday())


def _add_months(d, months):
    y = d.year + ((d.month - 1 + months) // 12)
    m = ((d.month - 1 + months) % 12) + 1
    return date(y, m, 1)


def _month_start(d):
    return date(d.year, d.month, 1)


def _bucket_items_daily(date_from, date_to, rows):
    index = {str(r.get('date')): r for r in rows}
    out = []
    cur = date_from
    while cur <= date_to:
        key = cur.isoformat()
        r = index.get(key) or {}
        out.append(
            {
                'date': key,
                'uniqueWordCount': int(r.get('unique_word_count') or 0),
                'totalWordCount': int(r.get('total_word_count') or 0),
                'interactionCount': int(r.get('interaction_count') or 0),
                'speakingSeconds': float(r.get('speaking_seconds') or 0),
                'avgResponseLatencyS': None if r.get('avg_response_latency_s') is None else float(r.get('avg_response_latency_s')),
            }
        )
        cur += timedelta(days=1)
    return out


def _bucket_items_weekly(week_from, week_to, rows):
    index = {str(r.get('week_start')): r for r in rows}
    out = []
    cur = week_from
    while cur <= week_to:
        key = cur.isoformat()
        r = index.get(key) or {}
        out.append(
            {
                'weekStart': key,
                'uniqueWordCount': int(r.get('unique_word_count') or 0),
                'totalWordCount': int(r.get('total_word_count') or 0),
                'interactionCount': int(r.get('interaction_count') or 0),
                'speakingSeconds': float(r.get('speaking_seconds') or 0),
                'avgResponseLatencyS': None if r.get('avg_response_latency_s') is None else float(r.get('avg_response_latency_s')),
            }
        )
        cur += timedelta(days=7)
    return out


def _bucket_items_monthly(month_from, month_to, rows):
    index = {str(r.get('month_start'))[:7]: r for r in rows}
    out = []
    cur = month_from
    while cur <= month_to:
        month_key = cur.strftime('%Y-%m')
        r = index.get(month_key) or {}
        out.append(
            {
                'month': month_key,
                'uniqueWordCount': int(r.get('unique_word_count') or 0),
                'totalWordCount': int(r.get('total_word_count') or 0),
                'interactionCount': int(r.get('interaction_count') or 0),
                'speakingSeconds': float(r.get('speaking_seconds') or 0),
                'avgResponseLatencyS': None if r.get('avg_response_latency_s') is None else float(r.get('avg_response_latency_s')),
            }
        )
        cur = _add_months(cur, 1)
    return out


def _sum_unique(items):
    return int(sum(int(x.get('uniqueWordCount') or 0) for x in (items or [])))


def _speaker_scope(value):
    raw = (value or 'all').strip().lower()
    aliases = {
        'kid': 'kid',
        'client1': 'client1',
        'client 1': 'client1',
        'parent1': 'client1',
        'parent 1': 'client1',
        'client2': 'client2',
        'client 2': 'client2',
        'parent2': 'client2',
        'parent 2': 'client2',
        'all': 'all',
    }
    return aliases.get(raw, 'all')


def _category_response(categories):
    ordered = sorted(
        categories,
        key=lambda item: (-int(item.get('uniqueWordCount') or 0), item.get('label') or ''),
    )
    return ordered


def _build_pos_category_payload(user_id, speaker, unit, tz, as_of, date_from, date_to, rows):
    grouped = {}
    for row in rows or []:
        word = str(row.get('normalized_word') or '').strip()
        key = str(row.get('category_key') or 'other').strip().lower() or 'other'
        if not word:
            continue
        bucket = grouped.setdefault(
            key,
            {
                'key': key,
                'label': pos_category_label(key),
                'words': set(),
            },
        )
        bucket['words'].add(word)

    categories = []
    for bucket in grouped.values():
        words = sorted(bucket['words'])
        categories.append(
            {
                'key': bucket['key'],
                'label': bucket['label'],
                'uniqueWordCount': len(words),
                'words': words,
            }
        )

    payload = {
        'userId': user_id,
        'speaker': speaker,
        'unit': unit,
        'tz': tz,
        'asOfDate': as_of.isoformat(),
        'categories': _category_response(categories),
        'source': 'sql',
    }
    if unit == 'month':
        payload['fromMonth'] = date_from.strftime('%Y-%m')
        payload['toMonth'] = date_to.strftime('%Y-%m')
    else:
        payload['from'] = date_from.isoformat()
        payload['to'] = date_to.isoformat()
    return payload


def _sql_required():
    global _SQL_SCHEMA_READY
    if not sql_enabled():
        return response(503, {'message': 'SQL analytics is not configured'})
    if not _SQL_SCHEMA_READY:
        try:
            ensure_schema()
            _SQL_SCHEMA_READY = True
        except Exception as e:
            print('SQL schema ensure failed:', repr(e))
            return response(503, {'message': 'SQL schema initialization failed', 'detail': str(e)})
    return None


def handle_progression_daily(event):
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    date_to = _parse_ymd(_date_or_default(q.get('to'), as_of.isoformat()))
    date_from = _parse_ymd(_date_or_default(q.get('from'), date_to.isoformat()))
    if date_from > date_to:
        return response(400, {'message': 'from must be <= to'})
    missing = _sql_required()
    if missing:
        return missing
    speaker = _speaker_scope(q.get('speaker'))
    try:
        rows = query_daily(user_id, date_from.isoformat(), date_to.isoformat(), speaker)
        items = _bucket_items_daily(date_from, date_to, rows)
    except Exception as e:
        print('ProgressionDaily SQL query failed:', repr(e))
        return response(503, {'message': 'Daily progression query failed', 'detail': str(e)})
    payload = {'userId': user_id, 'speaker': speaker, 'from': date_from.isoformat(), 'to': date_to.isoformat(), 'tz': tz, 'asOfDate': as_of.isoformat(), 'items': items, 'source': 'sql'}
    return response(200, payload)


def handle_progression_weekly(event):
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    date_to = _parse_ymd(_date_or_default(q.get('to'), as_of.isoformat()))
    date_from = _parse_ymd(_date_or_default(q.get('from'), date_to.isoformat()))
    if date_from > date_to:
        return response(400, {'message': 'from must be <= to'})
    missing = _sql_required()
    if missing:
        return missing
    speaker = _speaker_scope(q.get('speaker'))
    week_from = _week_start_utc(date_from)
    week_to = _week_start_utc(date_to)
    try:
        rows = query_weekly(user_id, week_from.isoformat(), week_to.isoformat(), speaker)
        items = _bucket_items_weekly(week_from, week_to, rows)
    except Exception as e:
        print('ProgressionWeekly SQL query failed:', repr(e))
        return response(503, {'message': 'Weekly progression query failed', 'detail': str(e)})
    payload = {'userId': user_id, 'speaker': speaker, 'from': week_from.isoformat(), 'to': week_to.isoformat(), 'tz': tz, 'asOfDate': as_of.isoformat(), 'items': items, 'source': 'sql'}
    return response(200, payload)


def handle_progression_monthly(event):
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    to_raw = q.get('toMonth')
    from_raw = q.get('fromMonth')
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    now_month = as_of.strftime('%Y-%m')
    to_month = to_raw or now_month
    from_month = from_raw or to_month
    try:
        month_to = _parse_ymd(f'{to_month}-01')
        month_from = _parse_ymd(f'{from_month}-01')
    except Exception:
        return response(400, {'message': 'fromMonth/toMonth must be YYYY-MM'})
    if month_from > month_to:
        return response(400, {'message': 'fromMonth must be <= toMonth'})
    missing = _sql_required()
    if missing:
        return missing
    speaker = _speaker_scope(q.get('speaker'))
    month_to_last_day = _add_months(month_to, 1) - timedelta(days=1)
    try:
        rows = query_monthly(user_id, month_from.isoformat(), month_to_last_day.isoformat(), speaker)
        items = _bucket_items_monthly(month_from, month_to, rows)
    except Exception as e:
        print('ProgressionMonthly SQL query failed:', repr(e))
        return response(503, {'message': 'Monthly progression query failed', 'detail': str(e)})
    payload = {
        'userId': user_id,
        'speaker': speaker,
        'fromMonth': month_from.strftime('%Y-%m'),
        'toMonth': month_to.strftime('%Y-%m'),
        'tz': tz,
        'asOfDate': as_of.isoformat(),
        'items': items,
        'source': 'sql',
    }
    return response(
        200,
        payload,
    )


def handle_progression_pos_categories(event):
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    unit = (q.get('unit') or '').strip().lower()
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    speaker = _speaker_scope(q.get('speaker'))
    missing = _sql_required()
    if missing:
        return missing

    if unit == 'month' or q.get('fromMonth') or q.get('toMonth'):
        to_raw = q.get('toMonth') or as_of.strftime('%Y-%m')
        from_raw = q.get('fromMonth') or to_raw
        try:
            month_to = _parse_ymd(f'{to_raw}-01')
            month_from = _parse_ymd(f'{from_raw}-01')
        except Exception:
            return response(400, {'message': 'fromMonth/toMonth must be YYYY-MM'})
        if month_from > month_to:
            return response(400, {'message': 'fromMonth must be <= toMonth'})
        query_from = month_from
        query_to = _add_months(month_to, 1) - timedelta(days=1)
        payload_unit = 'month'
        response_from = month_from
        response_to = month_to
    elif unit == 'week':
        date_to = _parse_ymd(_date_or_default(q.get('to'), as_of.isoformat()))
        date_from = _parse_ymd(_date_or_default(q.get('from'), date_to.isoformat()))
        if date_from > date_to:
            return response(400, {'message': 'from must be <= to'})
        query_from = _week_start_utc(date_from)
        query_to = _week_start_utc(date_to) + timedelta(days=6)
        payload_unit = 'week'
        response_from = _week_start_utc(date_from)
        response_to = _week_start_utc(date_to)
    else:
        date_to = _parse_ymd(_date_or_default(q.get('to'), as_of.isoformat()))
        date_from = _parse_ymd(_date_or_default(q.get('from'), date_to.isoformat()))
        if date_from > date_to:
            return response(400, {'message': 'from must be <= to'})
        query_from = date_from
        query_to = date_to
        payload_unit = 'day'
        response_from = date_from
        response_to = date_to

    try:
        ensure_wordbank_seeded()
        rows = query_pos_categories(user_id, query_from.isoformat(), query_to.isoformat(), speaker)
    except Exception as e:
        print('ProgressionPosCategories SQL query failed:', repr(e))
        return response(503, {'message': 'POS categories query failed', 'detail': str(e)})
    return response(
        200,
        _build_pos_category_payload(
            user_id,
            speaker,
            payload_unit,
            tz,
            as_of,
            response_from,
            response_to,
            rows,
        ),
    )


def _clamp_min_cosine(value):
    default = 0.5
    try:
        parsed = float(value if value is not None else default)
    except Exception:
        parsed = default
    parsed = max(0.125, min(0.7, parsed))
    return round(parsed, 2)


def _semantic_window(unit, q, as_of):
    if unit == 'month' or q.get('fromMonth') or q.get('toMonth'):
        to_raw = q.get('toMonth') or as_of.strftime('%Y-%m')
        from_raw = q.get('fromMonth') or to_raw
        month_to = _parse_ymd(f'{to_raw}-01')
        month_from = _parse_ymd(f'{from_raw}-01')
        if month_from > month_to:
            raise ValueError('fromMonth must be <= toMonth')
        return month_from, (_add_months(month_to, 1) - timedelta(days=1))
    if unit == 'week':
        date_to = _parse_ymd(_date_or_default(q.get('to'), as_of.isoformat()))
        date_from = _parse_ymd(_date_or_default(q.get('from'), date_to.isoformat()))
        if date_from > date_to:
            raise ValueError('from must be <= to')
        return _week_start_utc(date_from), (_week_start_utc(date_to) + timedelta(days=6))
    date_to = _parse_ymd(_date_or_default(q.get('to'), as_of.isoformat()))
    date_from = _parse_ymd(_date_or_default(q.get('from'), date_to.isoformat()))
    if date_from > date_to:
        raise ValueError('from must be <= to')
    return date_from, date_to


def _apply_topk_edges(rows, top_k=15):
    if not rows:
        return []
    by_node = defaultdict(list)
    for row in rows:
        a = str(row.get('word_a') or '')
        b = str(row.get('word_b') or '')
        c = float(row.get('cosine') or 0)
        if not a or not b or a == b:
            continue
        by_node[a].append((b, c))
        by_node[b].append((a, c))
    keep = set()
    for node, neighbors in by_node.items():
        ranked = sorted(neighbors, key=lambda item: (-item[1], item[0]))[:top_k]
        for other, _ in ranked:
            keep.add((node, other) if node < other else (other, node))
    out = []
    for row in rows:
        a = str(row.get('word_a') or '')
        b = str(row.get('word_b') or '')
        key = (a, b) if a < b else (b, a)
        if key in keep:
            out.append(row)
    return out


def handle_semantic_network(event):
    missing = _sql_required()
    if missing:
        return missing
    started = time.time()
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    unit = (q.get('unit') or '').strip().lower()
    min_cosine = _clamp_min_cosine(q.get('minCosine'))

    try:
        query_from, query_to = _semantic_window(unit, q, as_of)
    except ValueError as e:
        return response(400, {'message': str(e)})
    except Exception:
        return response(400, {'message': 'Invalid date filter'})

    try:
        ensure_wordbank_seeded()
        profile = get_user_profile(user_id) or {}
    except Exception as e:
        return response(503, {'message': 'Semantic network unavailable', 'detail': str(e)})

    age_max_months = profile.get('kidAgeMonths')
    meta = {
        'ageMaxMonths': age_max_months,
        'minCosine': min_cosine,
        'weightedThreshold': round(min_cosine * 4.0, 3),
        'topKApplied': False,
        'transcriptWordCount': 0,
        'eligibleWordCount': 0,
        'finalWordCount': 0,
    }
    if age_max_months is None:
        meta['message'] = 'Set kid age in profile to view an age-matched semantic map.'
        return response(200, {'nodes': [], 'edges': [], 'meta': meta})

    try:
        eligible_rows = query_semantic_eligible_words(age_max_months)
        transcript_rows = query_transcript_words(user_id, query_from.isoformat(), query_to.isoformat(), speaker='kid')
    except Exception as e:
        return response(503, {'message': 'Semantic network query failed', 'detail': str(e)})

    eligible_map = {}
    for row in eligible_rows or []:
        word = str(row.get('word') or '').strip().lower()
        if not word:
            continue
        eligible_map[word] = row
    transcript_words = {str(r.get('normalized_word') or '').strip().lower() for r in (transcript_rows or []) if str(r.get('normalized_word') or '').strip()}
    final_words = sorted(set(eligible_map.keys()) & transcript_words)
    meta['eligibleWordCount'] = len(eligible_map)
    meta['transcriptWordCount'] = len(transcript_words)
    meta['finalWordCount'] = len(final_words)
    if not final_words:
        meta['message'] = 'No words matched current filters and age cap.'
        return response(200, {'nodes': [], 'edges': [], 'meta': meta})

    try:
        edge_rows = query_semantic_edges(final_words, min_cosine)
    except Exception as e:
        return response(503, {'message': 'Semantic edge query failed', 'detail': str(e)})

    if len(final_words) > 500:
        edge_rows = _apply_topk_edges(edge_rows, top_k=15)
        meta['topKApplied'] = True

    nodes = []
    for word in final_words:
        row = eligible_map[word]
        group = str(row.get('cdi_category') or row.get('lexical_class') or 'other').strip().lower() or 'other'
        nodes.append(
            {
                'id': word,
                'label': str(row.get('display_label') or word),
                'aoaMonths': None if row.get('aoa_months') is None else float(row.get('aoa_months')),
                'group': group,
            }
        )

    edges = []
    for row in edge_rows or []:
        cosine = float(row.get('cosine') or 0)
        weight = cosine * 4.0
        if weight < (min_cosine * 4.0):
            continue
        edges.append(
            {
                'source': row.get('word_a'),
                'target': row.get('word_b'),
                'cosine': cosine,
                'weight': weight,
            }
        )

    elapsed_ms = int((time.time() - started) * 1000)
    print(
        json.dumps(
            {
                'event': 'SemanticNetworkRequest',
                'userId': user_id,
                'ageMaxMonths': age_max_months,
                'minCosine': min_cosine,
                'finalWordCount': len(final_words),
                'edgeCount': len(edges),
                'topKApplied': meta['topKApplied'],
                'elapsedMs': elapsed_ms,
            }
        )
    )
    return response(200, {'nodes': nodes, 'edges': edges, 'meta': meta})


def handle_kid_percentile(event):
    missing = _sql_required()
    if missing:
        return missing
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    month_start = as_of.replace(day=1)
    month_end = _add_months(month_start, 1) - timedelta(days=1)
    out = {
        'userId': user_id,
        'asOfDate': as_of.isoformat(),
        'timeZone': tz,
        'monthWindow': month_start.strftime('%Y-%m'),
        'rankRule': 'cumulative_lte',
        'available': False,
    }
    try:
        profile = get_user_profile(user_id) or {}
    except Exception as e:
        return response(503, {'userId': user_id, 'available': False, 'message': f'Benchmark unavailable: user profile lookup failed', 'detail': str(e)})

    kid_age_months = profile.get('kidAgeMonths')
    out['kidAgeMonths'] = kid_age_months
    if kid_age_months is None:
        out['message'] = 'No kid age benchmark available yet. Complete Kid calibration and select age.'
        return response(200, out)

    current_unique = 0
    try:
        rows = query_monthly(user_id, month_start.isoformat(), month_end.isoformat(), 'kid')
        target_key = month_start.strftime('%Y-%m')
        for r in rows or []:
            if str(r.get('month_start') or '').startswith(target_key):
                current_unique = int(r.get('unique_word_count') or 0)
                break
    except Exception as e:
        return response(503, {'userId': user_id, 'available': False, 'message': 'Benchmark unavailable: monthly analytics query failed', 'detail': str(e)})
    out['currentMonthUniqueWordCount'] = current_unique
    out['benchmarkAgeMonths'] = int(kid_age_months)
    try:
        pct = compute_kid_percentile_smoothed(int(kid_age_months), current_unique)
    except Exception as e:
        return response(503, {'userId': user_id, 'available': False, 'message': 'Benchmark unavailable: percentile query failed', 'detail': str(e)})
    if not pct:
        out['message'] = f'No benchmark available for age {kid_age_months} months.'
        return response(200, out)

    out['percentile'] = pct['percentile']
    out['benchmarkSampleSize'] = pct['benchmarkSampleSize']
    out['benchmarkMonthsUsed'] = pct.get('benchmarkMonthsUsed') or [int(kid_age_months)]
    out['benchmarkAgeRangeMonths'] = pct.get('benchmarkAgeRangeMonths') or {'min': int(kid_age_months), 'max': int(kid_age_months)}
    out['ageWindowRule'] = 'adjacent_months_pooled'
    out['available'] = True
    months_used = out['benchmarkMonthsUsed']
    if months_used:
        if len(months_used) >= 2 and months_used == list(range(months_used[0], months_used[-1] + 1)):
            age_label = f"{months_used[0]}-{months_used[-1]}"
        else:
            age_label = ','.join(str(m) for m in months_used)
        out['message'] = f'Percentile based on pooled benchmark ages {age_label} months.'
    else:
        out['message'] = f'Percentile based on benchmark distribution for age {kid_age_months} months.'
    return response(200, out)


def handle_monthly_calendar(event):
    missing = _sql_required()
    if missing:
        return missing
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    tz = _normalize_tz(q.get('tz'))
    as_of = _parse_effective_date(q.get('asOfDate')) or _today_for_tz(tz)
    try:
        year = int(q.get('year') or as_of.year)
        month = int(q.get('month') or as_of.month)
    except Exception:
        return response(400, {'message': 'year and month must be integers'})
    if month < 1 or month > 12:
        return response(400, {'message': 'month must be 1..12'})
    speaker = _speaker_scope(q.get('speaker'))
    payload = query_monthly_calendar(user_id, year, month, speaker)
    return response(200, payload)


def handle_interactions_latency(event):
    missing = _sql_required()
    if missing:
        return missing
    q = event.get('queryStringParameters') or {}
    user_id = (q.get('userId') or '').strip()
    if not user_id:
        return response(400, {'message': 'userId is required'})
    date_to = _date_or_default(q.get('to'), datetime.now(timezone.utc).date().isoformat())
    date_from = _date_or_default(q.get('from'), date_to)
    from_speaker = q.get('fromSpeaker')
    to_speaker = q.get('toSpeaker')
    rows = query_latency(user_id, date_from, date_to, from_speaker, to_speaker)
    return response(
        200,
        {
            'userId': user_id,
            'from': date_from,
            'to': date_to,
            'fromSpeaker': from_speaker,
            'toSpeaker': to_speaker,
            'items': [
                {
                    'date': str(r.get('date')),
                    'fromSpeaker': r.get('from_speaker'),
                    'toSpeaker': r.get('to_speaker'),
                    'transitions': int(r.get('transitions') or 0),
                    'avgResponseLatencyS': None if r.get('avg_response_latency_s') is None else float(r.get('avg_response_latency_s')),
                    'minResponseLatencyS': None if r.get('min_response_latency_s') is None else float(r.get('min_response_latency_s')),
                    'maxResponseLatencyS': None if r.get('max_response_latency_s') is None else float(r.get('max_response_latency_s')),
                }
                for r in rows
            ],
        },
    )


def handle_transcript_interactions(event):
    missing = _sql_required()
    if missing:
        return missing
    transcript_id = event.get('pathParameters', {}).get('transcriptId')
    if not transcript_id:
        return response(400, {'message': 'transcriptId is required'})
    return response(200, {'transcriptId': transcript_id, 'items': get_transcript_interactions(transcript_id)})


def handler(event, _context):
    if isinstance(event, dict) and event.get('action') == 'sync_sql':
        return handle_async_sql_sync(event)
    if isinstance(event, dict) and event.get('action') == 'dispatch_asr_run_task':
        return handle_async_asr_dispatch(event)

    method = event.get('requestContext', {}).get('http', {}).get('method')
    path = event.get('rawPath', '')

    if method == 'OPTIONS':
        return response(200, {'ok': True})

    try:
        if method == 'POST' and path == '/upload-url':
            return handle_upload_url(event)
        if method == 'POST' and path == '/v1/calibration/presign':
            return handle_calibration_presign(event)
        if method == 'GET' and path == '/v1/calibration/status':
            return handle_calibration_status(event)
        if method == 'GET' and path == '/v1/app-config':
            return handle_app_config(event)
        if method == 'POST' and path == '/v1/asr/warmup':
            return handle_asr_warmup(event)
        if method == 'GET' and path == '/v1/asr/runtime-status':
            return handle_asr_runtime_status(event)
        if method == 'POST' and path == '/transcriptions':
            return handle_create_transcription(event)
        if method == 'GET' and re.fullmatch(r'/transcriptions/[^/]+/status', path):
            return handle_status(event)
        if method == 'GET' and re.fullmatch(r'/transcriptions/[^/]+/interactions', path):
            return handle_transcript_interactions(event)
        if method == 'GET' and re.fullmatch(r'/transcriptions/[^/]+', path):
            return handle_get_transcription(event)
        if method == 'GET' and path == '/transcriptions':
            return handle_list_transcriptions(event)
        if method == 'GET' and path == '/analytics/progression/daily':
            return handle_progression_daily(event)
        if method == 'GET' and path == '/analytics/progression/weekly':
            return handle_progression_weekly(event)
        if method == 'GET' and path == '/analytics/progression/monthly':
            return handle_progression_monthly(event)
        if method == 'GET' and path == '/analytics/progression/pos-categories':
            return handle_progression_pos_categories(event)
        if method == 'GET' and path == '/analytics/semantic/network':
            return handle_semantic_network(event)
        if method == 'GET' and path == '/analytics/kid-percentile':
            return handle_kid_percentile(event)
        if method == 'GET' and path == '/analytics/progression/monthly-calendar':
            return handle_monthly_calendar(event)
        if method == 'GET' and path == '/analytics/interactions/latency':
            return handle_interactions_latency(event)
        return response(404, {'message': 'Not found'})
    except Exception as e:
        return response(500, {'message': 'Internal server error', 'detail': str(e)})
