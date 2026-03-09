"""
Compliance monitoring for ads and targeting
=========================================

The :mod:`compliance_monitor` module provides utilities to scan ad
creatives, keywords, and targeting settings against advertising
platform policies.  By enforcing these rules, the agent helps prevent
ad disapprovals and ensures that campaigns adhere to legal and ethical
standards.  The module includes simple keyword scanning and data
structure validation; it does not perform actual platform policy
lookups and therefore should be extended with up‑to‑date policy rules.
"""

from __future__ import annotations

from typing import Iterable, Dict, Any, List


class PolicyViolation(Exception):
    """Exception raised when a compliance violation is detected."""
    pass


def scan_text_for_policies(text: str, banned_terms: Iterable[str]) -> List[str]:
    """Check text for the presence of banned terms.

    Args:
        text: The text to scan (e.g., ad headline, description).
        banned_terms: Iterable of lower‑case substrings that violate
            advertising policies.

    Returns:
        List of terms found in the text that are banned.  If the list
        is empty, no violation was detected.
    """
    found: List[str] = []
    lowered = text.lower()
    for term in banned_terms:
        if term.lower() in lowered:
            found.append(term)
    return found


def validate_targeting_settings(settings: Dict[str, Any]) -> None:
    """Validate targeting settings for compliance.

    This function checks for some basic policy restrictions, such as
    avoiding sensitive personal data categories.  It raises
    :class:`PolicyViolation` if an issue is found.  Real implementations
    must incorporate the full set of platform policies【19109617805451†L89-L135】.

    Args:
        settings: Targeting parameters (e.g., audience segments, locations).

    Raises:
        PolicyViolation: If any setting violates policy.
    """
    # Example: reject targeting that includes sensitive personal hardships
    sensitive_categories = {
        "medical_conditions",
        "sexual_orientation",
        "religion",
        "political_affiliation",
    }
    audiences = settings.get("audiences", [])
    for audience in audiences:
        if audience in sensitive_categories:
            raise PolicyViolation(f"Audience {audience} is disallowed under platform policies")
    # Example: location targeting must not be overly granular (zip code granularity)
    if settings.get("location_granularity") == "zip_code":
        raise PolicyViolation("Location targeting at the zip code level may violate policy")
    # Additional checks can be added here as needed
