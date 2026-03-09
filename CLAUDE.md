# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is the **idlookup.ai / PeopleSearch paid advertising automation system** — an AI-driven agent that manages Google Ads and Microsoft/Bing Ads campaigns for a people-search subscription service. The goal is to keep customer acquisition cost (CPA) below the 6-month customer lifetime value (LTV), currently modeled at **$18**.

The codebase is **scaffolding/stubs** — most modules define the correct interfaces and algorithms but do not yet make real API calls. See `implementation.md` for the phased plan to make it operational, and `deployment.md` for the AWS deployment architecture.

---

## Repository Structure

```
marketing/
├── ads_agent_project/ads_agent/    # Core agent package (Python 3.11) — single source of truth
│   ├── main.py                     # Orchestrator — canonical pipeline call order
│   ├── ingestion.py                # GoogleAdsConnector, MicrosoftAdsConnector (stubs)
│   ├── transformation.py           # normalize_campaigns, aggregate_metrics, compute_ltv
│   ├── monitoring.py               # check_thresholds, Alert dataclass, summarize_alerts
│   ├── decision_engine.py          # allocate_budget, adjust_bids, CampaignStats, KeywordStats
│   ├── creative_generator.py       # CreativeGenerator — LLM prompt builder, filter, scorer
│   ├── keyword_manager.py          # cluster_queries, suggest_keywords, suggest_audiences
│   ├── experiment_manager.py       # ExperimentManager, ExperimentConfig, ExperimentResult
│   └── compliance_monitor.py       # scan_text_for_policies, validate_targeting_settings
│
├── platform/                       # AWS deployment layer — thin wrappers only, no business logic
│   ├── handlers/
│   │   ├── ingest_handler.py       # Lambda: ingest metrics (10:00 UTC daily)
│   │   ├── agent_handler.py        # Lambda: run decisions (11:00 UTC daily)
│   │   └── bing_poll_handler.py    # Lambda: Step Functions Bing report poller
│   ├── lib/
│   │   ├── secrets.py              # Secrets Manager → env var injection
│   │   ├── dynamo_store.py         # DynamoDB read/write
│   │   ├── s3_store.py             # S3 read/write
│   │   ├── sns_notifier.py         # SNS alerts
│   │   └── audit.py               # Audit trail + 20% budget cap guard
│   ├── infra/
│   │   ├── cloudformation.yaml     # Full AWS infrastructure
│   │   └── stepfunctions_bing.json # Bing report polling state machine
│   ├── layers/
│   │   └── build_sklearn_layer.sh  # Docker build for sklearn Lambda layer
│   ├── requirements.txt
│   └── deploy.sh                   # Build + upload + deploy to AWS
│
├── CLAUDE.md                       # This file
├── implementation.md               # Phased plan to implement all stubs
├── deployment.md                   # AWS deployment architecture and model
└── *.pdf / *.docx                  # Strategy documents (not code)
```

**Key architectural rule:** All business logic lives in `ads_agent_project/ads_agent/`. The `platform/` layer handles only AWS I/O (DynamoDB, S3, SNS, Secrets Manager). The handlers import individual functions from `ads_agent` submodules — they do not duplicate logic.

---

## Running the Agent Locally

```bash
# From the ads_agent_project directory
python -m ads_agent.main
```

Runs the full pipeline with synthetic data — no real API calls, no credentials needed. Prints budget allocations, bid adjustments, keyword suggestions, and experiment outcomes.

---

## AWS Deployment

```bash
cd platform
bash deploy.sh [STACK_NAME] [CODE_BUCKET] [ALERT_EMAIL] [DRY_RUN]
# Example: bash deploy.sh idlookup-ai-agent my-code-bucket ops@idlookup.ai true
```

**Prerequisites:** AWS CLI configured, Python 3.11, Docker (for sklearn layer build).

Deploys two scheduled Lambda functions:
- `IngestMetrics` at 10:00 UTC — fetches metrics from Google/Bing → DynamoDB + S3
- `RunAgent` at 11:00 UTC — reads DynamoDB, runs decisions, writes audit trail, applies changes if `DRY_RUN=false`

Bing report polling runs via a Step Functions state machine to avoid Lambda timeout risk. See `deployment.md` for the full architecture.

**To enable live writes** after reviewing dry-run audit output in S3:
```bash
aws lambda update-function-configuration \
  --function-name <stack>-RunAgent \
  --environment Variables={...,DRY_RUN=false,...}
```

---

## Architecture: Agent Pipeline

The agent follows an **observe → analyze → decide → act** cycle. `main.py` defines the canonical call order:

