"""SQLAlchemy ORM models.

The schema enforces the data model at the database level too (types, NOT NULL,
lengths, enum CHECK, format CHECKs on PostgreSQL) so bad data can't sneak in
even if a future code path skips the Pydantic layer.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sex(str, enum.Enum):
    MALE = "Male"
    FEMALE = "Female"
    OTHER = "Other"
    DECLINE = "Decline to Answer"


def _pg_check(sql: str, name: str) -> CheckConstraint:
    """Regex CHECKs only exist on PostgreSQL; SQLite relies on the length checks + app validation."""
    return CheckConstraint(sql, name=name).ddl_if(dialect="postgresql")


class Patient(Base):
    __tablename__ = "patients"

    patient_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    first_name: Mapped[str] = mapped_column(String(50), nullable=False)
    last_name: Mapped[str] = mapped_column(String(50), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[Sex] = mapped_column(
        SAEnum(
            Sex,
            name="sex",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
        ),
        nullable=False,
    )
    phone_number: Mapped[str] = mapped_column(String(10), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))

    address_line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    address_line_2: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    zip_code: Mapped[str] = mapped_column(String(10), nullable=False)

    insurance_provider: Mapped[str | None] = mapped_column(String(100))
    insurance_member_id: Mapped[str | None] = mapped_column(String(30))
    preferred_language: Mapped[str] = mapped_column(String(50), nullable=False, default="English", server_default="English")
    emergency_contact_name: Mapped[str | None] = mapped_column(String(100))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(10))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    calls: Mapped[list[CallLog]] = relationship(back_populates="patient", order_by="CallLog.created_at.desc()")
    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient", order_by="Appointment.scheduled_at")

    __table_args__ = (
        CheckConstraint("length(first_name) BETWEEN 1 AND 50", name="first_name_length"),
        CheckConstraint("length(last_name) BETWEEN 1 AND 50", name="last_name_length"),
        CheckConstraint("length(phone_number) = 10", name="phone_number_length"),
        CheckConstraint("length(city) BETWEEN 1 AND 100", name="city_length"),
        CheckConstraint("length(state) = 2", name="state_length"),
        CheckConstraint("length(zip_code) IN (5, 10)", name="zip_code_length"),
        CheckConstraint("date_of_birth >= '1900-01-01'", name="date_of_birth_min"),
        CheckConstraint(
            "emergency_contact_phone IS NULL OR length(emergency_contact_phone) = 10",
            name="emergency_contact_phone_length",
        ),
        _pg_check("phone_number ~ '^[2-9][0-9]{9}$'", "phone_number_format"),
        _pg_check("emergency_contact_phone IS NULL OR emergency_contact_phone ~ '^[2-9][0-9]{9}$'", "emergency_contact_phone_format"),
        _pg_check("state ~ '^[A-Z]{2}$'", "state_format"),
        _pg_check("zip_code ~ '^[0-9]{5}(-[0-9]{4})?$'", "zip_code_format"),
        _pg_check("insurance_member_id IS NULL OR insurance_member_id ~ '^[A-Z0-9]+$'", "insurance_member_id_format"),
        # The three documented search filters, plus a lookup index for duplicate detection.
        Index("ix_patients_last_name_lower", text("lower(last_name)")),
        Index("ix_patients_date_of_birth", "date_of_birth"),
        Index("ix_patients_phone_number", "phone_number"),
    )


class CallLog(Base):
    """One row per phone call: transcript, summary and any partially collected data.

    Partial data is captured as the agent validates fields, so a dropped call
    still leaves a trace that staff can follow up on.
    """

    __tablename__ = "call_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    vapi_call_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("patients.patient_id"))
    caller_number: Mapped[str | None] = mapped_column(String(20))

    # in_progress -> completed (patient saved/updated) | incomplete (ended without saving)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    outcome: Mapped[str | None] = mapped_column(String(30))  # registered | updated | save_failed | abandoned
    collected_data: Mapped[dict | None] = mapped_column(JSON)
    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    ended_reason: Mapped[str | None] = mapped_column(String(100))
    recording_url: Mapped[str | None] = mapped_column(String(500))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    patient: Mapped[Patient | None] = relationship(back_populates="calls")

    __table_args__ = (
        CheckConstraint("status IN ('in_progress', 'completed', 'incomplete')", name="status_values"),
    )


class Appointment(Base):
    """Bonus: first appointment booked at the end of the call (mock provider schedule)."""

    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.patient_id"), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)  # one booking per slot
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="booked")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    patient: Mapped[Patient] = relationship(back_populates="appointments")
