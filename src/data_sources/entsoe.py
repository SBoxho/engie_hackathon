"""ENTSO-E configuration boundary for a post-MVP European comparison feed."""
from __future__ import annotations

from src.config import settings


def is_configured() -> bool:
    return bool(settings.entsoe_api_token)


def require_token() -> str:
    if not settings.entsoe_api_token:
        raise RuntimeError(
            "ENTSO-E access is not configured. Add ENTSOE_API_TOKEN to .env after "
            "registering for Transparency Platform API access."
        )
    return settings.entsoe_api_token

