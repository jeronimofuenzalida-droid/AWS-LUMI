#!/usr/bin/env python3
"""Copy all items from one DynamoDB table to another, across regions/accounts.

Usage:
  python tools/migrate_dynamo_table.py \
    --source-region us-east-2 \
    --target-region us-west-1 \
    --source-table SourceTable \
    --target-table TargetTable \
    --source-profile test-admin \
    --target-profile ucb-admin
"""

from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal
from typing import Any, Dict

import boto3
from botocore.config import Config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate DynamoDB table data using scan + batch-write.")
    parser.add_argument("--source-region", required=True, help="Source AWS region")
    parser.add_argument("--target-region", required=True, help="Target AWS region")
    parser.add_argument("--source-table", required=True, help="Source table name")
    parser.add_argument("--target-table", required=True, help="Target table name")
    parser.add_argument("--profile", default="", help="Optional AWS profile (legacy: applies to both source and target)")
    parser.add_argument("--source-profile", default="", help="Optional AWS profile for source table")
    parser.add_argument("--target-profile", default="", help="Optional AWS profile for target table")
    parser.add_argument("--page-size", type=int, default=200, help="Scan page size")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Sleep between pages")
    return parser.parse_args()


def session_for_profile(profile: str) -> boto3.session.Session:
    return boto3.session.Session(profile_name=profile) if profile else boto3.session.Session()


def to_plain_number(v: Any) -> Any:
    # Keep integers as int and non-integers as float for readable reporting only.
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, list):
        return [to_plain_number(x) for x in v]
    if isinstance(v, dict):
        return {k: to_plain_number(x) for k, x in v.items()}
    return v


def migrate(args: argparse.Namespace) -> Dict[str, Any]:
    source_profile = args.source_profile or args.profile
    target_profile = args.target_profile or args.profile
    src_session = session_for_profile(source_profile)
    dst_session = session_for_profile(target_profile)
    cfg = Config(retries={"max_attempts": 10, "mode": "standard"})

    src_ddb = src_session.resource("dynamodb", region_name=args.source_region, config=cfg)
    dst_ddb = dst_session.resource("dynamodb", region_name=args.target_region, config=cfg)

    src = src_ddb.Table(args.source_table)
    dst = dst_ddb.Table(args.target_table)

    scanned = 0
    written = 0
    pages = 0
    start = time.time()
    last_evaluated_key = None

    while True:
        scan_kwargs: Dict[str, Any] = {"Limit": args.page_size}
        if last_evaluated_key:
            scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

        resp = src.scan(**scan_kwargs)
        items = resp.get("Items") or []
        pages += 1
        scanned += len(items)

        if items:
            with dst.batch_writer(overwrite_by_pkeys=None) as batch:
                for item in items:
                    batch.put_item(Item=item)
                    written += 1

        if pages % 20 == 0:
            elapsed = round(time.time() - start, 1)
            print(
                {
                    "pages": pages,
                    "scanned": scanned,
                    "written": written,
                    "elapsedSec": elapsed,
                }
            )

        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    elapsed = round(time.time() - start, 2)
    return {
        "sourceRegion": args.source_region,
        "targetRegion": args.target_region,
        "sourceTable": args.source_table,
        "targetTable": args.target_table,
        "sourceProfile": source_profile or None,
        "targetProfile": target_profile or None,
        "pages": pages,
        "scanned": scanned,
        "written": written,
        "elapsedSec": elapsed,
    }


def main() -> int:
    args = parse_args()
    summary = migrate(args)
    print(to_plain_number(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
