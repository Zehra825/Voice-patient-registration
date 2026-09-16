import uuid


def test_create_and_get_patient(client, valid_patient):
    res = client.post("/patients", json=valid_patient)
    assert res.status_code == 201
    body = res.json()
    assert body["error"] is None
    patient = body["data"]
    uuid.UUID(patient["patient_id"])
    assert patient["phone_number"] == "5125550199"
    assert patient["date_of_birth"] == "03/15/1988"
    assert patient["preferred_language"] == "English"
    assert patient["created_at"].endswith("Z")

    got = client.get(f"/patients/{patient['patient_id']}")
    assert got.status_code == 200
    assert got.json()["data"]["last_name"] == "Doe"


def test_validation_errors_return_422_with_field_details(client, valid_patient):
    bad = valid_patient | {"phone_number": "555", "date_of_birth": "01/01/2999", "state": "ZZ"}
    res = client.post("/patients", json=bad)
    assert res.status_code == 422
    body = res.json()
    assert body["data"] is None
    fields = {d["field"] for d in body["error"]["details"]}
    assert {"phone_number", "date_of_birth", "state"} <= fields


def test_missing_required_and_unknown_fields(client, valid_patient):
    missing = dict(valid_patient)
    missing.pop("last_name")
    assert client.post("/patients", json=missing).status_code == 422
    assert client.post("/patients", json=valid_patient | {"patient_id": str(uuid.uuid4())}).status_code == 422


def test_malformed_json_is_400(client):
    res = client.post("/patients", content="{not json", headers={"Content-Type": "application/json"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "BAD_REQUEST"


def test_list_filters(client, valid_patient):
    client.post("/patients", json=valid_patient)
    client.post("/patients", json=valid_patient | {"first_name": "John", "last_name": "Smith", "phone_number": "2125550111"})
    assert len(client.get("/patients").json()["data"]) == 2
    assert [p["first_name"] for p in client.get("/patients?last_name=doe").json()["data"]] == ["Jane"]
    assert len(client.get("/patients?date_of_birth=03/15/1988").json()["data"]) == 2
    assert [p["last_name"] for p in client.get("/patients?phone_number=(212) 555-0111").json()["data"]] == ["Smith"]
    assert client.get("/patients?date_of_birth=notadate").status_code == 422


def test_partial_update(client, valid_patient):
    pid = client.post("/patients", json=valid_patient).json()["data"]["patient_id"]
    res = client.put(f"/patients/{pid}", json={"last_name": "Davis", "address_line_2": "Apt 4"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["last_name"] == "Davis" and data["address_line_2"] == "Apt 4" and data["first_name"] == "Jane"
    assert data["updated_at"] >= data["created_at"]
    assert client.put(f"/patients/{pid}", json={"first_name": None}).status_code == 422
    assert client.put(f"/patients/{pid}", json={}).status_code == 400
    assert client.put(f"/patients/{uuid.uuid4()}", json={"city": "Dallas"}).status_code == 404


def test_soft_delete(client, valid_patient, db):
    from app.models import Patient

    pid = client.post("/patients", json=valid_patient).json()["data"]["patient_id"]
    res = client.delete(f"/patients/{pid}")
    assert res.status_code == 200 and res.json()["data"]["deleted_at"]
    assert client.get(f"/patients/{pid}").status_code == 404
    assert client.get("/patients").json()["data"] == []
    # Row still exists in the database (soft delete, not hard delete).
    row = db.get(Patient, uuid.UUID(pid))
    assert row is not None and row.deleted_at is not None


def test_not_found_and_bad_uuid(client):
    assert client.get(f"/patients/{uuid.uuid4()}").status_code == 404
    assert client.get("/patients/not-a-uuid").status_code == 422
    assert client.get("/nope").json()["error"]["code"] == "NOT_FOUND"


def test_optional_api_key(client, valid_patient, monkeypatch):
    from app.config import Settings

    monkeypatch.setattr("app.api.patients.get_settings", lambda: Settings(api_key="k"))
    assert client.post("/patients", json=valid_patient).status_code == 401
    assert client.post("/patients", json=valid_patient, headers={"X-API-Key": "k"}).status_code == 201
    assert client.get("/patients").status_code == 200  # reads stay open


def test_health(client):
    assert client.get("/health").json()["data"]["status"] == "ok"
