"""
ads_agent.ingestion
=====================

Connects to Google Ads and Microsoft Advertising APIs, fetches campaign,
keyword, and conversion data, and returns it in a normalized internal schema
consumed by ads_agent.transformation.

Classes
-------
BaseConnector
    Abstract interface for all platform connectors.

GoogleAdsConnector
    Fetches data from the Google Ads API using the google-ads Python SDK.
    Credentials are read from environment variables injected by
    platform/lib/secrets.py (or passed directly as constructor arguments).

MicrosoftAdsConnector
    Fetches data from the Microsoft Advertising (Bing) API using the bingads
    SDK.  Report polling is handled externally by the BingReportPoller Step
    Functions state machine; this connector reads the completed CSV from S3.
    Phase 3 stub — fetch methods return empty lists until implemented.

Functions
---------
fetch_all_data
    Orchestrates data retrieval across all connectors and returns a unified
    dict keyed by platform name.

Environment variables (Google Ads)
-----------------------------------
GOOGLE_ADS_DEVELOPER_TOKEN
GOOGLE_ADS_CLIENT_ID
GOOGLE_ADS_CLIENT_SECRET
GOOGLE_ADS_REFRESH_TOKEN
GOOGLE_ADS_CUSTOMER_ID
GOOGLE_ADS_MANAGER_ID      (optional — MCC account ID)
GOOGLE_ADS_API_VERSION     (optional — default v18)

Environment variables (Microsoft Ads)
--------------------------------------
MS_ADS_CLIENT_ID
MS_ADS_CLIENT_SECRET
MS_ADS_TENANT_ID
MS_ADS_REFRESH_TOKEN
MS_ADS_CUSTOMER_ID
MS_ADS_ACCOUNT_ID
MS_ADS_DEVELOPER_TOKEN
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# Default lookback window for all GAQL queries
_DEFAULT_DATE_RANGE = "LAST_30_DAYS"

# Maximum number of retries for transient API errors
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds


# ---------------------------------------------------------------------------
# Base connector
# ---------------------------------------------------------------------------

class BaseConnector(ABC):
    """Abstract base class for all platform connectors."""

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        self.api_key = api_key
        self.kwargs = kwargs

    @abstractmethod
    def fetch_campaigns(self) -> list[dict[str, Any]]:
        """Retrieve campaign-level performance data."""
        raise NotImplementedError

    @abstractmethod
    def fetch_keywords(self) -> list[dict[str, Any]]:
        """Retrieve keyword-level performance data."""
        raise NotImplementedError

    @abstractmethod
    def fetch_conversions(self) -> list[dict[str, Any]]:
        """Retrieve conversion-level or aggregated conversion data."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Google Ads connector
# ---------------------------------------------------------------------------

