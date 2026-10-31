"""Deserialize and validate path, query, header, and cookie parameters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from specguard.errors import ErrorDetail, RequestValidationError
from specguard.json_values import parse_json
from specguard.parameter_encoding import (
    decode_parameter_value,
    deserialize_parameter,
    parameter_serialization,
    parse_parameter_pairs,
)
from specguard.parameter_schemas import (
    ParameterDefinitionError,
    resolve_object,
    schema_kinds,
    schema_parts,
    typed_candidates,
)
from specguard.routing import OperationMatch, _resolve_path_item, resolve_operation
from specguard.schema_validation import create_schema_validator

__all__ = [
    "ParameterDefinitionError",
    "ParameterIssue",
    "ParameterValidationError",
    "ValidatedParameters",
    "validate_parameters",
]


@dataclass(frozen=True, slots=True)
class ParameterIssue:
    location: str
    name: str
    message: str
    code: str = "invalid_value"


class ParameterValidationError(RequestValidationError):
    """Request parameters do not satisfy the contract."""

    status_code = 400
    code = "parameter_validation_failed"
    public_message = "Request parameters do not satisfy the contract."

    def __init__(self, issues: Sequence[ParameterIssue]) -> None:
        self.issues = tuple(issues)
        details = tuple(ErrorDetail(i.location, i.name or None, (), i.code) for i in self.issues)
        super().__init__(
            "; ".join(f"{i.location}.{i.name}: {i.message}" for i in self.issues), details
        )


@dataclass(frozen=True, slots=True)
class ValidatedParameters:
    match: OperationMatch
    path: dict[str, Any]
    query: dict[str, Any]
    headers: dict[str, Any]
    cookies: dict[str, Any] = field(default_factory=dict)


def validate_parameters(
    document: dict[str, Any],
    request_target: str,
    request_method: str,
    headers: Mapping[str, str] | Sequence[tuple[str, str]] = (),
    *,
    content_decoders: Mapping[str, Callable[[str], Any]] | None = None,
) -> ValidatedParameters:
    """Route, deserialize, and validate declared parameters without mutating inputs.

    Standard styles, objects, arrays, unions, JSON/text content, and cookies are
    supported. Custom media types can supply a decoder. Undefined OpenAPI style
    combinations raise ParameterDefinitionError. Missing optional values are
    omitted; schema defaults are never injected.
    """
    match = resolve_operation(document, request_target, request_method)
    path_item = _resolve_path_item(document, document["paths"][match.path_template])
    definitions: dict[tuple[str, str], dict[str, Any]] = {}
    for owner in (path_item, match.operation):
        for raw in owner.get("parameters", []):
            parameter = resolve_object(document, raw)
            location, name = parameter["in"], parameter["name"]
            key = (location, name.lower() if location == "header" else name)
            definitions[key] = parameter

    sources = _collect_sources(match, request_target, headers)

    validator = create_schema_validator(document)
    values: dict[str, dict[str, Any]] = {location: {} for location in sources}
    issues: list[ParameterIssue] = []
    for (location, lookup_name), parameter in definitions.items():
        name = parameter["name"]
        if location == "header" and lookup_name in {"accept", "content-type", "authorization"}:
            continue
        if location not in sources:
            raise ParameterDefinitionError(f"Invalid parameter location: {location}.")
        media_type, schema = _parameter_schema(parameter)
        kinds = schema_kinds(document, schema)
        if media_type is None:
            style, _ = parameter_serialization(parameter)
            if style in {"deepObject", "spaceDelimited", "pipeDelimited"}:
                allowed_kinds = {"object"} if style == "deepObject" else {"array", "object"}
                kinds = [kind for kind in kinds if kind in allowed_kinds]
                if not kinds:
                    raise ParameterDefinitionError(f"Schema has no supported type for {style}.")
        elif media_type not in {"application/json", "text/plain"} and not media_type.endswith(
            "+json"
        ):
            if not content_decoders or media_type not in content_decoders:
                raise ParameterDefinitionError(f"Provide a content decoder for {media_type}.")
        reserved = _reserved_names(document, definitions, parameter)
        present = False
        failures: list[str] = []
        failure_codes: set[str] = set()
        found = False
        # Union types may have distinct wire shapes. Try string first when allowed,
        # then schema declaration order, and validate each against the FULL schema.
        shapes = sorted(kinds, key=lambda kind: kind != "string")
        for kind in shapes if media_type is None else ["content"]:
            try:
                if media_type is not None:
                    raw_values = sources[location].get(lookup_name)
                    if raw_values is None:
                        continue
                    present = True
                    value = _decode_content(raw_values, location, media_type, content_decoders)
                    interpretations = [value]
                else:
                    raw = deserialize_parameter(
                        document, parameter, schema, kind, sources[location], reserved
                    )
                    if raw is None:
                        continue
                    present = True
                    interpretations = typed_candidates(document, schema, raw)
                if not interpretations:
                    failures.append("Value cannot be converted to the declared type.")
                for value in interpretations:
                    errors = list(validator.evolve(schema=schema).iter_errors(value))
                    if errors:
                        failures.extend(error.message for error in errors)
                        failure_codes.update(error.validator for error in errors)
                        continue
                    values[location][name] = value
                    found = True
                    break
                if found:
                    break
            except ParameterDefinitionError:
                raise
            except (ValueError, UnicodeError) as exc:
                present = True
                failures.append(str(exc))
        if found:
            continue
        if present:
            message = "; ".join(dict.fromkeys(failures)) or "Invalid parameter value."
            code = next(iter(failure_codes)) if len(failure_codes) == 1 else "invalid_value"
            issues.append(ParameterIssue(location, name, message, code))
        elif parameter.get("required") or location == "path":
            issues.append(
                ParameterIssue(location, name, "Required parameter is missing.", "required")
            )
    if issues:
        raise ParameterValidationError(issues)
    return ValidatedParameters(
        match, values["path"], values["query"], values["header"], values["cookie"]
    )


def _decode_content(
    raw_values: list[str],
    location: str,
    media_type: str,
    content_decoders: Mapping[str, Callable[[str], Any]] | None,
) -> Any:
    if len(raw_values) != 1:
        raise ValueError("Content-encoded parameter must occur only once.")
    text = decode_parameter_value(raw_values[0], location)
    if media_type == "application/json" or media_type.endswith("+json"):
        return parse_json(text)
    if media_type == "text/plain":
        return text
    if not content_decoders or media_type not in content_decoders:
        raise ParameterDefinitionError(f"Provide a content decoder for {media_type}.")
    return content_decoders[media_type](text)


def _collect_sources(
    match: OperationMatch,
    request_target: str,
    headers: Mapping[str, str] | Sequence[tuple[str, str]],
) -> dict[str, dict[str, list[str]]]:
    sources: dict[str, dict[str, list[str]]] = {
        "path": {name: [value] for name, value in match.raw_path_parameters.items()},
        "query": {},
        "header": {},
        "cookie": {},
    }
    try:
        sources["query"] = parse_parameter_pairs(request_target.partition("?")[2])
    except ValueError as exc:
        raise ParameterValidationError(
            [ParameterIssue("query", "", str(exc), "invalid_encoding")]
        ) from exc
    header_pairs = headers.items() if isinstance(headers, Mapping) else headers
    for name, value in header_pairs:
        sources["header"].setdefault(name.lower(), []).append(value)
    try:
        for cookie_header in sources["header"].get("cookie", []):
            for name, raw_values in parse_parameter_pairs(cookie_header, cookie=True).items():
                sources["cookie"].setdefault(name, []).extend(raw_values)
    except ValueError as exc:
        raise ParameterValidationError(
            [ParameterIssue("cookie", "", str(exc), "invalid_encoding")]
        ) from exc

    return sources


def _parameter_schema(parameter: dict[str, Any]) -> tuple[str | None, Any]:
    if "content" not in parameter:
        return None, parameter.get("schema", {})
    if "schema" in parameter or len(parameter["content"]) != 1:
        raise ParameterDefinitionError("Use either schema or exactly one content media type.")
    media_type, media = next(iter(parameter["content"].items()))
    return media_type.split(";", 1)[0].strip().lower(), media.get("schema", {})


def _reserved_names(
    document: dict[str, Any],
    definitions: dict[tuple[str, str], dict[str, Any]],
    current: dict[str, Any],
) -> set[str]:
    reserved = set()
    for (location, name), parameter in definitions.items():
        if location != current["in"] or parameter is current:
            continue
        reserved.add(name)
        _, schema = _parameter_schema(parameter)
        for part in schema_parts(document, schema):
            reserved.update(part.get("properties", {}))
    return reserved
