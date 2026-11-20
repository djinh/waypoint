"""Errors the API answers with (docs/API.md, Errors)."""

from __future__ import annotations

STATUS = {
    "invalid": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "loop": 409,
    "version_conflict": 409,
    "content": 422,
    "unavailable": 503,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None, cycle: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.cycle = cycle

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.field is not None:
            err["field"] = self.field
        if self.cycle is not None:
            err["cycle"] = self.cycle
        return {"error": err}
