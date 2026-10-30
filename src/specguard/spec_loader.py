"""Load and validate a local OpenAPI contract."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate


class SpecLoadError(ValueError):
    """Raised when an OpenAPI document cannot be loaded or validated."""


_OPENAPI_VERSION = re.compile(r"^3\.(0|1|2)\.\d+$")


def load_openapi_spec(path: str | Path) -> dict[str, Any]:
    """Read and validate a local JSON or YAML OpenAPI 3.0–3.2 document.

    External ``$ref`` documents are rejected. Internal references within the
    loaded document are supported by the OpenAPI validator.
    """
    spec_path = Path(path)
    suffix = spec_path.suffix.lower()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise SpecLoadError(
            f"Unsupported spec file extension '{spec_path.suffix}'. Use .json, .yaml, or .yml."
        )

    try:
        raw_document = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecLoadError(f"Cannot read OpenAPI spec '{spec_path}': {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SpecLoadError(f"OpenAPI spec '{spec_path}' is not valid UTF-8 text.") from exc

    try:
        document = json.loads(raw_document) if suffix == ".json" else yaml.safe_load(raw_document)
    except json.JSONDecodeError as exc:
        raise SpecLoadError(
            f"Invalid JSON in '{spec_path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise SpecLoadError(f"Invalid YAML in '{spec_path}'{location}: {exc}") from exc

    if not isinstance(document, dict):
        raise SpecLoadError(
            f"OpenAPI spec '{spec_path}' must contain a mapping at the document root."
        )

    version = document.get("openapi")
    if not isinstance(version, str) or not _OPENAPI_VERSION.fullmatch(version):
        raise SpecLoadError(
            f"Unsupported OpenAPI version '{version}'. SpecGuard supports OpenAPI 3.0, 3.1, "
            "and 3.2."
        )

    _reject_container_cycles(document)
    try:
        external_ref = _find_external_ref(document)
    except RecursionError as exc:
        raise SpecLoadError("OpenAPI document nesting is too deep.") from exc
    if external_ref is not None:
        raise SpecLoadError(
            f"External $ref '{external_ref}' is not supported. Use references within this file."
        )

    try:
        validate(_validation_copy(document))
    except Exception as exc:
        raise SpecLoadError(
            f"Invalid OpenAPI spec '{spec_path}': {exc}"
        ) from exc

    return document


def _validation_copy(document: dict[str, Any]) -> dict[str, Any]:
    """Avoid resolving ``$ref`` values nested inside JSON Schema defaults."""
    validation_document = deepcopy(document)
    _remove_default_annotations(validation_document)
    return validation_document


def _reject_container_cycles(document: dict[str, Any]) -> None:
    """Reject YAML object cycles, while allowing shared acyclic aliases and $ref."""
    active: set[int] = set()
    finished: set[int] = set()
    stack: list[tuple[Any, bool]] = [(document, False)]
    while stack:
        value, leaving = stack.pop()
        if not isinstance(value, (dict, list)):
            continue
        identity = id(value)
        if leaving:
            active.remove(identity)
            finished.add(identity)
            continue
        if identity in active:
            raise SpecLoadError("Cyclic YAML aliases are not supported. Use internal $ref instead.")
        if identity in finished:
            continue
        active.add(identity)
        stack.append((value, True))
        children = value.values() if isinstance(value, dict) else value
        stack.extend((child, False) for child in children)


def _remove_default_annotations(document: dict[str, Any]) -> None:
    """Visit schema contexts only; map keys such as properties.default are data."""
    fields = {
        "root": {"paths": "map:path", "webhooks": "map:path", "components": "components"},
        "components": {
            "schemas": "map:schema", "pathItems": "map:path", "responses": "map:response",
            "parameters": "map:parameter", "headers": "map:parameter",
            "requestBodies": "map:body", "callbacks": "map:callback",
            "mediaTypes": "map:media",
        },
        "path": {
            **dict.fromkeys(
                ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query"),
                "operation",
            ),
            "parameters": "list:parameter", "additionalOperations": "map:operation",
        },
        "operation": {
            "parameters": "list:parameter", "requestBody": "body",
            "responses": "map:response", "callbacks": "map:callback",
        },
        "parameter": {"schema": "schema", "content": "map:media"},
        "body": {"content": "map:media"},
        "response": {"content": "map:media", "headers": "map:parameter"},
        "media": {"schema": "schema", "itemSchema": "schema", "encoding": "map:encoding"},
        "encoding": {"headers": "map:parameter"},
        "schema": {
            **dict.fromkeys(
                ("additionalItems", "additionalProperties", "contains", "contentSchema", "else",
                 "if", "items", "not", "propertyNames", "then", "unevaluatedItems",
                 "unevaluatedProperties"), "schema",
            ),
            **dict.fromkeys(
                ("$defs", "definitions", "dependentSchemas", "patternProperties", "properties",
                 "dependencies"), "map:schema",
            ),
            **dict.fromkeys(("allOf", "anyOf", "oneOf", "prefixItems"), "list:schema"),
        },
    }
    stack: list[tuple[Any, str]] = [(document, "root")]
    seen: set[tuple[int, str]] = set()
    while stack:
        value, context = stack.pop()
        key = (id(value), context)
        if key in seen:
            continue
        seen.add(key)
        if context.startswith("list:") and isinstance(value, list):
            stack.extend((child, context[5:]) for child in value)
        elif context.startswith("map:") and isinstance(value, dict):
            stack.extend((child, context[4:]) for name, child in value.items()
                         if not (isinstance(name, str) and name.startswith("x-"))
                         or context == "map:schema")
        elif isinstance(value, dict):
            if context == "callback":
                stack.extend((child, "path") for name, child in value.items()
                             if not name.startswith("x-"))
            else:
                if context == "schema":
                    value.pop("default", None)
                stack.extend((value[name], child_context)
                             for name, child_context in fields[context].items() if name in value)


def _find_external_ref(document: dict[str, Any]) -> str | None:
    for section in ("paths", "webhooks"):
        path_items = document.get(section)
        if not isinstance(path_items, dict):
            continue
        for path, path_item in path_items.items():
            if section == "paths" and (not isinstance(path, str) or not path.startswith("/")):
                continue
            if isinstance(path, str) and path.startswith("x-"):
                continue
            external_ref = _find_path_item_external_ref(path_item)
            if external_ref is not None:
                return external_ref

    components = document.get("components")
    if isinstance(components, dict):
        component_scanners = {
            "schemas": _find_schema_external_ref,
            "responses": _find_response_external_ref,
            "parameters": _find_parameter_external_ref,
            "examples": _find_reference_object_external_ref,
            "requestBodies": _find_request_body_external_ref,
            "headers": _find_parameter_external_ref,
            "securitySchemes": _find_reference_object_external_ref,
            "links": _find_link_external_ref,
            "callbacks": _find_callback_external_ref,
            "pathItems": _find_path_item_external_ref,
        }
        for section, scanner in component_scanners.items():
            values = components.get(section)
            if not isinstance(values, dict):
                continue
            for value in values.values():
                external_ref = scanner(value)
                if external_ref is not None:
                    return external_ref
    return None


def _external_ref(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    reference = value.get("$ref")
    if isinstance(reference, str) and not reference.startswith("#"):
        return reference
    return None


def _find_reference_object_external_ref(value: Any) -> str | None:
    return _external_ref(value)


def _find_schema_external_ref(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    external_ref = _external_ref(schema)
    if external_ref is not None:
        return external_ref

    single_schema_fields = (
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
    )
    for field in single_schema_fields:
        child = schema.get(field)
        children = child if isinstance(child, list) else [child]
        for nested_schema in children:
            external_ref = _find_schema_external_ref(nested_schema)
            if external_ref is not None:
                return external_ref

    schema_map_fields = (
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    )
    for field in schema_map_fields:
        children = schema.get(field)
        if isinstance(children, dict):
            for nested_schema in children.values():
                external_ref = _find_schema_external_ref(nested_schema)
                if external_ref is not None:
                    return external_ref

    schema_array_fields = ("allOf", "anyOf", "oneOf", "prefixItems")
    for field in schema_array_fields:
        children = schema.get(field)
        if isinstance(children, list):
            for nested_schema in children:
                external_ref = _find_schema_external_ref(nested_schema)
                if external_ref is not None:
                    return external_ref

    dependencies = schema.get("dependencies")
    if isinstance(dependencies, dict):
        for dependency in dependencies.values():
            if isinstance(dependency, dict):
                external_ref = _find_schema_external_ref(dependency)
                if external_ref is not None:
                    return external_ref
    return None


def _find_path_item_external_ref(path_item: Any) -> str | None:
    if not isinstance(path_item, dict):
        return None
    external_ref = _external_ref(path_item)
    if external_ref is not None:
        return external_ref

    parameters = path_item.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            external_ref = _find_parameter_external_ref(parameter)
            if external_ref is not None:
                return external_ref

    for method in ("get", "head", "post", "put", "patch", "delete", "options", "trace", "query"):
        external_ref = _find_operation_external_ref(path_item.get(method))
        if external_ref is not None:
            return external_ref

    additional = path_item.get("additionalOperations")
    if isinstance(additional, dict):
        for operation in additional.values():
            external_ref = _find_operation_external_ref(operation)
            if external_ref is not None:
                return external_ref
    return None


def _find_operation_external_ref(operation: Any) -> str | None:
    if not isinstance(operation, dict):
        return None
    parameters = operation.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            external_ref = _find_parameter_external_ref(parameter)
            if external_ref is not None:
                return external_ref

    external_ref = _find_request_body_external_ref(operation.get("requestBody"))
    if external_ref is not None:
        return external_ref

    responses = operation.get("responses")
    if isinstance(responses, dict):
        for response in responses.values():
            external_ref = _find_response_external_ref(response)
            if external_ref is not None:
                return external_ref

    callbacks = operation.get("callbacks")
    if isinstance(callbacks, dict):
        for callback in callbacks.values():
            external_ref = _find_callback_external_ref(callback)
            if external_ref is not None:
                return external_ref
    return None


def _find_parameter_external_ref(parameter: Any) -> str | None:
    if not isinstance(parameter, dict):
        return None
    external_ref = _external_ref(parameter)
    if external_ref is not None:
        return external_ref

    external_ref = _find_schema_external_ref(parameter.get("schema"))
    if external_ref is not None:
        return external_ref

    external_ref = _find_content_external_ref(parameter.get("content"))
    if external_ref is not None:
        return external_ref

    examples = parameter.get("examples")
    if isinstance(examples, dict):
        for example in examples.values():
            external_ref = _find_reference_object_external_ref(example)
            if external_ref is not None:
                return external_ref
    return None


def _find_request_body_external_ref(request_body: Any) -> str | None:
    if not isinstance(request_body, dict):
        return None
    external_ref = _external_ref(request_body)
    if external_ref is not None:
        return external_ref
    return _find_content_external_ref(request_body.get("content"))


def _find_response_external_ref(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    external_ref = _external_ref(response)
    if external_ref is not None:
        return external_ref

    headers = response.get("headers")
    if isinstance(headers, dict):
        for header in headers.values():
            external_ref = _find_parameter_external_ref(header)
            if external_ref is not None:
                return external_ref

    external_ref = _find_content_external_ref(response.get("content"))
    if external_ref is not None:
        return external_ref

    links = response.get("links")
    if isinstance(links, dict):
        for link in links.values():
            external_ref = _find_link_external_ref(link)
            if external_ref is not None:
                return external_ref
    return None


def _find_content_external_ref(content: Any) -> str | None:
    if not isinstance(content, dict):
        return None
    for media_type in content.values():
        if not isinstance(media_type, dict):
            continue
        external_ref = _find_schema_external_ref(media_type.get("schema"))
        if external_ref is not None:
            return external_ref

        examples = media_type.get("examples")
        if isinstance(examples, dict):
            for example in examples.values():
                external_ref = _find_reference_object_external_ref(example)
                if external_ref is not None:
                    return external_ref

        encodings = media_type.get("encoding")
        if isinstance(encodings, dict):
            for encoding in encodings.values():
                if not isinstance(encoding, dict):
                    continue
                headers = encoding.get("headers")
                if isinstance(headers, dict):
                    for header in headers.values():
                        external_ref = _find_parameter_external_ref(header)
                        if external_ref is not None:
                            return external_ref
    return None


def _find_link_external_ref(link: Any) -> str | None:
    return _external_ref(link)


def _find_callback_external_ref(callback: Any) -> str | None:
    if not isinstance(callback, dict):
        return None
    external_ref = _external_ref(callback)
    if external_ref is not None:
        return external_ref
    for expression, path_item in callback.items():
        if isinstance(expression, str) and expression.startswith("x-"):
            continue
        external_ref = _find_path_item_external_ref(path_item)
        if external_ref is not None:
            return external_ref
    return None
