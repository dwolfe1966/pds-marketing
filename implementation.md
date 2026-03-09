# Implementation Plan: ads_agent — Stubs to Production

## Architecture

**Single code path:** The `ads_agent_project/ads_agent/` package is the only implementation of all business logic. The `platform/` directory contains thin AWS Lambda handlers and lib utilities that handle AWS I/O and call into `ads_agent` — no business logic lives there. The old `platform/advertising_agent_platform.py` monolith has been deleted.

See `deployment.md` for the full AWS deployment architecture.

---

## Required Packages (`platform/requirements.txt`)

```
google-ads>=24.1.0       # Google Ads Python SDK (API v18)
bingads>=13.0.21         # Microsoft Advertising SOAP SDK
anthropic>=0.25.0        # Claude API client
scikit-learn>=1.4.0      # TF-IDF + KMeans for keyword clustering
scipy>=1.12.0            # z-test for experiment significance
numpy>=1.26.0
```

> `scikit-learn`/`scipy` must be in a separate Lambda layer (compiled Linux binaries). Built via Docker — see `platform/layers/build_sklearn_layer.sh`.

---

## Secrets Manager Schema

The `adsCredentials` secret must contain:

```json
{
  "google_customer_id":          "123-456-7890",
  "google_developer_token":      "...",
  "google_client_id":            "...",
  "google_client_secret":        "...",
  "google_refresh_token":        "...",
  "google_manager_customer_id":  "...",
  "google_ads_api_version":      "v18",

  "bing_customer_id":            "...",
  "bing_account_id":             "...",
  "bing_client_id":              "...",
  "bing_client_secret":          "...",
  "bing_tenant_id":              "...",
  "bing_refresh_token":          "...",
  "bing_developer_token":        "...",

  "anthropic_api_key":           "sk-ant-..."
}
```

> `bing_account_id` is required by the Bing SDK but currently missing from all code. Must be added to `MicrosoftAdsConnector.__init__` as `os.getenv("MS_ADS_ACCOUNT_ID")`.

---

## Phase 1 — AWS Platform Layer (New Files) `[Days 1–2]`

**Prerequisite for all other phases.** Creates the `platform/` deployment layer from scratch.

### 1a. `platform/lib/secrets.py`

Fetches `adsCredentials` from Secrets Manager at Lambda cold start and injects all keys as environment variables using this mapping:

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

This bridges Secrets Manager keys to the `os.getenv()` calls already in `GoogleAdsConnector.__init__` and `MicrosoftAdsConnector.__init__`. No changes to `ads_agent/ingestion.py` needed for credential wiring.

Call once outside the handler function for warm invocation reuse:
```python
# At module level in each handler
from lib.secrets import load_secrets
load_secrets(os.environ["SECRET_NAME"])
```

### 1b. `platform/lib/dynamo_store.py`

Functions:
- `write_campaign_metrics(table_name, campaigns: List[dict], date: str)` — batch writes normalized campaign dicts to DynamoDB
- `get_today_metrics(table_name, date: str) -> List[dict]` — queries the ByDate GSI to retrieve all campaigns for a given date
- `get_metrics_range(table_name, platform: str, days: int) -> List[dict]` — queries ByPlatform GSI for LTV modeling

### 1c. `platform/lib/s3_store.py`

Functions:
- `put_raw_metrics(bucket, platform, data: dict)` — writes to `raw-metrics/YYYY/MM/DD/<platform>_raw_<ts>.json`
- `put_normalized(bucket, data: list)` — writes to `normalized-metrics/YYYY/MM/DD/`
- `put_recommendations(bucket, data: dict)` — writes to `recommendations/YYYY/MM/DD/`

### 1d. `platform/lib/sns_notifier.py`

Function:
- `publish_alerts(topic_arn, alerts: List[Alert])` — serializes `Alert` dataclasses and publishes to SNS

### 1e. `platform/lib/audit.py`

Functions:
- `write_audit_record(table_name, bucket, run_id, actions: List[dict])` — writes to both DynamoDB AuditTrail and S3 `audit/`
- `check_budget_cap(current_budget, proposed_budget, cap=0.20) -> bool` — returns `False` and logs/alerts if increase exceeds `cap`

### 1f. `platform/handlers/ingest_handler.py`

Thin Lambda handler. Calls in order:
1. `lib.secrets.load_secrets()`
2. `ads_agent.ingestion.fetch_all_data(connectors)` (Google directly; Bing via Step Functions)
3. `ads_agent.transformation.normalize_campaigns()` + `aggregate_metrics()`
4. `ads_agent.monitoring.check_thresholds()`
5. `lib.dynamo_store.write_campaign_metrics()`
6. `lib.s3_store.put_raw_metrics()` + `put_normalized()`
7. `lib.sns_notifier.publish_alerts()` if alerts exist

