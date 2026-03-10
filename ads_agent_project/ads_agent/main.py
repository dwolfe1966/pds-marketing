"""
Main orchestrator for the advertising agent
==========================================

This script demonstrates how the various components of the ads_agent
package can be combined into a workflow.  It does not perform any
network calls or require external credentials; instead, it shows the
sequence of steps a production system might follow: ingestion, data
transformation, monitoring, decision making, creative generation,
keyword management, experiment scheduling, and compliance checks.

Usage::

    python -m ads_agent.main

The orchestrator prints out sample recommendations and alerts but does
not apply any changes.  To integrate with real advertising platforms,
developers should implement the connectors and API interactions in the
respective modules.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .ingestion import BaseConnector, GoogleAdsConnector, MicrosoftAdsConnector, fetch_all_data
from .transformation import normalize_campaigns, aggregate_metrics, compute_ltv
from .monitoring import check_thresholds, summarize_alerts
from .decision_engine import allocate_budget, adjust_bids, CampaignStats, KeywordStats
from .creative_generator import CreativeGenerator
from .keyword_manager import QueryRecord, cluster_queries, suggest_keywords
from .experiment_manager import ExperimentManager, ExperimentConfig
from .compliance_monitor import scan_text_for_policies, PolicyViolation


logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")


def main() -> None:
    """Run the orchestrated workflow with sample data."""
    logging.info("Starting ads agent orchestrator")

    # 1. Ingestion: create connectors and fetch raw data (stubbed)
    # Instantiate connectors with placeholder credentials; these will
    # satisfy the validation in the stub classes.  In production, real
    # OAuth credentials must be provided.
    # Provide placeholder credential strings to satisfy constructor checks.  The
    # connectors expect separate keyword arguments rather than a single dict.
    connectors: list[BaseConnector] = [
        GoogleAdsConnector(
            developer_token="placeholder",
            client_id="placeholder",
            client_secret="placeholder",
            refresh_token="placeholder",
            customer_id="0000000000",
        ),
        MicrosoftAdsConnector(
            client_id="placeholder",
            client_secret="placeholder",
            tenant_id="placeholder",
            refresh_token="placeholder",
            customer_id="placeholder",
            account_id="placeholder",
        ),
    ]
    # Use the helper from ingestion module to fetch campaign/keyword/conversion data.
    # The helper will call the stubbed methods and return empty lists.
    try:
        raw = fetch_all_data(connectors)  # type: ignore[name-defined]
    except Exception as exc:
        logging.warning("Failed to fetch data: %s", exc)
        raw = {}
    logging.info("Fetched raw data from connectors (stubbed)")

    # 2. Sample data: define synthetic campaigns in normalized form
    normalized = [
        {
            "entity_id": "c1",
            "platform": "google",
            "cpa": 25.0,
            "conversions": 40,
            "clicks": 400,
            "impressions": 4000,
            "max_scalable_spend": 500.0,
        },
        {
            "entity_id": "c2",
            "platform": "microsoft",
            "cpa": 45.0,
            "conversions": 10,
            "clicks": 100,
            "impressions": 2000,
            "max_scalable_spend": 300.0,
        },
    ]
    agg_metrics = aggregate_metrics(normalized)
    logging.info("Aggregated metrics: %s", agg_metrics)

    # 3. Monitoring: check metrics vs thresholds
    thresholds = {"cpa": {"max": 37.5}, "conversion_rate": {"min": 0.02}}
    alerts = check_thresholds(normalized, thresholds)
    if alerts:
        logging.warning("Alerts detected:\n%s", "\n".join(summarize_alerts(alerts)))
    else:
        logging.info("No alerts detected")

    # 4. Decision engine: allocate budget and adjust bids
    campaign_stats = [
        CampaignStats(id=row["entity_id"], cpa=row["cpa"], conversions=row.get("conversions", 0), ltv=30.0, max_scalable_spend=row.get("max_scalable_spend", 100.0))
        for row in normalized
    ]
    new_budget = allocate_budget(campaign_stats, total_budget=1000.0)
    logging.info("Proposed budget allocations: %s", new_budget)

    # Example keyword stats for bid adjustment
    kw_stats = [
        KeywordStats(keyword="john smith", cpa=20.0, conversions=6, current_bid=1.0),
        KeywordStats(keyword="people search", cpa=50.0, conversions=8, current_bid=1.5),
    ]
    updated_bids = adjust_bids(kw_stats, target_cpa=30.0)
    logging.info("Updated bids: %s", updated_bids)

    # 5. Creative generation: generate and filter ads (stubbed)
    generator = CreativeGenerator()
    prompt = generator.build_prompts("idlookup.ai")
    raw_creatives = generator.generate_raw_creatives(prompt)
    filtered = generator.filter_creatives(raw_creatives)
    selected = generator.select_top_creatives(filtered, performance_history={})
    logging.info("Selected creatives: %s", selected)

    # 6. Keyword & audience management: cluster queries and suggest keywords
    query_records = [
        QueryRecord(term="john doe", clicks=100, conversions=5, cost=20.0),
        QueryRecord(term="how to search people", clicks=200, conversions=1, cost=50.0),
    ]
    clusters = cluster_queries(query_records, n_clusters=5)
    suggestions = suggest_keywords(clusters, cpa_target=30.0)
    logging.info("Keyword suggestions: %s", suggestions)

    # 7. Experiment management: propose and evaluate a simple experiment
    exp_manager = ExperimentManager()
    config = ExperimentConfig(
        name="Ad Copy Test",
        objective="cpa",
        variable="headline",
        control_settings={"headline": "Find anyone legally"},
        test_settings={"headline": "Manage your digital footprint"},
        split=0.5,
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(days=14),
    )
    exp_manager.propose_experiment(config)
    exp_manager.launch_experiment()
    # Example metrics for evaluation
    metrics_by_exp = {
        "Ad Copy Test": {
            "control": {"cpa": 32.0},
            "test": {"cpa": 28.0},
        }
    }
    exp_manager.evaluate_experiments(metrics_by_exp)
    logging.info("Completed experiments: %s", exp_manager.completed)

    # 8. Compliance monitoring: scan creatives and targeting
    banned_terms = ["violence", "harassment", "illegal"]
    for creative in selected:
        try:
            violations = scan_text_for_policies(creative.headline + " " + creative.description, banned_terms)
            if violations:
                raise PolicyViolation(f"Banned terms found: {violations}")
        except PolicyViolation as e:
            logging.error("Compliance violation in creative: %s", e)

    logging.info("Orchestrator run complete")


if __name__ == "__main__":
    main()