1. **Ingestion** (`ingestion.py`): Pull campaigns, keywords, and conversions from Google Ads API and Microsoft Advertising API. Credentials flow from Secrets Manager → `lib/secrets.py` → `os.getenv()` fallbacks already in each connector's `__init__`.

2. **Transformation** (`transformation.py`): Normalize platform-specific fields into a common schema. Compute CPA (`cost / conversions`), CTR (`clicks / impressions`), LTV.

3. **Monitoring** (`monitoring.py`): Evaluate metrics against thresholds. CPA alert fires at **1.25× target ($22.50)**. Alerts are pure data — no side effects.

4. **Decision Engine** (`decision_engine.py`): Proportional budget allocation by `LTV / CPA` efficiency score, capped at `max_scalable_spend`. Bid adjustment: ≥5 conversions required; if CPA > target, reduce bid by `target / actual`; if CPA < target, increase 10%.

5. **Creative Generation** (`creative_generator.py`): Build prompt → call Anthropic API → filter banned phrases → score by historical performance. Headlines ≤30 chars, descriptions ≤90 chars.

6. **Keyword Management** (`keyword_manager.py`): TF-IDF + KMeans clustering of search query terms → promote high-converting clusters as positive keywords, suppress high-CPA/zero-conversion terms as negatives.

7. **Experiment Management** (`experiment_manager.py`): Queue, launch, and evaluate A/B tests. Winner declared if test improves primary metric by >10% (will be replaced with proper z-test).

8. **Compliance** (`compliance_monitor.py`): Scan all ad text for banned terms; validate targeting settings (no sensitive audience categories, no zip-code granularity targeting).

---

## Key Business Parameters

| Parameter | Value | Where set |
|-----------|-------|-----------|
| Target CPA | $18.00 | `CPA_TARGET` Lambda env var |
| Target LTV | $18.00 | `TARGET_LTV` Lambda env var ($3/month × 6 months) |
| CPA alert threshold | $22.50 | `CPA_ALERT_THRESHOLD` (1.25 × CPA_TARGET) |
| Bid min. conversions | 5 | `decision_engine.adjust_bids` |
| Max budget increase | 20% per run | `BUDGET_INCREASE_CAP` — enforced in `lib/audit.py` |
| Total daily budget | $1,000 | `TOTAL_BUDGET` Lambda env var |

---

## Credential Environment Variables

These are set automatically by `lib/secrets.py` from the `adsCredentials` Secrets Manager secret. The connectors in `ingestion.py` already fall back to these via `os.getenv()`.

| Env Var | Secret Key | Purpose |
|---------|-----------|---------|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | `google_developer_token` | Google Ads API access |
| `GOOGLE_ADS_CLIENT_ID` | `google_client_id` | OAuth client ID |
| `GOOGLE_ADS_CLIENT_SECRET` | `google_client_secret` | OAuth client secret |
| `GOOGLE_ADS_REFRESH_TOKEN` | `google_refresh_token` | OAuth refresh token |
| `GOOGLE_ADS_MANAGER_ID` | `google_manager_customer_id` | MCC account ID |
| `MS_ADS_CLIENT_ID` | `bing_client_id` | Microsoft Ads OAuth |
| `MS_ADS_CLIENT_SECRET` | `bing_client_secret` | |
| `MS_ADS_TENANT_ID` | `bing_tenant_id` | Azure AD tenant |
| `MS_ADS_REFRESH_TOKEN` | `bing_refresh_token` | |
| `MS_ADS_ACCOUNT_ID` | `bing_account_id` | Required by Bing SDK (add to `MicrosoftAdsConnector.__init__`) |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | Claude API for creative generation |

---

## What Needs to Be Implemented

See `implementation.md` for the full phased plan. Summary:

| Module | Method | Status |
|--------|--------|--------|
| `ingestion.py` | `GoogleAdsConnector.fetch_*` | Stub — needs `google-ads` SDK |
| `ingestion.py` | `MicrosoftAdsConnector.fetch_*` | Stub — needs `bingads` SDK + report polling |
| `creative_generator.py` | `generate_raw_creatives` | Stub — needs Anthropic API call |
| `keyword_manager.py` | `cluster_queries` | Stub — needs TF-IDF + KMeans |
| `experiment_manager.py` | `launch_experiment` | Stub — needs platform API calls |
| `experiment_manager.py` | `evaluate_experiments` | Partial — significance is hardcoded `0.05` |
| `platform/handlers/` | All three handlers | Not yet created |
| `platform/lib/` | All five lib modules | Not yet created |
| `platform/infra/` | CloudFormation + SFN | Not yet created |
