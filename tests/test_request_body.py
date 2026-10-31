import json
from copy import deepcopy

import pytest

from specguard.parameters import validate_parameters
from specguard.request_body import (
    BodyDefinitionError,
    BodyValidationError,
    UnsupportedMediaTypeError,
    validate_request_body,
)
from specguard.routing import MethodNotAllowedError, RouteNotFoundError
from specguard.spec_loader import load_openapi_spec


def contract(schema, *, required=True, version="3.1.0", media_type="application/json"):
    return {
        "openapi": version,
        "info": {"title": "Orders", "version": "1"},
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "required": required,
                        "content": {media_type: {"schema": schema}},
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }


def order_schema():
    return {
        "type": "object",
        "required": ["name", "quantity"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "quantity": {"type": "integer", "minimum": 1},
            "status": {"type": "string", "enum": ["draft", "confirmed"], "default": "draft"},
        },
        "additionalProperties": False,
    }


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0", "3.2.0"])
def test_valid_body_is_parsed_without_mutation_or_defaults(version):
    document = contract(order_schema(), version=version)
    before = deepcopy(document)
    raw = b'{"name":"coffee","quantity":2}'
    result = validate_request_body(
        document, "/orders?notify=true", "POST", raw, content_type="Application/JSON; charset=utf-8"
    )
    assert result.present is True
    assert result.value == {"name": "coffee", "quantity": 2}
    assert result.media_type == "application/json"
    assert result.match.method == "post"
    assert document == before
    assert raw == b'{"name":"coffee","quantity":2}'


@pytest.mark.parametrize("body", [None, b"", ""])
def test_required_body_rejects_missing_or_empty_input(body):
    with pytest.raises(BodyValidationError, match="Required request body is missing") as caught:
        validate_request_body(contract({}), "/orders", "POST", body)
    assert caught.value.status_code == 400
    assert caught.value.issues[0].path == ()


@pytest.mark.parametrize("required", [False, None])
def test_optional_body_can_be_absent(required):
    document = contract(order_schema(), required=required)
    if required is None:
        del document["paths"]["/orders"]["post"]["requestBody"]["required"]
    result = validate_request_body(document, "/orders", "POST")
    assert result.present is False
    assert result.value is None
    assert result.media_type is None


def test_optional_body_is_validated_when_present():
    with pytest.raises(BodyValidationError):
        validate_request_body(
            contract(order_schema(), required=False),
            "/orders",
            "POST",
            b"{}",
            content_type="application/json",
        )


@pytest.mark.parametrize(
    "raw",
    [
        b" ",
        b"{",
        b'{"name":}',
        b"{}{}",
        b"[1,]",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1e999",
        b'{"n":[-1e999]}',
    ],
)
def test_malformed_and_nonfinite_json_are_request_errors(raw):
    with pytest.raises(BodyValidationError, match="Invalid JSON request body"):
        validate_request_body(contract({}), "/orders", "POST", raw, content_type="application/json")


@pytest.mark.parametrize("raw", [b'"\xff"', '{"x":1}'.encode("utf-16")])
def test_json_bytes_must_be_utf8(raw):
    with pytest.raises(BodyValidationError):
        validate_request_body(contract({}), "/orders", "POST", raw, content_type="application/json")


def test_unicode_json_is_preserved():
    raw = '{"name":"café","quantity":1}'.encode()
    result = validate_request_body(
        contract(order_schema()), "/orders", "POST", raw, content_type="application/json"
    )
    assert result.value["name"] == "café"


@pytest.mark.parametrize("raw", [b"null", b"[]", b"true", b"12", b'"text"'])
def test_schema_rejects_wrong_top_level_types(raw):
    with pytest.raises(BodyValidationError):
        validate_request_body(
            contract(order_schema()), "/orders", "POST", raw, content_type="application/json"
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"name":"x"}',
        b'{"name":"x","quantity":"2"}',
        b'{"name":"","quantity":0,"status":"bad","extra":true}',
    ],
)
def test_required_properties_types_enums_and_constraints(raw):
    with pytest.raises(BodyValidationError) as caught:
        validate_request_body(
            contract(order_schema()), "/orders", "POST", raw, content_type="application/json"
        )
    assert caught.value.issues


