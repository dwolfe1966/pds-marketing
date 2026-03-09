"""
platform.lib.audit
==================

Audit trail writer and budget safety guard.

Every recommendation or mutation the agent makes — whether applied or dry-run
— is recorded here before any platform API call is made.  This provides a
complete, immutable log of agent decisions that can be reviewed before
enabling live writes.

Budget cap
----------
check_budget_cap() enforces the 20% maximum single-step budget increase rule.
It is called before every budget mutation regardless of DRY_RUN mode.
Rejections are logged, SNS-alerted, and recorded in the audit trail.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from lib import dynamo_store, s3_store, sns_notifier

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_INCREASE_CAP = float(os.environ.get("BUDGET_INCREASE_CAP", "0.20"))


# ---------------------------------------------------------------------------
# Budget safety guard
# ---------------------------------------------------------------------------

def check_budget_cap(
    campaign_id: str,
    current_budget: float,
    proposed_budget: float,
    cap: float = DEFAULT_BUDGET_INCREASE_CAP,
    topic_arn: str | None = None,
    region: str = "us-east-1",
) -> bool:
    """Check whether a proposed budget increase is within the safety cap.

    Returns True if the change is acceptable, False if it must be rejected.
    When rejected, logs an error and optionally publishes an SNS alert.

    Budget decreases are always allowed.  The cap applies only to increases.

    Args:
        campaign_id: Campaign identifier (for logging and alerting).
        current_budget: Current daily budget in USD.
        proposed_budget: Proposed new daily budget in USD.
        cap: Maximum allowed fractional increase (default 0.20 = 20%).
        topic_arn: SNS topic ARN for rejection alerts.  No alert if None.
        region: AWS region.

    Returns:
        True if the change is safe to apply, False if it must be rejected.
    """
    if current_budget <= 0:
        logger.warning(
            "Campaign %s has current_budget=%.2f; skipping cap check.",
            campaign_id, current_budget,
        )
        return True

    if proposed_budget <= current_budget:
        # Decrease or no change — always safe
        return True

    increase_fraction = (proposed_budget - current_budget) / current_budget
    if increase_fraction <= cap:
        return True

    # Rejected
    logger.error(
        "Budget cap REJECTED for campaign %s: $%.2f → $%.2f (%.1f%% > %.0f%% cap)",
        campaign_id, current_budget, proposed_budget, increase_fraction * 100, cap * 100,
    )
    if topic_arn:
        sns_notifier.publish_budget_cap_rejection(
            topic_arn=topic_arn,
            campaign_id=campaign_id,
            current_budget=current_budget,
            proposed_budget=proposed_budget,
            cap=cap,
            region=region,
        )
    return False


# ---------------------------------------------------------------------------
# Audit record builder
# ---------------------------------------------------------------------------

def build_audit_record(
    run_id: str,
    dry_run: bool,
    budget_allocations: dict[str, float],
    bid_updates: dict[str, dict[str, float]],
    keyword_suggestions: dict[str, list[str]],
    selected_creatives: list[Any],
    alerts: list[Any],
    experiment_configs: list[Any] | None = None,
    s3_ref: str = "",
) -> list[dict[str, Any]]:
    """Convert agent outputs into a flat list of audit action dicts.

    Each dict matches the AuditTrail DynamoDB schema and can be passed
    directly to dynamo_store.write_audit_actions().

    Args:
        run_id: ISO datetime string for this agent run.
        dry_run: Whether mutations were applied or only logged.
        budget_allocations: {campaign_id: new_daily_budget}
        bid_updates: {campaign_id: {keyword: new_bid}}
        keyword_suggestions: {"positive": [...], "negative": [...]}
        selected_creatives: List of Creative dataclass instances.
        alerts: List of Alert dataclass instances.
        experiment_configs: Optional list of ExperimentConfig instances.
        s3_ref: S3 key of the recommendations JSON for cross-reference.

    Returns:
        List of action dicts ready to write to DynamoDB.
    """
    actions: list[dict[str, Any]] = []

    # Budget changes
    for campaign_id, new_budget in budget_allocations.items():
        actions.append({
            "action_type": "BUDGET_CHANGE",
            "entity_id": campaign_id,
            "entity_platform": _infer_platform(campaign_id),
            "after_value": new_budget,
            "dry_run": dry_run,
            "applied": not dry_run,
            "s3_ref": s3_ref,
        })

    # Bid changes
    for campaign_id, kw_bids in bid_updates.items():
        for keyword, new_bid in kw_bids.items():
            actions.append({
                "action_type": "BID_CHANGE",
                "entity_id": keyword,
                "entity_platform": _infer_platform(campaign_id),
                "after_value": new_bid,
                "dry_run": dry_run,
                "applied": not dry_run,
                "s3_ref": s3_ref,
            })

    # Keyword suggestions
    for kw in keyword_suggestions.get("positive", []):
        actions.append({
            "action_type": "KEYWORD_ADD",
            "entity_id": kw,
            "dry_run": dry_run,
            "applied": not dry_run,
            "s3_ref": s3_ref,
        })
    for kw in keyword_suggestions.get("negative", []):
        actions.append({
            "action_type": "KEYWORD_NEGATE",
            "entity_id": kw,
            "dry_run": dry_run,
            "applied": not dry_run,
            "s3_ref": s3_ref,
        })

    # Selected creatives
    for creative in selected_creatives:
        headline = getattr(creative, "headline", str(creative))
        actions.append({
            "action_type": "CREATIVE_SELECT",
            "entity_id": headline[:64],
            "dry_run": dry_run,
            "applied": False,  # creatives require manual upload; never auto-applied
            "s3_ref": s3_ref,
        })

    # Experiments
    for config in (experiment_configs or []):
        name = getattr(config, "name", str(config))
        actions.append({
            "action_type": "EXPERIMENT_LAUNCH",
            "entity_id": name[:64],
            "dry_run": dry_run,
            "applied": not dry_run,
            "s3_ref": s3_ref,
        })

    # Alerts
    for alert in alerts:
        actions.append({
            "action_type": "ALERT",
            "entity_id": getattr(alert, "entity_id", "unknown"),
            "alert_type": getattr(alert, "metric", "unknown"),
            "alert_value": getattr(alert, "value", None),
            "alert_threshold": getattr(alert, "threshold", None),
            "dry_run": False,
            "applied": True,
            "s3_ref": s3_ref,
        })

    return actions


def write_audit_record(
    audit_table: str,
    bucket: str,
    run_id: str,
    dry_run: bool,
    budget_allocations: dict[str, float],
    bid_updates: dict[str, dict[str, float]],
    keyword_suggestions: dict[str, list[str]],
    selected_creatives: list[Any],
    alerts: list[Any],
    experiment_configs: list[Any] | None = None,
    region: str = "us-east-1",
) -> str:
    """Write a full audit record to both S3 and DynamoDB.

    Args:
        audit_table: DynamoDB AuditTrail table name.
        bucket: S3 bucket for audit JSON.
        run_id: ISO datetime string for this run.
        dry_run: Whether mutations were applied or only logged.
        budget_allocations: {campaign_id: new_budget}
        bid_updates: {campaign_id: {keyword: new_bid}}
        keyword_suggestions: {"positive": [...], "negative": [...]}
        selected_creatives: List of Creative instances.
        alerts: List of Alert instances.
        experiment_configs: Optional list of ExperimentConfig instances.
        region: AWS region.

    Returns:
        S3 key of the audit JSON.
    """
    # Build full audit payload for S3
    payload: dict[str, Any] = {
        "run_id": run_id,
        "dry_run": dry_run,
        "budget_allocations": budget_allocations,
        "bid_updates": bid_updates,
        "keyword_suggestions": keyword_suggestions,
        "selected_creatives": [
            {"headline": getattr(c, "headline", ""), "description": getattr(c, "description", "")}
            for c in selected_creatives
        ],
        "alerts": [
            {
                "entity_id": getattr(a, "entity_id", ""),
                "metric": getattr(a, "metric", ""),
                "value": getattr(a, "value", None),
                "threshold": getattr(a, "threshold", None),
                "direction": getattr(a, "direction", ""),
            }
            for a in alerts
        ],
        "experiment_configs": [
            {"name": getattr(c, "name", str(c))}
            for c in (experiment_configs or [])
        ],
    }

    # Write to S3 first to get the key for DynamoDB cross-reference
    s3_key = s3_store.put_audit(bucket, run_id, payload, region)

    # Build and write flat action list to DynamoDB
    actions = build_audit_record(
        run_id=run_id,
        dry_run=dry_run,
        budget_allocations=budget_allocations,
        bid_updates=bid_updates,
        keyword_suggestions=keyword_suggestions,
        selected_creatives=selected_creatives,
        alerts=alerts,
        experiment_configs=experiment_configs,
        s3_ref=s3_key,
    )
    dynamo_store.write_audit_actions(audit_table, run_id, actions, region)

    logger.info(
        "Audit record written: run_id=%s dry_run=%s actions=%d s3=%s",
        run_id, dry_run, len(actions), s3_key,
    )
    return s3_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_platform(campaign_id: str) -> str:
    """Infer platform from campaign ID prefix convention.

    Google campaign IDs are numeric; Bing campaign IDs are prefixed with
    'bing_' in our normalized data model.
    """
    if str(campaign_id).lower().startswith("bing"):
        return "microsoft"
    return "google"
