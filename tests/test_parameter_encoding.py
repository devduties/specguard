import json
from urllib.parse import quote

import pytest

from specguard.parameters import ParameterValidationError, validate_parameters


def validate_request(parameter, target="/items/a", headers=(), version="3.1.0"):
    document = {
        "openapi": version,
        "paths": {
            "/items/{id}": {
                "get": {"parameters": [parameter]},
            }
        },
    }
    return validate_parameters(document, target, "GET", headers)


@pytest.mark.parametrize(
    "location,target,headers",
    [
        ("query", "/items/a?v=a%2Cb", {}),
        ("path", "/items/a%2Cb", {}),
    ],
)
def test_encoded_comma_is_data_not_an_array_delimiter(location, target, headers):
    p = {
        "name": "id" if location == "path" else "v",
        "in": location,
        "style": "simple" if location == "path" else "form",
        "explode": False,
        "schema": {"type": "array", "items": {"type": "string"}, "maxItems": 1},
    }
    result = validate_request(p, target, headers)
    assert (result.path if location == "path" else result.query)[p["name"]] == ["a,b"]


@pytest.mark.parametrize(
    "style,explode,value,expected",
    [
        ("simple", False, "a,b", ["a", "b"]),
        ("simple", True, "a,b", ["a", "b"]),
        ("label", False, ".a,b", ["a", "b"]),
        ("label", True, ".a.b", ["a", "b"]),
        ("label", True, ".a%2Eb.c", ["a.b", "c"]),
        ("matrix", False, ";id=a,b", ["a", "b"]),
        ("matrix", True, ";id=a;id=b", ["a", "b"]),
        ("matrix", True, ";id=a%3Bb;id=c", ["a;b", "c"]),
    ],
)
def test_path_array_styles(style, explode, value, expected):
    p = {
        "name": "id",
        "in": "path",
        "style": style,
        "explode": explode,
        "schema": {"type": "array", "items": {"type": "string"}},
    }
    assert validate_request(p, "/items/" + value).path == {"id": expected}


@pytest.mark.parametrize(
    "style,explode,value",
    [
        ("simple", False, "x,1,y,2"),
        ("simple", True, "x=1,y=2"),
        ("label", False, ".x,1,y,2"),
        ("label", True, ".x=1.y=2"),
        ("matrix", False, ";id=x,1,y,2"),
        ("matrix", True, ";x=1;y=2"),
    ],
)
def test_path_object_styles(style, explode, value):
    p = {"name": "id", "in": "path", "style": style, "explode": explode, "schema": object_schema()}
    assert validate_request(p, "/items/" + value).path == {"id": {"x": 1, "y": 2}}


def object_schema():
    return {
        "type": "object",
        "required": ["x", "y"],
        "additionalProperties": False,
        "properties": {"x": {"type": "integer", "minimum": 1}, "y": {"type": "integer"}},
    }


@pytest.mark.parametrize(
    "style,explode,query",
    [
        ("form", False, "?v=x,1,y,2"),
        ("form", True, "?x=1&y=2"),
        ("deepObject", True, "?v%5Bx%5D=1&v%5By%5D=2"),
        ("spaceDelimited", False, "?v=x%201%20y%202"),
        ("pipeDelimited", False, "?v=x%7C1%7Cy%7C2"),
    ],
)
def test_query_objects(style, explode, query):
    p = {"name": "v", "in": "query", "style": style, "explode": explode, "schema": object_schema()}
    assert validate_request(p, "/items/a" + query).query == {"v": {"x": 1, "y": 2}}
    with pytest.raises(ParameterValidationError):
        validate_request(p, "/items/a" + query.replace("1", "0"))


@pytest.mark.parametrize("explode,raw", [(False, "x,1,y,2"), (True, "x=1,y=2")])
def test_header_objects(explode, raw):
    p = {"name": "V", "in": "header", "explode": explode, "schema": object_schema()}
    assert validate_request(p, headers={"v": raw}).headers == {"V": {"x": 1, "y": 2}}


@pytest.mark.parametrize(
    "schema,cookie,expected,explode",
    [
        ({"type": "integer"}, "v=42; other=ignored", 42, True),
        ({"type": "string"}, "v=a+b%2Cc", "a+b,c", True),
        ({"type": "array", "items": {"type": "integer"}}, "v=1,2", [1, 2], False),
        ({"type": "array", "items": {"type": "integer"}}, "v=1; v=2", [1, 2], True),
        (object_schema(), "v=x,1,y,2", {"x": 1, "y": 2}, False),
        (object_schema(), "x=1; y=2", {"x": 1, "y": 2}, True),
    ],
)
def test_cookie_parameters(schema, cookie, expected, explode):
    p = {"name": "v", "in": "cookie", "explode": explode, "schema": schema}
    assert validate_request(p, headers={"Cookie": cookie}).cookies == {"v": expected}


@pytest.mark.parametrize(
    "schema,raw,expected",
    [
        ({"type": ["integer", "null"]}, "null", None),
        ({"type": ["integer", "boolean"]}, "true", True),
        ({"type": ["integer", "boolean"]}, "12", 12),
        ({"type": ["integer", "string"]}, "12", "12"),
        ({"oneOf": [{"type": "integer"}, {"type": "boolean"}]}, "12", 12),
        ({"anyOf": [{"type": "integer", "minimum": 2}, {"type": "boolean"}]}, "false", False),
        ({"allOf": [{"type": "integer"}, {"minimum": 2}]}, "3", 3),
    ],
)
def test_union_and_composed_schemas(schema, raw, expected):
    p = {"name": "v", "in": "query", "schema": schema}
    assert validate_request(p, "/items/a?v=" + raw).query == {"v": expected}


