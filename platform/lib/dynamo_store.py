"""
platform.lib.dynamo_store
=========================

DynamoDB read/write wrappers for the ads agent platform.

Tables
------
CampaignMetrics
    Partition key : PK  (str)  "CAMPAIGN#<platform>#<campaign_id>"
    Sort key      : SK  (str)  "DATE#<YYYY-MM-DD>"
    GSI ByDate    : SK (partition) + PK (sort)
    GSI ByPlatform: platform (partition) + SK (sort)

AuditTrail
    Partition key : PK  (str)  "RUN#<ISO-datetime>"
    Sort key      : SK  (str)  "ACTION#<action_type>#<entity_id>"
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# TTL constants
METRICS_TTL_DAYS = 400
AUDIT_TTL_DAYS = 730


def _dynamo_safe(value: Any) -> Any:
    """Convert floats to Decimal for DynamoDB compatibility."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value


def _ttl(days: int) -> int:
    return int(time.time()) + days * 86400


# ---------------------------------------------------------------------------
# CampaignMetrics table
# ---------------------------------------------------------------------------

def write_campaign_metrics(
    table_name: str,
    campaigns: list[dict[str, Any]],
    run_date: str | None = None,
    region: str = "us-east-1",
) -> int:
    """Write normalized campaign dicts to the CampaignMetrics table.

    Args:
        table_name: DynamoDB table name.
        campaigns: List of normalized campaign dicts from
            ads_agent.transformation.normalize_campaigns().
        run_date: Date string YYYY-MM-DD.  Defaults to today.
        region: AWS region.

    Returns:
        Number of items written.
    """
    run_date = run_date or date.today().isoformat()
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    ttl_value = _ttl(METRICS_TTL_DAYS)

    written = 0
    with table.batch_writer() as batch:
        for camp in campaigns:
            platform = camp.get("platform", "unknown")
            campaign_id = str(camp.get("campaign_id") or camp.get("id", "unknown"))
            item = {
                "PK": f"CAMPAIGN#{platform}#{campaign_id}",
                "SK": f"DATE#{run_date}",
                "platform": platform,
                "campaign_id": campaign_id,
                "campaign_name": camp.get("campaign_name") or camp.get("name", ""),
                "date": run_date,
                "cost": _dynamo_safe(camp.get("cost", 0.0)),
                "impressions": _dynamo_safe(camp.get("impressions", 0)),
                "clicks": _dynamo_safe(camp.get("clicks", 0)),
                "conversions": _dynamo_safe(camp.get("conversions", 0)),
                "cpa": _dynamo_safe(camp.get("cpa", 0.0)),
                "ctr": _dynamo_safe(camp.get("ctr", 0.0)),
                "budget": _dynamo_safe(camp.get("budget", 0.0)),
                "ttl": ttl_value,
            }
            batch.put_item(Item=item)
            written += 1

    logger.info("Wrote %d campaign metrics for date %s", written, run_date)
    return written


def get_today_metrics(
    table_name: str,
    run_date: str | None = None,
    region: str = "us-east-1",
) -> list[dict[str, Any]]:
    """Read all campaign metrics for a given date via the ByDate GSI.

    Args:
        table_name: DynamoDB table name.
        run_date: Date string YYYY-MM-DD.  Defaults to today.
        region: AWS region.

    Returns:
        List of campaign metric dicts (DynamoDB Decimal values converted to float).
    """
    run_date = run_date or date.today().isoformat()
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    response = table.query(
        IndexName="ByDate",
        KeyConditionExpression=Key("SK").eq(f"DATE#{run_date}"),
    )
    items = response.get("Items", [])

    # Handle pagination
    while "LastEvaluatedKey" in response:
        response = table.query(
            IndexName="ByDate",
            KeyConditionExpression=Key("SK").eq(f"DATE#{run_date}"),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    # Convert Decimal → float for ads_agent compatibility
    return [_decimal_to_float(item) for item in items]


def get_metrics_range(
    table_name: str,
    platform: str,
    days: int = 30,
    region: str = "us-east-1",
) -> list[dict[str, Any]]:
    """Read campaign metrics for a platform over the last N days via ByPlatform GSI.

    Used for LTV modeling and historical trend analysis.

    Args:
        table_name: DynamoDB table name.
        platform: "google" or "microsoft".
        days: Number of days to look back.
        region: AWS region.

    Returns:
        List of campaign metric dicts ordered by date descending.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    start_date = (date.today() - timedelta(days=days)).isoformat()
    end_date = date.today().isoformat()

    response = table.query(
        IndexName="ByPlatform",
        KeyConditionExpression=(
            Key("platform").eq(platform) &
            Key("SK").between(f"DATE#{start_date}", f"DATE#{end_date}")
        ),
        ScanIndexForward=False,
    )
    items = response.get("Items", [])

    while "LastEvaluatedKey" in response:
        response = table.query(
            IndexName="ByPlatform",
            KeyConditionExpression=(
                Key("platform").eq(platform) &
                Key("SK").between(f"DATE#{start_date}", f"DATE#{end_date}")
            ),
            ScanIndexForward=False,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    logger.info("Retrieved %d metric rows for platform=%s over %d days", len(items), platform, days)
    return [_decimal_to_float(item) for item in items]


# ---------------------------------------------------------------------------
# AuditTrail table
# ---------------------------------------------------------------------------

def write_audit_actions(
    table_name: str,
    run_id: str,
    actions: list[dict[str, Any]],
    region: str = "us-east-1",
) -> int:
    """Write a batch of audit action records to the AuditTrail table.

    Args:
        table_name: DynamoDB table name.
        run_id: ISO datetime string identifying this agent run.
        actions: List of action dicts.  Each must contain at minimum:
            action_type (str), entity_id (str).
        region: AWS region.

    Returns:
        Number of items written.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    ttl_value = _ttl(AUDIT_TTL_DAYS)

    written = 0
    with table.batch_writer() as batch:
        for action in actions:
            action_type = action.get("action_type", "UNKNOWN")
            entity_id = str(action.get("entity_id", "unknown"))
            item = {
                "PK": f"RUN#{run_id}",
                "SK": f"ACTION#{action_type}#{entity_id}",
                "run_id": run_id,
                "action_type": action_type,
                "entity_id": entity_id,
                "entity_platform": action.get("entity_platform", ""),
                "before_value": _dynamo_safe(action.get("before_value")),
                "after_value": _dynamo_safe(action.get("after_value")),
                "dry_run": action.get("dry_run", True),
                "applied": action.get("applied", False),
                "s3_ref": action.get("s3_ref", ""),
                "alert_type": action.get("alert_type", ""),
                "alert_value": _dynamo_safe(action.get("alert_value")),
                "alert_threshold": _dynamo_safe(action.get("alert_threshold")),
                "ttl": ttl_value,
            }
            # Remove None values — DynamoDB does not accept None
            item = {k: v for k, v in item.items() if v is not None}
            batch.put_item(Item=item)
            written += 1

    logger.info("Wrote %d audit records for run %s", written, run_id)
    return written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decimal_to_float(obj: Any) -> Any:
    """Recursively convert Decimal values to float for ads_agent compatibility."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(v) for v in obj]
    return obj
