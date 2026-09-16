"""Simulates Vapi calling our webhook the way it does during a real phone call."""

import json

from sqlalchemy.exc import OperationalError

CALL = {"id": "call-123", "customer": {"number": "+15125550199"}}


def tool_call(client, name, args, call=CALL, shape="modern"):
    if shape == "modern":
        item = {"id": f"tc-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        message = {"type": "tool-calls", "call": call, "toolCallList": [item]}
    else:
        message = {"type": "tool-calls", "call": call,
                   "toolWithToolCallList": [{"name": name, "toolCall": {"id": f"tc-{name}", "parameters": args}}]}
    res = client.post("/vapi/webhook", json={"message": message})
    assert res.status_code == 200
    result = res.json()["results"][0]
    assert result["toolCallId"] == f"tc-{name}"
    return json.loads(result["result"])


def test_validate_fields_reports_specific_invalid_field(client):
    out = tool_call(client, "validate_fields", {"fields": {"first_name": "Jane", "phone_number": "512555", "date_of_birth": "01/01/2999"}})
    assert out["status"] == "invalid"
    assert out["valid_fields"] == {"first_name": "Jane"}
    assert set(out["invalid_fields"]) == {"phone_number", "date_of_birth"}


def test_full_registration_flow_persists_and_logs_call(client, valid_patient):
    out = tool_call(client, "validate_fields", {"fields": {"state": "Texas", "zip_code": "787011234"}}, shape="legacy")
    assert out["valid_fields"] == {"state": "TX", "zip_code": "78701-1234"}

    not_confirmed = tool_call(client, "save_patient", {"patient": valid_patient, "caller_confirmed": False})
    assert not_confirmed["status"] == "not_saved"

    saved = tool_call(client, "save_patient", {"patient": valid_patient, "caller_confirmed": True})
    assert saved["status"] == "saved" and saved["first_name"] == "Jane"

    # Saved record is visible via the REST API (second "call" / restart-safe storage).
    listed = client.get("/patients?phone_number=5125550199").json()["data"]
    assert len(listed) == 1 and listed[0]["patient_id"] == saved["patient_id"]

    # Correction after save within the same call updates instead of duplicating.
    again = tool_call(client, "save_patient", {"patient": valid_patient | {"last_name": "Davis"}, "caller_confirmed": True})
    assert again["status"] == "saved" and again["patient_id"] == saved["patient_id"]
    assert client.get(f"/patients/{saved['patient_id']}").json()["data"]["last_name"] == "Davis"

    report = {"message": {"type": "end-of-call-report", "call": CALL, "endedReason": "assistant-ended-call",
                          "artifact": {"transcript": "AI: Hi\nUser: Hello"}, "analysis": {"summary": "Registered Jane."}}}
    assert client.post("/vapi/webhook", json=report).status_code == 200
    calls = client.get(f"/patients/{saved['patient_id']}/calls").json()["data"]
    assert calls[0]["status"] == "completed" and calls[0]["transcript"].startswith("AI:")


def test_duplicate_detection_on_second_call(client, valid_patient):
    client.post("/patients", json=valid_patient)
    second_call = {"id": "call-456", "customer": {"number": "+15125550199"}}

    out = tool_call(client, "validate_fields", {"fields": {"phone_number": "512-555-0199"}}, call=second_call)
    assert out["existing_patient"]["first_name"] == "Jane"

    dup = tool_call(client, "save_patient", {"patient": valid_patient, "caller_confirmed": True}, call=second_call)
    assert dup["status"] == "duplicate_found"

    pid = dup["existing_patient"]["patient_id"]
    assert tool_call(client, "verify_existing_patient", {"patient_id": pid, "date_of_birth": "01/01/1990"}, call=second_call)["status"] == "not_verified"
    assert tool_call(client, "verify_existing_patient", {"patient_id": pid, "date_of_birth": "03/15/1988"}, call=second_call)["status"] == "verified"

    upd = tool_call(client, "update_patient", {"patient_id": pid, "changes": {"city": "Round Rock"}, "caller_confirmed": True}, call=second_call)
    assert upd["status"] == "updated"
    assert client.get(f"/patients/{pid}").json()["data"]["city"] == "Round Rock"
    assert len(client.get("/patients").json()["data"]) == 1

    family = tool_call(client, "save_patient", {"patient": valid_patient | {"first_name": "Jill", "date_of_birth": "05/05/2015"},
                                                "caller_confirmed": True, "allow_duplicate": True}, call={"id": "call-789"})
    assert family["status"] == "saved"


def test_database_failure_returns_graceful_error(client, valid_patient, monkeypatch):
    def boom(*_a, **_k):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr("app.services.patients.create_patient", boom)
    out = tool_call(client, "save_patient", {"patient": valid_patient, "caller_confirmed": True})
    assert out["status"] == "error" and "next_step" in out
    assert client.get("/patients").json()["data"] == []


def test_dropped_call_keeps_partial_data(client):
    call = {"id": "call-drop"}
    tool_call(client, "validate_fields", {"fields": {"first_name": "Sam", "last_name": "Lee"}}, call=call)
    client.post("/vapi/webhook", json={"message": {"type": "end-of-call-report", "call": call, "endedReason": "customer-ended-call"}})
    incomplete = client.get("/calls?status=incomplete").json()["data"]
    assert incomplete[0]["collected_data"] == {"first_name": "Sam", "last_name": "Lee"}
    assert incomplete[0]["outcome"] == "abandoned"


def test_appointment_booking(client, valid_patient):
    pid = tool_call(client, "save_patient", {"patient": valid_patient, "caller_confirmed": True})["patient_id"]
    slots = tool_call(client, "get_available_appointments", {"time_of_day": "morning"})["slots"]
    assert 1 <= len(slots) <= 3
    booked = tool_call(client, "book_appointment", {"patient_id": pid, "slot_id": slots[0]["slot_id"]})
    assert booked["status"] == "booked"
    taken = tool_call(client, "book_appointment", {"patient_id": pid, "slot_id": slots[0]["slot_id"]})
    assert taken["status"] == "unavailable"
    assert len(client.get(f"/patients/{pid}/appointments").json()["data"]) == 1


def test_webhook_secret_enforced(client, monkeypatch):
    from app.config import Settings

    monkeypatch.setattr("app.voice.webhook.get_settings", lambda: Settings(vapi_webhook_secret="shh"))
    msg = {"message": {"type": "status-update", "status": "ringing", "call": CALL}}
    assert client.post("/vapi/webhook", json=msg).status_code == 401
    assert client.post("/vapi/webhook", json=msg, headers={"X-Vapi-Secret": "shh"}).status_code == 200


def test_unknown_tool_never_crashes(client):
    out = tool_call(client, "does_not_exist", {})
    assert out["status"] == "error"
