"""API errors and JSON responses."""

from __future__ import annotations

from starlette.responses import JSONResponse

from sci import publication as pub


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.headers = headers or {}

    def response(self) -> JSONResponse:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.field:
            body["error"]["field"] = self.field
        return json_response(body, self.status, self.headers)



_ERROR_STATUS = {
    "invalid_signature": 403,
    "too_large": 413,
    "unsupported_schema": 400,
}



def from_publication_error(exc: pub.PublicationError) -> ApiError:
    return ApiError(_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc), exc.field)



class FinalizationFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason



class OperatorAttention(Exception):
    """A conflict that must never be resolved automatically."""



def finalization_failed_error(reason: str) -> ApiError:
    return ApiError(
        409,
        "finalization_failed",
        f"Finalization of this publication failed permanently ({reason}). This UUID cannot be reused."
        " Sign the publication again ('sci sign') to create a new UUID, then push it.",
    )



def json_response(body, status: int = 200, headers: dict | None = None) -> JSONResponse:
    h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    h.update(headers or {})
    return JSONResponse(body, status_code=status, headers=h)

