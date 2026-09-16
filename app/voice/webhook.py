"""Vapi server webhook: the only boundary between telephony and our backend.

Vapi (telephony + STT + LLM orchestration + TTS) POSTs events here:
  * tool-calls          -> run tools against the service layer, return results
  * end-of-call-report  -> store transcript/summary, mark incomplete calls
  * status-update       -> log call lifecycle
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.responses import AppError
from app.config import get_settings
from app.database import get_db
from app.services import calls as call_service
from app.voice.handlers import CallContext, dispatch

logger = logging.getLogger("voice.webhook")
router = APIRouter(tags=["voice"])


def verify_vapi_secret(
    x_vapi_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    expected = get_settings().vapi_webhook_secret
    if not expected:
        return  # secret not configured (local dev)
    provided = x_vapi_secret or (authorization or "").removeprefix("Bearer ").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise AppError(401, "UNAUTHORIZED", "Invalid webhook secret.")


def extract_tool_calls(message: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Normalize the tool call shapes Vapi has used over time into (id, name, args)."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    raw_list = message.get("toolCallList") or []
    if not raw_list:
        raw_list = [item.get("toolCall", {}) | {"name": item.get("name")} for item in message.get("toolWithToolCallList") or []]
    for item in raw_list:
        fn = item.get("function") or {}
        name = item.get("name") or fn.get("name") or ""
        args = item.get("arguments", item.get("parameters", fn.get("arguments", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        calls.append((str(item.get("id", "")), name, args if isinstance(args, dict) else {}))
    return calls


def _call_context(message: dict[str, Any]) -> CallContext:
    call = message.get("call") or {}
    customer = message.get("customer") or call.get("customer") or {}
    return CallContext(call_id=str(call.get("id") or "unknown-call"), caller_number=customer.get("number"))


@router.post("/vapi/webhook", dependencies=[Depends(verify_vapi_secret)], include_in_schema=True, summary="Vapi server events")
async def vapi_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise AppError(400, "BAD_REQUEST", "Body must be JSON.") from None
    message = (body or {}).get("message") or {}
    kind = message.get("type")
    ctx = _call_context(message)

    if kind == "tool-calls":
        results = []
        for tool_call_id, name, args in extract_tool_calls(message):
            logger.info("tool.request call_id=%s name=%s args=%s", ctx.call_id, name, json.dumps(args))
            result = dispatch(db, ctx, name, args)
            results.append({"toolCallId": tool_call_id, "name": name, "result": json.dumps(result)})
        return JSONResponse({"results": results})

    if kind == "end-of-call-report":
        call = call_service.get_or_create_call(db, ctx.call_id, ctx.caller_number)
        call = call_service.record_end_of_call(db, call, message)
        # Observability: one structured line per call with the outcome and whatever data we captured.
        logger.info(
            "call.ended call_id=%s status=%s outcome=%s ended_reason=%s patient_id=%s collected=%s",
            ctx.call_id, call.status, call.outcome, call.ended_reason, call.patient_id, json.dumps(call.collected_data or {}),
        )
        return JSONResponse({"ok": True})

    if kind == "status-update":
        logger.info("call.status call_id=%s status=%s", ctx.call_id, message.get("status"))
        if message.get("status") == "in-progress":
            call_service.get_or_create_call(db, ctx.call_id, ctx.caller_number)
        return JSONResponse({"ok": True})

    logger.debug("vapi.event ignored type=%s", kind)
    return JSONResponse({"ok": True})
