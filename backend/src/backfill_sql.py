import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

from sql_store import ensure_schema, sql_enabled, upsert_transcript_record


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


def main():
    if not sql_enabled():
        raise RuntimeError('SQL env not configured: SQL_CLUSTER_ARN, SQL_SECRET_ARN, SQL_DATABASE')
    ensure_schema()

    table_name = os.environ['TRANSCRIPTS_TABLE']
    ddb = boto3.resource('dynamodb')
    table = ddb.Table(table_name)

    scanned = 0
    migrated = 0
    last_key = None
    while True:
        kwargs = {
            'FilterExpression': Attr('status').eq('COMPLETED'),
        }
        if last_key:
            kwargs['ExclusiveStartKey'] = last_key
        resp = table.scan(**kwargs)
        items = resp.get('Items', [])
        for item in items:
            scanned += 1
            upsert_transcript_record(_normalize_dynamo_value(item))
            migrated += 1
            if migrated % 10 == 0:
                print(f'migrated={migrated}')

        last_key = resp.get('LastEvaluatedKey')
        if not last_key:
            break

    print({'scanned': scanned, 'migrated': migrated})


if __name__ == '__main__':
    main()
