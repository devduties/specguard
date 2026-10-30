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


_HTTP_METHODS = ("get", "head", "post", "put", "patch", "delete", "options", "trace")


def match_operation(
    document: dict[str, Any], request_path: str, request_method: str
) -> OperationMatch | None:
    """Return a matching operation, or ``None`` when no path or method matches.

    Equally specific matching paths raise :class:`AmbiguousRouteError`, since
    that indicates the OpenAPI document does not define a unique route. The
    request target may include a query string; only its path is matched.
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
    candidates: list[tuple[tuple[int, ...], str, dict[str, Any], dict[str, str]]] = []
    for template, path_item in paths.items():
        if not isinstance(template, str) or not isinstance(path_item, dict):
            continue
        parameters = _match_path(template, request_path)
        if parameters is None:
            continue
        segments = _segments(template)
        specificity = tuple(0 if _is_parameter(segment) else 1 for segment in segments)
        candidates.append((specificity, template, path_item, parameters))

    if not candidates:
        raise RouteNotFoundError(f"No OpenAPI path matches '{request_path}'.")

    # Earlier literal segments take precedence. Resolve the path before checking
    # the method, then reject ties instead of relying on document order.
    best_specificity = max(candidate[0] for candidate in candidates)
    best_candidates = [
        candidate for candidate in candidates if candidate[0] == best_specificity
    ]
    if len(best_candidates) > 1:
        ordered_candidates = sorted(best_candidates, key=lambda candidate: candidate[1])
        routes = ", ".join(f"'{candidate[1]}'" for candidate in ordered_candidates)
        raise AmbiguousRouteError(
            f"Ambiguous OpenAPI routes match '{request_path}': {routes}."
        )

    _, template, path_item, parameters = best_candidates[0]
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
