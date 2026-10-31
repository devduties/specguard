import asyncio
import gzip
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from specguard.errors import UpstreamError, error_response
from specguard.forwarding import (
    ProxyRequestError,
    ProxySettings,
    RequestTooLargeError,
    UnsupportedContentEncodingError,
    UpstreamProxy,
    UpstreamResponseTooLargeError,
    UpstreamTimeoutError,
)
from specguard.parameters import ParameterValidationError
from specguard.request_body import BodyValidationError, UnsupportedMediaTypeError
from specguard.routing import MethodNotAllowedError, RouteNotFoundError


def contract():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer", "minimum": 1},
                    },
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                    {
                        "name": "X-Client",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "get": {},
                "post": {
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
                    }
                },
            }
        },
    }


def upstream_response(body=b"ok", status=200, headers=()):
    # A stream mirrors a network response and keeps compressed bytes untouched.
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def test_valid_request_preserves_method_query_and_original_body():
    sent = []
    raw = b'{ "quantity" : 2 }'
    headers = [
        ("Content-Type", "application/json"),
        ("X-Client", "demo"),
        ("Host", "proxy.test"),
        ("Content-Length", "999"),
        ("Transfer-Encoding", "chunked"),
    ]
    before = list(headers)
    document = contract()
    original = deepcopy(document)

    def receiver(request):
        sent.append(request)
        return upstream_response(b"created", 201, {"Content-Type": "text/plain"})

    async def exercise():
        async with UpstreamProxy(
            document,
            ProxySettings("https://upstream.test/api/"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            return await proxy.forward("/items/2?limit=3&tag=a%2Fb&tag=a+b", "POST", headers, raw)

    response = asyncio.run(exercise())
    assert response.status_code == 201
    assert response.body == b"created"
    assert sent[0].method == "POST"
    assert sent[0].url.raw_path == b"/api/items/2?limit=3&tag=a%2Fb&tag=a+b"
    assert sent[0].content == raw
    assert sent[0].headers["host"] == "upstream.test"
    assert sent[0].headers["content-length"] == str(len(raw))
    assert "transfer-encoding" not in sent[0].headers
    assert headers == before
    assert document == original


@pytest.mark.parametrize(
    "target,method,headers,body,error",
    [
        ("/missing", "GET", {"X-Client": "demo"}, b"", RouteNotFoundError),
        ("/items/1", "DELETE", {"X-Client": "demo"}, b"", MethodNotAllowedError),
        ("/items/0", "GET", {"X-Client": "demo"}, b"", ParameterValidationError),
        ("/items/1?limit=0", "GET", {"X-Client": "demo"}, b"", ParameterValidationError),
        ("/items/1", "GET", {}, b"", ParameterValidationError),
        ("/items/1", "POST", {"X-Client": "demo"}, b"", BodyValidationError),
        (
            "/items/1",
            "POST",
            {"X-Client": "demo", "Content-Type": "application/json"},
            b"{",
            BodyValidationError,
        ),
        (
            "/items/1",
            "POST",
            {"X-Client": "demo", "Content-Type": "application/json"},
            b'{"quantity":0}',
            BodyValidationError,
        ),
        (
            "/items/1",
            "POST",
            {"X-Client": "demo", "Content-Type": "text/plain"},
            b"hello",
            UnsupportedMediaTypeError,
        ),
    ],
)
def test_rejected_requests_never_reach_upstream(target, method, headers, body, error):
    calls = []

    def receiver(request):
        calls.append(request)
        return upstream_response()

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(error):
                await proxy.forward(target, method, headers, body)

    asyncio.run(exercise())
    assert calls == []


def test_hop_headers_and_connection_nominations_are_removed_both_ways():
    sent = []
    compressed = gzip.compress(b"original response")

    def receiver(request):
        sent.append(request)
        return upstream_response(
            compressed,
            202,
            [
                ("Connection", "X-Internal"),
                ("X-Internal", "private"),
                ("Keep-Alive", "timeout=10"),
                ("Transfer-Encoding", "chunked"),
                ("Content-Encoding", "gzip"),
                ("Content-Length", str(len(compressed))),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
            ],
        )

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            return await proxy.forward(
                "/items/1",
                "GET",
                [
                    ("X-Client", "demo"),
                    ("Connection", "X-Secret"),
                    ("X-Secret", "private"),
                    ("Proxy-Authorization", "private"),
                    ("TE", "trailers"),
                    ("Upgrade", "websocket"),
                    ("Authorization", "Bearer user-token"),
                ],
            )

    response = asyncio.run(exercise())
    assert response.body == compressed
    assert gzip.decompress(response.body) == b"original response"
    assert sent[0].headers["authorization"] == "Bearer user-token"
    for name in ("connection", "x-secret", "proxy-authorization", "te", "upgrade"):
        assert name not in sent[0].headers
    returned = httpx.Headers(response.headers)
    for name in ("connection", "x-internal", "keep-alive", "transfer-encoding"):
        assert name not in returned
    assert returned["content-encoding"] == "gzip"
    assert returned["content-length"] == str(len(compressed))
    assert returned.get_list("set-cookie") == ["a=1", "b=2"]


def test_connection_nominated_required_header_cannot_bypass_validation():
    def receiver(request):
        pytest.fail("Invalid request reached upstream")

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(ParameterValidationError):
                await proxy.forward(
                    "/items/1", "GET", {"X-Client": "demo", "Connection": "X-Client"}
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [302, 404, 500, 503])
def test_upstream_status_is_preserved_without_redirects_or_retries(status):
    calls = []

    def receiver(request):
        calls.append(request)
        return upstream_response(b"upstream result", status, {"Location": "https://other.test/"})

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            return await proxy.forward("/items/1", "GET", {"X-Client": "demo"})

    response = asyncio.run(exercise())
    assert response.status_code == status
    assert response.body == b"upstream result"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error,expected,status",
    [
        (httpx.ConnectTimeout("private target"), UpstreamTimeoutError, 504),
        (httpx.ReadTimeout("private target"), UpstreamTimeoutError, 504),
        (httpx.ConnectError("private target"), UpstreamError, 502),
        (httpx.RemoteProtocolError("private target"), UpstreamError, 502),
    ],
)
def test_transport_failures_have_safe_predictable_responses(error, expected, status):
    def receiver(request):
        raise error

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(expected) as caught:
                await proxy.forward("/items/1", "GET", {"X-Client": "demo"})
            response = error_response(caught.value)
            assert response.status_code == status
            assert "private" not in str(response.to_dict())

    asyncio.run(exercise())


def test_overall_timeout_bounds_an_upstream_that_keeps_waiting():
    async def receiver(request):
        await asyncio.sleep(1)
        return upstream_response()

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test", timeout_seconds=0.01),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(UpstreamTimeoutError):
                await proxy.forward("/items/1", "GET", {"X-Client": "demo"})

    asyncio.run(exercise())


def test_upstream_cookies_are_not_replayed_between_requests():
    received = []

    def receiver(request):
        received.append(request.headers.get("cookie"))
        return upstream_response(headers={"Set-Cookie": "private=session; Path=/"})

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            await proxy.forward("/items/1", "GET", {"X-Client": "first"})
            await proxy.forward("/items/2", "GET", {"X-Client": "second"})
            await proxy.forward("/items/3", "GET", {"X-Client": "third", "Cookie": "own=value"})

    asyncio.run(exercise())
    assert received == [None, None, "own=value"]


def test_request_size_is_checked_before_sending_and_exact_limit_is_allowed():
    raw = b'{"quantity":1}'
    sent = []

    def receiver(request):
        sent.append(request)
        return upstream_response()

    async def exercise():
        settings = ProxySettings("https://upstream.test", max_request_bytes=len(raw))
        async with UpstreamProxy(
            contract(), settings, transport=httpx.MockTransport(receiver)
        ) as proxy:
            with pytest.raises(RequestTooLargeError) as caught:
                await proxy.forward("/items/1", "POST", {}, raw + b" ")
            assert error_response(caught.value).status_code == 413
            await proxy.forward(
                "/items/1", "POST", {"X-Client": "demo", "Content-Type": "application/json"}, raw
            )

    asyncio.run(exercise())
    assert len(sent) == 1


class ChunkedResponse(httpx.AsyncByteStream):
    def __init__(self):
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in [b"12", b"345", b"678"]:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def test_response_size_limit_stops_reading_and_closes_the_stream():
    stream = ChunkedResponse()

    def receiver(request):
        return httpx.Response(200, stream=stream)

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test", max_response_bytes=4),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(UpstreamResponseTooLargeError) as caught:
                await proxy.forward("/items/1", "GET", {"X-Client": "demo"})
            assert error_response(caught.value).status_code == 502

    asyncio.run(exercise())
    assert stream.closed
    assert stream.read_count == 2


@pytest.mark.parametrize(
    "target",
    [
        "https://other.test/items/1",
        "/items/../admin",
        "/items/%2e%2e/admin",
        "/items/1#fragment",
        "/items/1\r\nInjected: yes",
        "/\\other.test/",
    ],
)
def test_unsafe_targets_are_rejected_without_sending(target):
    def receiver(request):
        pytest.fail("Unsafe request reached upstream")

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test/base"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(ProxyRequestError):
                await proxy.forward(target, "GET", {"X-Client": "demo"})

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "target,expected",
    [("//tenant/a%2Fb?x=%2f&x=+", b"//tenant/a%2Fb?x=%2f&x=+"), ("/café", b"/caf%C3%A9")],
)
def test_leading_slashes_and_encoded_paths_keep_the_configured_upstream(target, expected):
    sent = []
    doc = {"paths": {"//tenant/{id}": {"get": {}}, "/{id}": {"get": {}}}}

    def receiver(request):
        sent.append(request)
        return upstream_response()

    async def exercise():
        async with UpstreamProxy(
            doc, ProxySettings("https://upstream.test"), transport=httpx.MockTransport(receiver)
        ) as proxy:
            await proxy.forward(target, "GET")

    asyncio.run(exercise())
    assert sent[0].url.host == "upstream.test"
    assert sent[0].url.raw_path == expected


