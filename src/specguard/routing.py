"""Match incoming requests to OpenAPI operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit


@dataclass(frozen=True, slots=True)
class OperationMatch:
    """An OpenAPI operation matched to a request path and method."""

    path_template: str
    method: str
    operation: dict[str, Any]
    path_parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class PathToken:
    value: str
    is_parameter: bool = False


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    path_template: str
    path_item: dict[str, Any]
    path_parameters: dict[str, str]
    specificity: tuple[tuple[int, ...], ...]


class RouteNotFoundError(LookupError):
    """Raised when no OpenAPI path matches the request target."""

    status_code = 404


class AmbiguousRouteError(LookupError):
    """Raised when equally specific OpenAPI paths match the same request."""


class MethodNotAllowedError(LookupError):
    """Raised when a path matches but does not define the requested method."""

    status_code = 405

    def __init__(self, method: str, allowed_methods: tuple[str, ...]) -> None:
        self.method = method.upper()
        self.allowed_methods = allowed_methods
        allowed = ", ".join(allowed_methods) or "none"
        super().__init__(f"Method {self.method} is not allowed. Allowed methods: {allowed}.")


class InvalidRequestTargetError(ValueError):
    """Raised when a request target contains invalid path encoding."""

    status_code = 400


class InvalidPathTemplateError(ValueError):
    """Raised when a path template contains malformed encoding or expressions."""


class PathItemReferenceError(ValueError):
    """Raised when a Path Item reference cannot be resolved safely."""


_HTTP_METHODS = ("get", "head", "post", "put", "patch", "delete", "options", "trace", "query")
_ABSOLUTE_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")


def match_operation(
    document: dict[str, Any], request_path: str, request_method: str
) -> OperationMatch | None:
    """Return a matching operation, or ``None`` when no path or method matches.

    Equally specific matching paths raise :class:`AmbiguousRouteError`, since
    that indicates the OpenAPI document does not define a unique route. Invalid
    request path encoding and unresolved Path Item references also raise errors.
    """
    try:
        return resolve_operation(document, request_path, request_method)
    except (RouteNotFoundError, MethodNotAllowedError):
        return None


def resolve_operation(
    document: dict[str, Any], request_target: str, request_method: str
) -> OperationMatch:
    """Resolve a request or raise a routing or specification error."""
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise RouteNotFoundError("The OpenAPI document has no paths object.")

    request_path = _request_path(request_target)
    request_segments = tuple(
        _strict_unquote(segment, InvalidRequestTargetError, "request path")
        for segment in _segments(request_path)
    )
    candidates: list[RouteCandidate] = []

    for template, path_item in paths.items():
        if not isinstance(template, str) or not template.startswith("/"):
            continue
        if not isinstance(path_item, dict):
            continue

        template_segments = _compile_path_template(template)
        parameters = _match_path(template_segments, request_segments)
        if parameters is None:
            continue

        specificity = tuple(_segment_specificity(segment) for segment in template_segments)
        path_item = _resolve_path_item(document, path_item)
        candidates.append(RouteCandidate(template, path_item, parameters, specificity))

    if not candidates:
        raise RouteNotFoundError(f"No OpenAPI path matches '{request_path}'.")

    # Compare each path segment from left to right, with literal text ahead of
    # parameter expressions. Reject equal best scores instead of using spec order.
    best_specificity = max(candidate.specificity for candidate in candidates)
    best_candidates = [
        candidate for candidate in candidates if candidate.specificity == best_specificity
    ]
    if len(best_candidates) > 1:
        ordered_candidates = sorted(best_candidates, key=lambda candidate: candidate.path_template)
        routes = ", ".join(f"'{candidate.path_template}'" for candidate in ordered_candidates)
        raise AmbiguousRouteError(f"Ambiguous OpenAPI routes match '{request_path}': {routes}.")

    selected = best_candidates[0]
    method_key, operation = _find_operation(selected.path_item, request_method)
    if not isinstance(operation, dict):
        raise MethodNotAllowedError(
            request_method,
            _allowed_methods(selected.path_item),
        )

    return OperationMatch(selected.path_template, method_key, operation, selected.path_parameters)


def _request_path(request_target: str) -> str:
    """Extract a path without treating an origin-form ``//...`` as an authority."""
    if request_target.startswith("/"):
        return request_target.split("?", 1)[0]

    if _ABSOLUTE_URL.match(request_target):
        try:
            parsed = urlsplit(request_target)
        except ValueError as exc:
            raise InvalidRequestTargetError(
                f"Invalid absolute request target '{request_target}': {exc}"
            ) from exc
        if not parsed.netloc:
            raise InvalidRequestTargetError(
                f"Invalid absolute request target '{request_target}': missing authority."
            )
        return parsed.path or "/"

    return request_target.split("?", 1)[0]


def _strict_unquote(value: str, error_type: type[ValueError], context: str) -> str:
    if _INVALID_PERCENT_ESCAPE.search(value):
        raise error_type(f"Invalid percent escape in {context}: '{value}'.")
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise error_type(f"Invalid UTF-8 percent encoding in {context}: '{value}'.") from exc


def _segments(path: str) -> list[str]:
    if path == "/":
        return []
    return path.removeprefix("/").split("/")


