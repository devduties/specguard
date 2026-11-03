import asyncio
import gzip
import json
import logging
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from specguard.config import ServerSettings
from specguard.forwarding import ProxySettings
from specguard.request_logging import RequestFormatter
from specguard.server import create_app
from specguard.spec_loader import SpecLoadError


@pytest.fixture
def spec_path(tmp_path):
    schema = {
        "type": "object",
        "required": ["quantity"],
        "properties": {"quantity": {"type": "integer", "minimum": 1}},
    }
    document = {
        "openapi": "3.2.0",
        "info": {"title": "Demo", "version": "1"},
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer", "minimum": 1},
                    }
                ],
                "get": {"responses": {"200": {"description": "OK"}}},
                "head": {"responses": {"200": {"description": "OK"}}},
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}},
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/files/{name}": {
                "get": {
                    "parameters": [
                        {
                            "name": "name",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/custom": {
                "additionalOperations": {"Copy": {"responses": {"200": {"description": "OK"}}}}
            },
        },
    }
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def response(body=b"ok", status=200, headers=()):
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def test_server_preserves_raw_request_and_upstream_response(spec_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="specguard.requests")
    clock = iter([10.0, 10.025])
    monkeypatch.setattr("specguard.server.time", SimpleNamespace(perf_counter=lambda: next(clock)))
    received = []
    compressed = gzip.compress(b"created")
    body = b'{ "quantity" : 2 }'

    def receiver(request):
        received.append(request)
        return response(
            compressed,
            201,
            [
                ("Content-Encoding", "gzip"),
                ("Content-Length", str(len(compressed))),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
            ],
        )

    settings = ServerSettings(spec_path, ProxySettings("http://up.test/api"))
    app = create_app(settings, transport=httpx.MockTransport(receiver))

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.post(
                    "/items/2?tag=a%2Fb&tag=a+b",
                    content=body,
                    headers={"Content-Type": "application/json"},
                )

    result = asyncio.run(exercise())
    assert result.status_code == 201
    assert result.content == b"created"
    assert result.headers["content-encoding"] == "gzip"
    assert result.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert received[0].url.raw_path == b"/api/items/2?tag=a%2Fb&tag=a+b"
    assert received[0].content == body
    assert app.state.proxy is None
    request_id = result.headers["x-request-id"]
    assert UUID(hex=request_id).version == 4
    assert received[0].headers["x-request-id"] == request_id
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 1
    event = json.loads(RequestFormatter().format(records[0]))
    assert event["request_id"] == request_id
    assert event["route"] == "/items/{id}"
    assert event["method"] == "post"
    assert event["duration_ms"] == 25.0
    assert event["status_code"] == 201
    assert event["outcome"] == "upstream_response"
    assert event["error_code"] is None
    assert event["level"] == "info"


@pytest.mark.parametrize(
    "target,method,body,content_type,status,code",
    [
        ("/missing", "GET", b"", None, 404, "route_not_found"),
        ("/items/1", "DELETE", b"", None, 405, "method_not_allowed"),
        ("/items/0", "GET", b"", None, 400, "parameter_validation_failed"),
        ("/items/%FF", "GET", b"", None, 400, "invalid_request_target"),
        ("/items/1", "POST", b"{", "application/json", 400, "body_validation_failed"),
        ("/items/1", "POST", b'{"quantity":0}', "application/json", 400, "body_validation_failed"),
        ("/items/1", "POST", b"{}", "text/plain", 415, "unsupported_media_type"),
    ],
)
def test_http_rejections_use_the_envelope_and_never_reach_upstream(
    spec_path, target, method, body, content_type, status, code, caplog
):
    calls = []

    def receiver(request):
        calls.append(request)
        return response()

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.request(
                    method,
                    target,
                    content=body,
                    headers={"Content-Type": content_type} if content_type else {},
                )

    result = asyncio.run(exercise())
    assert result.status_code == status
    assert result.json()["error"]["code"] == code
    assert set(result.json()["error"]) == {"code", "message", "details"}
    if status == 405:
        assert set(result.headers["allow"].split(", ")) == {"GET", "HEAD", "POST"}
    assert calls == []
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 1
    assert records[0].request_id == result.headers["x-request-id"]
    assert records[0].error_code == code
    assert records[0].status_code == status
    assert records[0].levelno == logging.WARNING
    if code in {"route_not_found", "method_not_allowed", "invalid_request_target"}:
        assert records[0].method is None
        assert records[0].route is None
        assert records[0].duration_ms >= 0
    else:
        assert records[0].route == "/items/{id}"
        assert records[0].duration_ms >= 0


