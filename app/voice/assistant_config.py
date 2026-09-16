"""Vapi assistant definition (model, voice, transcriber, tools) as code.

Keeping the assistant in the repo — instead of only clicking it together in the
Vapi dashboard — makes it reviewable, diffable and reproducible:
`python -m scripts.setup_vapi` (or POST /admin/vapi/sync) pushes this config.
"""

from __future__ import annotations

import os
from typing import Any

from app.config import Settings
from app.voice import prompts

ASSISTANT_NAME = "Patient Registration Agent"

# --- JSON schema shared by the patient tools --------------------------------
PATIENT_FIELD_SCHEMA: dict[str, Any] = {
    "first_name": {"type": "string", "description": "Patient first name"},
    "last_name": {"type": "string", "description": "Patient last name"},
    "date_of_birth": {"type": "string", "description": "MM/DD/YYYY"},
    "sex": {"type": "string", "enum": ["Male", "Female", "Other", "Decline to Answer"]},
    "phone_number": {"type": "string", "description": "10 digits, no country code or punctuation"},
    "email": {"type": "string", "description": "Email address, e.g. jane.doe@gmail.com"},
    "address_line_1": {"type": "string", "description": "Street address"},
    "address_line_2": {"type": "string", "description": "Apartment, suite or unit"},
    "city": {"type": "string"},
    "state": {"type": "string", "description": "Two-letter US state abbreviation"},
    "zip_code": {"type": "string", "description": "5-digit ZIP or ZIP+4 (12345-6789)"},
    "insurance_provider": {"type": "string", "description": "Insurance company name"},
    "insurance_member_id": {"type": "string", "description": "Member/subscriber ID, letters and digits"},
    "preferred_language": {"type": "string", "description": "e.g. English, Spanish"},
    "emergency_contact_name": {"type": "string", "description": "Emergency contact full name"},
    "emergency_contact_phone": {"type": "string", "description": "10 digits"},
}

REQUIRED_FOR_SAVE = [
    "first_name", "last_name", "date_of_birth", "sex", "phone_number",
    "address_line_1", "city", "state", "zip_code",
]


def _function_tool(name: str, description: str, properties: dict, required: list[str], server: dict,
                   messages: list[dict] | None = None) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
        "server": server,
    }
    if messages:
        tool["messages"] = messages
    return tool


# Spoken only if our server is slow or unreachable, so the caller never hears dead air.
_DELAY_MESSAGES = [
    {"type": "request-response-delayed", "content": "Thanks for bearing with me, this is taking a moment.", "timingMilliseconds": 4000},
    {"type": "request-failed", "content": "I'm sorry, I'm having trouble reaching our system right now."},
]


def build_tools(server: dict) -> list[dict]:
    return [
        _function_tool(
            "validate_fields",
            "Validate and normalize patient details as you collect them. Pass any subset of fields gathered since the last check. "
            "Returns valid_fields (normalized), invalid_fields (with reasons to re-ask), and existing_patient when the phone number matches a record.",
            {"fields": {"type": "object", "properties": PATIENT_FIELD_SCHEMA, "description": "Fields collected so far"}},
            ["fields"],
            server,
        ),
        _function_tool(
            "save_patient",
            "Create the patient record. Only call after reading back all information and the caller explicitly confirmed it is correct.",
            {
                "patient": {"type": "object", "properties": PATIENT_FIELD_SCHEMA, "required": REQUIRED_FOR_SAVE},
                "caller_confirmed": {"type": "boolean", "description": "True only if the caller confirmed the read-back"},
                "allow_duplicate": {"type": "boolean", "description": "True only if the caller said they are a different person from the existing record with this phone number"},
            },
            ["patient", "caller_confirmed"],
            server,
            _DELAY_MESSAGES,
        ),
        _function_tool(
            "verify_existing_patient",
            "Verify a returning caller's identity with their date of birth before discussing or updating their existing record.",
            {
                "patient_id": {"type": "string", "description": "patient_id from existing_patient"},
                "date_of_birth": {"type": "string", "description": "MM/DD/YYYY as stated by the caller"},
            },
            ["patient_id", "date_of_birth"],
            server,
        ),
        _function_tool(
            "update_patient",
            "Update an existing, verified patient record with only the changed fields, after the caller confirmed the changes.",
            {
                "patient_id": {"type": "string"},
                "changes": {"type": "object", "properties": PATIENT_FIELD_SCHEMA},
                "caller_confirmed": {"type": "boolean"},
            },
            ["patient_id", "changes", "caller_confirmed"],
            server,
            _DELAY_MESSAGES,
        ),
        _function_tool(
            "get_available_appointments",
            "Get up to three open first-visit appointment slots.",
            {
                "preferred_date": {"type": "string", "description": "Optional MM/DD/YYYY"},
                "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
            },
            [],
            server,
        ),
        _function_tool(
            "book_appointment",
            "Book one of the slots returned by get_available_appointments for a registered patient.",
            {
                "patient_id": {"type": "string"},
                "slot_id": {"type": "string"},
                "reason": {"type": "string", "description": "Optional short visit reason, e.g. 'new patient checkup'"},
            },
            ["patient_id", "slot_id"],
            server,
            _DELAY_MESSAGES,
        ),
        {"type": "endCall"},
    ]