### 1g. `platform/handlers/agent_handler.py`

Thin Lambda handler. Calls in order:
1. `lib.secrets.load_secrets()`
2. `lib.dynamo_store.get_today_metrics()` — read from DynamoDB (skips re-ingestion)
3. `ads_agent.decision_engine.allocate_budget()` + `adjust_bids()`
4. `ads_agent.creative_generator.CreativeGenerator` pipeline
5. `ads_agent.keyword_manager.cluster_queries()` + `suggest_keywords()`
6. `ads_agent.experiment_manager.ExperimentManager` lifecycle
7. `ads_agent.compliance_monitor.scan_text_for_policies()` on all creatives
8. `lib.audit.write_audit_record()`
9. If `DRY_RUN == "false"`: apply mutations via Google/Bing SDK calls

### 1h. `platform/handlers/bing_poll_handler.py`

Lambda invoked by Step Functions only. Checks Bing `ReportingService.PollGenerateReport`, returns `COMPLETE`/`PENDING`/`FAILED`. Downloads CSV to S3 when complete.

### 1i. `platform/infra/cloudformation.yaml` and `stepfunctions_bing.json`

Full CloudFormation template and Step Functions state machine definition. See `deployment.md` for the complete resource list, IAM permissions, and environment variable definitions.

### 1j. `platform/deploy.sh` and `platform/layers/build_sklearn_layer.sh`

See `deployment.md` for the full deploy script logic.

**Verify setup:**
```bash
aws secretsmanager get-secret-value --secret-id adsCredentials
aws cloudformation describe-stacks --stack-name idlookup-ai-agent
```

---

## Phase 2 — Google Ads API `[Days 3–5]`

**Depends on Phase 1.**

**File:** `ads_agent_project/ads_agent/ingestion.py`

Uses `google-ads` Python SDK with `GoogleAdsClient.load_from_dict(...)`.

### `GoogleAdsConnector.fetch_campaigns`

```python
client = GoogleAdsClient.load_from_dict({
    "developer_token": self.developer_token,
    "client_id": self.client_id,
    "client_secret": self.client_secret,
    "refresh_token": self.refresh_token,
    "login_customer_id": self.manager_customer_id,
    "use_proto_plus": True,
})
ga_service = client.get_service("GoogleAdsService")
```

**GAQL query:**
```sql
SELECT
  campaign.id, campaign.name, campaign.status,
  campaign_budget.amount_micros,
  metrics.cost_micros, metrics.impressions,
  metrics.clicks, metrics.conversions
FROM campaign
WHERE segments.date DURING LAST_30_DAYS
  AND campaign.status = 'ENABLED'
```

**Return schema** (normalized dict for `transformation.normalize_campaigns`):
```python
{
    "id": str(row.campaign.id),
    "name": row.campaign.name,
    "cost_micros": row.metrics.cost_micros,
    "cost": row.metrics.cost_micros / 1_000_000,
    "impressions": row.metrics.impressions,
    "clicks": row.metrics.clicks,
    "conversions": row.metrics.conversions,
    "budget_micros": row.campaign_budget.amount_micros,
}
```

Use `search_stream()` for pagination. Wrap in `try/except GoogleAdsException`.

### `GoogleAdsConnector.fetch_keywords`

```sql
SELECT
  ad_group_criterion.criterion_id,
  ad_group_criterion.keyword.text,
  ad_group_criterion.keyword.match_type,
  ad_group_criterion.cpc_bid_micros,
  campaign.id, ad_group.id,
  metrics.cost_micros, metrics.clicks, metrics.conversions
FROM keyword_view
WHERE segments.date DURING LAST_30_DAYS
  AND ad_group_criterion.status = 'ENABLED'
  AND campaign.status = 'ENABLED'
```

### `GoogleAdsConnector.fetch_conversions`

```sql
SELECT
  conversion_action.id, conversion_action.name,
  metrics.all_conversions, metrics.conversions_value,
  campaign.id
FROM campaign
WHERE segments.date DURING LAST_30_DAYS
```

### Budget and bid mutations (called from `agent_handler.py`, not from `ads_agent`)

- **Budget update:** Query `campaign.campaign_budget` resource name → `CampaignBudgetService.mutate_campaign_budgets` with `amount_micros = new_budget * 1_000_000`. Note: budgets can be shared across campaigns.
- **Bid update:** Query `ad_group_criterion.resource_name` → `AdGroupCriterionService.mutate_ad_group_criteria` with `cpc_bid_micros = new_bid * 1_000_000` and `update_mask=["cpc_bid_micros"]`.

