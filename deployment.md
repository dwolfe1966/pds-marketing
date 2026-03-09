# Deployment Model: ads_agent on AWS

## Overview

The `ads_agent` package is the single source of truth for all business logic. The deployment layer consists of thin AWS Lambda handlers that handle AWS I/O (DynamoDB, S3, SNS, Secrets Manager) and delegate all business logic to the `ads_agent` package. There is no separate monolith or duplicate implementation.

---

## AWS Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  AWS us-east-1                                                          │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  EventBridge Scheduler                                           │   │
│  │  ┌──────────────────────┐   ┌────────────────────────────────┐   │   │
│  │  │  cron(0 10 * * ? *)  │   │  cron(0 11 * * ? *)           │   │   │
│  │  │  10:00 UTC daily     │   │  11:00 UTC daily               │   │   │
│  │  └──────────┬───────────┘   └───────────────┬────────────────┘   │   │
│  └─────────────┼─────────────────────────────────┼──────────────────┘   │
│                │                                 │                      │
│                ▼                                 ▼                      │
│  ┌─────────────────────────┐   ┌─────────────────────────────────────┐  │
│  │  Lambda: IngestMetrics  │   │  Lambda: RunAgent                   │  │
│  │  (Timeout: 900s)        │   │  (Timeout: 300s)                    │  │
│  │                         │   │                                     │  │
│  │  ads_agent.ingestion    │   │  ads_agent.decision_engine          │  │
│  │  ads_agent.transform    │   │  ads_agent.creative_generator       │  │
│  │  ads_agent.monitoring   │   │  ads_agent.keyword_manager          │  │
│  └──────┬────────┬─────────┘   │  ads_agent.experiment_manager       │  │
│         │        │             │  ads_agent.compliance_monitor        │  │
│         │        │             └──────┬────────────────┬─────────────┘  │
│         │        │                   │                │                 │
│         │        │    ┌──────────────┴──────────┐     │                 │
│         │        │    │  Step Functions          │     │                 │
│         │        │    │  BingReportPoller        │     │                 │
│         │        │    │  (Wait-and-Retry loop)   │     │                 │
│         │        │    └──────────────┬───────────┘     │                 │
│         │        │                  │                  │                 │
│         ▼        ▼                  ▼                  ▼                 │
│  ┌───────────┐ ┌─────┐  ┌──────────────────┐  ┌──────────────────────┐  │
│  │  DynamoDB │ │ S3  │  │ Secrets Manager  │  │  SNS Alerts Topic    │  │
│  │           │ │     │  │  adsCredentials  │  │  + email sub         │  │
│  │  Campaign │ │ raw │  └──────────────────┘  └──────────────────────┘  │
│  │  Metrics  │ │ met-│                                                   │
│  │  Audit    │ │ rics│  ┌──────────────────────────────────────────┐     │
│  │  Trail    │ │ rec-│  │  Lambda Layer: sklearn-layer             │     │
│  └───────────┘ │ omm │  │  scikit-learn + scipy + numpy (py3.11)  │     │
│                └─────┘  └──────────────────────────────────────────┘     │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  CloudWatch Logs  (structured JSON logs from all Lambdas)        │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

External APIs:
  Google Ads API     ◄──►  IngestMetrics Lambda + RunAgent Lambda
  Microsoft Ads API  ◄──►  IngestMetrics Lambda (via BingReportPoller SFN)
  Anthropic API      ◄──►  RunAgent Lambda
