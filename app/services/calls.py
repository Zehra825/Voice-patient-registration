"""Call log persistence: partial data, outcomes, transcripts."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CallLog, utcnow

logger = logging.getLogger(__name__)


def get_or_create_call(db: Session, vapi_call_id: str, caller_number: str | None = None) -> CallLog:
    call = db.scalar(select(CallLog).where(CallLog.vapi_call_id == vapi_call_id))
    if call:
        if caller_number and not call.caller_number:
            call.caller_number = caller_number
        return call
    call = CallLog(vapi_call_id=vapi_call_id, caller_number=caller_number, started_at=utcnow())
    db.add(call)
    try:
        db.commit()
    except IntegrityError:  # concurrent webhook for the same call created it first
        db.rollback()
        call = db.scalar(select(CallLog).where(CallLog.vapi_call_id == vapi_call_id))
    return call


def merge_collected_data(db: Session, call: CallLog, fields: dict[str, Any]) -> None:
    """Keep the latest validated values so a dropped call still leaves partial data."""
    if not fields:
        return
    merged = dict(call.collected_data or {})
    merged.update(fields)
    call.collected_data = merged  # reassign so SQLAlchemy detects the JSON change
    db.commit()


def link_patient(db: Session, call: CallLog, patient_id: uuid.UUID, outcome: str) -> None:
    call.patient_id = patient_id
    call.outcome = outcome
    call.status = "completed"
    db.commit()


def mark_outcome(db: Session, call: CallLog, outcome: str) -> None:
    call.outcome = outcome
    db.commit()


def _parse_ts(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_end_of_call(db: Session, call: CallLog, message: dict[str, Any]) -> CallLog:
    artifact = message.get("artifact") or {}
    analysis = message.get("analysis") or {}
    recording = artifact.get("recording") or {}

    call.transcript = artifact.get("transcript") or message.get("transcript") or call.transcript
    call.summary = analysis.get("summary") or message.get("summary") or call.summary
    call.ended_reason = (message.get("endedReason") or "")[:100] or None
    call.recording_url = (
        artifact.get("recordingUrl")
        or message.get("recordingUrl")
        or (recording.get("mono") or {}).get("combinedUrl")
        or call.recording_url
    )
    call.started_at = _parse_ts(message.get("startedAt")) or call.started_at
    call.ended_at = _parse_ts(message.get("endedAt")) or utcnow()

    if call.patient_id:
        call.status = "completed"
    else:
        call.status = "incomplete"
        call.outcome = call.outcome or "abandoned"
    db.commit()
    return call
