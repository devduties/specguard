"""Load and validate a local OpenAPI contract."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from openapi_schema_validator import OAS30Validator, oas30_format_checker
from openapi_spec_validator import validate
from referencing import Registry

from specguard.spec_objects import iter_spec_objects


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
    external_ref = _find_external_ref(document)
    if external_ref is not None:
        raise SpecLoadError(
            f"External $ref '{external_ref}' is not supported. Use references within this file."
        )

    try:
        validate(_validation_copy(document))
        if version.startswith("3.0."):
            _validate_defaults(document)
    except Exception as exc:
        raise SpecLoadError(f"Invalid OpenAPI spec '{spec_path}': {exc}") from exc

    return document


def _validation_copy(document: dict[str, Any]) -> dict[str, Any]:
    # The dependency treats $ref inside defaults as references. Remove schema
    # defaults from its copy and check 3.0 defaults separately as payload data.
    validation_document = deepcopy(document)
    for item in iter_spec_objects(validation_document):
        if item.kind == "schema":
            item.value.pop("default", None)
        elif item.kind in {"responses", "webhooks", "callback"}:
            # These maps allow extension entries which the dependency may
            # otherwise interpret as response or path definitions.
            for name in list(item.value):
                if str(name).startswith("x-"):
                    del item.value[name]
    return validation_document


def _validate_defaults(document: dict[str, Any]) -> None:
    validator = OAS30Validator(document, registry=Registry(), format_checker=oas30_format_checker)
    for item in iter_spec_objects(document):
        if item.kind == "schema" and "default" in item.value:
            validator.evolve(schema=item.value).validate(item.value["default"])


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


def _find_external_ref(document: dict[str, Any]) -> str | None:
    for item in iter_spec_objects(document):
        reference = item.value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            return reference
    return None
