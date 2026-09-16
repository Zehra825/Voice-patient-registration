"""Provisioning logic against a fake Vapi API (no network)."""

import json

import httpx

from app.config import Settings
from app.voice import assistant_config, provisioning

SETTINGS = Settings(vapi_api_key="test", public_base_url="https://app.example.com", vapi_webhook_secret="shh")


def fake_vapi(reject_voice=False, existing_numbers=None):
    state = {"assistant_posts": [], "number_posts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/assistant" and method == "GET":
            return httpx.Response(200, json=[])
        if path == "/assistant" and method == "POST":
            body = json.loads(request.content)
            state["assistant_posts"].append(body)
            if reject_voice and body["voice"]["provider"] == "azure":
                return httpx.Response(400, json={"message": ["voice.voiceId must be one of ..."]})
            return httpx.Response(201, json={"id": "asst_1", **body})
        if path == "/phone-number" and method == "GET":
            return httpx.Response(200, json=existing_numbers or [])
        if path == "/phone-number" and method == "POST":
            body = json.loads(request.content)
            state["number_posts"].append(body)
            return httpx.Response(201, json={"id": "pn_1", "number": "+15125550100", "status": "active", **body})
        if path.startswith("/phone-number/") and method == "PATCH":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "number": "+14155550100", **json.loads(request.content)})
        return httpx.Response(404)

    return state, handler


def run(monkeypatch, handler):
    monkeypatch.setattr(provisioning, "_client", lambda s: httpx.Client(base_url="https://api.vapi.ai", transport=httpx.MockTransport(handler)))
    return provisioning.sync(SETTINGS, area_code="512")


def test_payload_wires_webhook_and_secret():
    payload = assistant_config.build_assistant_payload(SETTINGS)
    assert payload["server"]["url"] == "https://app.example.com/vapi/webhook"
    assert payload["server"]["headers"] == {"X-Vapi-Secret": "shh"}
    names = [t.get("function", {}).get("name", t["type"]) for t in payload["model"]["tools"]]
    assert names == ["validate_fields", "save_patient", "verify_existing_patient", "update_patient",
                     "get_available_appointments", "book_appointment", "endCall"]
    assert "{{customer.number}}" in payload["model"]["messages"][0]["content"]


def test_sync_creates_assistant_and_number(monkeypatch):
    state, handler = fake_vapi()
    result = run(monkeypatch, handler)
    assert result["assistant_id"] == "asst_1" and result["assistant_variant"] == "full"
    assert result["phone_number"] == "+15125550100"
    assert state["number_posts"][0] == {"provider": "vapi", "assistantId": "asst_1", "name": "Patient Registration Line", "numberDesiredAreaCode": "512"}


def test_sync_falls_back_when_voice_rejected(monkeypatch):
    state, handler = fake_vapi(reject_voice=True)
    result = run(monkeypatch, handler)
    assert result["assistant_variant"].startswith("fallback-voice/")
    assert len(result["rejected_variants"]) == 2


def test_sync_reuses_unassigned_number(monkeypatch):
    state, handler = fake_vapi(existing_numbers=[{"id": "pn_existing", "number": "+14155550100"}])
    result = run(monkeypatch, handler)
    assert result["phone_number_id"] == "pn_existing"
    assert state["number_posts"] == []


def test_webhook_secret_derived_from_api_key(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("VAPI_API_KEY", "abc")
    first, second = Settings().vapi_webhook_secret, Settings().vapi_webhook_secret
    assert first and first == second and "abc" not in first
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "explicit")
    assert Settings().vapi_webhook_secret == "explicit"


def test_autosetup_records_phone_number(monkeypatch):
    from app.voice import autosetup

    monkeypatch.setattr(provisioning, "sync", lambda s: {"assistant_id": "a1", "phone_number": "+15125550100", "assistant_variant": "full"})
    autosetup._run(SETTINGS, attempts=1, wait_seconds=0)
    status = autosetup.snapshot()
    assert status["state"] == "ready" and status["phone_number"] == "+15125550100"


def test_autosetup_error_does_not_raise(monkeypatch):
    from app.voice import autosetup

    def fail(_s):
        raise provisioning.VapiError("HTTP 401 invalid key")

    monkeypatch.setattr(provisioning, "sync", fail)
    autosetup._run(SETTINGS, attempts=3, wait_seconds=0)
    assert autosetup.snapshot()["state"] == "error"
