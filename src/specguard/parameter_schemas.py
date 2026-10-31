"""Schema-guided conversion of deserialized parameter values."""

from __future__ import annotations

import math
import re
from itertools import islice, product
from typing import Any

from specguard.routing import PathItemReferenceError, _json_pointer_target


class ParameterDefinitionError(ValueError):
    """An invalid contract or undefined parameter representation."""


def resolve_object(document: dict[str, Any], value: Any) -> dict[str, Any]:
    visited: set[str] = set()
    while isinstance(value, dict) and "$ref" in value:
        reference = value["$ref"]
        if not isinstance(reference, str) or reference in visited:
            raise ParameterDefinitionError("Invalid or cyclic parameter/schema reference.")
        visited.add(reference)
        try:
            value = _json_pointer_target(document, reference)
        except PathItemReferenceError as exc:
            raise ParameterDefinitionError("Unresolved parameter/schema reference.") from exc
    if not isinstance(value, dict):
        raise ParameterDefinitionError("Parameter must resolve to an object.")
    return value


def schema_parts(
    document: dict[str, Any], schema: Any, seen: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Describe possible wire shapes without replacing final schema validation."""
    if not isinstance(schema, dict):
        return []
    parts = [schema]
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or reference in seen:
            raise ParameterDefinitionError("Invalid or cyclic schema reference.")
        target = resolve_object(document, {"$ref": reference})
        parts.extend(schema_parts(document, target, seen | {reference}))
    for keyword in ("allOf", "anyOf", "oneOf"):
        for branch in schema.get(keyword, []):
            parts.extend(schema_parts(document, branch, seen))
    return parts


def allows_extra_properties(
    document: dict[str, Any], schema: Any, seen: frozenset[str] = frozenset()
) -> bool:
    """Decide whether an exploded object can collect undeclared source fields.

    References and allOf intersect permissions; anyOf/oneOf offer alternatives.
    An unconstrained wrapper cannot reopen an object closed by a child schema.
    """
    if not isinstance(schema, dict):
        return schema is not False
    if schema.get("additionalProperties") is False or schema.get("unevaluatedProperties") is False:
        return False
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or reference in seen:
            raise ParameterDefinitionError("Invalid or cyclic schema reference.")
        target = resolve_object(document, {"$ref": reference})
        if not allows_extra_properties(document, target, seen | {reference}):
            return False
    if not all(
        allows_extra_properties(document, branch, seen) for branch in schema.get("allOf", [])
    ):
        return False
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema and not any(
            allows_extra_properties(document, branch, seen) for branch in schema[keyword]
        ):
            return False
    return True


def schema_kinds(document: dict[str, Any], schema: Any) -> list[str]:
    kinds: list[str] = []
    for part in schema_parts(document, schema):
        declared = part.get("type", [])
        if isinstance(declared, str):
            declared = [declared]
        if not declared:
            if "properties" in part or "additionalProperties" in part:
                declared = ["object"]
            elif "items" in part or "prefixItems" in part:
                declared = ["array"]
            elif "enum" in part or "const" in part:
                values = part.get("enum", [part.get("const")])
                declared = [_json_kind(value) for value in values]
        if part.get("nullable"):
            declared = [*declared, "null"]
        for kind in declared:
            if kind not in kinds:
                kinds.append(kind)
    return kinds or ["string"]


def property_schema(document: dict[str, Any], schema: Any, name: str) -> Any:
    found = []
    for part in schema_parts(document, schema):
        properties = part.get("properties", {})
        if name in properties:
            found.append(properties[name])
        elif isinstance(part.get("additionalProperties"), dict):
            found.append(part["additionalProperties"])
    # Include branch schemas as alternatives for conversion; the complete parent
    # validator subsequently applies allOf/anyOf/oneOf and property constraints.
    return {"anyOf": found} if found else {}


def typed_candidates(document: dict[str, Any], schema: Any, raw: Any, depth: int = 0) -> list[Any]:
    if depth > 32:
        raise ValueError("Parameter nesting exceeds 32 levels.")
    if isinstance(raw, dict):
        names = list(raw)
        choices = [
            typed_candidates(
                document, property_schema(document, schema, name), raw[name], depth + 1
            )
            for name in names
        ]
        return [dict(zip(names, values, strict=True)) for values in _combinations(choices)]
    if isinstance(raw, list):
        parts = schema_parts(document, schema)
        choices = []
        for index, item in enumerate(raw):
            item_schemas = []
            for part in parts:
                prefixes = part.get("prefixItems", [])
                item_schemas.append(
                    prefixes[index] if index < len(prefixes) else part.get("items", {})
                )
            item_schema = {"anyOf": item_schemas} if item_schemas else {}
            choices.append(typed_candidates(document, item_schema, item, depth + 1))
        return [list(values) for values in _combinations(choices)]
    if not isinstance(raw, str):
        return [raw]
    result = []
    kinds = schema_kinds(document, schema)
    # Preserve strings when the schema admits them; conversion is deterministic.
    if "string" in kinds:
        result.append(raw)
    for kind in kinds:
        if kind == "integer" and re.fullmatch(r"-?[0-9]+", raw):
            result.append(int(raw))
        elif kind == "number" and re.fullmatch(
            r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw
        ):
            value = float(raw)
            if math.isfinite(value):
                result.append(value)
        elif kind == "boolean" and raw in {"true", "false"}:
            result.append(raw == "true")
        elif kind == "null" and raw == "null":
            result.append(None)
    return result


def _combinations(choices: list[list[Any]]) -> list[tuple[Any, ...]]:
    combinations = list(islice(product(*choices), 129))
    if len(combinations) > 128:
        raise ValueError("Parameter has more than 128 possible typed interpretations.")
    return combinations


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"
