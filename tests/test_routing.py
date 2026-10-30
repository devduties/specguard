import pytest

from specguard.routing import (
    MethodNotAllowedError,
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
