"""
handlers.bing_poll_handler
==========================

AWS Lambda handler invoked by the BingReportPoller Step Functions state machine.

This Lambda is NOT triggered directly by EventBridge.  It is the atomic unit
of the Bing report polling loop managed by the Step Functions state machine
(platform/infra/stepfunctions_bing.json).

The state machine calls this Lambda with three different task types:
  - "submit"   : Submit a new report request to the Bing Reporting Service
  - "poll"     : Check the status of a previously submitted report
  - "download" : Download a completed report CSV and write it to S3

Each call is fast (< 60 seconds) because the wait between poll attempts is
handled by a Step Functions Wait state at zero Lambda cost.

Environment variables
---------------------
SECRET_NAME, METRICS_BUCKET, AWS_REGION_NAME, LOG_LEVEL

Event schema
------------
{
    "task":              "submit" | "poll" | "download",
    "date":              "YYYY-MM-DD",
    "report_request_id": "...",   # present for poll and download tasks
    "attempt":           0,        # incremented by SFN between polls
}

Response schema
---------------
{
    "status":            "SUBMITTED" | "PENDING" | "COMPLETE" | "FAILED",
    "report_request_id": "...",
    "s3_key":            "...",    # present only on COMPLETE
    "attempt":           N,
}
"""

from __future__ import annotations

import csv
import io
import logging
import os
import urllib.request

from lib.secrets import load_secrets
load_secrets(os.environ["SECRET_NAME"])

import boto3

METRICS_BUCKET = os.environ["METRICS_BUCKET"]
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)


def lambda_handler(event: dict, context) -> dict:
    task = event.get("task", "poll")
    report_date = event.get("date", "")
    report_request_id = event.get("report_request_id", "")
    attempt = int(event.get("attempt", 0))

    logger.info(
        '{"lambda":"BingPoller","task":"%s","date":"%s","attempt":%d}',
        task, report_date, attempt,
    )

    if task == "submit":
        return _submit_report(report_date)
    elif task == "poll":
        return _poll_report(report_request_id, attempt)
    elif task == "download":
        return _download_report(report_request_id, report_date)
    else:
        logger.error("Unknown task type: %s", task)
        return {"status": "FAILED", "reason": f"unknown_task:{task}"}


def _submit_report(report_date: str) -> dict:
    """Submit a CampaignPerformanceReportRequest to the Bing Reporting Service.

    TODO (Phase 3): Replace stub with real bingads ReportingService call.

    The real implementation should:
    1. Build auth_data using credentials from environment (injected by lib.secrets)
    2. Instantiate ServiceClient("ReportingService", version=13, ...)
    3. Create a CampaignPerformanceReportRequest with the required columns
    4. Call reporting_service.SubmitGenerateReport(report_request)
    5. Return {"status": "SUBMITTED", "report_request_id": response.ReportRequestId}
    """
    logger.info("Submitting Bing report for date %s (stub — returns fake ID)", report_date)
    # Stub: return a placeholder request ID so the SFN loop can proceed in tests
    return {
        "status": "SUBMITTED",
        "report_request_id": f"stub-report-{report_date}",
        "date": report_date,
        "attempt": 0,
    }


def _poll_report(report_request_id: str, attempt: int) -> dict:
    """Check the status of a submitted Bing report.

    TODO (Phase 3): Replace stub with real bingads ReportingService call.

    The real implementation should:
    1. Build auth_data and instantiate ServiceClient("ReportingService", ...)
    2. Call reporting_service.PollGenerateReport(report_request_id)
    3. Map response.Status to PENDING / COMPLETE / FAILED
    4. Return the status and incremented attempt counter
    """
    logger.info(
        "Polling Bing report %s attempt %d (stub — always returns COMPLETE)",
        report_request_id, attempt,
    )
    # Stub: immediately complete so the SFN proceeds to download
    return {
        "status": "COMPLETE",
        "report_request_id": report_request_id,
        "attempt": attempt + 1,
    }


def _download_report(report_request_id: str, report_date: str) -> dict:
    """Download a completed Bing report and write the CSV to S3.

    TODO (Phase 3): Replace stub with real download URL retrieval and parsing.

    The real implementation should:
    1. Call PollGenerateReport again to get the download URL
    2. urllib.request.urlretrieve(download_url) to get the ZIP file
    3. Unzip the CSV and write the raw CSV to S3
    4. Return the S3 key so IngestMetrics Lambda can read it

    CSV columns expected:
        CampaignId, CampaignName, Spend, Impressions, Clicks,
        Conversions, CostPerConversion
    """
    logger.info(
        "Downloading Bing report %s for date %s (stub — writes empty CSV)",
        report_request_id, report_date,
    )

    # Stub: write an empty CSV with headers so downstream code doesn't break
    headers = [
        "CampaignId", "CampaignName", "Spend", "Impressions",
        "Clicks", "Conversions", "CostPerConversion",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    csv_bytes = buf.getvalue().encode("utf-8")

    safe_date = report_date.replace("-", "")
    s3_key = f"raw-metrics/{report_date[:4]}/{report_date[5:7]}/{report_date[8:]}/microsoft_raw_{safe_date}.csv"

    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(
        Bucket=METRICS_BUCKET,
        Key=s3_key,
        Body=csv_bytes,
        ContentType="text/csv",
        ServerSideEncryption="AES256",
    )

    logger.info("Bing report CSV written to s3://%s/%s", METRICS_BUCKET, s3_key)
    return {
        "status": "COMPLETE",
        "report_request_id": report_request_id,
        "s3_key": s3_key,
    }
