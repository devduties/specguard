"""Walk OpenAPI objects without interpreting examples or extension payloads."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SpecObject:
    kind: str
    value: dict[str, Any]


# Each table describes one shape of child field. Both loader checks use this
# walk so new schema locations need to be added in only one place.
_OBJECT_FIELDS = {
    "root": {"components": "components", "webhooks": "webhooks"},
    "operation": {"requestBody": "body", "responses": "responses"},
    "parameter": {"schema": "schema"},
    "media": {"schema": "schema", "itemSchema": "schema"},
    "schema": dict.fromkeys(
        (
            "additionalItems",
            "additionalProperties",
            "contains",
            "contentSchema",
            "else",
            "if",
            "items",
            "not",
            "propertyNames",
            "then",
            "unevaluatedItems",
            "unevaluatedProperties",
        ),
        "schema",
    ),
    "path": dict.fromkeys(
        ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query"),
        "operation",
    ),
}
_MAP_FIELDS = {
    "root": {"paths": "path"},
    "components": {
        "schemas": "schema",
        "pathItems": "path",
        "responses": "response",
        "parameters": "parameter",
        "headers": "parameter",
        "requestBodies": "body",
        "callbacks": "callback",
        "mediaTypes": "media",
        "examples": "reference",
        "securitySchemes": "reference",
        "links": "reference",
    },
    "path": {"additionalOperations": "operation"},
    "operation": {"callbacks": "callback"},
    "parameter": {"content": "media", "examples": "reference"},
    "body": {"content": "media"},
    "response": {"content": "media", "headers": "parameter", "links": "reference"},
    "media": {"examples": "reference", "encoding": "encoding"},
    "encoding": {"headers": "parameter"},
    "schema": dict.fromkeys(
        (
            "$defs",
            "definitions",
            "dependentSchemas",
            "patternProperties",
            "properties",
            "dependencies",
        ),
        "schema",
    ),
}
_LIST_FIELDS = {
    "path": {"parameters": "parameter"},
    "operation": {"parameters": "parameter"},
    "schema": dict.fromkeys(("allOf", "anyOf", "oneOf", "prefixItems"), "schema"),
}


def iter_spec_objects(document: dict[str, Any]) -> Iterator[SpecObject]:
    pending = [SpecObject("root", document)]
    seen: set[tuple[str, int]] = set()
    while pending:
        current = pending.pop()
        kind, value = current.kind, current.value
        identity = (kind, id(value))
        if identity in seen:
            continue
        seen.add(identity)
        yield current

        for field, child_kind in _OBJECT_FIELDS.get(kind, {}).items():
            child = value.get(field)
            if isinstance(child, dict):
                pending.append(SpecObject(child_kind, child))
        for field, child_kind in _MAP_FIELDS.get(kind, {}).items():
            children = value.get(field)
            if not isinstance(children, dict):
                continue
            for name, child in children.items():
                if kind == "root" and field == "paths" and not str(name).startswith("/"):
                    continue
                if isinstance(child, dict):
                    pending.append(SpecObject(child_kind, child))
        for field, child_kind in _LIST_FIELDS.get(kind, {}).items():
            children = value.get(field)
            if isinstance(children, list):
                pending.extend(
                    SpecObject(child_kind, child) for child in children if isinstance(child, dict)
                )
        if kind in {"callback", "responses", "webhooks"}:
            child_kind = "response" if kind == "responses" else "path"
            pending.extend(
                SpecObject(child_kind, child)
                for name, child in value.items()
                if not str(name).startswith("x-") and isinstance(child, dict)
            )
