"""Request schemas (Pydantic v2) and response serialization.

Validation rules live in app.validators; these schemas only wire them up, so
the REST API and the voice agent can never disagree about what's valid.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app import validators as v
from app.models import Appointment, CallLog, Patient, Sex


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


class _PatientFields(BaseModel):
    # extra="forbid": unknown keys (or attempts to set patient_id/created_at) are rejected with 422.
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _normalize(cls, value: Any, info) -> Any:
        value = _blank_to_none(value)
        if value is None:
            return None  # requiredness is enforced by the field types / model validator
        return v.normalize_field(info.field_name, value)


class PatientCreate(_PatientFields):
    first_name: str
    last_name: str
    date_of_birth: date
    sex: Sex
    phone_number: str
    email: str | None = None
    address_line_1: str
    address_line_2: str | None = None
    city: str
    state: str
    zip_code: str
    insurance_provider: str | None = None
    insurance_member_id: str | None = None
    preferred_language: str | None = "English"
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None

    @model_validator(mode="after")
    def _defaults(self) -> PatientCreate:
        if not self.preferred_language:
            self.preferred_language = "English"
        return self


class PatientUpdate(_PatientFields):
    """Partial update: only fields present in the body are changed."""

    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    sex: Sex | None = None
    phone_number: str | None = None
    email: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    insurance_provider: str | None = None
    insurance_member_id: str | None = None
    preferred_language: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None

    @model_validator(mode="after")
    def _required_fields_cannot_be_cleared(self) -> PatientUpdate:
        cleared = [f for f in v.REQUIRED_FIELDS if f in self.model_fields_set and getattr(self, f) is None]
        if cleared:
            raise ValueError(f"Required field(s) cannot be empty: {', '.join(cleared)}.")
        return self

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(include=self.model_fields_set)
        if "preferred_language" in data and data["preferred_language"] is None:
            data["preferred_language"] = "English"
        return data


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------

def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite drops tzinfo; values are always stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def format_dob(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def serialize_patient(p: Patient) -> dict[str, Any]:
    return {
        "patient_id": str(p.patient_id),
        "first_name": p.first_name,
        "last_name": p.last_name,
        "date_of_birth": format_dob(p.date_of_birth),
        "sex": p.sex.value if isinstance(p.sex, Sex) else p.sex,
        "phone_number": p.phone_number,
        "email": p.email,
        "address_line_1": p.address_line_1,
        "address_line_2": p.address_line_2,
        "city": p.city,
        "state": p.state,
        "zip_code": p.zip_code,
        "insurance_provider": p.insurance_provider,
        "insurance_member_id": p.insurance_member_id,
        "preferred_language": p.preferred_language,
        "emergency_contact_name": p.emergency_contact_name,
        "emergency_contact_phone": p.emergency_contact_phone,
        "created_at": iso_utc(p.created_at),
        "updated_at": iso_utc(p.updated_at),
        "deleted_at": iso_utc(p.deleted_at),
    }


def serialize_call(c: CallLog, include_transcript: bool = True) -> dict[str, Any]:
    data = {
        "call_id": str(c.id),
        "vapi_call_id": c.vapi_call_id,
        "patient_id": str(c.patient_id) if c.patient_id else None,
        "caller_number": c.caller_number,
        "status": c.status,
        "outcome": c.outcome,
        "collected_data": c.collected_data,
        "summary": c.summary,
        "ended_reason": c.ended_reason,
        "recording_url": c.recording_url,
        "started_at": iso_utc(c.started_at),
        "ended_at": iso_utc(c.ended_at),
        "created_at": iso_utc(c.created_at),
    }
    if include_transcript:
        data["transcript"] = c.transcript
    return data


def serialize_appointment(a: Appointment) -> dict[str, Any]:
    return {
        "appointment_id": str(a.id),
        "patient_id": str(a.patient_id),
        "slot_id": a.slot_id,
        "scheduled_at": iso_utc(a.scheduled_at),
        "provider_name": a.provider_name,
        "reason": a.reason,
        "status": a.status,
        "created_at": iso_utc(a.created_at),
    }
