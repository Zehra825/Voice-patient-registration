"""Patient service layer.

Both the REST API and the voice agent call these functions, so there is
exactly one code path that writes patient records.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Patient, utcnow
from app.schemas import PatientCreate, PatientUpdate

logger = logging.getLogger(__name__)


def create_patient(db: Session, data: PatientCreate) -> Patient:
    patient = Patient(**data.model_dump())
    db.add(patient)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(patient)
    logger.info("patient.created patient_id=%s", patient.patient_id)
    return patient


def get_patient(db: Session, patient_id: uuid.UUID, include_deleted: bool = False) -> Patient | None:
    patient = db.get(Patient, patient_id)
    if patient is None or (patient.deleted_at is not None and not include_deleted):
        return None
    return patient


def list_patients(
    db: Session,
    *,
    last_name: str | None = None,
    date_of_birth: date | None = None,
    phone_number: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Patient]:
    stmt = select(Patient).where(Patient.deleted_at.is_(None))
    if last_name:
        stmt = stmt.where(func.lower(Patient.last_name) == last_name.lower())
    if date_of_birth:
        stmt = stmt.where(Patient.date_of_birth == date_of_birth)
    if phone_number:
        stmt = stmt.where(Patient.phone_number == phone_number)
    stmt = stmt.order_by(Patient.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


def find_active_by_phone(db: Session, phone_number: str) -> list[Patient]:
    return list_patients(db, phone_number=phone_number, limit=5)


def update_patient(db: Session, patient: Patient, data: PatientUpdate) -> Patient:
    changes = data.changes()
    for field, value in changes.items():
        setattr(patient, field, value)
    patient.updated_at = utcnow()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(patient)
    logger.info("patient.updated patient_id=%s fields=%s", patient.patient_id, sorted(changes))
    return patient


def soft_delete_patient(db: Session, patient: Patient) -> Patient:
    patient.deleted_at = utcnow()
    patient.updated_at = patient.deleted_at
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(patient)
    logger.info("patient.soft_deleted patient_id=%s", patient.patient_id)
    return patient
