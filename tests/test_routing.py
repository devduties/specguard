import subprocess
import sys

import pytest

from specguard.routing import (
    AmbiguousRouteError,
    InvalidRequestTargetError,
    MethodNotAllowedError,
    PathItemReferenceError,
    RouteNotFoundError,
    match_operation,
    resolve_operation,
)


def test_matches_method_and_extracts_path_parameters() -> None:
    document = {
        "paths": {
            "/orders/{order_id}": {"get": {"operationId": "getOrder"}},
        }
    }

    match = match_operation(document, "/orders/ord-42", "GET")

    assert match is not None
    assert match.path_template == "/orders/{order_id}"
    assert match.method == "get"
    assert match.operation["operationId"] == "getOrder"
    assert match.path_parameters == {"order_id": "ord-42"}


def test_prefers_literal_route_to_parameterized_route() -> None:
    document = {
        "paths": {
            "/reports/{report_id}": {"get": {"operationId": "getReport"}},
            "/reports/latest": {"get": {"operationId": "getLatestReport"}},
        }
    }

    match = match_operation(document, "/reports/latest", "GET")

    assert match is not None
    assert match.path_template == "/reports/latest"
    assert match.operation["operationId"] == "getLatestReport"


@pytest.mark.parametrize(
    "paths",
    [
        {
            "/a/{x}": {"get": {"operationId": "leftLiteral"}},
            "/{y}/b": {"get": {"operationId": "rightLiteral"}},
        },
        {
            "/{y}/b": {"get": {"operationId": "rightLiteral"}},
            "/a/{x}": {"get": {"operationId": "leftLiteral"}},
        },
    ],
)
def test_prefers_leftmost_literal_segment_independent_of_spec_order(
    paths: dict[str, dict[str, dict[str, str]]],
) -> None:
    match = resolve_operation({"paths": paths}, "/a/b", "GET")

    assert match.path_template == "/a/{x}"
    assert match.operation["operationId"] == "leftLiteral"


@pytest.mark.parametrize(
    "paths",
    [
        {
            "/a/{x}": {"get": {"operationId": "first"}},
            "/a/{y}": {"get": {"operationId": "second"}},
        },
        {
            "/a/{y}": {"get": {"operationId": "second"}},
            "/a/{x}": {"get": {"operationId": "first"}},
        },
    ],
)
def test_reports_ambiguous_routes_instead_of_using_spec_order(
    paths: dict[str, dict[str, dict[str, str]]],
) -> None:
    with pytest.raises(AmbiguousRouteError) as error:
        resolve_operation({"paths": paths}, "/a/b", "GET")

    assert str(error.value) == "Ambiguous OpenAPI routes match '/a/b': '/a/{x}', '/a/{y}'."


def test_match_operation_propagates_ambiguous_route_error() -> None:
    document = {
        "paths": {
            "/a/{x}": {"get": {"operationId": "first"}},
            "/a/{y}": {"get": {"operationId": "second"}},
        }
    }

    with pytest.raises(AmbiguousRouteError):
        match_operation(document, "/a/b", "GET")


def test_returns_none_when_method_is_not_defined() -> None:
    document = {"paths": {"/orders": {"get": {"operationId": "listOrders"}}}}

    assert match_operation(document, "/orders", "POST") is None


def test_returns_none_when_path_is_not_defined() -> None:
    document = {"paths": {"/orders": {"get": {"operationId": "listOrders"}}}}

    assert match_operation(document, "/customers", "GET") is None


def test_ignores_query_string_and_decodes_path_parameter() -> None:
    document = {"paths": {"/orders/{order_id}": {"get": {"operationId": "getOrder"}}}}

    match = resolve_operation(document, "/orders/order%2F42?expand=items", "GET")

    assert match.path_parameters == {"order_id": "order/42"}


def test_preserves_leading_double_slash_in_origin_form_request_target() -> None:
    document = {
        "paths": {
            "//tenant/health": {"get": {"operationId": "tenantHealth"}},
            "/health": {"get": {"operationId": "health"}},
        }
    }

    match = resolve_operation(document, "//tenant/health?full=true", "GET")

    assert match.path_template == "//tenant/health"
    assert match.operation["operationId"] == "tenantHealth"


def test_extracts_path_from_absolute_request_url() -> None:
    document = {"paths": {"/health": {"get": {"operationId": "health"}}}}

    match = resolve_operation(document, "https://api.example/health?full=true", "GET")

    assert match.path_template == "/health"


def test_skips_non_path_extension_fields() -> None:
    document = {"paths": {"x-internal": {"get": {"operationId": "internal"}}}}

    with pytest.raises(RouteNotFoundError):
        resolve_operation(document, "/x-internal", "GET")


def test_trailing_slash_is_not_treated_as_the_same_route() -> None:
    document = {"paths": {"/orders": {"get": {"operationId": "listOrders"}}}}

    with pytest.raises(RouteNotFoundError) as error:
        resolve_operation(document, "/orders/", "GET")

    assert error.value.status_code == 404


def test_distinguishes_path_not_found_from_method_not_allowed() -> None:
    document = {"paths": {"/orders": {"get": {"operationId": "listOrders"}}}}

    with pytest.raises(RouteNotFoundError) as missing_path:
        resolve_operation(document, "/customers", "GET")
    with pytest.raises(MethodNotAllowedError) as wrong_method:
        resolve_operation(document, "/orders", "POST")

    assert missing_path.value.status_code == 404
    assert wrong_method.value.status_code == 405
    assert wrong_method.value.allowed_methods == ("GET",)


