"""OpenAPI parameter serialization, preserving delimiter/data boundaries."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, unquote_plus

from specguard.parameter_schemas import (
    ParameterDefinitionError,
    allows_extra_properties,
    schema_parts,
)


def decode_parameter_value(value: str, location: str) -> str:
    if location == "header":
        return value
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Invalid percent escape.")
    try:
        return (unquote_plus if location == "query" else unquote)(value, errors="strict")
    except UnicodeError as exc:
        raise ValueError("Invalid UTF-8 encoding.") from exc


def parse_parameter_pairs(raw: str, *, cookie: bool = False) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    location = "cookie" if cookie else "query"
    for entry in raw.split(";" if cookie else "&"):
        if not entry:
            continue
        name, _, value = entry.strip().partition("=") if cookie else entry.partition("=")
        name = decode_parameter_value(name, location)
        # Check encoding now, but retain the original representation for splitting.
        decode_parameter_value(value, location)
        if cookie and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        result.setdefault(name, []).append(value)
    return result


def parameter_serialization(parameter: dict[str, Any]) -> tuple[str, bool]:
    location = parameter["in"]
    default = "form" if location in {"query", "cookie"} else "simple"
    style = parameter.get("style", default)
    allowed = {
        "query": {"form", "spaceDelimited", "pipeDelimited", "deepObject"},
        "path": {"simple", "label", "matrix"},
        "header": {"simple"},
        "cookie": {"form"},
    }
    if style not in allowed.get(location, set()):
        raise ParameterDefinitionError(f"Invalid {location} parameter style: {style}.")
    explode = parameter.get("explode", style == "form")
    if style == "deepObject" and not explode:
        raise ParameterDefinitionError("deepObject requires explode=true; false is undefined.")
    if style in {"spaceDelimited", "pipeDelimited"} and explode:
        raise ParameterDefinitionError(f"{style} with explode=true is undefined by OpenAPI.")
    return style, explode


def deserialize_parameter(
    document: dict[str, Any],
    parameter: dict[str, Any],
    schema: Any,
    kind: str,
    source: dict[str, list[str]],
    reserved: set[str],
) -> Any:
    """Return a scalar/list/map, or None when the parameter is absent."""
    name, location = parameter["name"], parameter["in"]
    lookup = name.lower() if location == "header" else name
    style, explode = parameter_serialization(parameter)
    if style == "deepObject":
        if kind != "object":
            raise ParameterDefinitionError("deepObject requires an object schema.")
        result = {}
        for key, values in source.items():
            if not key.startswith(name + "["):
                continue
            if (
                not key.endswith("]")
                or "[" in key[len(name) + 1 : -1]
                or "]" in key[len(name) + 1 : -1]
            ):
                raise ValueError("Nested deepObject fields have no defined OpenAPI representation.")
            prop = key[len(name) + 1 : -1]
            result[prop] = decode_parameter_value(_one(values), location)
        return result or None
    if kind == "object" and style == "form" and explode:
        properties = set()
        for part in schema_parts(document, schema):
            properties.update(part.get("properties", {}))
        free_form = allows_extra_properties(document, schema)
        names = properties | (set(source) - reserved if free_form else set())
        result = {
            key: decode_parameter_value(_one(source[key]), location)
            for key in names
            if key in source
        }
        return result or None
    raw_values = source.get(lookup)
    if raw_values is None:
        return None
    if style in {"spaceDelimited", "pipeDelimited"} and kind not in {"array", "object"}:
        raise ParameterDefinitionError(f"{style} requires an array or object schema.")
    if style == "form" and explode and kind == "array":
        return [decode_parameter_value(value, location) for value in raw_values]

    raw = (
        ",".join(raw_values)
        if location == "header" and kind in {"array", "object"}
        else _one(raw_values)
    )
    if style == "matrix":
        if not raw.startswith(";"):
            raise ValueError("Matrix parameter must start with a semicolon.")
        entries: dict[str, list[str]] = {}
        for entry in raw[1:].split(";"):
            key, _, value = entry.partition("=")
            entries.setdefault(decode_parameter_value(key, location), []).append(value)
        if kind == "object" and explode:
            return {
                key: decode_parameter_value(_one(values), location)
                for key, values in entries.items()
            }
        if name not in entries:
            raise ValueError("Matrix parameter name is missing.")
        if kind == "array" and explode:
            return [decode_parameter_value(value, location) for value in entries[name]]
        raw = _one(entries[name])
    if style == "label":
        if not raw.startswith("."):
            raise ValueError("Label parameter must start with a dot.")
        raw = raw[1:]
    if kind not in {"array", "object"}:
        return decode_parameter_value(raw, location)

    if style == "spaceDelimited":
        tokens = re.split(r"%20| |\+", raw, flags=re.IGNORECASE)
    elif style == "pipeDelimited":
        tokens = re.split(r"%7c|\|", raw, flags=re.IGNORECASE)
    else:
        tokens = raw.split("." if style == "label" and explode else ",")
    if kind == "array":
        return [decode_parameter_value(token, location) for token in tokens]
    if explode:
        entries = []
        for token in tokens:
            key, separator, value = token.partition("=")
            if not separator:
                raise ValueError("Exploded object fields require key=value pairs.")
            entries.append(
                (decode_parameter_value(key, location), decode_parameter_value(value, location))
            )
    else:
        if len(tokens) % 2:
            raise ValueError("Object parameter requires alternating keys and values.")
        entries = [
            (
                decode_parameter_value(tokens[i], location),
                decode_parameter_value(tokens[i + 1], location),
            )
            for i in range(0, len(tokens), 2)
        ]
    result = {}
    for key, value in entries:
        if key in result:
            raise ValueError("Object parameter contains a repeated property.")
        result[key] = value
    return result


def _one(values: list[str]) -> str:
    if len(values) != 1:
        raise ValueError("Parameter must occur only once in this encoding.")
    return values[0]
