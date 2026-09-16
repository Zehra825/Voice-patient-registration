"""Bonus: mock appointment scheduling.

Slots are generated deterministically (next 10 business days, four times a day,
three mock providers) so no schedule table is needed; booked slots are the rows
in `appointments`, and a UNIQUE(slot_id) constraint prevents double booking.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Appointment

PROVIDERS = (
    ("patel", "Dr. Priya Patel, Family Medicine"),
    ("okafor", "Dr. James Okafor, Internal Medicine"),
    ("chen", "Dr. Emily Chen, Family Medicine"),
)
DAILY_TIMES = (time(9, 0), time(10, 30), time(13, 30), time(15, 0))


@dataclass(frozen=True)
class Slot:
    slot_id: str
    starts_at: datetime  # timezone-aware, clinic local time
    provider_key: str
    provider_name: str

    def spoken(self) -> str:
        hour = self.starts_at.strftime("%I:%M %p").lstrip("0").replace(":00", "")
        return f"{self.starts_at.strftime('%A, %B')} {self.starts_at.day} at {hour} with {self.provider_name.split(',')[0]}"


def _clinic_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().clinic_timezone)


def generate_slots(start: date | None = None, business_days: int = 10) -> list[Slot]:
    tz = _clinic_tz()
    day = (start or datetime.now(tz).date()) + timedelta(days=1)
    slots: list[Slot] = []
    counted = 0
    while counted < business_days:
        if day.weekday() < 5:
            for i, t in enumerate(DAILY_TIMES):
                key, name = PROVIDERS[(day.toordinal() + i) % len(PROVIDERS)]
                starts = datetime.combine(day, t, tzinfo=tz)
                slots.append(Slot(f"{day.isoformat()}T{t.strftime('%H%M')}-{key}", starts, key, name))
            counted += 1
        day += timedelta(days=1)
    return slots


def available_slots(db: Session, preferred_date: date | None = None, time_of_day: str | None = None, limit: int = 3) -> list[Slot]:
    booked = set(db.scalars(select(Appointment.slot_id).where(Appointment.status == "booked")))
    slots = [s for s in generate_slots() if s.slot_id not in booked]
    if preferred_date:
        on_day = [s for s in slots if s.starts_at.date() == preferred_date]
        # If that day is full or not a business day, fall back to the nearest days after it.
        slots = on_day or [s for s in slots if s.starts_at.date() >= preferred_date] or slots
    if time_of_day in {"morning", "afternoon"}:
        filtered = [s for s in slots if (s.starts_at.hour < 12) == (time_of_day == "morning")]
        slots = filtered or slots
    return slots[:limit]


def find_slot(slot_id: str) -> Slot | None:
    return next((s for s in generate_slots() if s.slot_id == slot_id), None)


class SlotUnavailable(Exception):
    pass


def book(db: Session, patient_id: uuid.UUID, slot_id: str, reason: str | None = None) -> tuple[Appointment, Slot]:
    slot = find_slot(slot_id)
    if slot is None:
        raise SlotUnavailable("That time is no longer on the schedule.")
    appt = Appointment(
        patient_id=patient_id,
        slot_id=slot.slot_id,
        scheduled_at=slot.starts_at.astimezone(timezone.utc),
        provider_name=slot.provider_name,
        reason=(reason or None) and reason[:200],
    )
    db.add(appt)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SlotUnavailable("That time was just taken.") from None
    db.refresh(appt)
    return appt, slot
