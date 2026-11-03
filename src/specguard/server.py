"""HTTP adapter for the existing validation and upstream forwarding flow."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import quote
from uuid import uuid4

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
from specguard.routing import (
    InvalidRequestTargetError,
    MethodNotAllowedError,
    RouteNotFoundError,
    resolve_operation,
)
from specguard.spec_loader import load_openapi_spec

logger = logging.getLogger("specguard.requests")


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
    request_id = uuid4().hex
    status_code = None
    error_code = None
    outcome = "response_failed"
    try:
        try:
            response = await _validated_response(request, request_id)
            response_outcome = "upstream_response"
        except ClientDisconnect:
            outcome = "client_disconnected"
            return
        except (
            RequestValidationError,
            UpstreamError,
            RouteNotFoundError,
            MethodNotAllowedError,
            InvalidRequestTargetError,
        ) as exc:
            result = error_response(exc)
            error_code = result.code
            response = _error_response(exc)
            response_outcome = "proxy_error"
        except Exception as exc:
            error_code = "internal_error"
            response = _error_response(exc)
            response_outcome = "proxy_error"
        # Replace any upstream ID, including duplicates, so the log and client agree.
        response.headers["X-Request-ID"] = request_id
        status_code = response.status_code
        if request.method.upper() == "HEAD":
            response.body = b""
        await response(scope, receive, send)
        outcome = response_outcome
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        match = getattr(request.state, "operation", None)
        level = logging.INFO
        if outcome in {"response_failed", "cancelled"} or (status_code or 0) >= 500:
            level = logging.ERROR
        elif outcome == "client_disconnected" or (status_code or 0) >= 400:
            level = logging.WARNING
        logger.log(
            level,
            "request_finished",
            extra={
                "request_id": request_id,
                "method": match.method if match is not None else None,
                "route": match.path_template if match is not None else None,
                "status_code": status_code,
                "duration_ms": getattr(request.state, "duration_ms", None),
                "outcome": outcome,
                "error_code": error_code,
            },
        )


async def _validated_response(request: Request, request_id: str) -> Response:
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
    target = _request_target(request.scope)
    proxy = request.app.state.proxy
    try:
        request.state.operation = resolve_operation(proxy.document, target, request.method)
    except (LookupError, ValueError):
        # Metadata must not change the forwarding pipeline's error precedence.
        pass
    started = time.perf_counter()
    try:
        result = await proxy.forward(
            target, request.method, request.scope["headers"], bytes(body), request_id=request_id
        )
    finally:
        request.state.duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return _upstream_response(result)


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