def test_custom_method_capitalization_is_preserved():
    sent = []

    def receiver(request):
        sent.append(request.method)
        return upstream_response()

    async def exercise():
        doc = {"paths": {"/items": {"additionalOperations": {"Copy": {}}}}}
        async with UpstreamProxy(
            doc, ProxySettings("https://upstream.test"), transport=httpx.MockTransport(receiver)
        ) as proxy:
            await proxy.forward("/items", "Copy")

    asyncio.run(exercise())
    assert sent == ["Copy"]


def test_forwarding_uses_custom_parameter_decoders_before_sending():
    document = contract()
    document["paths"]["/items/{id}"]["get"]["parameters"] = [
        {
            "name": "count",
            "in": "query",
            "content": {
                "application/example": {
                    "schema": {"type": "integer", "minimum": 1},
                }
            },
        }
    ]
    sent = []

    def receiver(request):
        sent.append(request)
        return upstream_response()

    async def exercise():
        async with UpstreamProxy(
            document,
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
            content_decoders={"application/example": int},
        ) as proxy:
            with pytest.raises(ParameterValidationError):
                await proxy.forward("/items/1?count=0", "GET", {"X-Client": "demo"})
            await proxy.forward("/items/1?count=2", "GET", {"X-Client": "demo"})

    asyncio.run(exercise())
    assert len(sent) == 1
    assert sent[0].url.query == b"count=2"


