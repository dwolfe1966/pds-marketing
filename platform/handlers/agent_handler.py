"""
handlers.agent_handler
======================

AWS Lambda handler for the daily agent decisions phase.

Schedule : EventBridge cron(0 11 * * ? *)  — 11:00 UTC daily
Timeout  : 300 seconds
Memory   : 1024 MB
Layer    : sklearn-layer

Execution flow
--------------
1. Load credentials from Secrets Manager (lib.secrets)
2. Read today's campaign metrics from DynamoDB (lib.dynamo_store)
3. Reconstruct CampaignStats and KeywordStats dataclasses
4. Run decision engine: allocate_budget, adjust_bids
5. Run creative generation pipeline
6. Run keyword clustering and suggestions
7. Run experiment manager lifecycle
8. Run compliance scan on all creatives
9. Write full audit record to S3 + DynamoDB (lib.audit)
10. If DRY_RUN=false: apply mutations via platform SDK calls
11. Publish any CPA alerts to SNS

DRY_RUN mode
------------
DRY_RUN defaults to "true".  All recommendations are logged and written
to the audit trail, but no platform API mutations are made.  To enable
live writes, update the Lambda environment variable DRY_RUN=false.

Environment variables (set by CloudFormation)
---------------------------------------------
SECRET_NAME, METRICS_TABLE, AUDIT_TABLE, METRICS_BUCKET, ALERTS_TOPIC_ARN,
DRY_RUN, CPA_TARGET, CPA_ALERT_THRESHOLD, TARGET_LTV, TOTAL_BUDGET,
BUDGET_INCREASE_CAP, AWS_REGION_NAME, LOG_LEVEL
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime

# Inject credentials before importing ads_agent modules.
from lib.secrets import load_secrets
load_secrets(os.environ["SECRET_NAME"])

from ads_agent.decision_engine import (
    allocate_budget,
    adjust_bids,
    CampaignStats,
    KeywordStats,
)
from ads_agent.creative_generator import CreativeGenerator
from ads_agent.keyword_manager import QueryRecord, cluster_queries, suggest_keywords
from ads_agent.experiment_manager import ExperimentManager
from ads_agent.compliance_monitor import scan_text_for_policies, PolicyViolation
from ads_agent.monitoring import check_thresholds, Alert

from lib import dynamo_store, s3_store, sns_notifier, audit

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SECRET_NAME = os.environ["SECRET_NAME"]
METRICS_TABLE = os.environ["METRICS_TABLE"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]
METRICS_BUCKET = os.environ["METRICS_BUCKET"]
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
CPA_TARGET = float(os.environ.get("CPA_TARGET", "18.0"))
CPA_ALERT_THRESHOLD = float(os.environ.get("CPA_ALERT_THRESHOLD", "22.50"))
TARGET_LTV = float(os.environ.get("TARGET_LTV", "18.0"))
TOTAL_BUDGET = float(os.environ.get("TOTAL_BUDGET", "1000.0"))
BUDGET_INCREASE_CAP = float(os.environ.get("BUDGET_INCREASE_CAP", "0.20"))
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)

THRESHOLDS = {"cpa": {"max": CPA_ALERT_THRESHOLD}}
BANNED_TERMS = ["violence", "harassment", "illegal", "hack", "stalk", "free"]


# ---------------------------------------------------------------------------
# Data reconstruction from DynamoDB records
# ---------------------------------------------------------------------------

def _to_campaign_stats(metrics: list[dict]) -> list[CampaignStats]:
    """Convert DynamoDB campaign metric dicts to CampaignStats dataclasses."""
    stats: list[CampaignStats] = []
    for row in metrics:
        campaign_id = row.get("campaign_id", row.get("PK", "unknown"))
        cpa = float(row.get("cpa", 0.0))
        conversions = int(row.get("conversions", 0))
        budget = float(row.get("budget", 100.0))
        stats.append(CampaignStats(
            id=campaign_id,
            cpa=cpa if cpa > 0 else float("inf"),
            conversions=conversions,
            ltv=TARGET_LTV,
            max_scalable_spend=budget * 1.2,  # heuristic: 120% of current budget
        ))
    return stats


def _to_keyword_stats(metrics: list[dict]) -> list[KeywordStats]:
    """Build placeholder KeywordStats from campaign metrics.

    Until real keyword-level data flows from the ingestion phase, we create
    one representative KeywordStats per campaign for bid adjustment.  Replace
    with real keyword data once ingestion is fully implemented.
    """
    stats: list[KeywordStats] = []
    for row in metrics:
        campaign_id = row.get("campaign_id", "unknown")
        cpa = float(row.get("cpa", 0.0))
        conversions = int(row.get("conversions", 0))
        stats.append(KeywordStats(
            keyword=f"{campaign_id}_primary_kw",
            cpa=cpa if cpa > 0 else float("inf"),
            conversions=conversions,
            current_bid=1.0,
        ))
    return stats


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    run_id = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    run_date = date.today().isoformat()
    start_time = time.time()

    logger.info(
        '{"lambda":"RunAgent","run_id":"%s","dry_run":%s,"status":"started"}',
        run_id, str(DRY_RUN).lower(),
    )

    # ------------------------------------------------------------------
    # 1. Read today's metrics from DynamoDB
    # ------------------------------------------------------------------
    today_metrics = dynamo_store.get_today_metrics(METRICS_TABLE, run_date, REGION)
    if not today_metrics:
        logger.warning("No metrics found in DynamoDB for %s — aborting run", run_date)
        return {"status": "aborted", "reason": "no_metrics", "run_date": run_date}

    logger.info("Loaded %d campaign metrics from DynamoDB", len(today_metrics))

    # ------------------------------------------------------------------
    # 2. Check thresholds / produce alerts
    # ------------------------------------------------------------------
    alerts = check_thresholds(today_metrics, THRESHOLDS)
    if alerts and ALERTS_TOPIC_ARN:
        sns_notifier.publish_alerts(ALERTS_TOPIC_ARN, alerts, region=REGION)

    # ------------------------------------------------------------------
    # 3. Decision engine — budget allocation
    # ------------------------------------------------------------------
    campaign_stats = _to_campaign_stats(today_metrics)
    budget_allocations = allocate_budget(campaign_stats, total_budget=TOTAL_BUDGET)
    logger.info("Budget allocations: %s", budget_allocations)

    # Apply budget cap guard — reject unsafe increases
    safe_budget_allocations: dict[str, float] = {}
    for cid, proposed in budget_allocations.items():
        current = next(
            (float(m.get("budget", 0.0)) for m in today_metrics
             if m.get("campaign_id") == cid),
            proposed,
        )
        if audit.check_budget_cap(
            cid, current, proposed, BUDGET_INCREASE_CAP, ALERTS_TOPIC_ARN, REGION
        ):
            safe_budget_allocations[cid] = proposed
        else:
            safe_budget_allocations[cid] = current  # keep current if rejected

    # ------------------------------------------------------------------
    # 4. Decision engine — bid adjustment
    # ------------------------------------------------------------------
    keyword_stats = _to_keyword_stats(today_metrics)
    bid_updates_flat = adjust_bids(keyword_stats, target_cpa=CPA_TARGET)
    # Nest bid updates by campaign for audit and apply step
    bid_updates: dict[str, dict[str, float]] = {}
    for ks in keyword_stats:
        campaign_id = ks.keyword.replace("_primary_kw", "")
        if ks.keyword in bid_updates_flat:
            bid_updates.setdefault(campaign_id, {})[ks.keyword] = bid_updates_flat[ks.keyword]
    logger.info("Bid updates: %s", bid_updates)

    # ------------------------------------------------------------------
    # 5. Creative generation
    # ------------------------------------------------------------------
    generator = CreativeGenerator()
    prompt = generator.build_prompts("idlookup.ai")
    raw_creatives = generator.generate_raw_creatives(prompt)
    filtered_creatives = generator.filter_creatives(raw_creatives)
    selected_creatives = generator.select_top_creatives(filtered_creatives, {})
    logger.info("Selected %d creatives", len(selected_creatives))

    # ------------------------------------------------------------------
    # 6. Keyword management
    # ------------------------------------------------------------------
    # Placeholder query records — replace with real search term data
    # from ingestion once keyword-level fetch is implemented
    query_records = [
        QueryRecord(term=m.get("campaign_name", "people search"), clicks=int(m.get("clicks", 0)),
                    conversions=int(m.get("conversions", 0)), cost=float(m.get("cost", 0.0)))
        for m in today_metrics
    ]
    clusters = cluster_queries(query_records, n_clusters=min(5, max(1, len(query_records))))
    keyword_suggestions = suggest_keywords(clusters, cpa_target=CPA_TARGET)
    logger.info(
        "Keyword suggestions: %d positive, %d negative",
        len(keyword_suggestions.get("positive", [])),
        len(keyword_suggestions.get("negative", [])),
    )

    # ------------------------------------------------------------------
    # 7. Experiment management
    # ------------------------------------------------------------------
    exp_manager = ExperimentManager()
    # Experiments are proposed externally and stored; evaluation runs here.
    # Placeholder: evaluate with empty metrics until experiments are launched.
    exp_manager.evaluate_experiments({})

    # ------------------------------------------------------------------
    # 8. Compliance scan
    # ------------------------------------------------------------------
    compliance_violations: list[str] = []
    for creative in selected_creatives:
        text = f"{creative.headline} {creative.description}"
        found = scan_text_for_policies(text, BANNED_TERMS)
        if found:
            compliance_violations.append(f"{creative.headline}: {found}")
            logger.error("Compliance violation in creative '%s': %s", creative.headline, found)

    if compliance_violations:
        # Remove violating creatives from selection
        selected_creatives = [
            c for c in selected_creatives
            if not scan_text_for_policies(f"{c.headline} {c.description}", BANNED_TERMS)
        ]

    # ------------------------------------------------------------------
    # 9. Build recommendations payload and write audit record
    # ------------------------------------------------------------------
    recommendations = {
        "run_id": run_id,
        "run_date": run_date,
        "dry_run": DRY_RUN,
        "budget_allocations": safe_budget_allocations,
        "bid_updates": bid_updates,
        "keyword_suggestions": keyword_suggestions,
        "compliance_violations": compliance_violations,
        "creatives": [
            {"headline": c.headline, "description": c.description, "score": c.score}
            for c in selected_creatives
        ],
        "alerts": [
            {"entity_id": a.entity_id, "metric": a.metric, "value": a.value,
             "threshold": a.threshold, "direction": a.direction}
            for a in alerts
        ],
    }
    s3_key = s3_store.put_recommendations(METRICS_BUCKET, recommendations, REGION)

    audit.write_audit_record(
        audit_table=AUDIT_TABLE,
        bucket=METRICS_BUCKET,
        run_id=run_id,
        dry_run=DRY_RUN,
        budget_allocations=safe_budget_allocations,
        bid_updates=bid_updates,
        keyword_suggestions=keyword_suggestions,
        selected_creatives=selected_creatives,
        alerts=alerts,
        region=REGION,
    )

    # ------------------------------------------------------------------
    # 10. Apply mutations (only if DRY_RUN=false)
    # ------------------------------------------------------------------
    if DRY_RUN:
        logger.info(
            "DRY_RUN=true — recommendations written to %s. "
            "Set DRY_RUN=false to apply mutations.",
            s3_key,
        )
    else:
        logger.info("DRY_RUN=false — applying mutations")
        _apply_budget_changes(safe_budget_allocations)
        _apply_bid_changes(bid_updates)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    duration_ms = int((time.time() - start_time) * 1000)
    logger.info(
        '{"lambda":"RunAgent","run_id":"%s","dry_run":%s,"campaigns":%d,'
        '"alerts":%d,"creatives":%d,"duration_ms":%d,"s3":"%s","status":"completed"}',
        run_id, str(DRY_RUN).lower(), len(today_metrics),
        len(alerts), len(selected_creatives), duration_ms, s3_key,
    )

    return {
        "status": "completed",
        "run_id": run_id,
        "run_date": run_date,
        "dry_run": DRY_RUN,
        "campaigns_processed": len(today_metrics),
        "alerts_fired": len(alerts),
        "creatives_selected": len(selected_creatives),
        "compliance_violations": len(compliance_violations),
        "recommendations_s3": s3_key,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Mutation stubs — replace with real SDK calls in Phase 2 and 3
# ---------------------------------------------------------------------------

def _apply_budget_changes(budget_allocations: dict[str, float]) -> None:
    """Apply budget changes via Google Ads and Bing APIs.

    TODO (Phase 2): Implement GoogleAdsManager.update_campaign_budgets()
    TODO (Phase 3): Implement BingAdsManager.update_campaign_budgets()
    """
    for campaign_id, new_budget in budget_allocations.items():
        platform = audit._infer_platform(campaign_id)
        logger.info(
            "[LIVE] Would set %s campaign %s daily budget to $%.2f",
            platform, campaign_id, new_budget,
        )


def _apply_bid_changes(bid_updates: dict[str, dict[str, float]]) -> None:
    """Apply keyword bid changes via Google Ads and Bing APIs.

    TODO (Phase 2): Implement GoogleAdsManager.update_keyword_bids()
    TODO (Phase 3): Implement BingAdsManager.update_keyword_bids()
    """
    for campaign_id, kw_bids in bid_updates.items():
        platform = audit._infer_platform(campaign_id)
        for keyword, new_bid in kw_bids.items():
            logger.info(
                "[LIVE] Would set %s campaign %s keyword '%s' bid to $%.4f",
                platform, campaign_id, keyword, new_bid,
            )
