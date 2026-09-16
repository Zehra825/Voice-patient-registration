"""Voice agent tool implementations.

Each handler receives the tool arguments plus call context, talks to the
service layer, and returns a small JSON-able dict for the LLM. Handlers never
raise: any unexpected failure becomes {"status": "error", ...} so the caller
hears a graceful apology instead of silence.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import validators as v
from app.models import Sex
from app.schemas import PatientCreate, PatientUpdate, format_dob, serialize_patient
from app.services import appointments as appt_service
from app.services import calls as call_service
from app.services import patients as patient_service

logger = logging.getLogger("voice.tools")


@dataclass
class CallContext:
    call_id: str
    caller_number: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return format_dob(value)
    if isinstance(value, Sex):
        return value.value
    return value


def _clean_fields(raw: Any) -> dict[str, Any]:
    """Drop empty values the LLM sometimes sends for fields the caller never gave."""
    if not isinstance(raw, dict):
        return {}
    return {k: val for k, val in raw.items() if val not in (None, "", "null", "N/A", "n/a")}


def _pydantic_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        field = loc[0] if loc else "patient"
        msg = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
        if err.get("type") == "missing":
            msg = f"{v.field_label(field)} is required."
        elif err.get("type") == "extra_forbidden":
            msg = "Unknown field; leave it out."
        errors[field] = msg
    return errors


def _with_db_retry(db: Session, fn: Callable[[], Any], attempts: int = 2) -> Any:
    """Retry once on transient connection errors (e.g. a DB failover)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OperationalError:
            db.rollback()
            if attempt == attempts:
                raise
            logger.warning("db operational error, retrying (attempt %s)", attempt)
            time.sleep(0.5)


def _existing_summary(p) -> dict[str, Any]:
    # Minimal disclosure: enough for "we already have a record for Jane Doe", nothing more.
    return {"patient_id": str(p.patient_id), "first_name": p.first_name, "last_name": p.last_name}


