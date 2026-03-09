"""
ads_agent.transformation
========================

This module contains functions for transforming raw advertising data into
normalized, aggregated metrics suitable for analysis and optimization.  The
goal is to create a consistent schema across platforms so that campaign
performance can be compared and combined.

Functions
---------

``normalize_campaigns``
    Convert platform‑specific campaign dictionaries into a common format.

``aggregate_metrics``
    Compute aggregated metrics such as CPA, conversion rate, ROAS, etc.

``compute_ltv``
    Estimate lifetime value per channel or campaign using subscription data.
"""

from __future__ import annotations
from typing import Any, Dict, List, Tuple


def normalize_campaigns(raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize raw campaign data from multiple platforms into a unified schema.

    Parameters
    ----------
    raw_data : Dict[str, Any]
        A dictionary keyed by platform with raw campaign data returned from
        `ads_agent.ingestion.fetch_all_data()`.

    Returns
    -------
    List[Dict[str, Any]]
        A list of normalized campaign dictionaries with fields:

        * `platform`: ``str`` indicating the source (e.g., 'google', 'microsoft')
        * `campaign_id`: unique campaign ID
        * `campaign_name`: human‑readable name
        * `cost`: total cost for the period
        * `impressions`: total impressions
        * `clicks`: total clicks
        * `conversions`: total conversions
        * Additional fields may be added as needed.

    Notes
    -----
    Since the connectors currently return empty lists, this function returns
    an empty list.  When raw data is available, map each platform's fields
    into the above schema.
    """
    normalized: List[Dict[str, Any]] = []
    for platform, datasets in raw_data.items():
        campaigns = datasets.get("campaigns", [])
        for camp in campaigns:
            # Example mapping; adjust field names based on actual API response.
            cost = (
                camp.get("cost_micros", 0) / 1e6
                if camp.get("cost_micros") is not None
                else camp.get("cost", 0)
            )
            conversions = camp.get("conversions", 0)
            clicks = camp.get("clicks", 0)
            impressions = camp.get("impressions", 0)
            budget = (
                camp.get("budget_micros", 0) / 1e6
                if camp.get("budget_micros") is not None
                else camp.get("budget", 0)
            )
            normalized.append({
                "platform": platform,
                "campaign_id": str(camp.get("id") or camp.get("campaign_id", "")),
                "campaign_name": camp.get("name") or camp.get("campaign_name", ""),
                "cost": cost,
                "impressions": impressions,
                "clicks": clicks,
                "conversions": conversions,
                "budget": budget,
                "cpa": cost / conversions if conversions > 0 else float("inf"),
                "ctr": clicks / impressions if impressions > 0 else 0.0,
            })
    return normalized


def aggregate_metrics(campaigns: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """
    Aggregate normalized campaign data to compute key performance metrics.

    Parameters
    ----------
    campaigns : List[Dict[str, Any]]
        List of normalized campaign dictionaries.

    Returns
    -------
    Dict[str, Dict[str, float]]
        A dictionary keyed by platform and global total containing aggregated
        metrics.  Each inner dict contains:

        * `total_cost`
        * `total_conversions`
        * `cpa`: cost per acquisition (cost / conversions) or ``float('inf')`` if no conversions
        * `ctr`: click‑through rate (clicks / impressions) or 0 if no impressions

    Example
    -------
    >>> result = aggregate_metrics([{"platform": "google", "cost": 100, "impressions": 1000, "clicks": 50, "conversions": 5}])
    >>> result["google"]["cpa"]
    20.0
    """
    summary: Dict[str, Dict[str, float]] = {}
    for camp in campaigns:
        plat = camp["platform"]
        plat_sum = summary.setdefault(plat, {"total_cost": 0.0, "total_conversions": 0.0, "total_clicks": 0.0, "total_impressions": 0.0})
        plat_sum["total_cost"] += camp.get("cost", 0.0)
        plat_sum["total_conversions"] += camp.get("conversions", 0.0)
        plat_sum["total_clicks"] += camp.get("clicks", 0.0)
        plat_sum["total_impressions"] += camp.get("impressions", 0.0)
    # Compute metrics
    for plat, stats in summary.items():
        conversions = stats["total_conversions"]
        impressions = stats["total_impressions"]
        clicks = stats["total_clicks"]
        stats["cpa"] = stats["total_cost"] / conversions if conversions else float("inf")
        stats["ctr"] = clicks / impressions if impressions else 0.0
    return summary


def compute_ltv(subscription_data: List[Tuple[str, float]]) -> Dict[str, float]:
    """
    Compute estimated lifetime value (LTV) per channel or campaign.

    Parameters
    ----------
    subscription_data : List[Tuple[str, float]]
        A list of tuples `(source, revenue)` where ``source`` could be a
        platform or campaign identifier and ``revenue`` is the total revenue
        generated from subscriptions attributed to that source over a 6‑month
        horizon.

    Returns
    -------
    Dict[str, float]
        A dictionary mapping each source to its average lifetime value.

    Notes
    -----
    This function is simplistic; in practice, LTV should account for churn
    rates, cohort behavior, and other variables.  It may be integrated
    with your subscription billing system to compute LTV per user.
    """
    totals: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for source, revenue in subscription_data:
        totals[source] = totals.get(source, 0.0) + revenue
        counts[source] = counts.get(source, 0) + 1
    ltv: Dict[str, float] = {}
    for source, total in totals.items():
        ltv[source] = total / counts[source] if counts[source] else 0.0
    return ltv