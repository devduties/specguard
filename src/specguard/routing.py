"""Match incoming requests to OpenAPI operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True, slots=True)
class OperationMatch:
    """An OpenAPI operation matched to a request path and method."""

    path_template: str
    method: str
    operation: dict[str, Any]
    path_parameters: dict[str, str]


class RouteNotFoundError(LookupError):
    """Raised when no OpenAPI path matches the request target."""

    status_code = 404


class MethodNotAllowedError(LookupError):
    """Raised when a path matches but does not define the requested method."""

    status_code = 405

    def __init__(self, method: str, allowed_methods: tuple[str, ...]) -> None:
        self.method = method.upper()
        self.allowed_methods = allowed_methods
        allowed = ", ".join(allowed_methods) or "none"
        super().__init__(f"Method {self.method} is not allowed. Allowed methods: {allowed}.")


_HTTP_METHODS = ("get", "head", "post", "put", "patch", "delete", "options", "trace")


def match_operation(
    document: dict[str, Any], request_path: str, request_method: str
) -> OperationMatch | None:
    """Return the operation for a request, or ``None`` if it cannot be resolved.

    Literal paths take precedence over parameterized paths. The request target
    may include a query string; only its path is used for route matching.
    """
    try:
        return resolve_operation(document, request_path, request_method)
    except (RouteNotFoundError, MethodNotAllowedError):
        return None


def resolve_operation(
    document: dict[str, Any], request_target: str, request_method: str
) -> OperationMatch:
    """Resolve a request or raise a 404-style or 405-style routing error."""
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise RouteNotFoundError("The OpenAPI document has no paths object.")

    request_path = urlsplit(request_target).path
    candidates: list[tuple[int, int, str, dict[str, Any], dict[str, str]]] = []
    for template, path_item in paths.items():
        if not isinstance(template, str) or not isinstance(path_item, dict):
            continue
        parameters = _match_path(template, request_path)
        if parameters is None:
            continue
        segments = _segments(template)
        literal_segments = sum(not _is_parameter(segment) for segment in segments)
        literal_characters = sum(
            len(segment) for segment in segments if not _is_parameter(segment)
        )
        candidates.append((literal_segments, literal_characters, template, path_item, parameters))

    if not candidates:
        raise RouteNotFoundError(f"No OpenAPI path matches '{request_path}'.")

    # Select the best path before checking the method. Otherwise a parameterized
    # route could incorrectly catch a request intended for a more specific path.
    _, _, template, path_item, parameters = max(candidates, key=lambda item: (item[0], item[1]))
    method = request_method.lower()
    operation = path_item.get(method)
    if method not in _HTTP_METHODS or not isinstance(operation, dict):
        allowed = tuple(
            candidate.upper()
            for candidate in _HTTP_METHODS
            if isinstance(path_item.get(candidate), dict)
        )
        raise MethodNotAllowedError(request_method, allowed)

    return OperationMatch(template, method, operation, parameters)


def _match_path(template: str, request_path: str) -> dict[str, str] | None:
    template_segments = _segments(template)
    request_segments = _segments(request_path)
    if len(template_segments) != len(request_segments):
        return None

    parameters: dict[str, str] = {}
    for expected, actual in zip(template_segments, request_segments, strict=True):
        if _is_parameter(expected):
            parameters[expected[1:-1]] = unquote(actual)
        elif unquote(expected) != unquote(actual):
            return None
    return parameters


def _segments(path: str) -> list[str]:
    if path == "/":
        return []
    return path.removeprefix("/").split("/")


def _is_parameter(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}") and len(segment) > 2