@lru_cache(maxsize=512)
def _compile_path_template(template: str) -> tuple[tuple[PathToken, ...], ...]:
    compiled: list[tuple[PathToken, ...]] = []
    seen_parameters: set[str] = set()
    for segment in _segments(template):
        tokens: list[PathToken] = []
        cursor = 0
        for expression in _PATH_PARAMETER.finditer(segment):
            literal = segment[cursor : expression.start()]
            if "{" in literal or "}" in literal:
                raise InvalidPathTemplateError(
                    f"Malformed path parameter expression in '{template}'."
                )
            if literal:
                tokens.append(
                    PathToken(
                        _strict_unquote(literal, InvalidPathTemplateError, "path template"),
                    )
                )

            parameter_name = expression.group(1)
            if parameter_name in seen_parameters:
                raise InvalidPathTemplateError(
                    f"Path parameter '{parameter_name}' appears more than once in '{template}'."
                )
            seen_parameters.add(parameter_name)
            tokens.append(PathToken(parameter_name, is_parameter=True))
            cursor = expression.end()

        literal = segment[cursor:]
        if "{" in literal or "}" in literal:
            raise InvalidPathTemplateError(f"Malformed path parameter expression in '{template}'.")
        if literal:
            tokens.append(
                PathToken(
                    _strict_unquote(literal, InvalidPathTemplateError, "path template"),
                )
            )
        compiled.append(tuple(tokens))
    return tuple(compiled)


def _segment_specificity(segment: tuple[PathToken, ...]) -> tuple[int, ...]:
    score: list[int] = []
    for token in segment:
        score.extend((0,) if token.is_parameter else (1,) * len(token.value))
    return tuple(score)


def _match_path(
    template_segments: tuple[tuple[PathToken, ...], ...], request_segments: tuple[str, ...]
) -> dict[str, str] | None:
    if len(template_segments) != len(request_segments):
        return None

    parameters: dict[str, str] = {}
    for template_segment, request_segment in zip(template_segments, request_segments, strict=True):
        matched = _match_segment(template_segment, request_segment)
        if matched is None:
            return None
        parameters.update(matched)
    return parameters


def _match_segment(tokens: tuple[PathToken, ...], text: str) -> dict[str, str] | None:
    """Match the entire segment without regex backtracking.

    Suffix reachability takes O(tokens * characters) states. Reconstruction
    chooses the shortest successful nonempty capture, preserving lazy matching.
    """
    size = len(text)
    suffixes = [bytearray(size + 1) for _ in range(len(tokens) + 1)]
    suffixes[-1][size] = 1
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        row, following = suffixes[index], suffixes[index + 1]
        for offset in range(size, -1, -1):
            if token.is_parameter:
                row[offset] = offset < size and (following[offset + 1] or row[offset + 1])
            else:
                end = offset + len(token.value)
                row[offset] = (
                    end <= size and following[end] and text.startswith(token.value, offset)
                )
    if not suffixes[0][0]:
        return None

    parameters: dict[str, str] = {}
    offset = 0
    for index, token in enumerate(tokens):
        if token.is_parameter:
            end = offset + 1
            while not suffixes[index + 1][end]:
                end += 1
            parameters[token.value] = text[offset:end]
            offset = end
        else:
            offset += len(token.value)
    return parameters


def _resolve_path_item(document: dict[str, Any], path_item: dict[str, Any]) -> dict[str, Any]:
    resolved = path_item
    visited_references: list[str] = []
    while "$ref" in resolved:
        reference = resolved["$ref"]
        if not isinstance(reference, str):
            raise PathItemReferenceError("Path Item '$ref' must be a string.")
        if reference in visited_references:
            chain = " -> ".join([*visited_references, reference])
            raise PathItemReferenceError(f"Cyclic Path Item references detected: {chain}.")
        visited_references.append(reference)
        resolved = _json_pointer_target(document, reference)
        if not isinstance(resolved, dict):
            raise PathItemReferenceError(
                f"Path Item reference '{reference}' does not resolve to an object."
            )
    return resolved


def _json_pointer_target(document: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#"):
        raise PathItemReferenceError(
            f"External Path Item reference '{reference}' is not supported."
        )
    fragment = reference[1:]
    if not fragment:
        return document
    if not fragment.startswith("/"):
        raise PathItemReferenceError(
            f"Path Item reference '{reference}' is not a local JSON Pointer."
        )

    target: Any = document
    for raw_token in fragment[1:].split("/"):
        token = _strict_unquote(raw_token, PathItemReferenceError, "Path Item reference")
        if re.search(r"~(?![01])", token):
            raise PathItemReferenceError(
                f"Path Item reference '{reference}' contains an invalid JSON Pointer escape."
            )
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict):
            if token not in target:
                raise PathItemReferenceError(f"Path Item reference '{reference}' does not resolve.")
            target = target[token]
        elif isinstance(target, list) and token.isdigit() and str(int(token)) == token:
            index = int(token)
            if index >= len(target):
                raise PathItemReferenceError(f"Path Item reference '{reference}' does not resolve.")
            target = target[index]
        else:
            raise PathItemReferenceError(f"Path Item reference '{reference}' does not resolve.")
    return target


def _find_operation(path_item: dict[str, Any], request_method: str) -> tuple[str, Any]:
    normalized_method = request_method.lower()
    if normalized_method in _HTTP_METHODS:
        return normalized_method, path_item.get(normalized_method)

    additional = path_item.get("additionalOperations")
    if isinstance(additional, dict):
        return request_method, additional.get(request_method)
    return request_method, None


def _allowed_methods(path_item: dict[str, Any]) -> tuple[str, ...]:
    standard = [
        method.upper() for method in _HTTP_METHODS if isinstance(path_item.get(method), dict)
    ]
    additional = path_item.get("additionalOperations")
    if not isinstance(additional, dict):
        return tuple(standard)

    custom = sorted(
        method
        for method, operation in additional.items()
        if isinstance(method, str)
        and method.lower() not in _HTTP_METHODS
        and isinstance(operation, dict)
    )
    return tuple([*standard, *custom])
