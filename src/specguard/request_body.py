"""Validate JSON request bodies against the matched OpenAPI operation."""

import re
from dataclasses import dataclass
from typing import Any

from referencing.exceptions import Unresolvable

from specguard.errors import ErrorDetail, RequestValidationError
from specguard.json_values import parse_json
from specguard.routing import (
    OperationMatch,
    PathItemReferenceError,
    _json_pointer_target,
    resolve_operation,
)
from specguard.schema_validation import create_schema_validator


@dataclass(frozen=True, slots=True)
class BodyIssue:
    path: tuple[str | int, ...]
    message: str
    code: str = "invalid_value"


class BodyValidationError(RequestValidationError):
    """A missing, malformed, or schema-invalid request body."""

    status_code = 400
    code = "body_validation_failed"
    public_message = "Request body does not satisfy the contract."
    detail_code: str | None = None

    def __init__(self, issues: list[BodyIssue]) -> None:
        self.issues = tuple(issues)
        details = tuple(
            ErrorDetail("body", None, i.path, self.detail_code or i.code) for i in self.issues
        )
        super().__init__("; ".join(issue.message for issue in self.issues), details)


class UnsupportedMediaTypeError(BodyValidationError):
    """The supplied content type is not a supported JSON representation."""

    status_code = 415
    code = "unsupported_media_type"
    public_message = "Content-Type is missing or unsupported."
    detail_code = "unsupported_media_type"


class BodyDefinitionError(ValueError):
    """The contract's request body cannot be resolved or validated."""


@dataclass(frozen=True, slots=True)
class ValidatedBody:
    match: OperationMatch
    present: bool
    value: Any = None
    media_type: str | None = None


def validate_request_body(
    document: dict[str, Any],
    request_target: str,
    request_method: str,
    body: bytes | str | None = None,
    *,
    content_type: str | None = None,
) -> ValidatedBody:
    """Route and validate raw JSON without coercion or injecting schema defaults.

    None and zero-length bodies are absent. A JSON null is present and must
    satisfy the schema. Exact content entries precede type/* and */* entries.
    Only JSON media types are supported in this slice.
    """
    match = resolve_operation(document, request_target, request_method)
    definition = match.operation.get("requestBody")
    if definition is None:
        if body not in (None, b"", ""):
            raise BodyValidationError(
                [
                    BodyIssue(
                        (), "This operation does not declare a request body.", "unexpected_body"
                    )
                ]
            )
        return ValidatedBody(match, present=False)

    definition = _resolve_body_object(document, definition)
    if body in (None, b"", ""):
        if definition.get("required", False):
            raise BodyValidationError(
                [BodyIssue((), "Required request body is missing.", "required")]
            )
        return ValidatedBody(match, present=False)

    content = definition.get("content")
    if not isinstance(content, dict) or not content:
        raise BodyDefinitionError("Request body must declare a nonempty content mapping.")
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+/[!#$%&'*+.^_`|~0-9a-z-]+", media_type) or (
        "*" in media_type
    ):
        raise UnsupportedMediaTypeError([BodyIssue((), "A JSON Content-Type is required.")])
    subtype = media_type.split("/", 1)[1]
    if subtype != "json" and not subtype.endswith("+json"):
        raise UnsupportedMediaTypeError([BodyIssue((), "Only JSON request bodies are supported.")])

    media = _resolve_body_object(document, _select_media(content, media_type))
    try:
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        value = parse_json(text)
    except UnicodeDecodeError as exc:
        raise BodyValidationError(
            [BodyIssue((), "JSON request body must be UTF-8 encoded.", "invalid_encoding")]
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise BodyValidationError(
            [BodyIssue((), f"Invalid JSON request body: {exc}", "invalid_json")]
        ) from exc

    schema = media.get("schema", {})
    if not isinstance(schema, (dict, bool)):
        raise BodyDefinitionError("Request body schema must be an object or boolean.")
    validator = create_schema_validator(document, request_body=True)
    try:
        issues = [
            BodyIssue(tuple(error.absolute_path), error.message, error.validator)
            for error in validator.evolve(schema=schema).iter_errors(value)
        ]
    except (Unresolvable, RecursionError) as exc:
        raise BodyDefinitionError(
            "Request body schema contains an unresolved or cyclic reference."
        ) from exc
    if issues:
        raise BodyValidationError(issues)
    return ValidatedBody(match, present=True, value=value, media_type=media_type)


def _resolve_body_object(document: dict[str, Any], definition: Any) -> dict[str, Any]:
    visited: set[str] = set()
    while isinstance(definition, dict) and "$ref" in definition:
        reference = definition["$ref"]
        if not isinstance(reference, str) or reference in visited:
            raise BodyDefinitionError("Invalid or cyclic request body reference.")
        visited.add(reference)
        try:
            definition = _json_pointer_target(document, reference)
        except PathItemReferenceError as exc:
            raise BodyDefinitionError("Unresolved request body reference.") from exc
    if not isinstance(definition, dict):
        raise BodyDefinitionError("Request body must resolve to an object.")
    return definition


def _select_media(content: dict[str, Any], media_type: str) -> dict[str, Any]:
    entries = {name.lower(): value for name, value in content.items()}
    for candidate in (media_type, media_type.split("/", 1)[0] + "/*", "*/*"):
        if candidate in entries:
            media = entries[candidate]
            if not isinstance(media, dict):
                raise BodyDefinitionError("Request body media type must contain an object.")
            return media
    raise UnsupportedMediaTypeError(
        [BodyIssue((), f"Content-Type '{media_type}' is not declared.")]
    )
