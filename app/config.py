"""Application settings, read once from environment variables.

No secrets live in source code: everything sensitive (DB URL, Vapi key,
webhook secret, admin token) comes from the environment.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _public_base_url() -> str:
    """PUBLIC_BASE_URL wins; otherwise fall back to Railway's injected domain."""
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        return f"https://{railway_domain}"
    return ""


def _webhook_secret() -> str:
    """Explicit VAPI_WEBHOOK_SECRET wins; otherwise derive a stable one from VAPI_API_KEY.

    Deriving it means one less variable to configure: the same server that pushes the
    assistant config to Vapi knows the secret, and it never appears in source code.
    """
    explicit = os.getenv("VAPI_WEBHOOK_SECRET", "").strip()
    if explicit:
        return explicit
    api_key = os.getenv("VAPI_API_KEY", "").strip()
    if not api_key:
        return ""
    return hmac.new(api_key.encode(), b"vapi-webhook-secret", hashlib.sha256).hexdigest()[:40]


@dataclass(frozen=True)
class Settings:
    app_name: str = "Voice Patient Registration"
    clinic_name: str = field(default_factory=lambda: os.getenv("CLINIC_NAME", "Maple Valley Health"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./patients.db"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    seed_demo_data: bool = field(default_factory=lambda: _bool("SEED_DEMO_DATA", True))

    # Optional API key for write endpoints (POST/PUT/DELETE). Empty = open (easy review).
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", ""))

    # Voice / Vapi
    public_base_url: str = field(default_factory=_public_base_url)
    vapi_api_key: str = field(default_factory=lambda: os.getenv("VAPI_API_KEY", ""))
    vapi_webhook_secret: str = field(default_factory=_webhook_secret)
    # On startup, push the assistant config to Vapi and attach a phone number (idempotent).
    vapi_auto_setup: bool = field(default_factory=lambda: _bool("VAPI_AUTO_SETUP", True))
    vapi_phone_number: str = field(default_factory=lambda: os.getenv("VAPI_PHONE_NUMBER", ""))
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))

    # Timezone used for "today" checks shown to callers and for mock appointment slots.
    clinic_timezone: str = field(default_factory=lambda: os.getenv("CLINIC_TIMEZONE", "America/New_York"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
