"""Zero-touch voice setup: runs provisioning in the background at startup.

With VAPI_API_KEY set on the server, every deploy re-applies the assistant config
from code and makes sure a phone number is attached. It runs in a thread so the
API and health check come up immediately; progress is exposed at GET /vapi/status.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.config import Settings

logger = logging.getLogger("voice.autosetup")

STATUS: dict[str, Any] = {"state": "not_configured", "phone_number": None}
_lock = threading.Lock()


def _set(**values: Any) -> None:
    with _lock:
        STATUS.update(values, updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(STATUS)


def _run(settings: Settings, attempts: int = 20, wait_seconds: int = 30) -> None:
    from app.voice import provisioning

    for attempt in range(1, attempts + 1):
        try:
            _set(state="running", attempt=attempt)
            result = provisioning.sync(settings)
            number = result.get("phone_number")
            _set(
                state="ready" if number else "waiting_for_number",
                phone_number=number,
                assistant_id=result.get("assistant_id"),
                assistant_variant=result.get("assistant_variant"),
                error=None,
            )
            logger.info("vapi auto-setup %s number=%s variant=%s", STATUS["state"], number, result.get("assistant_variant"))
            if number:
                return
        except Exception as exc:  # keep the API running even if Vapi setup fails
            message = str(exc)[:600]
            _set(state="error", error=message)
            logger.error("vapi auto-setup failed (attempt %s): %s", attempt, message)
            if "VAPI_API_KEY" in message or "401" in message or "403" in message:
                return  # bad key: retrying won't help
        time.sleep(wait_seconds)


def start(settings: Settings) -> None:
    if not settings.vapi_api_key:
        _set(state="not_configured", error="Set VAPI_API_KEY to create the phone agent.")
        return
    if not settings.public_base_url:
        _set(state="error", error="No public URL. Generate a domain (Railway: Settings -> Networking) or set PUBLIC_BASE_URL.")
        return
    if not settings.vapi_auto_setup:
        _set(state="disabled")
        return
    threading.Thread(target=_run, args=(settings,), name="vapi-autosetup", daemon=True).start()