@pytest.mark.parametrize(
    "error,status,code",
    [
        (httpx.ConnectError("private target"), 502, "upstream_error"),
        (httpx.ReadTimeout("private target"), 504, "upstream_timeout"),
        (RuntimeError("private diagnostic"), 500, "internal_error"),
    ],
)
def test_transport_and_unexpected_errors_are_safe(spec_path, error, status, code, caplog):
    def receiver(request):
        raise error

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.get("/items/1")

    result = asyncio.run(exercise())
    assert result.status_code == status
    assert result.json()["error"]["code"] == code
    assert "private" not in result.text
    assert "private" not in caplog.text
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].request_id == result.headers["x-request-id"]
    assert records[0].error_code == code


def test_chunked_body_limit_stops_receiving_and_blocks_upstream(spec_path):
    read = []

    def receiver(request):
        pytest.fail("Oversized request reached upstream")

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test", max_request_bytes=4)),
        transport=httpx.MockTransport(receiver),
    )

    async def chunks():
        read.append(1)
        yield b"12345"
        read.append(2)
        yield b"never read"

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.post(
                    "/items/1", content=chunks(), headers={"Content-Type": "application/json"}
                )

    result = asyncio.run(exercise())
    assert result.status_code == 413
    assert result.json()["error"]["code"] == "request_too_large"
    assert read == [1]


def test_declared_body_over_limit_is_rejected_without_reading(spec_path):
    read = []

    async def chunks():
        read.append(True)
        yield b"unused"

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test", max_request_bytes=4))
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.post(
                    "/items/1", content=chunks(), headers={"Content-Length": "5000"}
                )

    assert asyncio.run(exercise()).status_code == 413
    assert read == []


def test_raw_encoded_path_is_not_decoded_twice_and_custom_methods_are_supported(spec_path):
    received = []

    def receiver(request):
        received.append((request.method, request.url.raw_path))
        return response()

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                assert (await client.get("/files/a%252Fb")).status_code == 200
                request = client.build_request("Copy", "/custom")
                request.method = "Copy"
                assert (await client.send(request)).status_code == 200

    asyncio.run(exercise())
    assert received == [("GET", b"/files/a%252Fb"), ("Copy", b"/custom")]


@pytest.mark.parametrize("method", ["HEAD", "head", "HeAd"])
def test_head_has_no_body_and_keeps_upstream_representation_length(spec_path, method):
    def receiver(request):
        assert request.method == "HEAD"
        return response(b"ignored", 200, {"Content-Length": "123", "Content-Type": "text/plain"})

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                request = client.build_request("HEAD", "/items/1")
                request.method = method
                return await client.send(request)

    result = asyncio.run(exercise())
    assert result.content == b""
    assert result.headers["content-length"] == "123"


def test_invalid_spec_fails_before_lifespan_or_network(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SpecLoadError):
        create_app(ServerSettings(path, ProxySettings("http://up.test")))


def test_disconnect_does_not_forward_or_send_a_response(spec_path, caplog):
    def receiver(request):
        pytest.fail("Disconnected request reached upstream")

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    async def exercise():
        async with app.router.lifespan_context(app):
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "method": "POST",
                    "path": "/items/1",
                    "raw_path": b"/items/1",
                    "query_string": b"",
                    "headers": [],
                    "root_path": "",
                },
                receive,
                send,
            )

    asyncio.run(exercise())
    assert messages == []
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 1
    assert records[0].outcome == "client_disconnected"
    assert records[0].status_code is None
    assert records[0].duration_ms is None


