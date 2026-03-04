#!/usr/bin/env python3
"""Copy SQL user profiles (external_user_id, kid_age_months) across Aurora Data API databases.

Example:
  python tools/migrate_sql_users_profiles.py \
    --source-profile test-admin \
    --source-region us-west-1 \
    --source-cluster-arn arn:aws:rds:...:cluster:... \
    --source-secret-arn arn:aws:secretsmanager:... \
    --source-database lumi \
    --target-profile ucb-admin \
    --target-region us-west-1 \
    --target-cluster-arn arn:aws:rds:...:cluster:... \
    --target-secret-arn arn:aws:secretsmanager:... \
    --target-database lumi
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, Iterable, List, Optional

import boto3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate SQL user profile rows via RDS Data API")
    p.add_argument("--source-profile", default="", help="AWS profile for source")
    p.add_argument("--source-region", required=True)
    p.add_argument("--source-cluster-arn", required=True)
    p.add_argument("--source-secret-arn", required=True)
    p.add_argument("--source-database", default="lumi")
    p.add_argument("--target-profile", default="", help="AWS profile for target")
    p.add_argument("--target-region", required=True)
    p.add_argument("--target-cluster-arn", required=True)
    p.add_argument("--target-secret-arn", required=True)
    p.add_argument("--target-database", default="lumi")
    p.add_argument("--include-null-kid-age", action="store_true", help="Also copy users with NULL kid_age_months")
    return p.parse_args()


def session(profile: str):
    return boto3.session.Session(profile_name=profile) if profile else boto3.session.Session()


def _param(name: str, value: Any) -> Dict[str, Any]:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    return {"name": name, "value": {"stringValue": str(value)}}


def _rec_to_dict(meta: List[Dict[str, Any]], record: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for i, col in enumerate(meta):
        name = col.get("name")
        v = record[i] if i < len(record) else {}
        if "stringValue" in v:
            out[name] = v["stringValue"]
        elif "longValue" in v:
            out[name] = int(v["longValue"])
        elif "doubleValue" in v:
            out[name] = float(v["doubleValue"])
        elif "booleanValue" in v:
            out[name] = bool(v["booleanValue"])
        else:
            out[name] = None
    return out


def query_rows(client, resource_arn: str, secret_arn: str, database: str, sql: str, params: Optional[List[Dict[str, Any]]] = None):
    resp = client.execute_statement(
        resourceArn=resource_arn,
        secretArn=secret_arn,
        database=database,
        sql=sql,
        parameters=params or [],
        includeResultMetadata=True,
    )
    meta = resp.get("columnMetadata", [])
    return [_rec_to_dict(meta, r) for r in (resp.get("records") or [])]


def exec_stmt(client, resource_arn: str, secret_arn: str, database: str, sql: str, params: Optional[List[Dict[str, Any]]] = None):
    return client.execute_statement(
        resourceArn=resource_arn,
        secretArn=secret_arn,
        database=database,
        sql=sql,
        parameters=params or [],
        includeResultMetadata=True,
    )


def ensure_target_columns(client, resource_arn: str, secret_arn: str, database: str):
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            external_user_id TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            kid_age_months INTEGER
        )
        """,
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS kid_age_months INTEGER",
    ]
    for st in stmts:
        exec_stmt(client, resource_arn, secret_arn, database, st)


def main() -> int:
    args = parse_args()
    src = session(args.source_profile).client("rds-data", region_name=args.source_region)
    dst = session(args.target_profile).client("rds-data", region_name=args.target_region)

    ensure_target_columns(dst, args.target_cluster_arn, args.target_secret_arn, args.target_database)

    where = "" if args.include_null_kid_age else "WHERE kid_age_months IS NOT NULL"
    rows = query_rows(
        src,
        args.source_cluster_arn,
        args.source_secret_arn,
        args.source_database,
        f"""
        SELECT external_user_id, kid_age_months
        FROM users
        {where}
        ORDER BY external_user_id
        """,
    )

    written = 0
    for r in rows:
        exec_stmt(
            dst,
            args.target_cluster_arn,
            args.target_secret_arn,
            args.target_database,
            """
            INSERT INTO users(external_user_id, kid_age_months)
            VALUES(:u, :m)
            ON CONFLICT(external_user_id)
            DO UPDATE SET kid_age_months = COALESCE(EXCLUDED.kid_age_months, users.kid_age_months)
            """,
            [
                _param("u", r.get("external_user_id") or ""),
                _param("m", r.get("kid_age_months")),
            ],
        )
        written += 1
        if written % 25 == 0:
            print({"written": written})

    print({"read": len(rows), "written": written})
    return 0


if __name__ == "__main__":
    sys.exit(main())
