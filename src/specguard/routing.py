"""Match incoming paths and methods to OpenAPI operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote


@dataclass(frozen=True, slots=True)
class OperationMatch:
    """An OpenAPI operation matched to a request path and method."""

    path_template: str
    method: str
    operation: dict[str, Any]
    path_parameters: dict[str, str]


def match_operation(
    document: dict[str, Any], request_path: str, request_method: str
) -> OperationMatch | None:
    """Find the most specific operation matching a request path and method.

    Literal path segments take precedence over parameterized segments. Returns
    ``None`` when the document has no matching path and method.
    """
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return None

    method = request_method.lower()
    matches: list[tuple[int, int, str, dict[str, Any], dict[str, str]]] = []
    for template, path_item in paths.items():
        if not isinstance(template, str) or not isinstance(path_item, dict):
            continue
        parameters = _match_path(template, request_path)
        if parameters is None:
            continue
        operation = path_item.get(method)
        if not isinstance(operation, dict):
            continue
        segments = template.strip("/").split("/")
        if segments == [""]:
            segments = []
        literal_segments = sum(not _is_parameter(segment) for segment in segments)
        literal_characters = sum(
            len(segment) for segment in segments if not _is_parameter(segment)
        )
        matches.append((literal_segments, literal_characters, template, operation, parameters))

    if not matches:
        return None

    _, _, template, operation, parameters = max(matches, key=lambda item: (item[0], item[1]))
    return OperationMatch(template, method, operation, parameters)


def _match_path(template: str, request_path: str) -> dict[str, str] | None:
    template_segments = template.strip("/").split("/")
    request_segments = request_path.strip("/").split("/")
    if template_segments == [""]:
        template_segments = []
    if request_segments == [""]:
        request_segments = []
    if len(template_segments) != len(request_segments):
        return None

    parameters: dict[str, str] = {}
    for expected, actual in zip(template_segments, request_segments, strict=True):
        if _is_parameter(expected):
            parameters[expected[1:-1]] = unquote(actual)
        elif expected != unquote(actual):
            return None
    return parameters


def _is_parameter(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}") and len(segment) > 2
