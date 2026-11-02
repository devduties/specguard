"""Stable client-facing error responses, independent of an HTTP framework."""

from dataclasses import dataclass
from typing import Any

from specguard.routing import InvalidRequestTargetError, MethodNotAllowedError, RouteNotFoundError

_DETAIL_MESSAGES = {
    "invalid_value": "Value does not satisfy the contract.",
    "required": "Required value is missing.",
    "type": "Value has an invalid type.",
    "enum": "Value is not one of the allowed choices.",
    "const": "Value does not match the required constant.",
    "format": "Value does not match the required format.",
    "pattern": "Value does not match the required pattern.",
    "minimum": "Value is below the allowed minimum.",
    "maximum": "Value exceeds the allowed maximum.",
    "minLength": "Value is shorter than allowed.",
    "maxLength": "Value is longer than allowed.",
    "minItems": "Array contains too few items.",
    "maxItems": "Array contains too many items.",
    "additionalProperties": "Object contains undeclared properties.",
    "invalid_json": "Request body is not valid JSON.",
    "invalid_encoding": "Value contains invalid text encoding.",
    "unexpected_body": "This operation does not declare a request body.",
    "unsupported_media_type": "Content-Type is missing or unsupported.",
}


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    location: str
    name: str | None
    path: tuple[str | int, ...]
    code: str

    def to_dict(self) -> dict[str, Any]:
        code = self.code if self.code in _DETAIL_MESSAGES else "invalid_value"
        return {
            "location": self.location,
            "name": self.name,
            "path": list(self.path),
            "code": code,
            "message": _DETAIL_MESSAGES[code],
        }


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    status_code: int
    code: str
    message: str
    details: tuple[ErrorDetail, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": [detail.to_dict() for detail in self.details],
            }
        }


class RequestValidationError(ValueError):
    """Shared response metadata while retaining each validator's diagnostic text."""

    status_code = 400
    code = "validation_failed"
    public_message = "Request validation failed."

    def __init__(self, message: str, details: tuple[ErrorDetail, ...]) -> None:
        self.details = details
        super().__init__(message)


class UpstreamError(Exception):
    """An upstream failure with a safe public response and private diagnostics."""

    status_code = 502
    code = "upstream_error"
    public_message = "The upstream service could not complete the request."
    details: tuple[ErrorDetail, ...] = ()


def error_response(error: Exception) -> ErrorResponse:
    """Expose known request errors; keep contract and unexpected failures private."""
    if isinstance(error, (RequestValidationError, UpstreamError)):
        return ErrorResponse(error.status_code, error.code, error.public_message, error.details)
    if isinstance(error, RouteNotFoundError):
        return ErrorResponse(404, "route_not_found", "No matching operation was found.")
    if isinstance(error, MethodNotAllowedError):
        return ErrorResponse(405, "method_not_allowed", "The request method is not allowed.")
    if isinstance(error, InvalidRequestTargetError):
        return ErrorResponse(400, "invalid_request_target", "The request target is invalid.")
    return ErrorResponse(500, "internal_error", "An internal error occurred.")
