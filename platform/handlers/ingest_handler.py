"""
handlers.ingest_handler
=======================

AWS Lambda handler for the daily metrics ingestion phase.

Schedule : EventBridge cron(0 10 * * ? *)  — 10:00 UTC daily
Timeout  : 900 seconds
Memory   : 1024 MB
Layer    : sklearn-layer

Execution flow
--------------
1. Load credentials from Secrets Manager (lib.secrets)
2. Instantiate Google and Microsoft connectors (ads_agent.ingestion)
3. For Bing: invoke the BingReportPoller Step Functions state machine
   synchronously to handle async report polling without blocking Lambda
4. Fetch all raw data (ads_agent.ingestion.fetch_all_data)
5. Normalize and aggregate (ads_agent.transformation)
6. Check thresholds and produce alerts (ads_agent.monitoring)
7. Write metrics to DynamoDB (lib.dynamo_store)
8. Write raw + normalized snapshots to S3 (lib.s3_store)
9. Publish any alerts to SNS (lib.sns_notifier)

Environment variables (set by CloudFormation)
---------------------------------------------
SECRET_NAME, METRICS_TABLE, METRICS_BUCKET, ALERTS_TOPIC_ARN,
BING_STATE_MACHINE_ARN, CPA_TARGET, CPA_ALERT_THRESHOLD, TARGET_LTV,
AWS_REGION_NAME, LOG_LEVEL
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime

import boto3

# Inject credentials before importing ads_agent modules so that
# GoogleAdsConnector and MicrosoftAdsConnector pick up os.getenv() values.
from lib.secrets import load_secrets
load_secrets(os.environ["SECRET_NAME"])

from ads_agent.ingestion import (
    GoogleAdsConnector,
    MicrosoftAdsConnector,
    fetch_all_data,
)
from ads_agent.transformation import normalize_campaigns, aggregate_metrics
from ads_agent.monitoring import check_thresholds, summarize_alerts

from lib import dynamo_store, s3_store, sns_notifier

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
SECRET_NAME = os.environ["SECRET_NAME"]
METRICS_TABLE = os.environ["METRICS_TABLE"]
METRICS_BUCKET = os.environ["METRICS_BUCKET"]
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")
BING_STATE_MACHINE_ARN = os.environ.get("BING_STATE_MACHINE_ARN", "")
CPA_ALERT_THRESHOLD = float(os.environ.get("CPA_ALERT_THRESHOLD", "22.50"))
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)

# Threshold config passed to ads_agent.monitoring.check_thresholds
THRESHOLDS = {
    "cpa": {"max": CPA_ALERT_THRESHOLD},
}


# ---------------------------------------------------------------------------
# Bing Step Functions integration
# ---------------------------------------------------------------------------

def _invoke_bing_poller(date_str: str) -> dict | None:
    """Start the BingReportPoller state machine and wait for completion.

    Returns the SFN output dict on success, or None if SFN is not configured
    or the state machine fails (ingest continues with Google-only data).
    """
    if not BING_STATE_MACHINE_ARN:
        logger.warning("BING_STATE_MACHINE_ARN not set — skipping Bing report polling")
        return None

    sfn = boto3.client("stepfunctions", region_name=REGION)
    execution_name = f"bing-ingest-{date_str}-{int(time.time())}"
    input_payload = json.dumps({"date": date_str, "attempt": 0})

    logger.info("Starting BingReportPoller state machine: %s", execution_name)
    try:
        start_resp = sfn.start_execution(
            stateMachineArn=BING_STATE_MACHINE_ARN,
            name=execution_name,
            input=input_payload,
        )
        execution_arn = start_resp["executionArn"]
    except Exception as exc:
        logger.error("Failed to start BingReportPoller: %s", exc)
        return None

    # Poll for completion (Step Functions sync integration via SDK)
    for _ in range(60):  # max ~5 minutes polling
        time.sleep(5)
        desc = sfn.describe_execution(executionArn=execution_arn)
        status = desc["status"]
        if status == "SUCCEEDED":
            output = json.loads(desc.get("output", "{}"))
            logger.info("BingReportPoller succeeded: %s", output)
            return output
        if status in ("FAILED", "TIMED_OUT", "ABORTED"):
            logger.error("BingReportPoller %s: %s", status, desc.get("cause", ""))
            if ALERTS_TOPIC_ARN:
                sns_notifier.publish_error(
                    ALERTS_TOPIC_ARN,
                    f"BingReportPoller {status}",
                    RuntimeError(desc.get("cause", status)),
                    REGION,
                )
            return None

    logger.error("BingReportPoller timed out after polling")
    return None


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    run_date = date.today().isoformat()
    start_time = time.time()
    logger.info('{"lambda":"IngestMetrics","run_date":"%s","status":"started"}', run_date)

    # ------------------------------------------------------------------
    # 1. Instantiate connectors
    # ------------------------------------------------------------------
    try:
        google_connector = GoogleAdsConnector()
    except ValueError as exc:
        logger.error("GoogleAdsConnector init failed: %s", exc)
        google_connector = None

    try:
        microsoft_connector = MicrosoftAdsConnector()
    except ValueError as exc:
        logger.error("MicrosoftAdsConnector init failed: %s", exc)
        microsoft_connector = None

    # ------------------------------------------------------------------
    # 2. Bing: trigger async report polling via Step Functions
    # ------------------------------------------------------------------
    bing_s3_key: str | None = None
    if microsoft_connector is not None:
        bing_result = _invoke_bing_poller(run_date)
        if bing_result:
            bing_s3_key = bing_result.get("s3_key")

    # ------------------------------------------------------------------
    # 3. Fetch all raw data
    # ------------------------------------------------------------------
    connectors = [c for c in [google_connector, microsoft_connector] if c is not None]
    try:
        raw = fetch_all_data(connectors)
    except Exception as exc:
        logger.error("fetch_all_data failed: %s", exc)
        raw = {}

    # Write raw snapshots to S3
    for platform, platform_data in raw.items():
        s3_store.put_raw_metrics(METRICS_BUCKET, platform, platform_data, REGION)

    # ------------------------------------------------------------------
    # 4. Normalize and aggregate
    # ------------------------------------------------------------------
    normalized = normalize_campaigns(raw)
    agg = aggregate_metrics(normalized)
    logger.info("Aggregated metrics: %s", agg)

    # Write normalized data to S3
    s3_store.put_normalized(METRICS_BUCKET, normalized, REGION)

    # ------------------------------------------------------------------
    # 5. Check thresholds
    # ------------------------------------------------------------------
    alerts = check_thresholds(normalized, THRESHOLDS)
    if alerts:
        alert_summaries = summarize_alerts(alerts)
        logger.warning("Alerts: %s", alert_summaries)
        if ALERTS_TOPIC_ARN:
            sns_notifier.publish_alerts(ALERTS_TOPIC_ARN, alerts, region=REGION)

    # ------------------------------------------------------------------
    # 6. Write metrics to DynamoDB
    # ------------------------------------------------------------------
    written = dynamo_store.write_campaign_metrics(
        METRICS_TABLE, normalized, run_date, REGION
    )

    duration_ms = int((time.time() - start_time) * 1000)
    logger.info(
        '{"lambda":"IngestMetrics","run_date":"%s","campaigns_ingested":%d,'
        '"alerts_fired":%d,"duration_ms":%d,"status":"completed"}',
        run_date, written, len(alerts), duration_ms,
    )

    return {
        "status": "completed",
        "run_date": run_date,
        "campaigns_ingested": written,
        "alerts_fired": len(alerts),
        "duration_ms": duration_ms,
        "bing_report_s3_key": bing_s3_key,
    }
