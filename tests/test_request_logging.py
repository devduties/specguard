import json
import logging

from specguard.request_logging import RequestFormatter


def test_request_json_has_a_utc_timestamp_and_excludes_private_record_data():
    record = logging.LogRecord("specguard.requests", logging.INFO, "", 0, "private", (), None)
    record.created = 0
    record.request_id = "23d9989203144b39911044b16363b66b"
    record.method = "get"
    record.route = "/orders/{id}"
    record.status_code = 200
    record.duration_ms = 25.0
    record.outcome = "upstream_response"
    record.error_code = None
    record.authorization = "private token"
    payload = json.loads(RequestFormatter().format(record))
    assert payload["timestamp"] == "1970-01-01T00:00:00+00:00"
    assert set(payload) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "method",
        "route",
        "status_code",
        "duration_ms",
        "outcome",
        "error_code",
    }
    assert "private" not in json.dumps(payload)