def test_nested_errors_include_field_and_array_index_paths():
    schema = {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": order_schema(),
                "minItems": 1,
            }
        },
    }
    raw = b'{"lines":[{"name":"","quantity":0}]}'
    with pytest.raises(BodyValidationError) as caught:
        validate_request_body(
            contract(schema), "/orders", "POST", raw, content_type="application/json"
        )
    assert {issue.path for issue in caught.value.issues} == {
        ("lines", 0, "name"),
        ("lines", 0, "quantity"),
    }


@pytest.mark.parametrize(
    "version,schema",
    [
        ("3.0.3", {"type": "object", "nullable": True}),
        ("3.1.0", {"type": ["object", "null"]}),
        ("3.2.0", {"type": ["object", "null"]}),
    ],
)
def test_json_null_is_present_and_can_satisfy_nullable_schema(version, schema):
    result = validate_request_body(
        contract(schema, version=version),
        "/orders",
        "POST",
        b"null",
        content_type="application/json",
    )
    assert result.present is True
    assert result.value is None


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "json",
        "text/plain",
        "application/xml",
        "application/*",
        # Valid JSON suffix type, but not declared in this contract's content map.
        # test_declared_json_suffix_media_type_is_supported covers the declared case.
        "application/problem+json",
    ],
)
def test_missing_or_undeclared_content_type_returns_415(content_type):
    with pytest.raises(UnsupportedMediaTypeError) as caught:
        validate_request_body(contract({}), "/orders", "POST", b"{}", content_type=content_type)
    assert caught.value.status_code == 415


def test_declared_json_suffix_media_type_is_supported():
    result = validate_request_body(
        contract({}, media_type="application/problem+json"),
        "/orders",
        "POST",
        b"{}",
        content_type="application/problem+json",
    )
    assert result.media_type == "application/problem+json"


@pytest.mark.parametrize("media_type", ["application/*", "*/*"])
def test_declared_media_range_accepts_json(media_type):
    result = validate_request_body(
        contract(order_schema(), media_type=media_type),
        "/orders",
        "POST",
        b'{"name":"x","quantity":1}',
        content_type="application/json",
    )
    assert result.value["quantity"] == 1


def test_specific_media_entry_wins_over_permissive_ranges():
    document = contract(order_schema())
    content = document["paths"]["/orders"]["post"]["requestBody"]["content"]
    content.update({"application/*": {"schema": {}}, "*/*": {"schema": {}}})
    with pytest.raises(BodyValidationError):
        validate_request_body(document, "/orders", "POST", b"{}", content_type="application/json")
    del content["application/json"]
    content["application/*"]["schema"] = order_schema()
    with pytest.raises(BodyValidationError):
        validate_request_body(document, "/orders", "POST", b"{}", content_type="application/json")


def test_non_json_body_is_not_supported_even_if_declared():
    with pytest.raises(UnsupportedMediaTypeError):
        validate_request_body(
            contract({}, media_type="text/plain"),
            "/orders",
            "POST",
            b"hi",
            content_type="text/plain",
        )


def test_undeclared_body_is_rejected_but_absence_is_allowed():
    document = contract({})
    del document["paths"]["/orders"]["post"]["requestBody"]
    assert validate_request_body(document, "/orders", "POST").present is False
    with pytest.raises(BodyValidationError, match="does not declare"):
        validate_request_body(document, "/orders", "POST", b"{}", content_type="application/json")


@pytest.mark.parametrize(
    "schema,raw",
    [
        ({}, b"[1,true,null]"),
        ({"type": "number"}, b"2.5"),
        ({"type": "boolean"}, b"false"),
        ({"type": "array", "items": {"type": "integer"}}, b"[1,2]"),
    ],
)
def test_json_values_need_not_be_objects(schema, raw):
    assert validate_request_body(
        contract(schema), "/orders", "POST", raw, content_type="application/json"
    ).value == json.loads(raw)


