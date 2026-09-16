"""Create/update the Vapi assistant and attach a free US phone number — idempotently.

Used by `python -m scripts.setup_vapi` and by POST /admin/vapi/sync.
Re-running is safe: the assistant is found by name and updated in place, and a
new number is only bought if none is attached to the assistant yet.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.voice import assistant_config as cfg

logger = logging.getLogger("voice.provisioning")
VAPI_BASE = "https://api.vapi.ai"


class VapiError(RuntimeError):
    pass


def _items(payload: Any) -> list[dict]:
    """List endpoints return a bare array; tolerate a {"results": [...]} wrapper too."""
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("data") or []
    return payload if isinstance(payload, list) else []


def _client(settings: Settings) -> httpx.Client:
    if not settings.vapi_api_key:
        raise VapiError("VAPI_API_KEY is not set.")
    return httpx.Client(base_url=VAPI_BASE, headers={"Authorization": f"Bearer {settings.vapi_api_key}"}, timeout=30)


def _variants(settings: Settings) -> list[tuple[str, dict]]:
    """Richest config first; progressively simpler fallbacks if Vapi rejects a field, voice or model."""
    variants: list[tuple[str, dict]] = []

    def add(label: str, model: str | None, voice: dict | None, extras: bool) -> None:
        payload = cfg.build_assistant_payload(settings, model=model, voice=voice)
        if not extras:
            for key in cfg.OPTIONAL_KEYS:
                payload.pop(key, None)
        variants.append((label, payload))

    add("full", None, None, True)
    add("no-tuning", None, None, False)
    for model in cfg.FALLBACK_MODELS:
        add(f"fallback-voice/{model}", model, cfg.FALLBACK_VOICE, True)
        add(f"fallback-voice/{model}/no-tuning", model, cfg.FALLBACK_VOICE, False)
    _, minimal = variants[-1]
    minimal = dict(minimal, transcriber={"provider": "deepgram", "model": "nova-2", "language": "en"})
    variants.append(("minimal", minimal))
    return variants


def upsert_assistant(client: httpx.Client, settings: Settings) -> tuple[dict, str, list[str]]:
    existing = client.get("/assistant", params={"limit": 100})
    existing.raise_for_status()
    match = next((a for a in _items(existing.json()) if a.get("name") == cfg.ASSISTANT_NAME), None)

    errors: list[str] = []
    for label, payload in _variants(settings):
        if match:
            resp = client.patch(f"/assistant/{match['id']}", json=payload)
        else:
            resp = client.post("/assistant", json=payload)
        if resp.status_code < 300:
            return resp.json(), label, errors
        errors.append(f"{label}: HTTP {resp.status_code} {resp.text[:300]}")
        logger.warning("vapi assistant variant rejected %s", errors[-1])
        if resp.status_code in (401, 403):
            break
    raise VapiError("Vapi rejected every assistant configuration:\n" + "\n".join(errors))


def ensure_phone_number(client: httpx.Client, settings: Settings, assistant_id: str, area_code: str | None) -> dict:
    numbers = client.get("/phone-number", params={"limit": 100})
    numbers.raise_for_status()
    existing = _items(numbers.json())
    attached = next((n for n in existing if n.get("assistantId") == assistant_id), None)
    if attached:
        return attached
    # Reuse a number created by hand in the dashboard (accounts get one free number).
    unassigned = next((n for n in existing if not n.get("assistantId") and not n.get("squadId")), None)
    if unassigned:
        resp = client.patch(f"/phone-number/{unassigned['id']}", json={"assistantId": assistant_id})
        if resp.status_code < 300:
            return resp.json()
        logger.warning("could not attach existing number %s: %s", unassigned.get("number"), resp.text[:300])

    body: dict[str, Any] = {"provider": "vapi", "assistantId": assistant_id, "name": "Patient Registration Line"}
    # Free Vapi numbers are allocated by area code; try the requested one, then a few common ones.
    candidates = [c for c in [area_code, "512", "737", "415", "628", "646", "332", "213"] if c]
    last_error = ""
    for code in dict.fromkeys(candidates):
        resp = client.post("/phone-number", json=body | {"numberDesiredAreaCode": code})
        if resp.status_code < 300:
            return _wait_for_number(client, resp.json())
        last_error = f"HTTP {resp.status_code} {resp.text[:300]}"
        logger.warning("vapi number area code %s rejected: %s", code, last_error)
    raise VapiError(f"Could not create a phone number: {last_error}")


def _wait_for_number(client: httpx.Client, number: dict, timeout_s: int = 60) -> dict:
    deadline = time.time() + timeout_s
    while not number.get("number") and time.time() < deadline:
        time.sleep(3)
        number = client.get(f"/phone-number/{number['id']}").json()
    return number


def sync(settings: Settings, area_code: str | None = None, with_phone: bool = True) -> dict[str, Any]:
    with _client(settings) as client:
        assistant, variant, rejected = upsert_assistant(client, settings)
        result: dict[str, Any] = {
            "assistant_id": assistant["id"],
            "assistant_variant": variant,
            "webhook_url": f"{settings.public_base_url}/vapi/webhook",
            "rejected_variants": rejected,
        }
        if with_phone:
            number = ensure_phone_number(client, settings, assistant["id"], area_code)
            result.update({
                "phone_number_id": number.get("id"),
                "phone_number": number.get("number"),
                "phone_status": number.get("status"),
            })
        return result