```

---

## New File Structure

```
marketing/
├── ads_agent_project/
│   └── ads_agent/                   ← UNCHANGED — single source of truth
│       ├── __init__.py
│       ├── main.py
│       ├── ingestion.py
│       ├── transformation.py
│       ├── monitoring.py
│       ├── decision_engine.py
│       ├── creative_generator.py
│       ├── keyword_manager.py
│       ├── experiment_manager.py
│       └── compliance_monitor.py
│
└── platform/                        ← NEW — thin AWS wrapper layer only
    ├── handlers/
    │   ├── ingest_handler.py        # Lambda entry point: ingest phase (10:00 UTC)
    │   ├── agent_handler.py         # Lambda entry point: decisions phase (11:00 UTC)
    │   └── bing_poll_handler.py     # Lambda entry point: Step Functions Bing poller
    │
    ├── lib/
    │   ├── secrets.py               # Secrets Manager loader → env var injection
    │   ├── dynamo_store.py          # DynamoDB read/write wrappers
    │   ├── s3_store.py              # S3 read/write wrappers
    │   ├── sns_notifier.py          # SNS publish helper
    │   └── audit.py                 # Audit trail writer + 20% budget cap guard
    │
    ├── infra/
    │   ├── cloudformation.yaml      # Full CloudFormation template
    │   └── stepfunctions_bing.json  # Step Functions state machine definition
    │
    ├── layers/
    │   └── build_sklearn_layer.sh   # Docker build for sklearn Lambda layer
    │
    ├── requirements.txt             # All Python deps for Lambda ZIPs
    └── deploy.sh                    # Full build + deploy script
```

**Design principle:** `platform/handlers/` and `platform/lib/` contain only AWS I/O code. All business logic — budget decisions, bid adjustments, keyword clustering, creative generation, compliance checks — lives exclusively in `ads_agent_project/ads_agent/`. The handlers import from `ads_agent` and call individual functions directly (not `main.main()`).

---

## Lambda Functions

### Lambda 1 — IngestMetrics (`handlers/ingest_handler.py`)

| Property | Value |
|----------|-------|
| Trigger | EventBridge `cron(0 10 * * ? *)` |
| Timeout | 900 seconds (15 min) |
| Memory | 1024 MB |
| Layer | sklearn-layer |

**Execution flow:**
1. Load credentials from Secrets Manager via `lib/secrets.py` (injected as env vars)
2. Instantiate `GoogleAdsConnector` and `MicrosoftAdsConnector` from `ads_agent.ingestion` — these already fall back to `os.getenv(...)`, so no constructor changes needed
3. For Bing: invoke the `BingReportPoller` Step Functions state machine synchronously to handle async report polling
4. Call `ads_agent.ingestion.fetch_all_data(connectors)`
5. Call `ads_agent.transformation.normalize_campaigns()` and `aggregate_metrics()`
6. Call `ads_agent.monitoring.check_thresholds()` against CPA threshold from env var
7. Write campaign rows to DynamoDB via `lib/dynamo_store`
8. Write raw JSON snapshot to S3 via `lib/s3_store`
9. Publish any alerts to SNS via `lib/sns_notifier`

**Environment variables:**

| Variable | Value |
|----------|-------|
| `SECRET_NAME` | `adsCredentials` |
| `METRICS_TABLE` | `!Ref MetricsTable` |
| `METRICS_BUCKET` | `!Ref MetricsBucket` |
| `ALERTS_TOPIC_ARN` | `!Ref AlertsTopic` |
| `BING_STATE_MACHINE_ARN` | `!Ref BingPollerStateMachine` |
| `CPA_TARGET` | `18.0` |
| `CPA_ALERT_THRESHOLD` | `22.50` |
| `TARGET_LTV` | `18.0` |
| `LOG_LEVEL` | `INFO` |

---

### Lambda 2 — RunAgent (`handlers/agent_handler.py`)

| Property | Value |
|----------|-------|
| Trigger | EventBridge `cron(0 11 * * ? *)` |
| Timeout | 300 seconds |
| Memory | 1024 MB |
| Layer | sklearn-layer |

**Execution flow:**
1. Load credentials from Secrets Manager via `lib/secrets.py`
2. Read today's campaign metrics from DynamoDB via `lib/dynamo_store`
3. Reconstruct `CampaignStats` / `KeywordStats` dataclasses from DynamoDB records
4. Call `ads_agent.decision_engine.allocate_budget()` and `adjust_bids()`
5. Call `ads_agent.creative_generator.CreativeGenerator` pipeline
6. Call `ads_agent.keyword_manager.cluster_queries()` and `suggest_keywords()`
7. Call `ads_agent.experiment_manager.ExperimentManager` lifecycle methods
8. Call `ads_agent.compliance_monitor.scan_text_for_policies()` on all creatives
9. Write full audit record to S3 and DynamoDB via `lib/audit`
10. If `DRY_RUN == "false"`: apply mutations via platform SDKs. If `"true"`: log only.
11. Publish CPA alerts to SNS if any campaigns breach threshold

**Environment variables:**

| Variable | Value |
|----------|-------|
| `SECRET_NAME` | `adsCredentials` |
| `METRICS_TABLE` | `!Ref MetricsTable` |
| `AUDIT_TABLE` | `!Ref AuditTable` |
| `METRICS_BUCKET` | `!Ref MetricsBucket` |
| `ALERTS_TOPIC_ARN` | `!Ref AlertsTopic` |
| `DRY_RUN` | `"true"` (change to `"false"` to enable live writes) |
| `CPA_TARGET` | `18.0` |
| `CPA_ALERT_THRESHOLD` | `22.50` |
| `TARGET_LTV` | `18.0` |
| `TOTAL_BUDGET` | `1000.0` |
| `BUDGET_INCREASE_CAP` | `0.20` |
| `LOG_LEVEL` | `INFO` |

---

### Lambda 3 — BingPoller (`handlers/bing_poll_handler.py`)

| Property | Value |
|----------|-------|
| Trigger | Step Functions state machine only (not EventBridge) |
| Timeout | 60 seconds |
| Memory | 256 MB |
| Layer | none |

Checks Bing report status and returns `COMPLETE`, `PENDING`, or `FAILED`. Downloads the CSV to S3 when complete. Called in a loop by the Step Functions state machine — not directly by either scheduled Lambda.

**Environment variables:** `SECRET_NAME`, `METRICS_BUCKET`, `LOG_LEVEL`

---

## Bing Report Polling — Step Functions Solution

Bing report generation is asynchronous and takes 30–120 seconds. A blocking Lambda poll loop wastes execution time and risks timeout. The solution is a Step Functions state machine that waits between polls at zero cost.

```
State Machine: BingReportPoller
──────────────────────────────────────────────────────────────────────
SubmitReport
  → BingPoller Lambda submits CampaignPerformanceReportRequest
  → returns { report_request_id }

