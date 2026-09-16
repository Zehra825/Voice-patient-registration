import os
import tempfile

# Configure the environment BEFORE the app is imported.
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_tmpdir}/test.db")
os.environ["SEED_DEMO_DATA"] = "false"
os.environ.pop("API_KEY", None)
os.environ.pop("VAPI_WEBHOOK_SECRET", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    from app import models  # noqa: F401

    database.Base.metadata.drop_all(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db(client):
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def valid_patient():
    return {
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "03/15/1988",
        "sex": "Female",
        "phone_number": "(512) 555-0199",
        "address_line_1": "742 Evergreen Terrace",
        "city": "Austin",
        "state": "TX",
        "zip_code": "78701",
    }
