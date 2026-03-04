import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Attr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'asr_worker'))

from pos_tagging import annotate_segments_with_pos  # noqa: E402
from sql_store import ensure_schema, sql_enabled, upsert_transcript_record  # noqa: E402


def _normalize_dynamo_value(value):
    if isinstance(value, list):
        return [_normalize_dynamo_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_dynamo_value(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    return value


def _decimalize(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    return value


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_raw_payload(s3_client, bucket, item):
    key = item.get('transcriptJsonS3Key') or ''
    if not key:
        return {}
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        return json.loads(obj['Body'].read().decode('utf-8'))
    except Exception as exc:
        print(f"raw_load_failed transcriptId={item.get('transcriptId')} key={key} error={exc!r}")
        return {}


def _annotate_item(item, raw_payload):
    if any(seg.get('tokens') for seg in (item.get('segments') or [])):
        return item, False

    segments = raw_payload.get('segments') or item.get('segments') or []
    if not segments:
        return item, False

    language = raw_payload.get('language') or item.get('language')
    annotated_segments, meta = annotate_segments_with_pos(segments, language)
    item = dict(item)
    item['segments'] = annotated_segments
    item['posTaggingStatus'] = meta.get('posTaggingStatus')
    item['posTaggingModel'] = meta.get('posTaggingModel')
    item['posTaggingVersion'] = meta.get('posTaggingVersion')
    return item, True


def _persist_item(table, item):
    table.update_item(
        Key={'userId': item['userId'], 'transcriptId': item['transcriptId']},
        UpdateExpression=(
            'SET #seg = :sg, posTaggingStatus = :pts, posTaggingModel = :ptm, '
            'posTaggingVersion = :ptv, updatedAt = :u'
        ),
        ExpressionAttributeNames={'#seg': 'segments'},
        ExpressionAttributeValues=_decimalize(
            {
                ':sg': item.get('segments') or [],
                ':pts': item.get('posTaggingStatus'),
                ':ptm': item.get('posTaggingModel'),
                ':ptv': item.get('posTaggingVersion'),
                ':u': _now_iso(),
            }
        ),
    )


def _persist_raw_payload(s3_client, bucket, item, raw_payload):
    key = item.get('transcriptJsonS3Key') or ''
    if not key or not raw_payload:
        return
    payload = dict(raw_payload)
    payload['segments'] = item.get('segments') or []
    payload['posTaggingStatus'] = item.get('posTaggingStatus')
    payload['posTaggingModel'] = item.get('posTaggingModel')
    payload['posTaggingVersion'] = item.get('posTaggingVersion')
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload).encode('utf-8'),
        ContentType='application/json',
    )


def main():
    if not sql_enabled():
        raise RuntimeError('SQL env not configured')

    ensure_schema()

    table_name = os.environ['TRANSCRIPTS_TABLE']
    artifacts_bucket = os.environ['ARTIFACTS_BUCKET']
    ddb = boto3.resource('dynamodb')
    s3 = boto3.client('s3')
    table = ddb.Table(table_name)

    scanned = 0
    enriched = 0
    synced = 0
    skipped = 0
    last_key = None

    while True:
        kwargs = {'FilterExpression': Attr('status').eq('COMPLETED')}
        if last_key:
            kwargs['ExclusiveStartKey'] = last_key
        resp = table.scan(**kwargs)
        for raw_item in resp.get('Items', []):
            scanned += 1
            item = _normalize_dynamo_value(raw_item)
            raw_payload = _load_raw_payload(s3, artifacts_bucket, item)
            item, changed = _annotate_item(item, raw_payload)
            if changed:
                _persist_item(table, item)
                _persist_raw_payload(s3, artifacts_bucket, item, raw_payload)
                enriched += 1
            if not any(seg.get('tokens') for seg in (item.get('segments') or [])):
                skipped += 1
                continue
            upsert_transcript_record(item)
            synced += 1
            if synced % 10 == 0:
                print(f'backfill_pos_categories synced={synced} enriched={enriched} skipped={skipped}')

        last_key = resp.get('LastEvaluatedKey')
        if not last_key:
            break

    print({'scanned': scanned, 'enriched': enriched, 'synced': synced, 'skipped': skipped})


if __name__ == '__main__':
    main()
