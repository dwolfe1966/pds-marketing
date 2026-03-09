"""
Monitoring and alerting functions
================================

The :mod:`monitoring` module contains logic for evaluating advertising
performance metrics against predefined thresholds and emitting alerts
when anomalies occur.  It is responsible for the *observe* stage of
the agent's cycle: after data has been ingested and transformed into
high‑level metrics (e.g., CPA, conversion rate, CTR), the monitoring
module checks whether any metric deviates significantly from desired
targets.  For example, if cost per acquisition (CPA) rises above a
specified threshold or if conversions drop sharply, an alert is
generated.

Alerts are represented as simple dictionaries so that they can be
serialized into logs or passed to downstream components.  Each alert
includes context about the entity being monitored (campaign, ad group,
keyword) and the metric values that triggered the alert.

This module intentionally avoids side effects—actions in response to
alerts are handled by the decision engine.  Developers can extend this
module to integrate with monitoring services (e.g., Slack, PagerDuty)
or dashboards.

Example usage::

    from ads_agent.monitoring import check_thresholds

    alerts = check_thresholds(campaign_metrics, thresholds)
    for alert in alerts:
        print(f"Alert for {alert['entity_id']}: {alert['metric']} {alert['value']} outside target")

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Iterable


@dataclass
class Alert:
    """Structured representation of an alert.

    Attributes:
        entity_id: Identifier for the campaign/ad group/keyword that triggered the alert.
        metric: Name of the metric (e.g., ``cpa``, ``conversion_rate``) that exceeded the threshold.
        value: The observed value of the metric.
        threshold: The threshold value that was violated.
        direction: ``'above'`` if the value exceeded the maximum threshold, ``'below'`` if it fell below the minimum.
        context: Optional dictionary with additional information (e.g., time period, channel).
    """

    entity_id: str
    metric: str
    value: float
    threshold: float
    direction: str
    context: Dict[str, Any] | None = None


def check_thresholds(
    metrics: Iterable[Dict[str, Any]],
    thresholds: Dict[str, Dict[str, float]],
) -> List[Alert]:
    """Evaluate metrics against channel‑specific thresholds.

    Args:
        metrics: An iterable of dictionaries, each representing performance
            metrics for a single entity (campaign, ad group, etc.).  Each
            dict should include a unique ``entity_id`` key as well as
            metrics such as ``cpa``, ``conversion_rate``, and others.
        thresholds: A mapping from metric names to a dictionary with
            ``min`` and/or ``max`` values.  For example::

                thresholds = {
                    "cpa": {"max": 37.5},
                    "conversion_rate": {"min": 0.02}
                }

    Returns:
        A list of :class:`Alert` objects for any metrics that fall outside
        their defined thresholds.  If no thresholds are violated, the list
        will be empty.

    The function does not raise exceptions for missing metrics; metrics
    without thresholds are ignored.  Threshold definitions can include only
    ``min`` or only ``max``, or both.
    """
    alerts: List[Alert] = []
    for row in metrics:
        entity_id = row.get("entity_id")
        for metric_name, bounds in thresholds.items():
            if metric_name not in row:
                continue
            value = row[metric_name]
            if "max" in bounds and value > bounds["max"]:
                alerts.append(
                    Alert(
                        entity_id=entity_id,
                        metric=metric_name,
                        value=float(value),
                        threshold=float(bounds["max"]),
                        direction="above",
                        context={k: v for k, v in row.items() if k != metric_name},
                    )
                )
            if "min" in bounds and value < bounds["min"]:
                alerts.append(
                    Alert(
                        entity_id=entity_id,
                        metric=metric_name,
                        value=float(value),
                        threshold=float(bounds["min"]),
                        direction="below",
                        context={k: v for k, v in row.items() if k != metric_name},
                    )
                )
    return alerts


def summarize_alerts(alerts: List[Alert]) -> List[str]:
    """Convert a list of alerts into human‑readable summary strings.

    This helper can be used to generate messages for dashboards or
    notifications.  Each summary includes the entity ID, metric, value,
    threshold, and direction of violation.

    Args:
        alerts: List of :class:`Alert` objects.

    Returns:
        List of strings summarizing each alert.
    """
    summaries: List[str] = []
    for alert in alerts:
        direction_word = "exceeds" if alert.direction == "above" else "is below"
        summaries.append(
            f"{alert.entity_id}: {alert.metric} {direction_word} threshold"
            f" (value={alert.value:.2f}, threshold={alert.threshold:.2f})"
        )
    return summaries