@pytest.mark.parametrize("schema", [False, {"oneOf": [{"type": "integer"}, {"type": "number"}]}])
def test_boolean_and_composed_schemas_are_enforced(schema):
    with pytest.raises(BodyValidationError):
        validate_request_body(
            contract(schema), "/orders", "POST", b"2", content_type="application/json"
        )


def test_known_formats_are_checked():
    schema = {"type": "string", "format": "date"}
    with pytest.raises(BodyValidationError):
        validate_request_body(
            contract(schema), "/orders", "POST", b'"2026-99-99"', content_type="application/json"
        )


def test_openapi_30_body_checks_request_specific_required_fields():
    schema = {
        "type": "object",
        "required": ["id", "password"],
        "properties": {
            "id": {"type": "integer", "readOnly": True},
            "password": {"type": "string", "writeOnly": True},
        },
    }
    document = contract(schema, version="3.0.3")
    assert validate_request_body(
        document, "/orders", "POST", b'{"password":"test"}', content_type="application/json"
    ).present
    with pytest.raises(BodyValidationError):
        validate_request_body(document, "/orders", "POST", b"{}", content_type="application/json")


@pytest.mark.parametrize("reference", ["outside.yaml", "#/missing", "#/loop"])
def test_invalid_request_body_references_are_contract_errors(reference):
    document = contract({})
    document["loop"] = {"$ref": "#/loop"}
    document["paths"]["/orders"]["post"]["requestBody"] = {"$ref": reference}
    with pytest.raises(BodyDefinitionError):
        validate_request_body(document, "/orders", "POST", b"{}", content_type="application/json")


@pytest.mark.parametrize("reference", ["outside.yaml", "#/missing"])
def test_unresolved_schema_reference_is_a_contract_error(reference):
    with pytest.raises(BodyDefinitionError):
        validate_request_body(
            contract({"$ref": reference}), "/orders", "POST", b"{}", content_type="application/json"
        )


def test_loaded_contract_validates_parameters_and_referenced_body(tmp_path):
    document = contract({"$ref": "#/components/schemas/Order"})
    definition = document["paths"]["/orders"]["post"].pop("requestBody")
    document["paths"]["/orders"]["post"]["requestBody"] = {
        "$ref": "#/components/requestBodies/Order"
    }
    document["paths"]["/orders"]["post"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    ]
    document["components"] = {
        "schemas": {"Order": order_schema()},
        "requestBodies": {"Order": definition},
    }
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    spec = load_openapi_spec(path)
    assert validate_parameters(spec, "/orders?limit=2", "POST").query == {"limit": 2}
    result = validate_request_body(
        spec,
        "/orders?limit=2",
        "POST",
        b'{"name":"x","quantity":2}',
        content_type="application/json",
    )
    assert result.value == {"name": "x", "quantity": 2}
    with pytest.raises(BodyValidationError):
        validate_request_body(
            spec, "/orders", "POST", b'{"name":"x","quantity":0}', content_type="application/json"
        )


def test_routing_errors_keep_their_existing_types():
    with pytest.raises(RouteNotFoundError):
        validate_request_body(contract({}), "/missing", "POST")
    with pytest.raises(MethodNotAllowedError):
        validate_request_body(contract({}), "/orders", "GET")


def test_openapi_32_referenced_media_type_preserves_schema_constraints():
    document = contract({}, version="3.2.0")
    document["components"] = {"mediaTypes": {"Order": {"schema": order_schema()}}}
    document["paths"]["/orders"]["post"]["requestBody"]["content"]["application/json"] = {
        "$ref": "#/components/mediaTypes/Order"
    }
    with pytest.raises(BodyValidationError):
        validate_request_body(document, "/orders", "POST", b"{}", content_type="application/json")


def test_malformed_content_type_cannot_match_wildcard():
    with pytest.raises(UnsupportedMediaTypeError):
        validate_request_body(
            contract({}, media_type="*/*"),
            "/orders",
            "POST",
            b"{}",
            content_type="invalid type/json",
        )
