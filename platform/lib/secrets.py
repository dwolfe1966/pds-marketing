"""
platform.lib.secrets
====================

Loads credentials from AWS Secrets Manager at Lambda cold start and injects
them as environment variables.  The mapping below aligns Secrets Manager key
names with the os.getenv() calls already present in ads_agent/ingestion.py
and ads_agent/creative_generator.py — no changes to the ads_agent package are
required for credential wiring.

Usage (call once at module level in each Lambda handler):

    from lib.secrets import load_secrets
    load_secrets(os.environ["SECRET_NAME"])
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Maps the key names stored in the Secrets Manager secret to the environment
# variable names consumed by the ads_agent package and platform handlers.
SECRET_KEY_TO_ENV: dict[str, str] = {
    "google_developer_token":     "GOOGLE_ADS_DEVELOPER_TOKEN",
    "google_client_id":           "GOOGLE_ADS_CLIENT_ID",
    "google_client_secret":       "GOOGLE_ADS_CLIENT_SECRET",
    "google_refresh_token":       "GOOGLE_ADS_REFRESH_TOKEN",
    "google_manager_customer_id": "GOOGLE_ADS_MANAGER_ID",
    "google_customer_id":         "GOOGLE_ADS_CUSTOMER_ID",
    "google_ads_api_version":     "GOOGLE_ADS_API_VERSION",
    "bing_customer_id":           "MS_ADS_CUSTOMER_ID",
    "bing_account_id":            "MS_ADS_ACCOUNT_ID",
    "bing_client_id":             "MS_ADS_CLIENT_ID",
    "bing_client_secret":         "MS_ADS_CLIENT_SECRET",
    "bing_tenant_id":             "MS_ADS_TENANT_ID",
    "bing_refresh_token":         "MS_ADS_REFRESH_TOKEN",
    "bing_developer_token":       "MS_ADS_DEVELOPER_TOKEN",
    "anthropic_api_key":          "ANTHROPIC_API_KEY",
}

_loaded = False


def load_secrets(secret_name: str, region: str | None = None) -> dict[str, str]:
    """Fetch the named secret and inject all known keys as environment variables.

    Safe to call multiple times — subsequent calls are no-ops once the secret
    has been loaded successfully (warm invocation reuse).

    Args:
        secret_name: Name or ARN of the Secrets Manager secret.
        region: AWS region.  Defaults to AWS_REGION_NAME env var or us-east-1.

    Returns:
        The full secret dict (all keys, not just mapped ones).

    Raises:
        RuntimeError: If the secret cannot be retrieved.
    """
    global _loaded
    if _loaded:
        return {}

    region = region or os.environ.get("AWS_REGION_NAME", "us-east-1")
    client = boto3.client("secretsmanager", region_name=region)

    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        raise RuntimeError(
            f"Failed to retrieve secret '{secret_name}': {exc}"
        ) from exc

    raw = response.get("SecretString", "{}")
    try:
        secret: dict[str, str] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Secret '{secret_name}' is not valid JSON: {exc}"
        ) from exc

    injected: list[str] = []
    for secret_key, env_var in SECRET_KEY_TO_ENV.items():
        value = secret.get(secret_key)
        if value is not None:
            os.environ[env_var] = str(value)
            injected.append(env_var)
        else:
            logger.warning("Secret key '%s' not found in secret '%s'", secret_key, secret_name)

    logger.info("Injected %d credentials from Secrets Manager: %s", len(injected), injected)
    _loaded = True
    return secret