def test_literal_route_is_selected_before_method_check() -> None:
    document = {
        "paths": {
            "/reports/{report_id}": {"get": {"operationId": "getReport"}},
            "/reports/latest": {"post": {"operationId": "refreshLatestReport"}},
        }
    }

    with pytest.raises(MethodNotAllowedError) as error:
        resolve_operation(document, "/reports/latest", "GET")

    assert error.value.allowed_methods == ("POST",)


def test_resolves_internal_path_item_reference() -> None:
    document = {
        "paths": {"/health": {"$ref": "#/components/pathItems/Health"}},
        "components": {
            "pathItems": {"Health": {"get": {"operationId": "health"}}}
        },
    }

    match = resolve_operation(document, "/health", "GET")

    assert match.operation["operationId"] == "health"


def test_resolves_escaped_tokens_in_path_item_reference() -> None:
    document = {
        "paths": {"/health": {"$ref": "#/components/pathItems/Health~1Live~0"}},
        "components": {
            "pathItems": {"Health/Live~": {"get": {"operationId": "health"}}}
        },
    }

    match = resolve_operation(document, "/health", "GET")

    assert match.operation["operationId"] == "health"


def test_missing_path_item_reference_target_has_clear_error() -> None:
    document = {"paths": {"/health": {"$ref": "#/components/pathItems/Missing"}}}

    with pytest.raises(PathItemReferenceError, match="does not resolve"):
        resolve_operation(document, "/health", "GET")


def test_path_item_reference_cycle_has_clear_error() -> None:
    document = {
        "paths": {
            "/a": {"$ref": "#/paths/~1b"},
            "/b": {"$ref": "#/paths/~1a"},
        }
    }

    with pytest.raises(PathItemReferenceError, match="Cyclic Path Item references"):
        resolve_operation(document, "/a", "GET")


def test_matches_and_extracts_parameters_embedded_in_path_segments() -> None:
    document = {
        "paths": {
            "/files/{year}-{name}.json": {
                "get": {"operationId": "getArchiveFile"}
            }
        }
    }

    match = resolve_operation(document, "/files/2026-report.json", "GET")

    assert match.path_parameters == {"year": "2026", "name": "report"}


def test_literal_suffix_in_embedded_template_wins_over_broad_parameter() -> None:
    document = {
        "paths": {
            "/files/{name}": {"get": {"operationId": "getAnyFile"}},
            "/files/{name}.json": {"get": {"operationId": "getJsonFile"}},
        }
    }

    match = resolve_operation(document, "/files/report.json", "GET")

    assert match.path_template == "/files/{name}.json"
    assert match.path_parameters == {"name": "report"}


def test_resolves_query_and_additional_operations_preserving_method_case() -> None:
    document = {
        "paths": {
            "/items": {
                "get": {"operationId": "getItems"},
                "query": {"operationId": "queryItems"},
                "additionalOperations": {"COPY": {"operationId": "copyItems"}},
            }
        }
    }

    query = resolve_operation(document, "/items", "QUERY")
    copy = resolve_operation(document, "/items", "COPY")

    assert query.method == "query"
    assert query.operation["operationId"] == "queryItems"
    assert copy.method == "COPY"
    assert copy.operation["operationId"] == "copyItems"


def test_allowed_methods_include_query_and_additional_operations() -> None:
    document = {
        "paths": {
            "/items": {
                "get": {"operationId": "getItems"},
                "query": {"operationId": "queryItems"},
                "additionalOperations": {"COPY": {"operationId": "copyItems"}},
            }
        }
    }

    with pytest.raises(MethodNotAllowedError) as error:
        resolve_operation(document, "/items", "DELETE")

    assert error.value.allowed_methods == ("GET", "QUERY", "COPY")


@pytest.mark.parametrize("request_path", ["/items/%FF", "/items/%GG"])
def test_rejects_invalid_percent_encoded_request_path(request_path: str) -> None:
    document = {"paths": {"/items/�": {"get": {"operationId": "replacement"}}}}

    with pytest.raises(InvalidRequestTargetError) as error:
        resolve_operation(document, request_path, "GET")

    assert error.value.status_code == 400


def test_encoded_newline_does_not_match_literal_route() -> None:
    document = {"paths": {"/health": {"get": {}}}}
    with pytest.raises(RouteNotFoundError):
        resolve_operation(document, "/health%0A", "GET")


@pytest.mark.parametrize("encoded, decoded", [("a%0A", "a\n"), ("a%0Ab", "a\nb")])
def test_encoded_newline_is_preserved_in_parameter(encoded: str, decoded: str) -> None:
    document = {"paths": {"/items/{id}": {"get": {}}}}
    assert resolve_operation(document, f"/items/{encoded}", "GET").path_parameters == {
        "id": decoded
    }


def test_many_embedded_parameters_have_bounded_matching_time() -> None:
    # Isolate the deadline so a regression cannot hang the pytest process.
    code = """
from specguard.routing import RouteNotFoundError, resolve_operation
document = {'paths': {'/{a}{b}{c}{d}{e}Z': {'get': {}}}}
try:
    resolve_operation(document, '/' + 'x' * 400, 'GET')
except RouteNotFoundError:
    pass
else:
    raise AssertionError('Unexpected match')
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=5, capture_output=True)


def test_adjacent_parameters_preserve_shortest_successful_captures() -> None:
    document = {"paths": {"/{a}{b}Z": {"get": {}}}}
    assert resolve_operation(document, "/abcZ", "GET").path_parameters == {"a": "a", "b": "bc"}
