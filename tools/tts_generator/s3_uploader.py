"""Upload synthesized audio to S3 and generate a presigned URL."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def upload_to_s3(
    file_path: Path,
    bucket: str,
    name: str = "conversation",
    key_prefix: str = "synthetic-audio",
    region: str = "us-west-1",
) -> tuple[str, str]:
    """Upload MP3 to S3 and return (s3_uri, presigned_url).

    S3 key: {key_prefix}/{timestamp}-{name}.mp3
    Presigned URL expires in 1 hour.
    """
    s3 = boto3.client(
        's3',
        region_name=region,
        endpoint_url=f'https://s3.{region}.amazonaws.com',
    )

    _check_bucket_access(s3, bucket)

    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    key = f"{key_prefix}/{ts}-{name}.mp3"

    s3.upload_file(
        str(file_path),
        bucket,
        key,
        ExtraArgs={'ContentType': 'audio/mpeg'},
    )

    s3_uri = f"s3://{bucket}/{key}"

    presigned_url = s3.generate_presigned_url(
        ClientMethod='get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=3600,
    )

    return s3_uri, presigned_url


def _check_bucket_access(s3_client, bucket: str) -> None:
    """Verify bucket exists and is accessible."""
    try:
        s3_client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '')
        if code == '404':
            raise SystemExit(f"S3 bucket not found: {bucket}") from exc
        if code == '403':
            raise SystemExit(
                f"Access denied to S3 bucket: {bucket}\n"
                f"Check your AWS credentials and bucket permissions."
            ) from exc
        raise SystemExit(
            f"Cannot access S3 bucket '{bucket}': {exc}"
        ) from exc