class GoogleAdsConnector(BaseConnector):
    """
    Connector for the Google Ads API.

    Uses the google-ads Python SDK (google-ads>=24.1.0).  Credentials are
    loaded from environment variables injected by platform/lib/secrets.py.
    All queries use GAQL (Google Ads Query Language) via search_stream(),
    which handles pagination automatically.

    Parameters
    ----------
    developer_token : str, optional
        Google Ads developer token.  Falls back to GOOGLE_ADS_DEVELOPER_TOKEN.
    client_id : str, optional
        OAuth2 client ID.  Falls back to GOOGLE_ADS_CLIENT_ID.
    client_secret : str, optional
        OAuth2 client secret.  Falls back to GOOGLE_ADS_CLIENT_SECRET.
    refresh_token : str, optional
        OAuth2 refresh token.  Falls back to GOOGLE_ADS_REFRESH_TOKEN.
    customer_id : str, optional
        Google Ads customer (account) ID without dashes.
        Falls back to GOOGLE_ADS_CUSTOMER_ID.
    manager_customer_id : str, optional
        MCC manager account ID.  Falls back to GOOGLE_ADS_MANAGER_ID.
    api_version : str, optional
        Google Ads API version (e.g. "v18").  Falls back to
        GOOGLE_ADS_API_VERSION or "v18".
    date_range : str, optional
        GAQL date range constant (default "LAST_30_DAYS").
    """

    def __init__(
        self,
        developer_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        customer_id: str | None = None,
        manager_customer_id: str | None = None,
        api_version: str | None = None,
        date_range: str = _DEFAULT_DATE_RANGE,
        **kwargs: Any,
    ) -> None:
        self.developer_token = developer_token or os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
        self.client_id = client_id or os.getenv("GOOGLE_ADS_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("GOOGLE_ADS_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.getenv("GOOGLE_ADS_REFRESH_TOKEN")
        self.customer_id = (
            (customer_id or os.getenv("GOOGLE_ADS_CUSTOMER_ID", ""))
            .replace("-", "")  # normalize "123-456-7890" → "1234567890"
        )
        self.manager_customer_id = (
            (manager_customer_id or os.getenv("GOOGLE_ADS_MANAGER_ID", ""))
            .replace("-", "")
        )
        self.api_version = api_version or os.getenv("GOOGLE_ADS_API_VERSION", "v18")
        self.date_range = date_range

        super().__init__(api_key=self.developer_token, **kwargs)

        missing = [
            name for name, val in [
                ("developer_token", self.developer_token),
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
                ("refresh_token", self.refresh_token),
                ("customer_id", self.customer_id),
            ]
            if not val
        ]
        if missing:
            raise ValueError(
                f"GoogleAdsConnector missing required credentials: {missing}. "
                "Set via constructor args or environment variables."
            )

        self._client = None  # lazy-initialised by _get_client()

    # ------------------------------------------------------------------
    # SDK client
    # ------------------------------------------------------------------

    def _get_client(self):
        """Return a cached GoogleAdsClient, creating it on first call."""
        if self._client is not None:
            return self._client

        try:
            from google.ads.googleads.client import GoogleAdsClient
        except ImportError as exc:
            raise ImportError(
                "google-ads package is not installed. "
                "Run: pip install google-ads>=24.1.0"
            ) from exc

        config: dict[str, Any] = {
            "developer_token": self.developer_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "use_proto_plus": True,
        }
        if self.manager_customer_id:
            config["login_customer_id"] = self.manager_customer_id

        self._client = GoogleAdsClient.load_from_dict(config, version=self.api_version)
        logger.info(
            "GoogleAdsClient initialised (version=%s, customer_id=%s)",
            self.api_version, self.customer_id,
        )
        return self._client

    # ------------------------------------------------------------------
    # GAQL query runner with retry
    # ------------------------------------------------------------------

    def _run_query(self, gaql: str) -> list[Any]:
        """Execute a GAQL query and return all rows, with exponential backoff retry.

        Args:
            gaql: GAQL query string.

        Returns:
            List of GoogleAdsRow proto-plus objects.

        Raises:
            google.ads.googleads.errors.GoogleAdsException: On non-retryable errors.
        """
        try:
            from google.ads.googleads.errors import GoogleAdsException
        except ImportError as exc:
            raise ImportError("google-ads package is not installed.") from exc

        client = self._get_client()
        ga_service = client.get_service("GoogleAdsService")

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                stream = ga_service.search_stream(
                    customer_id=self.customer_id,
                    query=gaql,
                )
                rows: list[Any] = []
                for batch in stream:
                    rows.extend(batch.results)
                logger.debug("GAQL returned %d rows (attempt %d)", len(rows), attempt)
                return rows

            except GoogleAdsException as exc:
                # Extract the first error code for logging
                error_codes = [
                    e.error_code.WhichOneof("error_code")
                    for e in exc.failure.errors
                ]
                logger.error(
                    "GoogleAdsException on attempt %d/%d: request_id=%s codes=%s",
                    attempt, _MAX_RETRIES, exc.request_id, error_codes,
                )
                # Retry on TRANSIENT_ERROR; raise immediately on others
                retryable = any("TRANSIENT" in str(c).upper() for c in error_codes)
                if not retryable or attempt == _MAX_RETRIES:
                    raise
                sleep_secs = _RETRY_BACKOFF_BASE ** attempt
                logger.info("Retrying in %.1f seconds...", sleep_secs)
                time.sleep(sleep_secs)

        return []  # unreachable, satisfies type checker

    # ------------------------------------------------------------------
    # fetch_campaigns
    # ------------------------------------------------------------------

    def fetch_campaigns(self) -> list[dict[str, Any]]:
        """Fetch enabled campaign performance data for the last 30 days.

        Returns
        -------
        List of dicts with keys:
            id, name, status, cost_micros, cost, impressions, clicks,
            conversions, budget_micros, budget
        """
        gaql = f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign_budget.amount_micros,
                metrics.cost_micros,
                metrics.impressions,
                metrics.clicks,
                metrics.conversions,
                metrics.all_conversions
            FROM campaign
            WHERE segments.date DURING {self.date_range}
                AND campaign.status = 'ENABLED'
            ORDER BY metrics.cost_micros DESC
        """
        rows = self._run_query(gaql)
        results: list[dict[str, Any]] = []
        for row in rows:
            cost_micros = row.metrics.cost_micros
            budget_micros = row.campaign_budget.amount_micros
            conversions = row.metrics.conversions
            results.append({
                "id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": row.campaign.status.name,
                "cost_micros": cost_micros,
                "cost": cost_micros / 1_000_000,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "conversions": conversions,
                "all_conversions": row.metrics.all_conversions,
                "budget_micros": budget_micros,
                "budget": budget_micros / 1_000_000,
                # Pre-compute CPA for transformation layer
                "cpa": (cost_micros / 1_000_000 / conversions) if conversions > 0 else None,
            })
        logger.info("Fetched %d Google campaigns", len(results))
        return results

    # ------------------------------------------------------------------
    # fetch_keywords
    # ------------------------------------------------------------------

    def fetch_keywords(self) -> list[dict[str, Any]]:
        """Fetch enabled keyword performance data for the last 30 days.

        Returns
        -------
        List of dicts with keys:
            criterion_id, keyword_text, match_type, campaign_id,
            ad_group_id, cpc_bid_micros, cpc_bid, cost_micros, cost,
            clicks, conversions, impressions, resource_name
        """
        gaql = f"""
            SELECT
                ad_group_criterion.criterion_id,
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.cpc_bid_micros,
                ad_group_criterion.resource_name,
                campaign.id,
                ad_group.id,
                metrics.cost_micros,
                metrics.clicks,
                metrics.conversions,
                metrics.impressions
            FROM keyword_view
            WHERE segments.date DURING {self.date_range}
                AND ad_group_criterion.status = 'ENABLED'
                AND campaign.status = 'ENABLED'
                AND ad_group.status = 'ENABLED'
            ORDER BY metrics.cost_micros DESC
        """
        rows = self._run_query(gaql)
        results: list[dict[str, Any]] = []
        for row in rows:
            cpc_bid_micros = row.ad_group_criterion.cpc_bid_micros
            cost_micros = row.metrics.cost_micros
            conversions = row.metrics.conversions
            results.append({
                "criterion_id": str(row.ad_group_criterion.criterion_id),
                "keyword_text": row.ad_group_criterion.keyword.text,
                "match_type": row.ad_group_criterion.keyword.match_type.name,
                "campaign_id": str(row.campaign.id),
                "ad_group_id": str(row.ad_group.id),
                "cpc_bid_micros": cpc_bid_micros,
                "cpc_bid": cpc_bid_micros / 1_000_000,
                "cost_micros": cost_micros,
                "cost": cost_micros / 1_000_000,
                "clicks": row.metrics.clicks,
                "conversions": conversions,
                "impressions": row.metrics.impressions,
                "resource_name": row.ad_group_criterion.resource_name,
                "cpa": (cost_micros / 1_000_000 / conversions) if conversions > 0 else None,
            })
        logger.info("Fetched %d Google keywords", len(results))
        return results

    # ------------------------------------------------------------------
    # fetch_conversions
    # ------------------------------------------------------------------

    def fetch_conversions(self) -> list[dict[str, Any]]:
        """Fetch conversion action performance data for the last 30 days.

        Returns
        -------
        List of dicts with keys:
            conversion_action_id, conversion_action_name, campaign_id,
            all_conversions, conversions_value
        """
        gaql = f"""
            SELECT
                conversion_action.id,
                conversion_action.name,
                conversion_action.category,
                campaign.id,
                metrics.all_conversions,
                metrics.conversions_value,
                metrics.cost_micros
            FROM campaign
            WHERE segments.date DURING {self.date_range}
                AND campaign.status = 'ENABLED'
            ORDER BY metrics.all_conversions DESC
        """
        rows = self._run_query(gaql)
        results: list[dict[str, Any]] = []
        for row in rows:
            all_conversions = row.metrics.all_conversions
            if all_conversions <= 0:
                continue
            results.append({
                "conversion_action_id": str(row.conversion_action.id),
                "conversion_action_name": row.conversion_action.name,
                "conversion_action_category": row.conversion_action.category.name,
                "campaign_id": str(row.campaign.id),
                "all_conversions": all_conversions,
                "conversions_value": row.metrics.conversions_value,
                "cost_micros": row.metrics.cost_micros,
                "cost": row.metrics.cost_micros / 1_000_000,
            })
        logger.info("Fetched %d Google conversion records", len(results))
        return results


# ---------------------------------------------------------------------------
# Google Ads mutation helpers (called from platform/handlers/agent_handler.py)
# ---------------------------------------------------------------------------

class GoogleAdsMutator:
    """
    Applies budget and bid mutations to the Google Ads API.

    Kept separate from GoogleAdsConnector (read-only) to make the
    distinction between fetch and mutate operations explicit.

    Parameters
    ----------
    connector : GoogleAdsConnector
        An authenticated connector whose _get_client() will be reused.
    """

    def __init__(self, connector: GoogleAdsConnector) -> None:
        self._connector = connector

    def update_campaign_budgets(
        self,
        budget_updates: dict[str, float],
        budget_increase_cap: float = 0.20,
    ) -> dict[str, bool]:
        """Update daily budgets for a set of campaigns.

        For each campaign, the current budget is queried first, the 20% cap
        is validated, and then mutated.  Campaigns that fail the cap check
        are skipped and recorded as False in the result.

        Args:
            budget_updates: Mapping of campaign ID → new daily budget (USD).
            budget_increase_cap: Maximum allowed fractional increase per step.

        Returns:
            Mapping of campaign ID → True (applied) / False (rejected or failed).
        """
        try:
            from google.ads.googleads.errors import GoogleAdsException
        except ImportError as exc:
            raise ImportError("google-ads package is not installed.") from exc

        client = self._connector._get_client()
        campaign_budget_service = client.get_service("CampaignBudgetService")
        ga_service = client.get_service("GoogleAdsService")
        results: dict[str, bool] = {}

        for campaign_id, new_budget_usd in budget_updates.items():
            try:
                # Step 1: look up the campaign's budget resource name and current amount
                query = f"""
                    SELECT
                        campaign.id,
                        campaign.campaign_budget,
                        campaign_budget.amount_micros,
                        campaign_budget.resource_name
                    FROM campaign
                    WHERE campaign.id = {campaign_id}
                    LIMIT 1
                """
                rows = list(self._connector._run_query(query))
                if not rows:
                    logger.warning("Campaign %s not found; skipping budget update", campaign_id)
                    results[campaign_id] = False
                    continue

                row = rows[0]
                budget_resource = row.campaign_budget.resource_name
                current_micros = row.campaign_budget.amount_micros
                current_usd = current_micros / 1_000_000

                # Step 2: cap check
                if current_usd > 0:
                    increase = (new_budget_usd - current_usd) / current_usd
                    if increase > budget_increase_cap:
                        logger.error(
                            "Budget cap rejected for campaign %s: $%.2f → $%.2f (%.1f%% > %.0f%% cap)",
                            campaign_id, current_usd, new_budget_usd,
                            increase * 100, budget_increase_cap * 100,
                        )
                        results[campaign_id] = False
                        continue

                # Step 3: mutate
                CampaignBudgetOperation = client.get_type("CampaignBudgetOperation")
                operation = CampaignBudgetOperation()
                operation.update.resource_name = budget_resource
                operation.update.amount_micros = int(new_budget_usd * 1_000_000)
                client.copy_from(
                    operation.update_mask,
                    client.get_type("FieldMask"),
                )
                operation.update_mask.paths.append("amount_micros")

                campaign_budget_service.mutate_campaign_budgets(
                    customer_id=self._connector.customer_id,
                    operations=[operation],
                )
                logger.info(
                    "[Google] Campaign %s budget updated: $%.2f → $%.2f",
                    campaign_id, current_usd, new_budget_usd,
                )
                results[campaign_id] = True

            except GoogleAdsException as exc:
                logger.error(
                    "Failed to update budget for campaign %s: %s", campaign_id, exc
                )
                results[campaign_id] = False

        return results

    def update_keyword_bids(
        self,
        bid_updates: dict[str, float],
        campaign_id: str,
    ) -> dict[str, bool]:
        """Update CPC bids for keywords within a campaign.

        Keyword resource names are looked up by keyword text within the given
        campaign, then mutated in a single batch call.

        Args:
            bid_updates: Mapping of keyword text → new CPC bid (USD).
            campaign_id: Campaign containing the keywords.

        Returns:
            Mapping of keyword text → True (applied) / False (failed).
        """
        try:
            from google.ads.googleads.errors import GoogleAdsException
        except ImportError as exc:
            raise ImportError("google-ads package is not installed.") from exc

        client = self._connector._get_client()
        ad_group_criterion_service = client.get_service("AdGroupCriterionService")
        results: dict[str, bool] = {kw: False for kw in bid_updates}

        if not bid_updates:
            return results

        # Step 1: look up resource names for all keywords in this campaign
        keyword_list = ", ".join(f"'{kw}'" for kw in bid_updates)
        query = f"""
            SELECT
                ad_group_criterion.resource_name,
                ad_group_criterion.keyword.text,
                ad_group_criterion.cpc_bid_micros
            FROM ad_group_criterion
            WHERE campaign.id = {campaign_id}
                AND ad_group_criterion.keyword.text IN ({keyword_list})
                AND ad_group_criterion.status = 'ENABLED'
        """
        try:
            rows = self._connector._run_query(query)
        except GoogleAdsException as exc:
            logger.error("Failed to look up keyword resource names: %s", exc)
            return results

        if not rows:
            logger.warning("No matching keywords found in campaign %s", campaign_id)
            return results

        # Step 2: build mutation operations
        AdGroupCriterionOperation = client.get_type("AdGroupCriterionOperation")
        operations: list[Any] = []
        keyword_to_resource: dict[str, str] = {}

        for row in rows:
            kw_text = row.ad_group_criterion.keyword.text
            resource_name = row.ad_group_criterion.resource_name
            keyword_to_resource[kw_text] = resource_name

            if kw_text not in bid_updates:
                continue

            new_bid_micros = int(bid_updates[kw_text] * 1_000_000)
            operation = AdGroupCriterionOperation()
            operation.update.resource_name = resource_name
            operation.update.cpc_bid_micros = new_bid_micros
            client.copy_from(operation.update_mask, client.get_type("FieldMask"))
            operation.update_mask.paths.append("cpc_bid_micros")
            operations.append(operation)

        if not operations:
            return results

        # Step 3: send batch mutation
        try:
            ad_group_criterion_service.mutate_ad_group_criteria(
                customer_id=self._connector.customer_id,
                operations=operations,
            )
            for kw_text in keyword_to_resource:
                if kw_text in bid_updates:
                    logger.info(
                        "[Google] Keyword '%s' (campaign %s) bid updated to $%.4f",
                        kw_text, campaign_id, bid_updates[kw_text],
                    )
                    results[kw_text] = True
        except GoogleAdsException as exc:
            logger.error(
                "Batch bid update failed for campaign %s: %s", campaign_id, exc
            )

        return results


# ---------------------------------------------------------------------------
# Microsoft Ads connector (Phase 3 stub)
# ---------------------------------------------------------------------------

class MicrosoftAdsConnector(BaseConnector):
    """
    Connector for the Microsoft Advertising (Bing) API.

    Phase 3 stub — fetch methods return empty lists.  The bingads SDK
    integration will be implemented in Phase 3.  Report polling is handled
    externally by the BingReportPoller Step Functions state machine; this
    connector will read completed CSV data from S3.

    Parameters
    ----------
    client_id : str, optional
        OAuth client ID.  Falls back to MS_ADS_CLIENT_ID.
    client_secret : str, optional
        Falls back to MS_ADS_CLIENT_SECRET.
    tenant_id : str, optional
        Azure AD tenant ID.  Falls back to MS_ADS_TENANT_ID.
    refresh_token : str, optional
        Falls back to MS_ADS_REFRESH_TOKEN.
    customer_id : str, optional
        Falls back to MS_ADS_CUSTOMER_ID.
    account_id : str, optional
        Advertiser account ID (required by bingads SDK).
        Falls back to MS_ADS_ACCOUNT_ID.
    developer_token : str, optional
        Falls back to MS_ADS_DEVELOPER_TOKEN.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str | None = None,
        refresh_token: str | None = None,
        customer_id: str | None = None,
        account_id: str | None = None,
        developer_token: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.client_id = client_id or os.getenv("MS_ADS_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("MS_ADS_CLIENT_SECRET")
        self.tenant_id = tenant_id or os.getenv("MS_ADS_TENANT_ID")
        self.refresh_token = refresh_token or os.getenv("MS_ADS_REFRESH_TOKEN")
        self.customer_id = customer_id or os.getenv("MS_ADS_CUSTOMER_ID")
        self.account_id = account_id or os.getenv("MS_ADS_ACCOUNT_ID")
        self.developer_token = developer_token or os.getenv("MS_ADS_DEVELOPER_TOKEN")

        super().__init__(api_key=self.client_id, **kwargs)

        missing = [
            name for name, val in [
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
                ("tenant_id", self.tenant_id),
                ("refresh_token", self.refresh_token),
                ("customer_id", self.customer_id),
                ("account_id", self.account_id),
            ]
            if not val
        ]
        if missing:
            raise ValueError(
                f"MicrosoftAdsConnector missing required credentials: {missing}. "
                "Set via constructor args or environment variables."
            )

    def fetch_campaigns(self) -> list[dict[str, Any]]:
        """Phase 3 stub — returns empty list."""
        logger.info("MicrosoftAdsConnector.fetch_campaigns: Phase 3 stub")
        return []

    def fetch_keywords(self) -> list[dict[str, Any]]:
        """Phase 3 stub — returns empty list."""
        logger.info("MicrosoftAdsConnector.fetch_keywords: Phase 3 stub")
        return []

    def fetch_conversions(self) -> list[dict[str, Any]]:
        """Phase 3 stub — returns empty list."""
        logger.info("MicrosoftAdsConnector.fetch_conversions: Phase 3 stub")
        return []


# ---------------------------------------------------------------------------
# fetch_all_data
# ---------------------------------------------------------------------------

def fetch_all_data(connectors: list[BaseConnector]) -> dict[str, Any]:
    """Fetch data from all configured connectors.

    Parameters
    ----------
    connectors : list[BaseConnector]
        Instantiated connector objects.

    Returns
    -------
    dict
        Keyed by platform name ("googleads", "microsoftads") with sub-keys
        "campaigns", "keywords", "conversions".
    """
    data: dict[str, Any] = {}
    for conn in connectors:
        platform_name = conn.__class__.__name__.replace("Connector", "").lower()
        logger.info("Fetching data from %s", platform_name)
        try:
            data[platform_name] = {
                "campaigns": conn.fetch_campaigns(),
                "keywords": conn.fetch_keywords(),
                "conversions": conn.fetch_conversions(),
            }
        except Exception as exc:
            logger.error("Failed to fetch data from %s: %s", platform_name, exc)
            data[platform_name] = {"campaigns": [], "keywords": [], "conversions": []}
    return data