PollReport (loop)
  → BingPoller Lambda checks PollGenerateReport
  → returns { status: "PENDING"|"COMPLETE"|"FAILED", attempt: N }
  │
  ├─ COMPLETE  →  DownloadAndStore
  │               → BingPoller Lambda downloads CSV, writes to S3
  │               → SUCCEED
  │
  ├─ PENDING AND attempt < 18
  │     →  Wait 20 seconds (Step Functions native wait — costs nothing)
  │     →  loop back to PollReport (attempt + 1)
  │
  └─ FAILED OR attempt >= 18
        →  Publish failure to SNS
        →  FAIL (IngestMetrics catches and continues with Google-only data)

Maximum wait: 18 × 20s = 6 minutes
```

The IngestMetrics Lambda invokes the state machine using the SDK's sync integration (`startExecution` + wait), which blocks until SFN completes and returns the result — within the 15-minute Lambda timeout. If accounts grow and polling regularly approaches 6 minutes, only the `Wait` duration needs to change — no code change required.

---

## Dry Run vs. Live Mode

Controlled by the `DRY_RUN` environment variable on the RunAgent Lambda. Defaults to `"true"` — all mutations are logged but not applied.

```python
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

if DRY_RUN:
    logger.info("DRY_RUN: would apply budget change", extra={"campaign": id, "new_budget": val})
else:
    apply_google_budget_changes(budget_changes)
    apply_bing_budget_changes(budget_changes)
```

The full audit record (what the agent would do) is **always written** to S3 and DynamoDB regardless of mode. To review and enable live writes:

1. Check `s3://idlookup-ai-agent-data-<acct>/recommendations/YYYY/MM/DD/` for the proposed changes
2. When satisfied: `aws lambda update-function-configuration --function-name <stack>-RunAgent --environment Variables={DRY_RUN=false,...}`

**Safety hard cap** (enforced in `lib/audit.py` in both modes): any budget increase >20% over current budget is rejected, logged, and SNS-alerted regardless of `DRY_RUN`.

---

## Secrets Manager Schema

**Secret name:** `adsCredentials`

