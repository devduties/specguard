import asyncio
import gzip
import json

import httpx
import pytest

from specguard.config import ServerSettings
from specguard.forwarding import ProxySettings
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


def test_server_preserves_raw_request_and_upstream_response(spec_path):
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
    spec_path, target, method, body, content_type, status, code
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


def test_head_has_no_body_and_keeps_upstream_representation_length(spec_path):
    def receiver(request):
        return response(b"", 200, {"Content-Length": "123", "Content-Type": "text/plain"})

    app = create_app(
        ServerSettings(spec_path, ProxySettings("http://up.test")),
        transport=httpx.MockTransport(receiver),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                return await client.head("/items/1")

    result = asyncio.run(exercise())
    assert result.content == b""
    assert result.headers["content-length"] == "123"


def test_invalid_spec_fails_before_lifespan_or_network(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SpecLoadError):
        create_app(ServerSettings(path, ProxySettings("http://up.test")))


def test_disconnect_does_not_forward_or_send_a_response(spec_path):
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
