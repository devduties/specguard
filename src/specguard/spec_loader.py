"""Load and validate a local OpenAPI contract."""

from __future__ import annotations

import json
import re
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

    external_ref = _find_external_ref(document)
    if external_ref is not None:
        raise SpecLoadError(
            f"External $ref '{external_ref}' is not supported. Use references within this file."
        )

    try:
        validate(document)
    except Exception as exc:
        raise SpecLoadError(
            f"Invalid OpenAPI spec '{spec_path}': {exc}"
        ) from exc

    return document


def _find_external_ref(value: Any, seen: set[int] | None = None) -> str | None:
    if seen is None:
        seen = set()
    if isinstance(value, dict):
        if id(value) in seen:
            return None
        seen.add(id(value))
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            return ref
        for child in value.values():
            result = _find_external_ref(child, seen)
            if result is not None:
                return result
    elif isinstance(value, list):
        if id(value) in seen:
            return None
        seen.add(id(value))
        for child in value:
            result = _find_external_ref(child, seen)
            if result is not None:
                return result
    return None