```json
{
  "google_customer_id":          "123-456-7890",
  "google_developer_token":      "xxxx",
  "google_client_id":            "xxxx.apps.googleusercontent.com",
  "google_client_secret":        "GOCSPX-xxxx",
  "google_refresh_token":        "1//xxxx",
  "google_manager_customer_id":  "098-765-4321",
  "google_ads_api_version":      "v18",

  "bing_customer_id":            "123456789",
  "bing_account_id":             "987654321",
  "bing_client_id":              "xxxx-xxxx-xxxx-xxxx",
  "bing_client_secret":          "xxxx",
  "bing_tenant_id":              "xxxx-xxxx-xxxx-xxxx",
  "bing_refresh_token":          "xxxx",
  "bing_developer_token":        "xxxx",

  "anthropic_api_key":           "sk-ant-xxxx"
}
```

**How `lib/secrets.py` bridges to `ads_agent`:**

```python
SECRET_KEY_TO_ENV = {
    "google_developer_token":     "GOOGLE_ADS_DEVELOPER_TOKEN",
    "google_client_id":           "GOOGLE_ADS_CLIENT_ID",
    "google_client_secret":       "GOOGLE_ADS_CLIENT_SECRET",
    "google_refresh_token":       "GOOGLE_ADS_REFRESH_TOKEN",
    "google_manager_customer_id": "GOOGLE_ADS_MANAGER_ID",
    "bing_customer_id":           "MS_ADS_CUSTOMER_ID",
    "bing_account_id":            "MS_ADS_ACCOUNT_ID",
    "bing_client_id":             "MS_ADS_CLIENT_ID",
    "bing_client_secret":         "MS_ADS_CLIENT_SECRET",
    "bing_tenant_id":             "MS_ADS_TENANT_ID",
    "bing_refresh_token":         "MS_ADS_REFRESH_TOKEN",
    "anthropic_api_key":          "ANTHROPIC_API_KEY",
}
```

The mapping aligns exactly with the `os.getenv(...)` calls already in `GoogleAdsConnector.__init__` and `MicrosoftAdsConnector.__init__`. No changes to `ads_agent/ingestion.py` are needed for credential wiring.

> Note: `bing_account_id` → `MS_ADS_ACCOUNT_ID` is a new env var. `MicrosoftAdsConnector.__init__` must be updated to read `os.getenv("MS_ADS_ACCOUNT_ID")` since the Bing SDK requires `account_id` for all service client calls.

---

## DynamoDB Table Design

### Table 1: `CampaignMetrics`

**Purpose:** Daily campaign performance — source of truth for RunAgent decisions.

| Attribute | Type | Notes |
|-----------|------|-------|
| `PK` | S | `CAMPAIGN#<platform>#<campaign_id>` |
| `SK` | S | `DATE#<YYYY-MM-DD>` |
| `platform` | S | `"google"` or `"microsoft"` |
| `campaign_name` | S | |
| `cost` | N | USD |
| `impressions` | N | |
| `clicks` | N | |
| `conversions` | N | |
| `cpa` | N | `cost / conversions` |
| `ctr` | N | `clicks / impressions` |
| `budget` | N | Daily budget at ingest time |
| `ttl` | N | Unix timestamp — auto-expire after 400 days |

**GSI 1 — ByDate:** `SK` (partition) + `PK` (sort) — "all campaigns for a given date" (used by RunAgent)

**GSI 2 — ByPlatform:** `platform` (partition) + `SK` (sort) — "all Google campaigns last 30 days" (LTV modeling)

### Table 2: `AuditTrail`

**Purpose:** Immutable record of every agent recommendation and action.

| Attribute | Type | Notes |
|-----------|------|-------|
| `PK` | S | `RUN#<YYYY-MM-DDThh:mm:ssZ>` |
| `SK` | S | `ACTION#<type>#<entity_id>` e.g. `ACTION#BUDGET#c123` |
| `action_type` | S | `BUDGET_CHANGE`, `BID_CHANGE`, `KEYWORD_ADD`, `KEYWORD_NEGATE`, `CREATIVE_SELECT`, `EXPERIMENT_LAUNCH`, `ALERT` |
| `before_value` | N | Previous budget/bid |
| `after_value` | N | Proposed/applied value |
| `dry_run` | BOOL | Whether this was applied or only logged |
| `applied` | BOOL | Whether the API mutation succeeded |
| `s3_ref` | S | S3 key to the full JSON payload for the run |
| `ttl` | N | Unix timestamp — 730 days (2-year audit retention) |