def test_oneof_does_not_accept_value_matching_both_branches():
    p = {"name": "v", "in": "query", "schema": {"oneOf": [{"type": "integer"}, {"type": "number"}]}}
    with pytest.raises(ParameterValidationError):
        validate_request(p, "/items/a?v=2")


def test_nullable_openapi_30():
    p = {"name": "v", "in": "query", "schema": {"type": "integer", "nullable": True}}
    assert validate_request(p, "/items/a?v=null", version="3.0.3").query == {"v": None}


@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
def test_json_content_supports_nested_objects_and_arrays(location):
    schema = {
        "type": "object",
        "required": ["rows"],
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "minimum": 1}},
                    "required": ["n"],
                },
            }
        },
    }
    p = {
        "name": "id" if location == "path" else "v",
        "in": location,
        "content": {"application/json": {"schema": schema}},
    }
    value = {"rows": [{"n": 2}]}
    raw = json.dumps(value, separators=(",", ":"))
    target = "/items/" + quote(raw, safe="") if location == "path" else "/items/a"
    headers = {}
    if location == "query":
        target += "?v=" + quote(raw, safe="")
    if location == "header":
        headers["v"] = raw
    if location == "cookie":
        headers["Cookie"] = "v=" + quote(raw, safe="")
    result = validate_request(p, target, headers)
    output = {
        "path": result.path,
        "query": result.query,
        "header": result.headers,
        "cookie": result.cookies,
    }[location]
    assert output[p["name"]] == value


def test_content_text_is_not_implicitly_converted():
    p = {"name": "v", "in": "query", "content": {"text/plain": {"schema": {"type": "string"}}}}
    assert validate_request(p, "/items/a?v=123").query == {"v": "123"}


def test_raw_path_capture_preserves_unicode_and_encoded_delimiters_in_embedded_template():
    doc = {
        "openapi": "3.1.0",
        "paths": {
            "/files/{id}.json": {
                "get": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "schema": {"type": "array", "items": {"type": "string"}},
                        }
                    ]
                }
            }
        },
    }
    result = validate_parameters(doc, "/files/%C3%A9%2Ca,b.json", "GET")
    assert result.path == {"id": ["é,a", "b"]}
    assert result.match.path_parameters == {"id": "é,a,b"}


def test_custom_content_decoder_is_validated():
    doc = {
        "paths": {
            "/": {
                "get": {
                    "parameters": [
                        {
                            "name": "v",
                            "in": "query",
                            "content": {
                                "application/example": {"schema": {"type": "integer", "minimum": 2}}
                            },
                        }
                    ]
                }
            }
        }
    }
    assert validate_parameters(
        doc, "/?v=3", "GET", content_decoders={"application/example": int}
    ).query == {"v": 3}
    with pytest.raises(ParameterValidationError):
        validate_parameters(doc, "/?v=1", "GET", content_decoders={"application/example": int})


def test_loaded_contract_routes_and_validates_parameters(tmp_path):
    from specguard.spec_loader import load_openapi_spec

    document = {
        "openapi": "3.1.0",
        "info": {"title": "Parameter demo", "version": "1"},
        "paths": {
            "/items/{id}": {
                "get": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        },
                        {
                            "name": "filter",
                            "in": "query",
                            "style": "deepObject",
                            "explode": True,
                            "schema": {"$ref": "#/components/schemas/Filter"},
                        },
                        {
                            "name": "session",
                            "in": "cookie",
                            "required": True,
                            "schema": {"type": "string", "minLength": 3},
                        },
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
        "components": {
            "schemas": {
                "Filter": {
                    "type": "object",
                    "properties": {"active": {"type": "boolean"}},
                    "required": ["active"],
                    "additionalProperties": False,
                }
            }
        },
    }
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document))
    spec = load_openapi_spec(path)
    result = validate_parameters(
        spec, "/items/42?filter%5Bactive%5D=true", "GET", {"Cookie": "session=abc"}
    )
    assert result.path == {"id": 42}
    assert result.query == {"filter": {"active": True}}
    assert result.cookies == {"session": "abc"}


def test_delimited_union_can_use_its_array_branch():
    p = {
        "name": "v",
        "in": "query",
        "style": "spaceDelimited",
        "explode": False,
        "schema": {"oneOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}}]},
    }
    assert validate_request(p, "/items/a?v=1%202").query == {"v": [1, 2]}


@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
@pytest.mark.parametrize("raw", ["1e999", "-1e999", '{"n":1e999}', "[1e999]"])
def test_json_numeric_overflow_is_a_request_error(location, raw):
    name = "id" if location == "path" else "v"
    p = {"name": name, "in": location, "content": {"application/json": {"schema": {}}}}
    target = "/items/" + quote(raw, safe="") if location == "path" else "/items/a"
    headers = {}
    if location == "query":
        target += "?v=" + quote(raw, safe="")
    elif location == "header":
        headers["v"] = raw
    elif location == "cookie":
        headers["Cookie"] = "v=" + quote(raw, safe="")
    with pytest.raises(ParameterValidationError) as caught:
        validate_request(p, target, headers)
    assert caught.value.status_code == 400
    assert caught.value.issues[0].location == location


def test_json_finite_numbers_are_preserved_without_converting_strings():
    p = {"name": "v", "in": "query", "content": {"application/json": {"schema": {}}}}
    raw = '{"large":1e308,"text":"1e999","small":1.25}'
    result = validate_request(p, "/items/a?v=" + quote(raw, safe=""))
    assert result.query == {"v": {"large": 1e308, "text": "1e999", "small": 1.25}}
