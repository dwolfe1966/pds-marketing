"""
platform.lib.sns_notifier
=========================

SNS publish helpers for the ads agent platform.

Publishes two types of messages:
  - Performance alerts (CPA threshold breaches) from ads_agent.monitoring
  - Budget cap rejection alerts from lib.audit
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def publish_alerts(
    topic_arn: str,
    alerts: list[Any],
    subject: str = "Advertising Agent — Performance Alert",
    region: str = "us-east-1",
) -> int:
    """Publish a list of Alert objects to SNS.

    Accepts ads_agent.monitoring.Alert dataclass instances or plain dicts.

    Args:
        topic_arn: ARN of the SNS topic.
        alerts: List of Alert dataclass instances or dicts.
        subject: Email subject line (max 100 chars for SNS).
        region: AWS region.

    Returns:
        Number of messages published (1 batched message, or 0 if no alerts).
    """
    if not alerts:
        return 0

    lines: list[str] = []
    for alert in alerts:
        if hasattr(alert, "entity_id"):
            # ads_agent.monitoring.Alert dataclass
            direction = "exceeds max" if alert.direction == "above" else "is below min"
            lines.append(
                f"  Campaign {alert.entity_id}: {alert.metric} {direction} "
                f"(value={alert.value:.2f}, threshold={alert.threshold:.2f})"
            )
        elif isinstance(alert, dict):
            lines.append(f"  {alert}")
        else:
            lines.append(f"  {alert!s}")

    message = "Advertising Agent Alerts:\n\n" + "\n".join(lines)
    _publish(topic_arn, subject, message, region)
    logger.warning("Published %d alerts to SNS", len(alerts))
    return 1


def publish_budget_cap_rejection(
    topic_arn: str,
    campaign_id: str,
    current_budget: float,
    proposed_budget: float,
    cap: float,
    region: str = "us-east-1",
) -> None:
    """Publish an SNS alert when a budget increase exceeds the safety cap.

    Args:
        topic_arn: ARN of the SNS topic.
        campaign_id: Campaign that triggered the rejection.
        current_budget: Current daily budget in USD.
        proposed_budget: Proposed daily budget in USD.
        cap: The cap fraction that was exceeded (e.g., 0.20 for 20%).
        region: AWS region.
    """
    pct = ((proposed_budget - current_budget) / current_budget * 100) if current_budget else 0
    message = (
        f"BUDGET CAP REJECTION\n\n"
        f"Campaign  : {campaign_id}\n"
        f"Current   : ${current_budget:.2f}\n"
        f"Proposed  : ${proposed_budget:.2f}\n"
        f"Increase  : {pct:.1f}%  (cap is {cap * 100:.0f}%)\n\n"
        f"The proposed budget change was rejected and NOT applied. "
        f"Review and apply manually if intentional."
    )
    _publish(
        topic_arn,
        "Advertising Agent — Budget Cap Rejection",
        message,
        region,
    )
    logger.error(
        "Budget cap rejection for campaign %s: $%.2f → $%.2f (%.1f%% increase, cap %.0f%%)",
        campaign_id, current_budget, proposed_budget, pct, cap * 100,
    )


def publish_error(
    topic_arn: str,
    context: str,
    error: Exception,
    region: str = "us-east-1",
) -> None:
    """Publish an unhandled error notification to SNS.

    Args:
        topic_arn: ARN of the SNS topic.
        context: Short description of what was happening when the error occurred.
        error: The exception instance.
        region: AWS region.
    """
    message = (
        f"Advertising Agent — Unhandled Error\n\n"
        f"Context : {context}\n"
        f"Error   : {type(error).__name__}: {error}\n"
    )
    _publish(topic_arn, "Advertising Agent — Error", message, region)


def _publish(topic_arn: str, subject: str, message: str, region: str) -> None:
    sns = boto3.client("sns", region_name=region)
    sns.publish(
        TopicArn=topic_arn,
        Subject=subject[:100],  # SNS subject limit
        Message=message,
    )
