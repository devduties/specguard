from copy import deepcopy

import pytest

from specguard.parameters import (
    ParameterDefinitionError,
    ParameterValidationError,
    validate_parameters,
)


def contract(parameters, *, inherited=(), version="3.1.0"):
    return {
        "openapi": version,
        "paths": {
            "/items/{id}": {
                "parameters": list(inherited),
                "get": {"parameters": parameters},
            }
        },
    }


def parameter(name, location="query", **schema):
    return {"name": name, "in": location, "schema": schema or {"type": "string"}}


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0", "3.2.0"])
def test_converts_and_validates_all_three_locations_without_mutation(version):
    doc = contract(
        [
            parameter("id", "path", type="integer", minimum=1),
            parameter("active", type="boolean"),
            parameter("X-Count", "header", type="number", maximum=3),
        ],
        version=version,
    )
    before = deepcopy(doc)
    result = validate_parameters(doc, "/items/2?active=false", "GET", {"x-COUNT": "2.5"})
    assert result.path == {"id": 2}
    assert result.query == {"active": False}
    assert result.headers == {"X-Count": 2.5}
    assert doc == before


def test_operation_parameters_override_inherited_and_keep_other_locations():
    doc = contract(
        [parameter("limit", type="integer", minimum=1)],
        inherited=[
            parameter("limit", type="integer", minimum=10),
            parameter("limit", "header", type="string"),
        ],
    )
    result = validate_parameters(doc, "/items/a?limit=2", "GET", {"LIMIT": "header"})
    assert result.query == {"limit": 2}
    assert result.headers == {"limit": "header"}


def test_collects_missing_and_constraint_errors():
    required = {**parameter("token", "header"), "required": True}
    doc = contract(
        [
            required,
            parameter("id", "path", type="integer", minimum=1),
            parameter("mode", type="string", enum=["fast", "slow"]),
        ]
    )
    with pytest.raises(ParameterValidationError) as caught:
        validate_parameters(doc, "/items/0?mode=other", "GET")
    assert caught.value.status_code == 400
    assert {(i.location, i.name) for i in caught.value.issues} == {
        ("header", "token"),
        ("path", "id"),
        ("query", "mode"),
    }


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("true", "integer"),
        ("1.2", "integer"),
        ("1", "boolean"),
        ("NaN", "number"),
        ("Infinity", "number"),
        ("1e999", "number"),
    ],
)
def test_invalid_scalar_types_are_request_errors(raw, kind):
    with pytest.raises(ParameterValidationError):
        validate_parameters(contract([parameter("v", type=kind)]), f"/items/a?v={raw}", "GET")


def test_optional_missing_is_omitted_and_empty_string_is_present():
    doc = contract(
        [parameter("missing", type="integer"), parameter("empty", type="string", minLength=1)]
    )
    with pytest.raises(ParameterValidationError) as caught:
        validate_parameters(doc, "/items/a?empty=", "GET")
    assert [i.name for i in caught.value.issues] == ["empty"]
    assert validate_parameters(doc, "/items/a", "GET").query == {}


@pytest.mark.parametrize("tail", ["v=1&v=2", "v=x"])
def test_scalar_duplicates_and_bad_integer_are_rejected(tail):
    with pytest.raises(ParameterValidationError):
        validate_parameters(contract([parameter("v", type="integer")]), f"/items/a?{tail}", "GET")


@pytest.mark.parametrize(
    "location,style,explode,raw,headers",
    [
        ("query", "form", True, "?v=1&v=2", {}),
        ("query", "form", False, "?v=1,2", {}),
        ("query", "spaceDelimited", False, "?v=1%202", {}),
        ("query", "pipeDelimited", False, "?v=1%7C2", {}),
        ("header", "simple", False, "", {"V": "1,2"}),
    ],
)
def test_array_serialization(location, style, explode, raw, headers):
    p = {
        **parameter("v", location, type="array", items={"type": "integer"}, minItems=2),
        "style": style,
        "explode": explode,
    }
    result = validate_parameters(contract([p]), "/items/a" + raw, "GET", headers)
    assert (result.query if location == "query" else result.headers) == {"v": [1, 2]}


def test_internal_parameter_and_schema_references():
    doc = contract([{"$ref": "#/components/parameters/Limit"}])
    doc["components"] = {
        "parameters": {"Limit": parameter("n", **{"$ref": "#/components/schemas/Count"})},
        "schemas": {"Count": {"type": "integer", "minimum": 2}},
    }
    assert validate_parameters(doc, "/items/a?n=2", "GET").query == {"n": 2}
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/items/a?n=1", "GET")


