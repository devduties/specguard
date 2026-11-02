"""HTTP adapter for the existing validation and upstream forwarding flow."""

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from specguard.config import ServerSettings
from specguard.errors import RequestValidationError, UpstreamError, error_response
from specguard.forwarding import (
    ProxyRequestError,
    RequestTooLargeError,
    UpstreamProxy,
    UpstreamResponse,
)
from specguard.routing import InvalidRequestTargetError, MethodNotAllowedError, RouteNotFoundError
from specguard.spec_loader import load_openapi_spec

logger = logging.getLogger(__name__)


def create_app(
    settings: ServerSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> Starlette:
    # Load before starting the listener so malformed contracts fail at startup.
    document = load_openapi_spec(settings.spec_path)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with UpstreamProxy(document, settings.proxy, transport=transport) as proxy:
            app.state.proxy = proxy
            yield
        app.state.proxy = None

    app = Starlette(debug=False, routes=[Mount("/", app=_forward_request)], lifespan=lifespan)
    app.state.settings = settings
    return app


async def _forward_request(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    request = Request(scope, receive)
    try:
        body = bytearray()
        maximum = request.app.state.settings.proxy.max_request_bytes
        lengths = request.headers.getlist("content-length")
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise ProxyRequestError("Invalid Content-Length.")
            claimed = lengths[0].lstrip("0") or "0"
            if len(claimed) > len(str(maximum)) or int(claimed) > maximum:
                raise RequestTooLargeError("Request body exceeds the configured size limit.")
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                raise RequestTooLargeError("Request body exceeds the configured size limit.")
            body.extend(chunk)
        result = await request.app.state.proxy.forward(
            _request_target(scope), request.method, scope["headers"], bytes(body)
        )
        response = _upstream_response(result)
    except ClientDisconnect:
        return
    except (
        RequestValidationError,
        UpstreamError,
        RouteNotFoundError,
        MethodNotAllowedError,
        InvalidRequestTargetError,
    ) as exc:
        response = _error_response(exc)
    except Exception as exc:
        logger.error("Request handling failed (%s).", type(exc).__name__)
        response = _error_response(exc)
    if request.method == "HEAD":
        response.body = b""
    await response(scope, receive, send)


def _request_target(scope: Scope) -> str:
    raw_path = scope.get("raw_path")
    if raw_path is None:
        raw_path = quote(scope["path"], safe="/:@!$&'()*+,;=-._~").encode("ascii")
    query = scope.get("query_string", b"")
    try:
        return (raw_path + (b"?" + query if query else b"")).decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProxyRequestError("Invalid request target encoding.") from exc


def _upstream_response(result: UpstreamResponse) -> Response:
    response = Response(result.body, status_code=result.status_code)
    headers = [(name.lower(), value) for name, value in result.headers]
    if not any(name == b"content-length" for name, _ in headers):
        headers.extend(response.raw_headers)
    response.raw_headers = headers
    return response


def _error_response(exc: Exception) -> JSONResponse:
    result = error_response(exc)
    headers = (
        {"Allow": ", ".join(exc.allowed_methods)}
        if isinstance(exc, MethodNotAllowedError)
        else None
    )
    return JSONResponse(result.to_dict(), status_code=result.status_code, headers=headers)
