import json

import pytest

from specguard.errors import error_response
from specguard.parameter_schemas import ParameterDefinitionError
from specguard.parameters import ParameterIssue, ParameterValidationError, validate_parameters
from specguard.request_body import (
    BodyDefinitionError,
    BodyIssue,
    BodyValidationError,
    UnsupportedMediaTypeError,
    validate_request_body,
)


def contract():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/orders": {
                "post": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["quantity"],
                                    "properties": {"quantity": {"type": "integer", "minimum": 1}},
                                }
                            }
                        },
                    },
                }
            }
        },
    }


def test_parameter_error_has_stable_json_envelope():
    with pytest.raises(ParameterValidationError) as caught:
        validate_parameters(contract(), "/orders", "POST")
    response = error_response(caught.value)
    assert response.status_code == 400
    assert response.to_dict() == {
        "error": {
            "code": "parameter_validation_failed",
            "message": "Request parameters do not satisfy the contract.",
            "details": [
                {
                    "location": "query",
                    "name": "limit",
                    "path": [],
                    "code": "required",
                    "message": "Required value is missing.",
                }
            ],
        }
    }
    assert json.loads(json.dumps(response.to_dict())) == response.to_dict()


def test_body_error_uses_the_same_shape_and_retains_field_path():
    with pytest.raises(BodyValidationError) as caught:
        validate_request_body(
            contract(), "/orders", "POST", b'{"quantity":0}', content_type="application/json"
        )
    response = error_response(caught.value)
    assert response.status_code == 400
    assert response.to_dict() == {
        "error": {
            "code": "body_validation_failed",
            "message": "Request body does not satisfy the contract.",
            "details": [
                {
                    "location": "body",
                    "name": None,
                    "path": ["quantity"],
                    "code": "minimum",
                    "message": "Value is below the allowed minimum.",
                }
            ],
        }
    }


def test_multiple_issues_keep_their_order_and_array_indexes():
    error = BodyValidationError(
        [
            BodyIssue(("lines", 0, "quantity"), "internal diagnostic", "type"),
            BodyIssue(("lines", 1, "quantity"), "internal diagnostic", "minimum"),
        ]
    )
    details = error_response(error).to_dict()["error"]["details"]
    assert [detail["path"] for detail in details] == [
        ["lines", 0, "quantity"],
        ["lines", 1, "quantity"],
    ]


def test_unsupported_media_type_keeps_415_and_uses_the_envelope():
    with pytest.raises(UnsupportedMediaTypeError) as caught:
        validate_request_body(contract(), "/orders", "POST", b"{}", content_type="text/plain")
    response = error_response(caught.value)
    assert response.status_code == 415
    assert response.code == "unsupported_media_type"
    assert response.to_dict()["error"]["details"][0]["code"] == "unsupported_media_type"


@pytest.mark.parametrize(
    "raw,content_type,code",
    [
        (b"{", "application/json", "invalid_json"),
        (b'"\xff"', "application/json", "invalid_encoding"),
        (None, None, "required"),
    ],
)
def test_body_parse_and_presence_errors_have_stable_codes(raw, content_type, code):
    with pytest.raises(BodyValidationError) as caught:
        validate_request_body(contract(), "/orders", "POST", raw, content_type=content_type)
    assert error_response(caught.value).to_dict()["error"]["details"][0]["code"] == code


@pytest.mark.parametrize("location", ["path", "query", "header", "cookie"])
def test_all_parameter_locations_use_the_same_detail_fields(location):
    error = ParameterValidationError(
        [ParameterIssue(location, "token", "raw diagnostic", "format")]
    )
    assert error_response(error).to_dict()["error"]["details"] == [
        {
            "location": location,
            "name": "token",
            "path": [],
            "code": "format",
            "message": "Value does not match the required format.",
        }
    ]


def test_schema_messages_cannot_expose_submitted_values():
    secret = "private-submitted-value"
    with pytest.raises(BodyValidationError) as caught:
        validate_request_body(
            contract(),
            "/orders",
            "POST",
            json.dumps({"quantity": secret}),
            content_type="application/json",
        )
    assert secret in str(caught.value)
    assert secret not in json.dumps(error_response(caught.value).to_dict())


def test_custom_decoder_failure_cannot_expose_internal_details():
    secret = "private-decoder-detail"
    document = contract()
    document["paths"]["/orders"]["post"]["parameters"] = [
        {
            "name": "value",
            "in": "query",
            "content": {"application/example": {"schema": {}}},
        }
    ]

    def decoder(_text):
        raise ValueError(secret)

    with pytest.raises(ParameterValidationError) as caught:
        validate_parameters(
            document, "/orders?value=x", "POST", content_decoders={"application/example": decoder}
        )
    assert secret in str(caught.value)
    assert secret not in json.dumps(error_response(caught.value).to_dict())


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("private stack trace"),
        BodyDefinitionError("private schema path"),
        ParameterDefinitionError("private contract details"),
    ],
)
def test_internal_and_contract_errors_are_safe_500_responses(error):
    response = error_response(error)
    assert response.status_code == 500
    assert response.to_dict() == {
        "error": {
            "code": "internal_error",
            "message": "An internal error occurred.",
            "details": [],
        }
    }


def test_unknown_detail_code_and_message_are_not_reflected():
    error = BodyValidationError([BodyIssue((), "private diagnostic", "private-code")])
    detail = error_response(error).to_dict()["error"]["details"][0]
    assert detail["code"] == "invalid_value"
    assert "private" not in json.dumps(detail)


def test_existing_exception_diagnostics_and_issues_are_preserved():
    issue = ParameterIssue("query", "limit", "Required parameter is missing.")
    error = ParameterValidationError([issue])
    assert isinstance(error, ValueError)
    assert error.issues == (issue,)
    assert str(error) == "query.limit: Required parameter is missing."


def test_serializing_a_response_does_not_mutate_exception_details():
    error = BodyValidationError([BodyIssue(("lines", 0), "diagnostic", "type")])
    response = error_response(error)
    first = response.to_dict()
    first["error"]["details"][0]["path"].append("changed")
    assert response.to_dict()["error"]["details"][0]["path"] == ["lines", 0]