@pytest.mark.parametrize("raw", ["%FF", "%GG", "%"])
def test_bad_query_encoding_is_rejected(raw):
    with pytest.raises(ParameterValidationError):
        validate_parameters(contract([]), "/items/a?q=" + raw, "GET")


def test_string_formats_are_checked():
    doc = contract([parameter("day", type="string", format="date")])
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/items/a?day=2026-99-99", "GET")


@pytest.mark.parametrize(
    "p",
    [
        {**parameter("v"), "style": "unknown"},
        {**parameter("v"), "style": "spaceDelimited", "explode": True},
        {**parameter("v", type="object"), "style": "deepObject", "explode": False},
    ],
)
def test_undefined_styles_are_explicit_contract_errors(p):
    with pytest.raises(ParameterDefinitionError):
        validate_parameters(contract([p]), "/items/a", "GET")


def test_reserved_header_definitions_are_ignored():
    p = {**parameter("Authorization", "header"), "required": True}
    assert validate_parameters(contract([p]), "/items/a", "GET").headers == {}


def test_path_item_reference_parameters_are_inherited():
    doc = contract([], inherited=[parameter("id", "path", type="integer")])
    doc["components"] = {"pathItems": {"Item": doc["paths"]["/items/{id}"]}}
    doc["paths"]["/items/{id}"] = {"$ref": "#/components/pathItems/Item"}
    assert validate_parameters(doc, "/items/3", "GET").path == {"id": 3}


@pytest.mark.parametrize("reference", ["other.yaml#/Parameter", "#/missing", "#/loop"])
def test_bad_parameter_references_are_contract_errors(reference):
    doc = contract([{"$ref": reference}])
    doc["loop"] = {"$ref": "#/loop"}
    with pytest.raises(ParameterDefinitionError):
        validate_parameters(doc, "/items/a", "GET")


def test_repeated_headers_cannot_bypass_scalar_validation():
    doc = contract([parameter("X-Count", "header", type="integer")])
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/items/a", "GET", [("x-count", "1"), ("X-COUNT", "2")])


def test_simple_path_array_and_item_constraints():
    doc = contract(
        [parameter("id", "path", type="array", items={"type": "integer", "minimum": 1}, minItems=2)]
    )
    assert validate_parameters(doc, "/items/1,2", "GET").path == {"id": [1, 2]}
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/items/1,0", "GET")


@pytest.mark.parametrize("shape", ["inline", "reference", "allOf", "anyOf", "oneOf"])
def test_closed_exploded_objects_ignore_unrelated_query_fields(shape):
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer", "minimum": 1}},
        "required": ["x"],
        "additionalProperties": False,
    }
    wrapped = {"$ref": "#/components/schemas/Filter"} if shape == "reference" else schema
    if shape in {"allOf", "anyOf", "oneOf"}:
        wrapped = {shape: [schema]}
        if shape == "allOf":
            wrapped[shape].append({"type": "object"})
    doc = contract([{**parameter("filter"), "schema": wrapped}])
    doc["components"] = {"schemas": {"Filter": schema}}
    result = validate_parameters(doc, "/items/a?x=1&unrelated=2", "GET")
    assert result.query == {"filter": {"x": 1}}
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/items/a?x=0&unrelated=2", "GET")


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf"])
def test_union_can_collect_extra_fields_from_its_open_branch(keyword):
    schema = {
        keyword: [
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
                "additionalProperties": False,
            },
            {"type": "object", "additionalProperties": {"type": "string"}},
        ]
    }
    doc = contract([{**parameter("filter"), "schema": schema}, parameter("limit", type="integer")])
    result = validate_parameters(doc, "/items/a?other=value&limit=2", "GET")
    assert result.query == {"filter": {"other": "value"}, "limit": 2}


def test_repeated_array_header_lines_are_combined_in_order():
    doc = contract([parameter("X-Tags", "header", type="array", items={"type": "string"})])
    headers = [("x-tags", "a,b"), ("X-Tags", "c")]
    before = list(headers)
    result = validate_parameters(doc, "/items/a", "GET", headers)
    assert result.headers == {"X-Tags": ["a", "b", "c"]}
    assert headers == before


def test_repeated_array_header_lines_still_check_item_constraints():
    doc = contract(
        [parameter("X-Counts", "header", type="array", items={"type": "integer", "minimum": 1})]
    )
    assert validate_parameters(
        doc, "/items/a", "GET", [("X-Counts", "1"), ("X-Counts", "2")]
    ).headers == {"X-Counts": [1, 2]}
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/items/a", "GET", [("X-Counts", "1"), ("X-Counts", "0")])
