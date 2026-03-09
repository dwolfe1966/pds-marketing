"""
Decision engine for budget and bid management
============================================

The :mod:`decision_engine` module contains core logic that decides how to
allocate budgets across campaigns and how to adjust bids for keywords or
ad groups.  These decisions are made based on historical performance,
predicted return, and business objectives such as target CPA and LTV.

This module implements simple heuristics and pseudo‑code from the
specification.  Developers can extend these base classes with more
sophisticated machine learning models or reinforcement learning
algorithms.  All decisions produced by this module should be logged
along with the metrics used to arrive at them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class CampaignStats:
    """Container for campaign performance statistics.

    Attributes:
        id: Unique identifier of the campaign.
        cpa: Cost per acquisition for the campaign.
        conversions: Number of conversions in the measurement window.
        ltv: Estimated lifetime value (revenue) from the campaign.
        max_scalable_spend: Optional upper bound on budget before diminishing returns.
    """

    id: str
    cpa: float
    conversions: int
    ltv: float
    max_scalable_spend: float


def allocate_budget(campaign_stats: List[CampaignStats], total_budget: float) -> Dict[str, float]:
    """Allocate a total budget across campaigns proportionally to efficiency.

    This function follows the simple proportional algorithm described in
    the specification: compute an efficiency score for each campaign
    (LTV divided by CPA), normalize across campaigns, and assign budget
    proportionally.  If a campaign's maximum scalable spend is lower than
    its proportional allocation, the remainder is distributed among other
    campaigns.

    Args:
        campaign_stats: List of :class:`CampaignStats` instances.
        total_budget: Total budget to distribute.

    Returns:
        Dictionary mapping campaign IDs to proposed budget allocations.
    """
    if total_budget <= 0 or not campaign_stats:
        return {}

    # Compute efficiency scores; avoid division by zero
    for c in campaign_stats:
        c.efficiency = (c.ltv / c.cpa) if c.cpa > 0 else 0

    total_efficiency = sum(c.efficiency for c in campaign_stats) or 1.0
    # Initial proportional allocations
    allocations = {}
    for c in campaign_stats:
        proposed = (c.efficiency / total_efficiency) * total_budget
        proposed = min(proposed, c.max_scalable_spend)
        allocations[c.id] = proposed

    # Distribute leftover budget if any
    leftover = total_budget - sum(allocations.values())
    if leftover > 0:
        scalable = [c for c in campaign_stats if allocations[c.id] < c.max_scalable_spend]
        if scalable:
            per_campaign_extra = leftover / len(scalable)
            for c in scalable:
                additional = min(per_campaign_extra, c.max_scalable_spend - allocations[c.id])
                allocations[c.id] += additional
    return allocations


@dataclass
class KeywordStats:
    """Container for keyword performance statistics used in bid adjustments.

    Attributes:
        keyword: The keyword text.
        cpa: Cost per acquisition for this keyword.
        conversions: Number of conversions attributed to this keyword.
        current_bid: Current maximum CPC bid for the keyword.
    """

    keyword: str
    cpa: float
    conversions: int
    current_bid: float


def adjust_bids(keyword_stats: List[KeywordStats], target_cpa: float) -> Dict[str, float]:
    """Adjust keyword bids based on performance against a target CPA.

    This function implements the pseudo‑code from the specification: if
    a keyword has sufficient conversions and its CPA is above the target,
    decrease its bid proportionally; if the CPA is below target, raise the
    bid slightly to capture more volume; if there is insufficient data,
    leave the bid unchanged.

    Args:
        keyword_stats: List of :class:`KeywordStats` objects.
        target_cpa: Desired cost per acquisition threshold.

    Returns:
        Dictionary mapping keyword strings to updated bid values.
    """
    updated = {}
    for kw in keyword_stats:
        # Only adjust bids if there is meaningful conversion data
        if kw.conversions >= 5:
            if kw.cpa > target_cpa and kw.cpa > 0:
                factor = target_cpa / kw.cpa
                updated[kw.keyword] = max(0.01, kw.current_bid * factor)
            else:
                updated[kw.keyword] = kw.current_bid * 1.1
        else:
            updated[kw.keyword] = kw.current_bid
    return updated