Both tables use **PAY_PER_REQUEST** billing.

---

## S3 Bucket Structure

**Bucket name:** `idlookup-ai-agent-data-<account_id>`

```
s3://idlookup-ai-agent-data-<acct>/
├── raw-metrics/YYYY/MM/DD/
│   ├── google_raw_<timestamp>.json
│   └── microsoft_raw_<timestamp>.json
├── normalized-metrics/YYYY/MM/DD/
│   └── normalized_<timestamp>.json
├── recommendations/YYYY/MM/DD/
│   └── recommendations_<timestamp>.json   ← Full agent decision payload
├── audit/YYYY/MM/DD/
│   └── audit_<run_id>.json
└── layers/                                ← deploy.sh uploads here pre-deploy
    ├── ingest.zip
    ├── agent.zip
    ├── bing_poller.zip
    └── sklearn-layer.zip
```

- Versioning enabled on `recommendations/` and `audit/`
- Server-side encryption: SSE-S3 (AES-256) on all objects
- No public access
- Lifecycle: `raw-metrics/` expires after 90 days; all other prefixes retained

---

## IAM Permissions

**Single role** `AdsAgentLambdaRole` used by all three Lambda functions:

```yaml
- secretsmanager:GetSecretValue   # scoped to adsCredentials secret only
- dynamodb:PutItem, GetItem, Query, BatchWriteItem, BatchGetItem
                                  # scoped to MetricsTable + AuditTable + their GSIs
- s3:PutObject, GetObject, ListBucket
                                  # scoped to MetricsBucket only
- sns:Publish                     # scoped to AlertsTopic only
- states:StartExecution           # scoped to BingPollerStateMachine only
- logs:CreateLogGroup, CreateLogStream, PutLogEvents  # CloudWatch Logs
```

**Separate Step Functions role** `StepFunctionsRole` with only `lambda:InvokeFunction` on `BingPollerFunction`.

---

## Build and Deploy

```bash
# Usage:
bash platform/deploy.sh [STACK_NAME] [CODE_BUCKET] [ALERT_EMAIL] [DRY_RUN]

# Example (first deploy, dry-run mode):
bash platform/deploy.sh idlookup-ai-agent my-code-bucket ops@idlookup.ai true
```

**What `deploy.sh` does:**

1. Creates the S3 code bucket if it does not exist
2. Builds the sklearn Lambda layer via Docker (skipped if `sklearn-layer.zip` already exists):
   ```bash
   docker run --rm -v $(pwd):/out public.ecr.aws/lambda/python:3.11 \
     /bin/bash -c "pip install scikit-learn>=1.4.0 scipy>=1.12.0 numpy>=1.26.0 \
       -t /opt/python && cd /opt && zip -r9 /out/sklearn-layer.zip python/"
   ```
3. Publishes the layer to Lambda and captures the ARN
4. For each Lambda, builds a ZIP containing:
   - The handler file from `platform/handlers/`
   - The `platform/lib/` directory
   - The full `ads_agent_project/ads_agent/` package
   - All non-layer pip dependencies (`google-ads`, `bingads`, `anthropic`, `boto3`)
5. Uploads all ZIPs to S3
6. Runs `aws cloudformation deploy` with all parameters

**To enable live writes after verifying dry-run output:**
```bash
aws lambda update-function-configuration \
  --function-name idlookup-ai-agent-RunAgent \
  --environment Variables={...,DRY_RUN=false,...}
```

---

## Key Business Parameters (set as CloudFormation parameters → Lambda env vars)

| Parameter | Default | Notes |
|-----------|---------|-------|
| `CPA_TARGET` | `18.0` | Must equal 6-month LTV |
| `TARGET_LTV` | `18.0` | $3/month × 6 months |
| `CPA_ALERT_THRESHOLD` | `22.50` | 1.25 × CPA_TARGET |
| `TOTAL_BUDGET` | `1000.0` | Total daily budget cap across all campaigns |
| `BUDGET_INCREASE_CAP` | `0.20` | Max single-step budget increase (20%) |
| `DRY_RUN` | `true` | Change to `false` to apply mutations |
