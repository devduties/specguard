from __future__ import annotations

import json
from pathlib import Path

import pytest

from specguard.spec_loader import SpecLoadError, load_openapi_spec


def _valid_spec(version: str = "3.0.3") -> dict[str, object]:
    return {
        "openapi": version,
        "info": {"title": "Example API", "version": "1.0.0"},
        "paths": {"/health": {"get": {"responses": {"200": {"description": "healthy"}}}}},
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


def test_keeps_example_default_and_extension_payloads_as_data(tmp_path: Path) -> None:
    spec = _valid_spec("3.1.0")
    spec["x-example-payload"] = {"$ref": "customer-provided-extension-value"}
    spec["components"] = {
        "schemas": {
            "Payload": {
                "type": "object",
                "properties": {
                    "metadata": {
                        "type": "object",
                        "default": {"$ref": "customer-provided-default-value"},
                    }
                },
            }
        }
    }
    response = spec["paths"]["/health"]["get"]["responses"]["200"]
    response["content"] = {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/Payload"},
            "example": {"$ref": "customer-provided-example-value"},
        }
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    assert load_openapi_spec(spec_path) == spec


def test_rejects_unsupported_file_extension(tmp_path: Path) -> None:
    with pytest.raises(SpecLoadError, match="Use .json, .yaml, or .yml"):
        load_openapi_spec(tmp_path / "openapi.txt")


def test_reports_missing_spec_file(tmp_path: Path) -> None:
    with pytest.raises(SpecLoadError, match="Cannot read OpenAPI spec"):
        load_openapi_spec(tmp_path / "missing.yaml")


def test_preserves_default_response_and_server_variable(tmp_path: Path) -> None:
    spec = _valid_spec("3.1.0")
    spec["paths"]["/health"]["get"]["responses"] = {"default": {"description": "fallback"}}
    spec["servers"] = [{"url": "https://{host}", "variables": {"host": {"default": "api.test"}}}]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert load_openapi_spec(path) == spec


def test_validates_schema_property_named_default(tmp_path: Path) -> None:
    spec = _valid_spec("3.1.0")
    spec["components"] = {
        "schemas": {
            "Payload": {"type": "object", "properties": {"default": {"type": "invalid-type"}}}
        }
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(SpecLoadError, match="Invalid OpenAPI spec"):
        load_openapi_spec(path)


def test_rejects_cyclic_yaml_alias_with_load_error(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(
        "openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
        "components:\n  schemas:\n    Node: &node\n      type: object\n"
        "      properties:\n        child: *node\n",
        encoding="utf-8",
    )
    with pytest.raises(SpecLoadError, match="Cyclic YAML aliases"):
        load_openapi_spec(path)


def test_accepts_shared_acyclic_yaml_alias(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(
        "openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
        "components:\n  schemas:\n    First: &schema {type: string}\n    Second: *schema\n",
        encoding="utf-8",
    )
    assert load_openapi_spec(path)["components"]["schemas"]["Second"] == {"type": "string"}


@pytest.mark.parametrize("location", ["component", "inline", "nested"])
def test_rejects_invalid_openapi_3_0_default(tmp_path: Path, location: str) -> None:
    spec = _valid_spec("3.0.3")
    schema = {"type": "integer", "default": "not-an-integer"}
    if location == "component":
        spec["components"] = {"schemas": {"Count": schema}}
    else:
        if location == "nested":
            schema = {"type": "object", "properties": {"count": schema}}
        spec["paths"]["/health"]["get"]["responses"]["200"]["content"] = {
            "application/json": {"schema": schema}
        }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(SpecLoadError, match="Invalid OpenAPI spec"):
        load_openapi_spec(path)


def test_preserves_valid_openapi_3_0_default(tmp_path: Path) -> None:
    spec = _valid_spec("3.0.3")
    spec["components"] = {"schemas": {"Count": {"type": "integer", "default": 42}}}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert load_openapi_spec(path) == spec


@pytest.mark.parametrize("version", ["3.1.0", "3.2.0"])
def test_json_schema_default_is_an_annotation(tmp_path: Path, version: str) -> None:
    spec = _valid_spec(version)
    spec["components"] = {"schemas": {"Count": {"type": "integer", "default": "annotation"}}}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert load_openapi_spec(path) == spec


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0", "3.2.0"])
def test_default_ref_field_is_payload_data(tmp_path: Path, version: str) -> None:
    spec = _valid_spec(version)
    spec["components"] = {
        "schemas": {
            "Payload": {
                "type": "object",
                "properties": {"$ref": {"type": "string"}},
                "default": {"$ref": "customer-provided-value"},
            }
        }
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert load_openapi_spec(path) == spec


@pytest.mark.parametrize("count", [42, "invalid"])
def test_default_validation_resolves_local_schema_references(tmp_path: Path, count: object) -> None:
    spec = _valid_spec("3.0.3")
    spec["components"] = {
        "schemas": {
            "Count": {"type": "integer"},
            "Payload": {
                "type": "object",
                "properties": {"count": {"$ref": "#/components/schemas/Count"}},
                "default": {"count": count},
            },
        }
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    if count == "invalid":
        with pytest.raises(SpecLoadError, match="Invalid OpenAPI spec"):
            load_openapi_spec(path)
    else:
        assert load_openapi_spec(path) == spec


@pytest.mark.parametrize("location", ["schema", "itemSchema", "component"])
def test_rejects_external_references_in_media_schemas(tmp_path: Path, location: str) -> None:
    spec = _valid_spec("3.2.0")
    media = {"itemSchema" if location == "component" else location: {"$ref": "outside.yaml"}}
    if location == "component":
        spec["components"] = {"mediaTypes": {"Events": media}}
    else:
        spec["paths"]["/health"]["get"]["responses"]["200"]["content"] = {
            "application/jsonl": media
        }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(SpecLoadError, match=r"External \$ref.*outside.yaml"):
        load_openapi_spec(path)


@pytest.mark.parametrize("section", ["parameters", "responses", "callbacks", "pathItems"])
def test_rejects_external_references_in_component_objects(tmp_path: Path, section: str) -> None:
    spec = _valid_spec("3.1.0")
    spec["components"] = {section: {"Shared": {"$ref": "outside.yaml"}}}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(SpecLoadError, match=r"External \$ref.*outside.yaml"):
        load_openapi_spec(path)


def test_response_and_webhook_extensions_remain_payload_data(tmp_path: Path) -> None:
    spec = _valid_spec("3.1.0")
    spec["webhooks"] = {"x-payload": {"$ref": "literal-extension"}}
    spec["paths"]["/health"]["get"]["responses"]["x-payload"] = {
        "$ref": "literal-extension",
        "content": {"application/json": {"schema": {"$ref": "also-literal"}}},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert load_openapi_spec(path) == spec


def test_schema_property_with_x_prefix_is_still_validated(tmp_path: Path) -> None:
    spec = _valid_spec("3.1.0")
    spec["components"] = {
        "schemas": {
            "Payload": {
                "type": "object",
                "properties": {"x-count": {"$ref": "outside.yaml"}},
            }
        }
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(SpecLoadError, match=r"External \$ref.*outside.yaml"):
        load_openapi_spec(path)
