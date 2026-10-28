from specguard.routing import match_operation


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
