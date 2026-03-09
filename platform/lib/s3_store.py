"""
platform.lib.s3_store
=====================

S3 read/write helpers for the ads agent platform.

Bucket layout
-------------
raw-metrics/YYYY/MM/DD/<platform>_raw_<timestamp>.json
normalized-metrics/YYYY/MM/DD/normalized_<timestamp>.json
recommendations/YYYY/MM/DD/recommendations_<timestamp>.json
audit/YYYY/MM/DD/audit_<run_id>.json
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def _today_prefix() -> str:
    d = date.today()
    return f"{d.year:04d}/{d.month:02d}/{d.day:02d}"


def _timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _put(bucket: str, key: str, data: Any, region: str) -> str:
    """Serialize data to JSON and upload to S3.  Returns the S3 key."""
    s3 = boto3.client("s3", region_name=region)
    body = json.dumps(data, default=str, indent=2).encode("utf-8")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    logger.info("Uploaded s3://%s/%s (%d bytes)", bucket, key, len(body))
    return key


def put_raw_metrics(
    bucket: str,
    platform: str,
    data: Any,
    region: str = "us-east-1",
) -> str:
    """Write raw API response data for a platform.

    Args:
        bucket: S3 bucket name.
        platform: "google" or "microsoft".
        data: Raw data to serialise (list of dicts or similar).
        region: AWS region.

    Returns:
        S3 key of the uploaded object.
    """
    key = f"raw-metrics/{_today_prefix()}/{platform}_raw_{_timestamp()}.json"
    return _put(bucket, key, data, region)


def put_normalized(
    bucket: str,
    data: list[dict[str, Any]],
    region: str = "us-east-1",
) -> str:
    """Write normalized campaign data (output of normalize_campaigns).

    Args:
        bucket: S3 bucket name.
        data: List of normalized campaign dicts.
        region: AWS region.

    Returns:
        S3 key of the uploaded object.
    """
    key = f"normalized-metrics/{_today_prefix()}/normalized_{_timestamp()}.json"
    return _put(bucket, key, data, region)


def put_recommendations(
    bucket: str,
    data: dict[str, Any],
    region: str = "us-east-1",
) -> str:
    """Write the full agent decision payload (budget, bids, keywords, creatives).

    Args:
        bucket: S3 bucket name.
        data: Agent recommendations dict.
        region: AWS region.

    Returns:
        S3 key of the uploaded object.
    """
    key = f"recommendations/{_today_prefix()}/recommendations_{_timestamp()}.json"
    return _put(bucket, key, data, region)


def put_audit(
    bucket: str,
    run_id: str,
    data: dict[str, Any],
    region: str = "us-east-1",
) -> str:
    """Write a full audit record for an agent run.

    Args:
        bucket: S3 bucket name.
        run_id: ISO datetime run identifier (used in the key).
        data: Full audit record dict.
        region: AWS region.

    Returns:
        S3 key of the uploaded object.
    """
    safe_run_id = run_id.replace(":", "-").replace(".", "-")
    key = f"audit/{_today_prefix()}/audit_{safe_run_id}.json"
    return _put(bucket, key, data, region)


def get_bing_report_csv(
    bucket: str,
    s3_key: str,
    region: str = "us-east-1",
) -> str:
    """Download a Bing report CSV written by the BingPoller Lambda.

    Args:
        bucket: S3 bucket name.
        s3_key: Key written by bing_poll_handler after report completion.
        region: AWS region.

    Returns:
        CSV content as a string.
    """
    s3 = boto3.client("s3", region_name=region)
    response = s3.get_object(Bucket=bucket, Key=s3_key)
    content = response["Body"].read().decode("utf-8")
    logger.info("Downloaded Bing report CSV from s3://%s/%s (%d bytes)", bucket, s3_key, len(content))
    return content