@pytest.mark.parametrize(
    "headers,error",
    [
        (
            [
                ("X-Client", "demo"),
                ("Content-Type", "application/json"),
                ("Content-Type", "text/plain"),
            ],
            ProxyRequestError,
        ),
        ([("X-Client", "demo"), ("Bad Header", "value")], ProxyRequestError),
        ([("X-Client", "demo\r\nInjected: yes")], ProxyRequestError),
        ([("X-Client", "demo"), ("Content-Encoding", "gzip")], UnsupportedContentEncodingError),
    ],
)
def test_invalid_metadata_is_rejected_before_sending(headers, error):
    def receiver(request):
        pytest.fail("Invalid metadata reached upstream")

    async def exercise():
        async with UpstreamProxy(
            contract(),
            ProxySettings("https://upstream.test"),
            transport=httpx.MockTransport(receiver),
        ) as proxy:
            with pytest.raises(error):
                await proxy.forward("/items/1", "GET", headers)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "https://",
        "https://user:secret@up.test",
        "https://up.test/?a=1",
        "https://up.test/#part",
        "http://up.test:0",
        "https://up.test:99999",
    ],
)
def test_invalid_upstream_configuration_is_rejected(url):
    with pytest.raises(ValueError):
        ProxySettings(url)


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"max_request_bytes": 0},
        {"max_response_bytes": -1},
        {"max_response_bytes": True},
    ],
)
def test_invalid_limits_are_rejected(options):
    with pytest.raises(ValueError):
        ProxySettings("https://upstream.test", **options)


def test_proxy_requires_an_open_context():
    async def exercise():
        proxy = UpstreamProxy(contract(), ProxySettings("https://upstream.test"))
        with pytest.raises(RuntimeError):
            await proxy.forward("/items/1", "GET")
        async with proxy:
            with pytest.raises(RuntimeError):
                await proxy.__aenter__()
        with pytest.raises(RuntimeError):
            await proxy.forward("/items/1", "GET")

    asyncio.run(exercise())


@pytest.fixture
def local_upstream():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.command, self.path, body, self.headers.get("X-Client")))
            response = gzip.compress(b"upstream created")
            self.send_response(201)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Set-Cookie", "a=1")
            self.send_header("Set-Cookie", "b=2")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_upstream_receives_only_valid_requests(local_upstream):
    url, received = local_upstream
    raw = b'{ "quantity" : 2 }'

    async def exercise():
        async with UpstreamProxy(contract(), ProxySettings(url)) as proxy:
            headers = {"X-Client": "demo", "Content-Type": "application/json"}
            with pytest.raises(BodyValidationError):
                await proxy.forward("/items/1", "POST", headers, b'{"quantity":0}')
            return await proxy.forward("/items/1?limit=2&x=a%2Fb&x=+", "POST", headers, raw)

    response = asyncio.run(exercise())
    assert received == [("POST", "/api/items/1?limit=2&x=a%2Fb&x=+", raw, "demo")]
    assert response.status_code == 201
    assert gzip.decompress(response.body) == b"upstream created"
    assert httpx.Headers(response.headers).get_list("set-cookie") == ["a=1", "b=2"]
