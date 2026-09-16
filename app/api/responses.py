"""Consistent JSON envelope: every response is {"data": ..., "error": ...}."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class AppError(Exception):
    """Raise from routes/services to return a clean enveloped error."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def ok(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"data": data, "error": None})


def fail(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"data": None, "error": error})
