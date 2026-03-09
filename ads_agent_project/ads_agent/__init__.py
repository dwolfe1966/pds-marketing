"""
ads_agent package initialization.

This package contains scaffolding for an AI‑driven advertising agent.  The
modules define class structures and function stubs for ingesting data from
advertising platforms, transforming it into analytics‑ready formats,
monitoring performance, optimizing budgets and bids, generating new
advertising creatives, managing keywords and audiences, scheduling and
evaluating experiments, and ensuring compliance with advertising policies.

Implementation of platform‑specific logic requires API credentials and
additional development.
"""

__all__ = [
    "ingestion",
    "transformation",
    "monitoring",
    "decision_engine",
    "creative_generator",
    "keyword_manager",
    "experiment_manager",
    "compliance_monitor",
]

# Import submodules to provide a convenient single namespace.  The
# presence of these imports does not load external dependencies; instead,
# they make symbols discoverable when consumers do `from ads_agent import *`.
from . import ingestion  # noqa: F401
from . import transformation  # noqa: F401
from . import monitoring  # noqa: F401
from . import decision_engine  # noqa: F401
from . import creative_generator  # noqa: F401
from . import keyword_manager  # noqa: F401
from . import experiment_manager  # noqa: F401
from . import compliance_monitor  # noqa: F401