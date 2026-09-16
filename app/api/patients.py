"""REST endpoints for patient records.

Routes stay thin: parse/validate input, call the service layer, wrap the
result in the {"data", "error"} envelope.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Header, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import validators as v
from app.api.responses import AppError, ok
from app.config import get_settings
from app.database import get_db
from app.models import Appointment, CallLog
from app.schemas import (
    PatientCreate,
    PatientUpdate,
    serialize_appointment,
    serialize_call,
    serialize_patient,
)
from app.services import patients as service

router = APIRouter(tags=["patients"])


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Write endpoints need X-API-Key only when API_KEY is configured."""
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise AppError(401, "UNAUTHORIZED", "Missing or invalid X-API-Key header.")


def _parse_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise AppError(422, "VALIDATION_ERROR", "patient_id must be a valid UUID.",
                       [{"field": "patient_id", "message": "Must be a valid UUID."}]) from None


def _load_patient(db: Session, raw_id: str):
    patient = service.get_patient(db, _parse_uuid(raw_id))
    if patient is None:
        raise AppError(404, "NOT_FOUND", f"Patient {raw_id} not found.")
    return patient


def _validation_details(exc: ValidationError) -> list[dict]:
    details = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc", ())) or "body"
        message = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
        if err.get("type") == "extra_forbidden":
            message = "Unknown field."
        details.append({"field": field, "message": message})
    return details


def _parse_body(model, body: object):
    if not isinstance(body, dict):
        raise AppError(400, "BAD_REQUEST", "Request body must be a JSON object.")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise AppError(422, "VALIDATION_ERROR", "One or more fields are invalid.", _validation_details(exc)) from None


def _query_filter(field: str, raw: str | None):
    if raw is None or raw.strip() == "":
        return None
    try:
        return v.normalize_field(field, raw)
    except ValueError as exc:
        raise AppError(422, "VALIDATION_ERROR", f"Invalid query parameter '{field}'.",
                       [{"field": field, "message": str(exc)}]) from None


@router.get("/patients", summary="List patients (optionally filtered)")
def list_patients(
    last_name: str | None = Query(default=None, description="Case-insensitive exact match"),
    date_of_birth: str | None = Query(default=None, description="MM/DD/YYYY or YYYY-MM-DD"),
    phone_number: str | None = Query(default=None, description="Any US format; normalized to 10 digits"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    patients = service.list_patients(
        db,
        last_name=_query_filter("last_name", last_name),
        date_of_birth=_query_filter("date_of_birth", date_of_birth),
        phone_number=_query_filter("phone_number", phone_number),
        limit=limit,
        offset=offset,
    )
    return ok([serialize_patient(p) for p in patients])


@router.get("/patients/{patient_id}", summary="Get one patient by UUID")
def get_patient(patient_id: str, db: Session = Depends(get_db)):
    return ok(serialize_patient(_load_patient(db, patient_id)))


@router.post("/patients", status_code=201, summary="Create a patient", dependencies=[Depends(require_api_key)])
def create_patient(body: dict = Body(..., examples=[{
    "first_name": "Jane", "last_name": "Doe", "date_of_birth": "03/15/1988", "sex": "Female",
    "phone_number": "(512) 555-0199", "address_line_1": "742 Evergreen Terrace",
    "city": "Austin", "state": "TX", "zip_code": "78701",
}]), db: Session = Depends(get_db)):
    data = _parse_body(PatientCreate, body)
    patient = service.create_patient(db, data)
    return ok(serialize_patient(patient), status_code=201)


@router.put("/patients/{patient_id}", summary="Partially update a patient", dependencies=[Depends(require_api_key)])
def update_patient(patient_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    patient = _load_patient(db, patient_id)
    data = _parse_body(PatientUpdate, body)
    if not data.model_fields_set:
        raise AppError(400, "BAD_REQUEST", "Provide at least one field to update.")
    return ok(serialize_patient(service.update_patient(db, patient, data)))


@router.delete("/patients/{patient_id}", summary="Soft-delete a patient", dependencies=[Depends(require_api_key)])
def delete_patient(patient_id: str, db: Session = Depends(get_db)):
    patient = service.soft_delete_patient(db, _load_patient(db, patient_id))
    return ok({"patient_id": str(patient.patient_id), "deleted_at": serialize_patient(patient)["deleted_at"]})


@router.get("/patients/{patient_id}/calls", summary="Call transcripts linked to a patient")
def patient_calls(patient_id: str, db: Session = Depends(get_db)):
    patient = _load_patient(db, patient_id)
    calls = db.scalars(select(CallLog).where(CallLog.patient_id == patient.patient_id).order_by(CallLog.created_at.desc()))
    return ok([serialize_call(c) for c in calls])


@router.get("/patients/{patient_id}/appointments", summary="Appointments booked for a patient")
def patient_appointments(patient_id: str, db: Session = Depends(get_db)):
    patient = _load_patient(db, patient_id)
    appts = db.scalars(select(Appointment).where(Appointment.patient_id == patient.patient_id).order_by(Appointment.scheduled_at))
    return ok([serialize_appointment(a) for a in appts])


calls_router = APIRouter(tags=["calls"])


@calls_router.get("/calls", summary="Recent calls, including incomplete registrations")
def list_calls(
    status: str | None = Query(default=None, pattern="^(in_progress|completed|incomplete)$"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(CallLog).order_by(CallLog.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(CallLog.status == status)
    return ok([serialize_call(c) for c in db.scalars(stmt)])