def build_assistant_payload(settings: Settings, model: str | None = None, voice: dict | None = None) -> dict:
    if not settings.public_base_url:
        raise ValueError("PUBLIC_BASE_URL (or RAILWAY_PUBLIC_DOMAIN) must be set so Vapi can reach the webhook.")

    server: dict[str, Any] = {"url": f"{settings.public_base_url}/vapi/webhook", "timeoutSeconds": 20}
    if settings.vapi_webhook_secret:
        server["headers"] = {"X-Vapi-Secret": settings.vapi_webhook_secret}

    return {
        "name": ASSISTANT_NAME,
        "firstMessage": prompts.first_message(settings.clinic_name),
        "firstMessageMode": "assistant-speaks-first",
        "model": {
            "provider": "openai",
            "model": model or os.getenv("VAPI_MODEL", "gpt-4.1"),
            "temperature": 0.3,  # low: consistent formats and tool usage, still natural phrasing
            "messages": [{"role": "system", "content": prompts.system_prompt(settings.clinic_name, settings.clinic_timezone)}],
            "tools": build_tools(server),
        },
        "voice": voice or default_voice(),
        # nova-3 "multi" handles English/Spanish code-switching for the multilingual bonus.
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "multi"},
        # Give callers time to finish digit strings and addresses before the agent jumps in.
        "startSpeakingPlan": {
            "waitSeconds": 0.6,
            "transcriptionEndpointingPlan": {
                "onPunctuationSeconds": 0.3,
                "onNoPunctuationSeconds": 1.5,
                "onNumberSeconds": 1.2,
            },
        },
        # numWords=2: a quick "mm-hmm" doesn't stop the agent mid-sentence, a real interruption does.
        "stopSpeakingPlan": {"numWords": 2, "voiceSeconds": 0.3, "backoffSeconds": 1},
        "endCallMessage": prompts.END_CALL_MESSAGE,
        "endCallPhrases": ["have a great day", "que tenga un buen día"],
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 900,
        "backgroundSpeechDenoisingPlan": {"smartDenoisingPlan": {"enabled": True}},
        "server": server,
        "serverMessages": ["tool-calls", "end-of-call-report", "status-update"],
        "analysisPlan": {"summaryPlan": {"messages": [{"role": "system", "content": prompts.SUMMARY_PROMPT}]}},
        "metadata": {"app": "voice-patient-registration"},
    }


def default_voice() -> dict:
    provider = os.getenv("VAPI_VOICE_PROVIDER")
    voice_id = os.getenv("VAPI_VOICE_ID")
    if provider and voice_id:
        return {"provider": provider, "voiceId": voice_id}
    # Azure multilingual neural voices speak natural English AND Spanish with the same voice.
    return {"provider": "azure", "voiceId": "en-US-AvaMultilingualNeural"}


FALLBACK_VOICE = {"provider": "vapi", "voiceId": "Elliot"}
# Nice-to-have tuning; dropped automatically if the Vapi account/API version rejects them.
OPTIONAL_KEYS = ("startSpeakingPlan", "stopSpeakingPlan", "backgroundSpeechDenoisingPlan", "analysisPlan", "endCallPhrases", "metadata")
FALLBACK_MODELS = ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]
