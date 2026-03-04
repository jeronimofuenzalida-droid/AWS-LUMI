import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-1')
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')
os.environ.setdefault('TRANSCRIPTS_TABLE', 'local-transcripts')
os.environ.setdefault('UPLOADS_BUCKET', 'local-uploads')
os.environ.setdefault('ARTIFACTS_BUCKET', 'local-artifacts')
sys.path.insert(0, str(REPO_ROOT / 'backend' / 'src'))

from app import auto_assign_speaker_names, build_full_text, build_segments, compute_stats, now_iso  # noqa: E402
from sql_store import ensure_schema, sql_enabled, upsert_transcript_record, upsert_user_kid_age_months  # noqa: E402
from load_kid_benchmark_csv import parse_csv  # noqa: E402
from sql_store import upsert_kid_benchmark_rows  # noqa: E402

FIXTURE_PATH = REPO_ROOT / 'fixtures' / 'conversationtest.transcribe.raw.json'
CSV_PATH = REPO_ROOT / 'files' / 'unique_words_per month.csv'


def main():
    if not sql_enabled():
        raise RuntimeError('Set LOCAL_POSTGRES_URL or SQL_* env before seeding local dev data')

    ensure_schema()

    parsed = parse_csv(CSV_PATH)
    upsert_kid_benchmark_rows(parsed['rows'], source_file=CSV_PATH.name)

    user_id = os.environ.get('LOCAL_DEV_USER_ID') or 'local-a1'
    kid_age_months = int(os.environ.get('LOCAL_DEV_KID_AGE_MONTHS') or '24')
    effective_date = os.environ.get('LOCAL_DEV_EFFECTIVE_DATE') or now_iso()[:10]
    time_zone = os.environ.get('LOCAL_DEV_TIME_ZONE') or 'America/Los_Angeles'

    upsert_user_kid_age_months(user_id, kid_age_months)

    raw = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    segments = build_segments(raw)
    segments = auto_assign_speaker_names(segments, {'kid': True, 'parent1': True, 'parent2': True})
    speaker_map = {'spk_0': 'Parent 1', 'spk_1': 'Parent 2', 'spk_2': 'Kid'}
    for seg in segments:
        seg['speakerName'] = speaker_map.get(seg.get('speakerLabel'), seg.get('speakerName') or 'Speaker')
    item = {
        'transcriptId': str(uuid.uuid4()),
        'userId': user_id,
        'displayUserId': user_id,
        'status': 'COMPLETED',
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
        'logicalDay': effective_date,
        'effectiveDate': effective_date,
        'userTimeZone': time_zone,
        'audioS3Key': 'local/uploads/conversationtest.mp3',
        'transcriptJsonS3Key': 'local/transcripts/conversationtest.json',
        'numSpeakers': len({s.get('speakerLabel') for s in segments}),
        'speakerStats': compute_stats(segments),
        'segments': segments,
        'fullText': build_full_text(segments),
        'message': 'Transcription complete.',
        'processingStage': 'COMPLETED',
        'workerMode': 'QUEUE_SERVICE',
        'workerCapacityType': 'LOCAL_GPU_MOCK',
    }
    upsert_transcript_record(item)
    print(json.dumps({'ok': True, 'userId': user_id, 'kidAgeMonths': kid_age_months, 'transcriptId': item['transcriptId']}, indent=2))


if __name__ == '__main__':
    main()
