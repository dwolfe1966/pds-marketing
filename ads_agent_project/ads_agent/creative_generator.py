"""
ads_agent.creative_generator
=============================

Generates, filters, and selects Google Ads creative assets (headlines and
descriptions) using the Anthropic Claude API.

The module handles the full creative pipeline:
  1. build_prompts()          — construct a structured prompt for the LLM
  2. generate_raw_creatives() — call Claude API, parse JSON response
  3. filter_creatives()       — remove creatives containing prohibited phrases
  4. select_top_creatives()   — rank by historical performance score

Character limits enforced per Google Ads policy:
  - Headline    : 30 characters maximum
  - Description : 90 characters maximum

Credentials
-----------
The Anthropic API key is read from the ANTHROPIC_API_KEY environment variable,
which is injected by platform/lib/secrets.py from the adsCredentials secret.
It can also be passed directly to the constructor.

Example usage::

    generator = CreativeGenerator()
    prompt = generator.build_prompts("idlookup.ai")
    raw = generator.generate_raw_creatives(prompt)
    filtered = generator.filter_creatives(raw)
    selected = generator.select_top_creatives(filtered, performance_history={})
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)

# Google Ads hard limits
HEADLINE_MAX_CHARS = 30
DESCRIPTION_MAX_CHARS = 90

# Minimum acceptable creative count before triggering a retry
MIN_HEADLINES = 3
MIN_DESCRIPTIONS = 2

# Retry config
_MAX_RETRIES = 2
_RETRY_BACKOFF = 3.0  # seconds

# Claude model to use
_MODEL = "claude-opus-4-6"


@dataclass
class Creative:
    """A single ad creative asset.

    Attributes:
        headline:    Headline text (enforced ≤ 30 chars).
        description: Description text (enforced ≤ 90 chars).
        score:       Performance score assigned by select_top_creatives().
    """

    headline: str
    description: str
    score: float | None = None


class CreativeGenerator:
    """Generates ad creatives via the Anthropic Claude API.

    Parameters
    ----------
    prohibited_phrases : iterable of str, optional
        Additional phrases to ban beyond the built-in policy list.
    api_key : str, optional
        Anthropic API key.  Falls back to ANTHROPIC_API_KEY env var.
    model : str, optional
        Claude model ID.  Defaults to claude-opus-4-6.
    max_retries : int, optional
        Number of retry attempts if the LLM returns insufficient creatives.
    """

    # Built-in policy-based banned phrases.  Extended via constructor.
    _DEFAULT_PROHIBITED: list[str] = [
        "violence",
        "harassment",
        "discrimination",
        "illegal",
        "hack",
        "stalk",
        "spy",
        "free",          # implies no subscription cost — misleading
        "guaranteed",    # unsubstantiated superlative
        "click here",    # Google Ads policy violation
    ]

    def __init__(
        self,
        prohibited_phrases: Iterable[str] | None = None,
        api_key: str | None = None,
        model: str = _MODEL,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        extra = set(prohibited_phrases or [])
        self.prohibited_phrases = set(self._DEFAULT_PROHIBITED) | extra
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._model = model
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # 1. Prompt construction
    # ------------------------------------------------------------------

    def build_prompts(self, service_name: str) -> str:
        """Construct a structured prompt for Claude.

        The prompt instructs the model to return strict JSON — no markdown,
        no prose — so that generate_raw_creatives() can parse it reliably.

        Args:
            service_name: Name of the subscription service (e.g. "idlookup.ai").

        Returns:
            Prompt string ready to send to the Claude API.
        """
        return (
            f'Generate ad creatives for "{service_name}", a subscription service '
            "that helps people search public records, monitor their digital footprint, "
            "and protect their personal information online.\n\n"
            "Return ONLY a valid JSON array — no markdown, no explanation, no code fences.\n"
            "Each element must have exactly two keys:\n"
            f'  "headline"    : string, STRICT maximum {HEADLINE_MAX_CHARS} characters\n'
            f'  "description" : string, STRICT maximum {DESCRIPTION_MAX_CHARS} characters\n\n'
            f"Generate at least 5 headlines and 4 descriptions as separate objects.\n"
            "Requirements:\n"
            "  - Emphasise privacy, peace of mind, and legitimate use\n"
            "  - Include a clear call to action (e.g. 'Start Your Search', 'Try It Now')\n"
            "  - Never imply the service is free, illegal, or used for harassment\n"
            "  - Never use superlatives like 'best', 'guaranteed', '#1'\n"
            "  - Comply fully with Google Ads editorial policies\n\n"
            "Example output format (do not copy these exact strings):\n"
            '[\n'
            '  {"headline": "Search Public Records Now", '
            '"description": "Find public records about anyone quickly and securely."},\n'
            '  {"headline": "Monitor Your Digital Image", '
            '"description": "See what others find when they search your name online."}\n'
            ']'
        )

    # ------------------------------------------------------------------
    # 2. LLM call + JSON parsing
    # ------------------------------------------------------------------

    def generate_raw_creatives(self, prompt: str) -> List[Creative]:
        """Call the Claude API and parse the response into Creative objects.

        Retries up to self._max_retries times if the response contains fewer
        than MIN_HEADLINES headlines or MIN_DESCRIPTIONS descriptions, or if
        JSON parsing fails.

        Args:
            prompt: Prompt string from build_prompts().

        Returns:
            List of Creative objects with headline and description populated.
            Returns an empty list if the API key is missing or all retries fail.
        """
        if not self._api_key:
            logger.error(
                "ANTHROPIC_API_KEY is not set — cannot generate creatives. "
                "Set via environment variable or CreativeGenerator(api_key=...)."
            )
            return []

        try:
            import anthropic
        except ImportError:
            logger.error(
                "anthropic package is not installed. Run: pip install anthropic>=0.25.0"
            )
            return []

        client = anthropic.Anthropic(api_key=self._api_key)
        system_prompt = (
            "You are an expert Google Ads copywriter specialising in subscription "
            "services. You always comply with Google Ads editorial policies. "
            "You respond only with valid JSON — no markdown, no explanation."
        )

        for attempt in range(1, self._max_retries + 2):  # +2: initial + retries
            try:
                logger.info(
                    "Calling Claude API for creatives (model=%s attempt=%d)",
                    self._model, attempt,
                )
                message = client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_text = message.content[0].text.strip()
                creatives = self._parse_response(raw_text)

                headlines = [c for c in creatives if c.headline]
                descriptions = [c for c in creatives if c.description]

                if len(headlines) >= MIN_HEADLINES and len(descriptions) >= MIN_DESCRIPTIONS:
                    logger.info(
                        "Claude returned %d creatives on attempt %d",
                        len(creatives), attempt,
                    )
                    return creatives

                logger.warning(
                    "Insufficient creatives on attempt %d: %d headlines, %d descriptions "
                    "(need %d/%d). Retrying...",
                    attempt, len(headlines), len(descriptions),
                    MIN_HEADLINES, MIN_DESCRIPTIONS,
                )

            except anthropic.APIError as exc:
                logger.error("Anthropic API error on attempt %d: %s", attempt, exc)

            if attempt <= self._max_retries:
                time.sleep(_RETRY_BACKOFF)

        logger.error("All %d attempts to generate creatives failed.", self._max_retries + 1)
        return []

    def _parse_response(self, raw_text: str) -> List[Creative]:
        """Parse a JSON array from the LLM response text.

        Handles common LLM output issues:
          - Markdown code fences (```json ... ```)
          - Leading/trailing prose before/after the JSON array
          - Truncated characters beyond the hard limits

        Args:
            raw_text: Raw string response from the Claude API.

        Returns:
            List of Creative objects.  Returns empty list on parse failure.
        """
        # Strip markdown code fences if present
        text = re.sub(r"```(?:json)?\s*", "", raw_text).strip()

        # Extract the JSON array — find the outermost [ ... ]
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            logger.warning("No JSON array found in LLM response: %r", text[:200])
            return []

        json_str = text[start:end + 1]
        try:
            data: list[dict[str, Any]] = json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.warning("JSON parse error: %s — raw: %r", exc, json_str[:200])
            return []

        if not isinstance(data, list):
            logger.warning("Expected JSON array, got %s", type(data).__name__)
            return []

        creatives: List[Creative] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            headline = str(item.get("headline", "")).strip()
            description = str(item.get("description", "")).strip()
            if not headline and not description:
                continue
            # Hard-enforce character limits
            headline = headline[:HEADLINE_MAX_CHARS]
            description = description[:DESCRIPTION_MAX_CHARS]
            creatives.append(Creative(headline=headline, description=description))

        logger.debug("Parsed %d creatives from LLM response", len(creatives))
        return creatives

    # ------------------------------------------------------------------
    # 3. Filter
    # ------------------------------------------------------------------

    def filter_creatives(self, creatives: Iterable[Creative]) -> List[Creative]:
        """Remove creatives containing any prohibited phrase.

        Matching is case-insensitive and checks the combined headline +
        description text.

        Args:
            creatives: Iterable of Creative objects.

        Returns:
            List of compliant creatives.
        """
        filtered: List[Creative] = []
        removed = 0
        for creative in creatives:
            text = f"{creative.headline} {creative.description}".lower()
            matched = [p for p in self.prohibited_phrases if p in text]
            if matched:
                logger.info(
                    "Filtered creative '%s': prohibited phrases %s",
                    creative.headline, matched,
                )
                removed += 1
                continue
            filtered.append(creative)

        if removed:
            logger.info("Filtered out %d non-compliant creatives", removed)
        return filtered

    # ------------------------------------------------------------------
    # 4. Select top creatives
    # ------------------------------------------------------------------

    def select_top_creatives(
        self,
        creatives: Iterable[Creative],
        performance_history: Dict[str, float] | None = None,
        top_n: int = 5,
    ) -> List[Creative]:
        """Rank and select the top N creatives by performance score.

        Creatives with historical data (keyed by "headline|description") are
        ranked by that score.  Unseen creatives score 0.0 and appear after
        historically-ranked ones.

        Args:
            creatives: Iterable of filtered Creative objects.
            performance_history: Optional dict mapping "headline|description"
                to a float score (e.g. CTR or conversion rate).
            top_n: Maximum number of creatives to return.

        Returns:
            List of up to top_n Creative objects, each with score set.
        """
        scored: List[Creative] = []
        for creative in creatives:
            key = f"{creative.headline}|{creative.description}"
            creative.score = (
                performance_history.get(key, 0.0)
                if performance_history
                else 0.0
            )
            scored.append(creative)

        scored.sort(key=lambda c: c.score or 0.0, reverse=True)
        selected = scored[:top_n]
        logger.info(
            "Selected %d of %d creatives (top_n=%d)",
            len(selected), len(scored), top_n,
        )
        return selected