def test_raw_header_bytes_are_preserved(spec_path):
    received = []

    def receiver(request):
        received.append(request.headers.raw)
        return response()

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.get("/items/1", headers=[(b"x-label", b"caf\xe9")])

    assert asyncio.run(exercise()).status_code == 200
    assert (b"x-label", b"caf\xe9") in received[0]


def test_concurrent_requests_keep_generated_ids_and_safe_log_fields(spec_path, caplog):
    caplog.set_level(logging.INFO, logger="specguard.requests")
    received = []

    async def exercise():
        both_received = asyncio.Event()

        async def receiver(request):
            received.append(request)
            if len(received) == 2:
                both_received.set()
            await both_received.wait()
            return response(
                status=503 if request.url.path.endswith("two") else 200,
                headers=[("X-Request-ID", "upstream-private"), ("X-Request-ID", "duplicate")],
            )

        app = create_app(
            ServerSettings(spec_path, ProxySettings("http://up.test")),
            transport=httpx.MockTransport(receiver),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            client.get(
                                f"/files/client-private-{suffix}?token=query-private",
                                headers=[
                                    ("X-Request-ID", "client-private"),
                                    ("X-Request-ID", "duplicate-private"),
                                    ("Connection", "X-Request-ID"),
                                    ("Authorization", "Bearer authorization-private"),
                                    ("Cookie", "secret=cookie-private"),
                                ],
                            )
                            for suffix in ("one", "two")
                        )
                    ),
                    timeout=2,
                )

    results = asyncio.run(exercise())
    ids = [result.headers["x-request-id"] for result in results]
    assert len(set(ids)) == 2
    assert all(UUID(hex=value).version == 4 for value in ids)
    assert all(len(result.headers.get_list("x-request-id")) == 1 for result in results)
    assert {request.headers["x-request-id"] for request in received} == set(ids)
    assert all(len(request.headers.get_list("x-request-id")) == 1 for request in received)
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 2
    assert {record.request_id for record in records} == set(ids)
    for record in records:
        assert record.method == "get"
        assert record.route == "/files/{name}"
        assert record.outcome == "upstream_response"
        assert record.error_code is None
        assert record.levelno == (logging.ERROR if record.status_code == 503 else logging.INFO)
        assert "private" not in RequestFormatter().format(record)


def test_forward_duration_excludes_reading_the_request_body(spec_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="specguard.requests")
    now = 0.0
    monkeypatch.setattr("specguard.server.time", SimpleNamespace(perf_counter=lambda: now))

    async def chunks():
        nonlocal now
        now += 5.0
        yield b'{"quantity":2}'

    def receiver(request):
        nonlocal now
        now += 0.025
        return response(status=201)

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.post(
                    "/items/1", content=chunks(), headers={"Content-Type": "application/json"}
                )

    assert asyncio.run(exercise()).status_code == 201
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 1
    assert records[0].duration_ms == 25.0


@pytest.mark.parametrize("failure", ["cancelled", "response_failed"])
def test_interrupted_requests_log_once_and_preserve_the_failure(spec_path, caplog, failure):
    def receiver(request):
        if failure == "cancelled":
            raise asyncio.CancelledError
        return response()

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        raise OSError("private send failure")

    async def exercise():
        async with app.router.lifespan_context(app):
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "method": "GET",
                    "path": "/items/1",
                    "raw_path": b"/items/1",
                    "query_string": b"",
                    "headers": [],
                    "root_path": "",
                },
                receive,
                send,
            )

    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else OSError):
        asyncio.run(exercise())
    records = [record for record in caplog.records if record.name == "specguard.requests"]
    assert len(records) == 1
    assert records[0].outcome == failure
    assert records[0].levelno == logging.ERROR
    assert records[0].duration_ms >= 0
    assert records[0].status_code == (None if failure == "cancelled" else 200)
    assert "private" not in RequestFormatter().format(records[0])


def test_log_route_lookup_does_not_change_forwarding_error_precedence(spec_path):
    app = create_app(ServerSettings(spec_path, ProxySettings("http://up.test")))

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.get("/missing", headers={"Content-Encoding": "gzip"})

    result = asyncio.run(exercise())
    assert result.status_code == 415
    assert result.json()["error"]["code"] == "unsupported_content_encoding"
