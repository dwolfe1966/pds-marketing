"""
Experiment management module
===========================

The :mod:`experiment_manager` module automates the design, execution and
analysis of A/B (or multivariate) experiments across advertising
platforms.  Experiments allow the agent to validate hypotheses about
budget allocation, bid strategies, and creative effectiveness in a
controlled manner before broad deployment.

This module does not directly communicate with Google or Microsoft Ads
APIs; instead, it defines structures and logic that can be used to
orchestrate experiments.  Developers must implement integration points
with the relevant platform APIs to create and monitor experiments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Any


@dataclass
class ExperimentConfig:
    """Configuration for a single experiment.

    Attributes:
        name: Descriptive name of the experiment.
        objective: Primary KPI being optimized (e.g., ``'cpa'``).
        variable: What is being tested (e.g., ``'headline'``, ``'bid_strategy'``).
        control_settings: Dict of settings for the control group.
        test_settings: Dict of settings for the test group.
        split: Fraction of traffic allocated to the test group (0–1).
        start_date: When the experiment begins.
        end_date: When the experiment ends.
    """

    name: str
    objective: str
    variable: str
    control_settings: Dict[str, Any]
    test_settings: Dict[str, Any]
    split: float
    start_date: datetime
    end_date: datetime


@dataclass
class ExperimentResult:
    """Stores the outcome of an experiment once concluded.

    Attributes:
        config: The configuration that defines the experiment.
        control_metrics: Aggregated metrics for the control group.
        test_metrics: Aggregated metrics for the test group.
        winner: Indicates whether the control, test, or neither won.
        significance: Statistical significance level (e.g., p‑value).
    """

    config: ExperimentConfig
    control_metrics: Dict[str, float]
    test_metrics: Dict[str, float]
    winner: str
    significance: float


class ExperimentManager:
    """Manager for scheduling and evaluating advertising experiments.

    The manager maintains a queue of pending experiments and active
    experiments.  It can propose new experiments based on agent logic,
    initiate them on advertising platforms, and evaluate results when
    they conclude.  Statistical analysis is simplified here and uses a
    basic threshold on performance lift; developers should implement
    proper significance testing.
    """

    def __init__(self) -> None:
        self.pending: List[ExperimentConfig] = []
        self.active: List[ExperimentConfig] = []
        self.completed: List[ExperimentResult] = []

    def propose_experiment(self, config: ExperimentConfig) -> None:
        """Add a new experiment to the pending queue.

        Args:
            config: The experiment configuration to schedule.
        """
        self.pending.append(config)

    def launch_experiment(self) -> None:
        """Start the next pending experiment if any exist.

        The config is moved from the ``pending`` to the ``active`` list,
        and its start date is set to now if not specified.  In a real
        implementation, this method would call the advertising platform
        API to create the experiment.
        """
        if not self.pending:
            return
        config = self.pending.pop(0)
        if config.start_date is None:
            config.start_date = datetime.utcnow()
        self.active.append(config)
        # TODO: integrate with platform to actually launch experiment

    def evaluate_experiments(self, metrics_by_experiment: Dict[str, Dict[str, Any]]) -> None:
        """Evaluate active experiments and mark them as completed if ended.

        Args:
            metrics_by_experiment: Mapping from experiment name to
                aggregated performance metrics, containing separate
                ``control`` and ``test`` metrics dictionaries.

        This method should be called periodically (e.g., daily) to check
        whether an experiment's end date has passed and evaluate its
        outcome.  The significance calculation here is a stub; replace
        with a statistical test (e.g., z‑test, Bayesian inference).
        """
        now = datetime.utcnow()
        still_active: List[ExperimentConfig] = []
        for config in self.active:
            if config.end_date and now >= config.end_date:
                # experiment has concluded; compute results
                metrics = metrics_by_experiment.get(config.name, {})
                control_metrics = metrics.get("control", {})
                test_metrics = metrics.get("test", {})
                # Determine winner: if test improves objective by >10%
                obj = config.objective
                control_val = control_metrics.get(obj, 0)
                test_val = test_metrics.get(obj, 0)
                winner = "test" if test_val > control_val * 1.1 else (
                    "control" if control_val > test_val * 1.1 else "none"
                )
                significance = 0.05  # placeholder p‑value threshold
                result = ExperimentResult(
                    config=config,
                    control_metrics=control_metrics,
                    test_metrics=test_metrics,
                    winner=winner,
                    significance=significance,
                )
                self.completed.append(result)
            else:
                still_active.append(config)
        self.active = still_active
