"""Demo seed data (fictional people, 555-01xx numbers reserved for fiction)."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Patient
from app.schemas import PatientCreate
from app.services.patients import create_patient

logger = logging.getLogger(__name__)

SEED_PATIENTS = [
    {
        "first_name": "Jane", "last_name": "Doe", "date_of_birth": "03/15/1988", "sex": "Female",
        "phone_number": "5125550147", "email": "jane.doe@example.com",
        "address_line_1": "742 Evergreen Terrace", "address_line_2": "Apt 4B", "city": "Austin", "state": "TX", "zip_code": "78701",
        "insurance_provider": "Blue Cross Blue Shield", "insurance_member_id": "XYZ123456789", "preferred_language": "English",
        "emergency_contact_name": "John Doe", "emergency_contact_phone": "5125550148",
    },
    {
        "first_name": "Carlos", "last_name": "Rivera", "date_of_birth": "11/02/1975", "sex": "Male",
        "phone_number": "3055550182",
        "address_line_1": "1200 Brickell Avenue", "city": "Miami", "state": "FL", "zip_code": "33131-2345",
        "preferred_language": "Spanish",
    },
]


def seed_if_empty(db: Session) -> int:
    if db.scalar(select(func.count()).select_from(Patient)):
        return 0
    for record in SEED_PATIENTS:
        create_patient(db, PatientCreate.model_validate(record))
    logger.info("seeded %s demo patients", len(SEED_PATIENTS))
    return len(SEED_PATIENTS)