Mutations live in `agent_handler.py`, not in `ads_agent/ingestion.py`, since `ads_agent` is read-only.

> **Risk:** Google developer token approval takes **1–2 weeks** for production access. Request immediately; develop against a test account in the meantime.

> **Safety:** `lib/audit.check_budget_cap()` must be called before every budget mutation. Reject increases >20%, log and SNS-alert.

---

## Phase 3 — Microsoft Advertising (Bing) API `[Days 3–5]`

**Depends on Phase 1. Parallel with Phase 2.**

**File:** `ads_agent_project/ads_agent/ingestion.py`

Uses `bingads` SOAP SDK. Critical difference from Google: **structural data and performance metrics come from separate services**.

**Add `account_id` to `MicrosoftAdsConnector.__init__`:**
```python
self.account_id = account_id or os.getenv("MS_ADS_ACCOUNT_ID")
```

**Authentication:**
```python
from bingads import AuthorizationData, OAuthWebAuthCodeGrant, ServiceClient

auth_data = AuthorizationData(
    account_id=self.account_id,
    customer_id=self.customer_id,
    developer_token=self.developer_token,
    authentication=OAuthWebAuthCodeGrant(
        client_id=self.client_id,
        client_secret=self.client_secret,
        redirection_uri="https://login.microsoftonline.com/common/oauth2/nativeclient",
    ),
)
auth_data.authentication.request_oauth_tokens_by_refresh_token(self.refresh_token)
```

**`fetch_campaigns` / `fetch_keywords`:**
- Structure: `CampaignManagementService.GetCampaignsByAccountId` / `GetKeywordsByAdGroupId`
- Metrics: `ReportingService.SubmitGenerateReport` → poll → download CSV
  - Campaign: `CampaignPerformanceReportRequest` (columns: `CampaignId`, `CampaignName`, `Spend`, `Impressions`, `Clicks`, `Conversions`, `CostPerConversion`)
  - Keywords: `KeywordPerformanceReportRequest`

**Optimization:** Add a private `_run_report(columns, report_type)` method with results cached in `self._report_cache`. All three `fetch_*` methods slice from the cache — avoids 3 separate 30–60 second polling cycles.

**Polling is handled externally** by the Step Functions `BingReportPoller` state machine. `MicrosoftAdsConnector.fetch_campaigns` should check for an already-downloaded CSV in S3 (written by `bing_poll_handler.py`) rather than polling directly. The IngestMetrics Lambda invokes the SFN before calling `fetch_all_data`.

**Mutations** (in `agent_handler.py`):
- Budget: `CampaignManagementService.UpdateCampaigns` with `Campaign(Id=..., DailyBudget=...)`
- Bids: `CampaignManagementService.UpdateKeywords` with `Keyword(Id=..., Bid=Bid(Amount=...))`

> **Risk:** Bing report generation takes 30–120 seconds. Handled by Step Functions (see `deployment.md`). If average polling time exceeds 4 minutes for large accounts, increase the SFN Wait state duration — no code change needed.

---

## Phase 4 — LLM Creative Generation `[Days 3–4]`

**Depends on Phase 1 only. Parallel with Phases 2/3.**

**File:** `ads_agent_project/ads_agent/creative_generator.py`

**Changes to `CreativeGenerator.__init__`:**
```python
def __init__(self, prohibited_phrases=None, api_key=None):
    ...
    self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
```

**Implement `generate_raw_creatives`:**
```python
import anthropic, json

def generate_raw_creatives(self, prompt: str) -> List[Creative]:
    client = anthropic.Anthropic(api_key=self._api_key)
    system = (
        "You are an expert Google Ads copywriter for a people-search subscription service "
        "called idlookup.ai. You always comply with Google Ads policies. "
        "Return only a JSON array of objects with keys 'headline' (max 30 chars) and "
        "'description' (max 90 chars). No prose, no markdown, only valid JSON."
    )
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        system=system,
    )
    try:
        data = json.loads(message.content[0].text)
    except json.JSONDecodeError:
        return []
    return [
        Creative(headline=item["headline"][:30], description=item["description"][:90])
        for item in data
        if "headline" in item and "description" in item
    ]
```

Retry with a second call if fewer than 3 headlines are returned. `ANTHROPIC_API_KEY` is injected automatically by `lib/secrets.py`.

---

## Phase 5 — NLP Keyword Clustering `[Days 3–4]`

**Independent — no API credentials needed.**

**File:** `ads_agent_project/ads_agent/keyword_manager.py`

Replace the naive first-letter stub in `cluster_queries`:

```python
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans

def cluster_queries(records, n_clusters=10):
    record_list = list(records)
    if not record_list:
        return {}
    terms = [r.term for r in record_list]
    vectorizer = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), stop_words="english")
    X = vectorizer.fit_transform(terms)
    k = min(n_clusters, len(terms))
    labels = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(X)
    clusters: Dict[int, List[QueryRecord]] = {}
    for label, record in zip(labels, record_list):
        clusters.setdefault(int(label), []).append(record)
    return clusters
```

Bigram TF-IDF groups name-search queries well (e.g., "john smith" and "find john smith" cluster together). For >10k terms, swap `KMeans` for `MiniBatchKMeans` as a drop-in.

---

## Phase 6 — Experiment Launch API `[Days 6–7]`

**Depends on Phases 2 and 3.**

**File:** `ads_agent_project/ads_agent/experiment_manager.py`

**Architecture change** — inject platform clients:
```python
class ExperimentManager:
    def __init__(self, google_client=None, bing_client=None):
        self.google_client = google_client
        self.bing_client = bing_client
        ...
```

**`launch_experiment` — Google Ads:**
1. `CampaignDraftService.mutate_campaign_drafts` (CREATE) from the base campaign
2. Apply the test variation (headline change, bid strategy change, etc.) to the draft
3. `CampaignExperimentService.create_campaign_experiment_async()` with `traffic_split_percent`

**`launch_experiment` — Microsoft Ads:**
- `CampaignManagementService.AddExperiments` (API v13+) with `BaseCampaignId`, `ExperimentCampaignId`, `StartDate`, `EndDate`, `TrafficSplitPercent`

Dispatch based on `config.control_settings.get("platform")`.

**Fix `evaluate_experiments` significance test:**
```python
from scipy.stats import proportions_ztest

count = [control_metrics["conversions"], test_metrics["conversions"]]
nobs  = [control_metrics["clicks"],      test_metrics["clicks"]]
_, p_value = proportions_ztest(count, nobs)
significance = p_value  # replaces hardcoded 0.05
```

---

## Timeline Summary

| Phase | What | Days | Dependencies |
|-------|------|------|-------------|
| 1 | AWS platform layer (new files) | 1–2 | None — start here |
| 2 | Google Ads API | 3–5 | Phase 1 |
| 3 | Microsoft Ads API | 3–5 | Phase 1 (parallel with Phase 2) |
| 4 | LLM creative generation | 3–4 | Phase 1 (parallel with Phases 2/3) |
| 5 | NLP keyword clustering | 3–4 | None (fully independent) |
| 6 | Experiment launch API | 6–7 | Phases 2 and 3 |

Phases 2, 3, 4, and 5 can all run in parallel after Phase 1.
**Total wall-clock time: ~7 working days with parallel execution.**

---

## Risks and Flags

| Risk | Impact | Mitigation |
|------|--------|------------|
| Google developer token approval takes 1–2 weeks | Blocks Phase 2 production | Request immediately; develop against test account |
| Bing report polling 30–120 seconds | Ingest Lambda timeout | Handled by Step Functions BingReportPoller (see `deployment.md`) |
| Budget/bid mutations irreversible with real money | Financial risk | `lib/audit.check_budget_cap()` rejects >20% increase; `DRY_RUN=true` by default |
| `bing_account_id` missing from `MicrosoftAdsConnector` | Bing SDK fails at init | Add `os.getenv("MS_ADS_ACCOUNT_ID")` in Phase 1 |
| `max_scalable_spend` has no derivation formula | Budget allocation hits arbitrary cap | Start with `campaign_budget * 1.2`; refine from 30-day spend curve |

---

## Key Files for Implementation

| File | What changes |
|------|-------------|
| `ads_agent/ingestion.py` | All 6 `fetch_*` methods + add `bing_account_id` to `MicrosoftAdsConnector` |
| `ads_agent/creative_generator.py` | `generate_raw_creatives` — Anthropic API call + JSON parsing |
| `ads_agent/keyword_manager.py` | `cluster_queries` — TF-IDF + KMeans |
| `ads_agent/experiment_manager.py` | `launch_experiment` + real z-test in `evaluate_experiments` |
| `platform/handlers/ingest_handler.py` | New file — create from scratch |
| `platform/handlers/agent_handler.py` | New file — create from scratch |
| `platform/handlers/bing_poll_handler.py` | New file — create from scratch |
| `platform/lib/*.py` | New files — create from scratch |
| `platform/infra/cloudformation.yaml` | New file — see `deployment.md` for full spec |
| `platform/infra/stepfunctions_bing.json` | New file — Bing polling state machine |
| `platform/deploy.sh` | New file — build + deploy script |
