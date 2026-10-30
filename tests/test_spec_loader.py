from __future__ import annotations

import json
from pathlib import Path

import pytest

from specguard.spec_loader import SpecLoadError, load_openapi_spec


def _valid_spec(version: str = "3.0.3") -> dict[str, object]:
    return {
        "openapi": version,
        "info": {"title": "Example API", "version": "1.0.0"},
        "paths": {
            "/health": {"get": {"responses": {"200": {"description": "healthy"}}}}
        },
    }


def test_loads_valid_json_spec(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    expected = _valid_spec()
    spec_path.write_text(json.dumps(expected), encoding="utf-8")

    assert load_openapi_spec(spec_path) == expected


def test_loads_valid_yaml_spec(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.yaml"
    spec_path.write_text(
        "openapi: 3.1.0\n"
        "info:\n  title: Example API\n  version: 1.0.0\n"
        "paths:\n  /health:\n    get:\n      responses:\n"
        "        '200':\n          description: healthy\n",
        encoding="utf-8",
    )

    assert load_openapi_spec(spec_path)["openapi"] == "3.1.0"


def test_loads_openapi_3_2_spec(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(_valid_spec("3.2.0")), encoding="utf-8")

    assert load_openapi_spec(spec_path)["openapi"] == "3.2.0"


def test_loads_internal_references(tmp_path: Path) -> None:
    spec = _valid_spec("3.1.0")
    spec["components"] = {"schemas": {"Health": {"type": "string"}}}
    spec["paths"]["/health"]["get"]["responses"]["200"]["content"] = {
        "application/json": {"schema": {"$ref": "#/components/schemas/Health"}}
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    assert load_openapi_spec(spec_path)["components"] == spec["components"]


def test_reports_invalid_json_with_location(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text('{"openapi": }', encoding="utf-8")

    with pytest.raises(SpecLoadError, match="Invalid JSON.*line 1"):
        load_openapi_spec(spec_path)


def test_rejects_invalid_openapi_document(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps({"openapi": "3.0.3"}), encoding="utf-8")

    with pytest.raises(SpecLoadError, match="Invalid OpenAPI spec"):
        load_openapi_spec(spec_path)


def test_rejects_unsupported_openapi_version(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(_valid_spec("2.0")), encoding="utf-8")

    with pytest.raises(SpecLoadError, match="supports OpenAPI 3.0, 3.1, and 3.2"):
        load_openapi_spec(spec_path)


def test_rejects_external_references(tmp_path: Path) -> None:
    spec = _valid_spec()
    spec["components"] = {"schemas": {"Health": {"$ref": "schemas/health.yaml"}}}
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(SpecLoadError, match=r"External \$ref.*not supported"):
        load_openapi_spec(spec_path)


def test_rejects_unsupported_file_extension(tmp_path: Path) -> None:
    with pytest.raises(SpecLoadError, match="Use .json, .yaml, or .yml"):
        load_openapi_spec(tmp_path / "openapi.txt")


def test_reports_missing_spec_file(tmp_path: Path) -> None:
    with pytest.raises(SpecLoadError, match="Cannot read OpenAPI spec"):
        load_openapi_spec(tmp_path / "missing.yaml")