def _system_error(action: str) -> dict[str, Any]:
    return {
        "status": "error",
        "message": f"A system error occurred while trying to {action}. Nothing was changed.",
        "next_step": "Apologize, tell the caller we're having trouble with our system, offer to try once more; "
                     "if it fails again, tell them nothing was saved and suggest calling back later.",
    }


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def validate_fields(db: Session, ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    fields = _clean_fields(args.get("fields", args))
    valid: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    for name, raw in fields.items():
        if name not in v.FIELD_NORMALIZERS:
            continue  # ignore stray keys rather than confusing the caller
        try:
            valid[name] = _jsonable(v.normalize_field(name, raw))
        except ValueError as exc:
            invalid[name] = str(exc)

    result: dict[str, Any] = {"status": "ok" if not invalid else "invalid", "valid_fields": valid, "invalid_fields": invalid}
    if invalid:
        result["next_step"] = "Explain the problem briefly and re-ask only the invalid field(s)."

    if "phone_number" in valid:
        matches = patient_service.find_active_by_phone(db, valid["phone_number"])
        if matches:
            result["existing_patient"] = _existing_summary(matches[0])
            result["next_step"] = (
                "Tell the caller we already have a record for this first and last name with that phone number and ask if they'd "
                "like to update their information instead. If it's a different person, continue registering normally."
            )

    call = call_service.get_or_create_call(db, ctx.call_id, ctx.caller_number)
    call_service.merge_collected_data(db, call, valid)
    return result


def save_patient(db: Session, ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    patient_fields = _clean_fields(args.get("patient"))
    if not args.get("caller_confirmed"):
        return {
            "status": "not_saved",
            "reason": "confirmation_required",
            "next_step": "Read all details back to the caller, get a clear yes, then call save_patient with caller_confirmed true.",
        }
    try:
        data = PatientCreate.model_validate(patient_fields)
    except ValidationError as exc:
        return {
            "status": "invalid",
            "invalid_fields": _pydantic_errors(exc),
            "next_step": "Explain which detail is missing or invalid, re-ask only that, confirm it, then save again.",
        }

    payload = {k: _jsonable(val) for k, val in data.model_dump().items()}
    call = call_service.get_or_create_call(db, ctx.call_id, ctx.caller_number)
    call_service.merge_collected_data(db, call, {k: val for k, val in payload.items() if val is not None})

    try:
        # Idempotency: a second save in the same call (e.g. a late correction) updates the same record.
        if call.patient_id and (existing := patient_service.get_patient(db, call.patient_id)):
            update = PatientUpdate.model_validate({k: val for k, val in patient_fields.items() if k in v.FIELD_NORMALIZERS})
            patient = _with_db_retry(db, lambda: patient_service.update_patient(db, existing, update))
            outcome = "updated_same_call"
        else:
            dupes = patient_service.find_active_by_phone(db, data.phone_number)
            same_person = next(
                (p for p in dupes if p.last_name.lower() == data.last_name.lower() and p.date_of_birth == data.date_of_birth),
                None,
            )
            if (same_person or dupes) and not args.get("allow_duplicate"):
                match = same_person or dupes[0]
                return {
                    "status": "duplicate_found",
                    "existing_patient": _existing_summary(match),
                    "next_step": "Tell the caller we already have a record for that name with this phone number and ask whether they want to "
                                 "update it instead. If they're a different person, call save_patient again with allow_duplicate true.",
                }
            patient = _with_db_retry(db, lambda: patient_service.create_patient(db, data))
            outcome = "registered"

        call_service.link_patient(db, call, patient.patient_id, "registered" if outcome == "registered" else "updated")
    except SQLAlchemyError:
        db.rollback()
        # Dead-letter log: the confirmed payload is written to stdout so staff can recover it.
        logger.exception("patient.save_failed call_id=%s payload=%s", ctx.call_id, json.dumps(payload))
        try:
            call_service.mark_outcome(db, call, "save_failed")
        except SQLAlchemyError:
            db.rollback()
        return _system_error("save the registration")

    logger.info("patient.final_payload call_id=%s outcome=%s data=%s", ctx.call_id, outcome, json.dumps(serialize_patient(patient)))
    return {
        "status": "saved",
        "patient_id": str(patient.patient_id),
        "first_name": patient.first_name,
        "next_step": "Tell the caller they're all set using their first name, then offer to schedule a first appointment.",
    }


def verify_existing_patient(db: Session, ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    patient = _find_patient(db, args.get("patient_id"))
    if patient is None:
        return {"status": "not_found", "next_step": "Apologize that you couldn't find the record and offer to register them as new."}
    try:
        dob = v.normalize_date_of_birth(args.get("date_of_birth"))
    except ValueError as exc:
        return {"status": "invalid", "invalid_fields": {"date_of_birth": str(exc)}, "next_step": "Re-ask their date of birth."}
    if dob != patient.date_of_birth:
        return {
            "status": "not_verified",
            "next_step": "Say the date of birth doesn't match our records. Allow one more try; after that, offer to register as a new "
                         "patient or suggest calling the front desk. Do not reveal any details of the record.",
        }
    record = serialize_patient(patient)
    for private in ("created_at", "updated_at", "deleted_at"):
        record.pop(private, None)
    return {"status": "verified", "patient": record, "next_step": "Ask what they'd like to update."}


def update_patient(db: Session, ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("caller_confirmed"):
        return {"status": "not_saved", "reason": "confirmation_required",
                "next_step": "Read the changes back, get a clear yes, then call update_patient with caller_confirmed true."}
    patient = _find_patient(db, args.get("patient_id"))
    if patient is None:
        return {"status": "not_found", "next_step": "Apologize that you couldn't find the record and offer to register them as new."}
    changes = {k: val for k, val in _clean_fields(args.get("changes")).items() if k in v.FIELD_NORMALIZERS}
    if not changes:
        return {"status": "invalid", "invalid_fields": {"changes": "No changes were provided."}, "next_step": "Ask what they want to change."}
    try:
        data = PatientUpdate.model_validate(changes)
    except ValidationError as exc:
        return {"status": "invalid", "invalid_fields": _pydantic_errors(exc), "next_step": "Re-ask only the invalid field(s)."}
    try:
        patient = _with_db_retry(db, lambda: patient_service.update_patient(db, patient, data))
        call = call_service.get_or_create_call(db, ctx.call_id, ctx.caller_number)
        call_service.link_patient(db, call, patient.patient_id, "updated")
    except SQLAlchemyError:
        db.rollback()
        logger.exception("patient.update_failed call_id=%s patient_id=%s changes=%s", ctx.call_id, args.get("patient_id"),
                         json.dumps({k: _jsonable(val) for k, val in data.changes().items()}))
        return _system_error("update the record")
    logger.info("patient.final_payload call_id=%s outcome=updated data=%s", ctx.call_id, json.dumps(serialize_patient(patient)))
    return {"status": "updated", "patient_id": str(patient.patient_id), "first_name": patient.first_name,
            "updated_fields": sorted(data.changes()), "next_step": "Confirm the update, then ask if there's anything else."}


def get_available_appointments(db: Session, ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    preferred: date | None = None
    if args.get("preferred_date"):
        try:
            from datetime import datetime
            preferred = datetime.strptime(str(args["preferred_date"]).strip(), "%m/%d/%Y").date()
        except ValueError:
            preferred = None
    slots = appt_service.available_slots(db, preferred, args.get("time_of_day"))
    if not slots:
        return {"status": "none_available", "next_step": "Apologize and say the front desk will call to schedule."}
    return {
        "status": "ok",
        "slots": [{"slot_id": s.slot_id, "description": s.spoken()} for s in slots],
        "next_step": "Offer these options in one natural sentence and ask which works best.",
    }


def book_appointment(db: Session, ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    patient = _find_patient(db, args.get("patient_id"))
    if patient is None:
        return {"status": "not_found", "next_step": "The patient must be registered before booking. Save the registration first."}
    try:
        appt, slot = appt_service.book(db, patient.patient_id, str(args.get("slot_id", "")), args.get("reason"))
    except appt_service.SlotUnavailable as exc:
        return {"status": "unavailable", "message": str(exc),
                "next_step": "Apologize, call get_available_appointments again and offer new options."}
    except SQLAlchemyError:
        db.rollback()
        logger.exception("appointment.book_failed call_id=%s", ctx.call_id)
        return _system_error("book the appointment")
    logger.info("appointment.booked call_id=%s patient_id=%s slot=%s", ctx.call_id, patient.patient_id, slot.slot_id)
    return {"status": "booked", "appointment_id": str(appt.id), "description": slot.spoken(),
            "next_step": "Confirm the day, time and doctor, and ask them to arrive 15 minutes early with their ID and insurance card."}


def _find_patient(db: Session, raw_id: Any):
    try:
        return patient_service.get_patient(db, uuid.UUID(str(raw_id)))
    except (ValueError, TypeError):
        return None


TOOL_HANDLERS: dict[str, Callable[[Session, CallContext, dict[str, Any]], dict[str, Any]]] = {
    "validate_fields": validate_fields,
    "save_patient": save_patient,
    "verify_existing_patient": verify_existing_patient,
    "update_patient": update_patient,
    "get_available_appointments": get_available_appointments,
    "book_appointment": book_appointment,
}


def dispatch(db: Session, ctx: CallContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"status": "error", "message": f"Unknown tool {name}"}
    started = time.perf_counter()
    try:
        result = handler(db, ctx, args)
    except Exception:  # last-resort guard: never leave the caller in silence
        db.rollback()
        logger.exception("tool.crashed name=%s call_id=%s", name, ctx.call_id)
        result = _system_error("process that")
    logger.info("tool.call name=%s call_id=%s status=%s ms=%d", name, ctx.call_id, result.get("status"),
                (time.perf_counter() - started) * 1000)
    return result
